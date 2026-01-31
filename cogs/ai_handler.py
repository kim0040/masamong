# -*- coding: utf-8 -*-
"""
마사몽 봇의 AI 상호작용을 총괄하는 핵심 Cog입니다.

2-Step Agent 아키텍처에 따라 다음의 역할을 수행합니다:
1.  **의도 분석 (Lite Model)**: 사용자의 메시지를 분석하여 간단한 대화인지, 도구 사용이 필요한지 판단합니다.
2.  **도구 실행**: 분석된 계획에 따라 `ToolsCog`의 도구들을 실행하고 결과를 수집합니다.
3.  **답변 생성 (Main Model)**: 도구 실행 결과를 바탕으로 사용자에게 제공할 최종 답변을 생성합니다.
4.  **대화 기록 관리**: RAG(Retrieval-Augmented Generation)를 위해 대화 내용을 데이터베이스에 저장하고 임베딩을 생성합니다.
"""

from __future__ import annotations


import discord
from discord.ext import commands
try:
    import google.generativeai as genai
except ModuleNotFoundError:  # pragma: no cover - 환경에 따라 설치되지 않을 수 있음
    genai = None

# CometAPI용 OpenAI 호환 클라이언트
try:
    from openai import AsyncOpenAI
except ModuleNotFoundError:  # pragma: no cover
    AsyncOpenAI = None

from datetime import datetime, timedelta, timezone
import asyncio
import pytz
from collections import deque
import re
from typing import Dict, Any, Tuple
import aiosqlite
# numpy는 AI 메모리 기능(RAG)에서만 필요하므로, 설치되지 않은 환경에서도 실행되도록 가드한다.
try:
    import numpy as np
except ModuleNotFoundError:  # pragma: no cover - 경량 설치 환경 고려
    np = None  # type: ignore
import random
import time
import json
import uuid
import requests

import config
from logger_config import logger
from utils import db as db_utils
from utils import http
from utils.embeddings import (
    DiscordEmbeddingStore,
    KakaoEmbeddingStore,
    get_embedding,
)
from database.bm25_index import BM25IndexManager
from utils.hybrid_search import HybridSearchEngine
from utils.hybrid_search import HybridSearchEngine
from utils.reranker import Reranker, RerankerConfig
from utils.api_handlers.finnhub import ALIAS_TO_TICKER  # [NEW] Import for robust stock detection

KST = pytz.timezone('Asia/Seoul')

class AIHandler(commands.Cog):
    """AI 에이전트 워크플로우를 통합 관리하는 Cog입니다.

    - Lite/Flash Gemini 모델을 사용해 의도 분석과 응답 생성을 수행합니다.
    - `ToolsCog`와 협력해 외부 API 호출, 후처리, 오류 복구를 담당합니다.
    - 대화 저장소(RAG)를 구축해 장기 기억과 능동형 제안을 지원합니다.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.tools_cog = bot.get_cog('ToolsCog')
        self.ai_user_cooldowns: Dict[int, datetime] = {}
        self.proactive_cooldowns: Dict[int, float] = {}
        self.gemini_configured = False
        self.api_call_lock = asyncio.Lock()
        self.discord_embedding_store = DiscordEmbeddingStore(config.DISCORD_EMBEDDING_DB_PATH)
        self.kakao_embedding_store = KakaoEmbeddingStore(
            config.KAKAO_EMBEDDING_DB_PATH,
            config.KAKAO_EMBEDDING_SERVER_MAP,
        ) if config.KAKAO_EMBEDDING_DB_PATH or config.KAKAO_EMBEDDING_SERVER_MAP else None
        self.bm25_manager = BM25IndexManager(config.BM25_DATABASE_PATH) if config.BM25_DATABASE_PATH else None

        reranker: Reranker | None = None
        if config.RERANK_ENABLED and config.RAG_RERANKER_MODEL_NAME:
            reranker_config = RerankerConfig(
                model_name=config.RAG_RERANKER_MODEL_NAME,
                device=config.RAG_RERANKER_DEVICE,
                score_threshold=config.RAG_RERANKER_SCORE_THRESHOLD,
            )
            reranker = Reranker(reranker_config)
        self.reranker = reranker
        self.hybrid_search_engine = HybridSearchEngine(
            self.discord_embedding_store,
            self.kakao_embedding_store,
            self.bm25_manager,
            reranker=self.reranker,
        )
        self._window_buffers: dict[tuple[int, int], deque[dict[str, Any]]] = {}
        self._window_counts: dict[tuple[int, int], int] = {}
        self.debug_enabled = config.AI_DEBUG_ENABLED
        self._debug_log_len = getattr(config, "AI_DEBUG_LOG_MAX_LEN", 400)

        if config.GEMINI_API_KEY and genai:
            try:
                genai.configure(api_key=config.GEMINI_API_KEY)
                logger.info("Gemini API가 성공적으로 설정되었습니다.")
                self.gemini_configured = True
            except Exception as e:
                logger.critical(f"Gemini API 설정 실패: {e}. AI 관련 기능이 비활성화됩니다.", exc_info=True)
        elif config.GEMINI_API_KEY and not genai:
            logger.critical("google-generativeai 패키지를 찾을 수 없어 Gemini 기능을 초기화하지 못했습니다.")

        # CometAPI 클라이언트 초기화 (Gemini 대체)
        self.cometapi_client = None
        self.use_cometapi = config.USE_COMETAPI and config.COMETAPI_KEY
        if self.use_cometapi:
            if AsyncOpenAI:
                try:
                    self.cometapi_client = AsyncOpenAI(
                        base_url=config.COMETAPI_BASE_URL,
                        api_key=config.COMETAPI_KEY,
                    )
                    logger.info(f"CometAPI 클라이언트가 초기화되었습니다. 모델: {config.COMETAPI_MODEL}")
                except Exception as e:
                    logger.error(f"CometAPI 클라이언트 초기화 실패: {e}")
                    self.use_cometapi = False
            else:
                logger.warning("openai 패키지가 설치되지 않아 CometAPI를 사용할 수 없습니다.")
                self.use_cometapi = False
        
        # [NEW] Location Cache from DB
        self.location_cache: set[str] = set()

    @property
    def is_ready(self) -> bool:
        """AI 핸들러가 모든 의존성(Gemini, DB, ToolsCog)을 포함하여 준비되었는지 확인합니다."""
        return self.gemini_configured and self.bot.db is not None and self.tools_cog is not None

    def _debug(self, message: str, log_extra: dict[str, Any] | None = None) -> None:
        """디버그 설정이 켜진 경우에만 메시지를 기록합니다."""
        if not self.debug_enabled:
            return
        if log_extra:
            logger.debug(message, extra=log_extra)
        else:
            logger.debug(message)

    def _truncate_for_debug(self, value: Any) -> str:
        """긴 문자열을 로그용으로 잘라냅니다."""
        if value is None:
            return ""
        rendered = str(value)
        max_len = self._debug_log_len
        if len(rendered) <= max_len:
            return rendered
        return rendered[:max_len] + "…"

    def _format_prompt_debug(self, prompt: Any) -> str:
        """Gemini 프롬프트를 JSON 문자열 또는 일반 문자열로 축약합니다."""
        try:
            if isinstance(prompt, (dict, list)):
                rendered = json.dumps(prompt, ensure_ascii=False)
            else:
                rendered = str(prompt)
        except Exception:
            rendered = repr(prompt)
        return self._truncate_for_debug(rendered)

    async def _load_location_cache(self):
        """DB에서 지역명 데이터를 로드하여 캐싱합니다."""
        if self.location_cache:
            return

        if not self.bot.db:
            return

        try:
            # 2글자 이상인 지역명만 로드 (1글자는 오탐지 가능성 높음)
            async with self.bot.db.execute("SELECT name FROM locations WHERE LENGTH(name) >= 2") as cursor:
                rows = await cursor.fetchall()
                if rows:
                    self.location_cache = {row['name'] for row in rows}
                    logger.info(f"DB에서 지역명 데이터 {len(self.location_cache)}개를 로드했습니다.")
        except Exception as e:
            logger.error(f"지역명 캐시 로드 중 오류: {e}")

    def _message_has_valid_mention(self, message: discord.Message) -> bool:
        """메시지에 봇 멘션이 존재하는지 확인합니다."""
        bot_user = getattr(self.bot, "user", None)
        if bot_user is None:
            return False

        try:
            mentions = getattr(message, "mentions", []) or []
        except AttributeError:
            mentions = []
        if any(getattr(member, "id", None) == bot_user.id for member in mentions):
            return True

        # 역할 멘션 확인
        found_role_ids = set()
        if message.content:
            found_role_ids = set(re.findall(r'<@&(\d+)>', message.content))
        
        guild = getattr(message, "guild", None)
        if found_role_ids and guild:
            guild_me = getattr(guild, "me", None)
            if guild_me:
                my_role_ids = {str(r.id) for r in guild_me.roles if r.id != guild.id}
                if not found_role_ids.isdisjoint(my_role_ids):
                    return True

        content = (message.content or "").lower()
        alias_candidates: set[str] = set()
        name = getattr(bot_user, "name", None)
        if name:
            alias_candidates.add(f"@{name.lower()}")
        display_name = getattr(bot_user, "display_name", None)
        if display_name:
            alias_candidates.add(f"@{display_name.lower()}")
        global_name = getattr(bot_user, "global_name", None)
        if global_name:
            alias_candidates.add(f"@{global_name.lower()}")

        guild = getattr(message, "guild", None)
        if guild is not None:
            guild_me = getattr(guild, "me", None)
            guild_display = getattr(guild_me, "display_name", None)
            if guild_display:
                alias_candidates.add(f"@{str(guild_display).lower()}")

        # 사용자들이 다양한 별칭으로 부를 수 있으므로, 모든 별칭을 소문자로 비교한다.
        alias_candidates = {alias for alias in alias_candidates if alias.strip("@")}
        return any(alias in content for alias in alias_candidates)

    def _strip_bot_references(self, content: str, guild: discord.Guild | None) -> str:
        """메시지 내용에서 봇 멘션 및 별칭을 제거합니다."""
        base_content = content or ""
        bot_user = getattr(self.bot, "user", None)
        if bot_user is None:
            return base_content.strip()

        patterns: set[str] = set()
        patterns.add(f"<@{bot_user.id}>")
        patterns.add(f"<@!{bot_user.id}>")

        # 역할 멘션 제거 패턴 추가
        if guild:
            guild_me = getattr(guild, "me", None)
            if guild_me:
                for role in guild_me.roles:
                    if role.id != guild.id:
                        patterns.add(f"<@&{role.id}>")

        for alias in (
            getattr(bot_user, "name", None),
            getattr(bot_user, "display_name", None),
            getattr(bot_user, "global_name", None),
        ):
            if alias:
                patterns.add(f"@{alias}")

        if guild is not None:
            guild_me = getattr(guild, "me", None)
            guild_display = getattr(guild_me, "display_name", None)
            if guild_display:
                patterns.add(f"@{guild_display}")

        patterns = {p for p in patterns if p}
        if not patterns:
            return base_content.strip()

        pattern = re.compile("|".join(re.escape(p) for p in patterns), flags=re.IGNORECASE)
        stripped = pattern.sub(" ", base_content)
        return re.sub(r"\s+", " ", stripped).strip()

    def _prepare_user_query(self, message: discord.Message, log_extra: dict[str, Any]) -> str | None:
        """멘션 검증 후 사용자 쿼리를 정제합니다."""
        # [NEW] DM에서는 멘션이 없어도 대화 가능 (여기서 None을 반환하면 대화가 종료되므로, DM이면 통과시킴)
        if not message.guild:
            # DM: 멘션 제거 (있다면)
            stripped = self._strip_bot_references(message.content or "", message.guild)
            if not stripped: # 멘션만 있고 내용이 없는 경우
                 self._debug("DM: 멘션만 존재해 쿼리가 비어 있습니다.", log_extra)
                 return None
            self._debug(f"DM 사용자 쿼리: {self._truncate_for_debug(stripped)}", log_extra)
            return stripped

        if not self._message_has_valid_mention(message):
            self._debug("멘션이 없어 메시지를 무시합니다.", log_extra)
            logger.info("멘션이 없는 메시지를 무시합니다.", extra=log_extra)
            return None
        # 멘션만 포함된 메시지는 Gemini 호출을 막기 위해 빈 문자열로 처리한다.
        stripped = self._strip_bot_references(message.content or "", message.guild)
        if not stripped:
            self._debug("멘션만 존재해 쿼리가 비어 있습니다.", log_extra)
            logger.info("봇 멘션만 포함된 메시지를 무시합니다.", extra=log_extra)
            return None
        self._debug(f"정제된 사용자 쿼리: {self._truncate_for_debug(stripped)}", log_extra)
        return stripped

    async def _safe_generate_content(self, model: genai.GenerativeModel, prompt: Any, log_extra: dict, generation_config: genai.types.GenerationConfig = None) -> genai.types.GenerateContentResponse | None:
        """Gemini `generate_content_async` 호출을 감싸 안정성을 높입니다.

        Args:
            model (genai.GenerativeModel): 사용할 Gemini 모델 인스턴스.
            prompt (Any): 모델에 전달할 프롬프트 또는 미디어 페이로드.
            log_extra (dict): 로깅 시 부가 정보를 담을 딕셔너리.
            generation_config (GenerationConfig, optional): 필요 시 덮어쓸 생성 설정.

        Returns:
            GenerateContentResponse | None: 성공 시 Gemini 응답, 실패 또는 속도 제한 시 None.
        """
        if generation_config is None:
            generation_config = genai.types.GenerationConfig(temperature=0.0)

        try:
            limit_key = 'gemini_intent' if config.AI_INTENT_MODEL_NAME in model.model_name else 'gemini_response'
            rpm = config.RPM_LIMIT_INTENT if limit_key == 'gemini_intent' else config.RPM_LIMIT_RESPONSE
            rpd = config.RPD_LIMIT_INTENT if limit_key == 'gemini_intent' else config.RPD_LIMIT_RESPONSE

            if self.debug_enabled:
                preview = self._format_prompt_debug(prompt)
                self._debug(f"[Gemini:{model.model_name}] 호출 프롬프트: {preview}", log_extra)

            if await db_utils.check_api_rate_limit(self.bot.db, limit_key, rpm, rpd):
                self._debug(f"[Gemini:{model.model_name}] 호출 차단 - rate limit 도달 ({limit_key})", log_extra)
                logger.warning(f"Gemini API 호출 제한({limit_key})에 도달했습니다.", extra=log_extra)
                return None

            response = await model.generate_content_async(
                prompt,
                generation_config=generation_config,
                safety_settings=config.GEMINI_SAFETY_SETTINGS,
            )
            await db_utils.log_api_call(self.bot.db, limit_key)
            if self.debug_enabled and response is not None:
                text = getattr(response, "text", None)
                self._debug(
                    f"[Gemini:{model.model_name}] 응답 요약: {self._truncate_for_debug(text)}",
                    log_extra,
                )
            return response
        except Exception as e:
            logger.error(f"Gemini 응답 생성 중 예기치 않은 오류: {e}", extra=log_extra, exc_info=True)
            return None

    async def _cometapi_generate_content(
        self,
        system_prompt: str,
        user_prompt: str,
        log_extra: dict,
        model: str | None = None,
    ) -> str | None:
        """CometAPI(OpenAI 호환)를 통해 응답을 생성합니다.

        Args:
            system_prompt: 시스템 프롬프트
            user_prompt: 사용자 프롬프트 (RAG 컨텍스트 포함)
            log_extra: 로깅용 추가 정보
            model: 사용할 모델명 (None이면 기본값 사용)

        Returns:
            생성된 응답 텍스트, 실패 시 None
        """
        if not self.cometapi_client:
            logger.warning("CometAPI 클라이언트가 초기화되지 않았습니다.", extra=log_extra)
            return None

        try:
            if self.debug_enabled:
                self._debug(f"[CometAPI] system={self._truncate_for_debug(system_prompt)}", log_extra)
                self._debug(f"[CometAPI] user={self._truncate_for_debug(user_prompt)}", log_extra)

            completion = await self.cometapi_client.chat.completions.create(
                model=model or config.COMETAPI_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=2048, # 약간 늘림
                temperature=config.AI_TEMPERATURE,
                frequency_penalty=config.AI_FREQUENCY_PENALTY,
                presence_penalty=config.AI_PRESENCE_PENALTY,
            )

            response_text = completion.choices[0].message.content
            reasoning_text = getattr(completion.choices[0].message, 'reasoning_content', None)
            
            # [Debug] 응답 내용 확인을 위한 강제 로깅
            logger.info(f"[CometAPI Debug] Raw Response: {response_text!r}", extra=log_extra)
            try:
                # model_dump()가 가능한지 확인 (Pydantic v2)
                logger.info(f"[CometAPI Debug] Message Obj: {completion.choices[0].message}", extra=log_extra)
            except:
                pass

            await db_utils.log_api_call(self.bot.db, "cometapi")

            # 만약 content가 비어있는데 reasoning_content가 있다면 그것을 반환 (Thinking 모델 대응)
            final_response = response_text
            if not final_response and reasoning_text:
                logger.warning("[CometAPI] Content is empty but reasoning_content exists. Using reasoning as fallback.", extra=log_extra)
                final_response = f"Thinking Process:\n{reasoning_text}" # 혹은 그냥 reasoning_text

            if self.debug_enabled:
                self._debug(f"[CometAPI] 응답: {self._truncate_for_debug(final_response)}", log_extra)

            return final_response.strip() if final_response else None

        except Exception as e:
            logger.error(f"CometAPI 응답 생성 중 오류: {e}", extra=log_extra, exc_info=True)
            return None

    async def _generate_local_embedding(self, content: str, log_extra: dict, prefix: str = "") -> np.ndarray | None:
        """SentenceTransformer 기반 임베딩을 생성합니다."""
        if not config.AI_MEMORY_ENABLED:
            return None
        if np is None:
            logger.warning("numpy가 설치되어 있지 않아 AI 메모리 기능을 사용할 수 없습니다.", extra=log_extra)
            return None

        embedding = await get_embedding(content, prefix=prefix)
        if embedding is None:
            logger.error("임베딩 생성 실패", extra=log_extra)
        return embedding

    async def add_message_to_history(self, message: discord.Message):
        """AI 허용 채널의 메시지를 대화 기록 DB에 저장합니다.

        Args:
            message (discord.Message): Discord 원본 메시지.

        Notes:
            메시지가 충분히 길면 임베딩 생성을 비동기 태스크로 예약합니다.
        """
        if not self.is_ready or not config.AI_MEMORY_ENABLED: return

        guild_id = message.guild.id if message.guild else 0
        
        # Guild인 경우에만 채널 화이트리스트 체크
        if message.guild:
            try:
                channel_config = config.CHANNEL_AI_CONFIG.get(message.channel.id, {})
                if not channel_config.get("allowed", False): return
            except AttributeError:
                pass # message.channel has no id? rare.

        try:
            await self.bot.db.execute(
                "INSERT INTO conversation_history (message_id, guild_id, channel_id, user_id, user_name, content, is_bot, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message.id,
                    guild_id,
                    message.channel.id,
                    message.author.id,
                    message.author.display_name,
                    message.content,
                    message.author.bot,
                    message.created_at.isoformat(),
                ),
            )
            await self._update_conversation_windows(message)
            await self.bot.db.commit()
            # 단일 메시지 임베딩 생성 로직 제거 (윈도우 기반 임베딩으로 전환)
            # if not message.author.bot and message.content.strip():
            #     asyncio.create_task(self._create_and_save_embedding(message))
        except Exception as e:
            logger.error(f"대화 기록 저장 중 DB 오류: {e}", exc_info=True, extra={'guild_id': guild_id})

    async def _summarize_content(self, text: str) -> str:
        """긴 텍스트를 임베딩용으로 요약합니다. DeepSeek 모델을 사용하여 검색 품질을 최적화합니다."""
        # [Optimization] 텍스트가 짧으면(400자 미만) 요약하지 않고 원본 그대로 사용
        # (E5 모델의 512 토큰 제한을 고려하여 안전한 길이로 설정)
        if len(text) < 400:
            return text

        if not self.use_cometapi:
            # CometAPI가 꺼져있다면 원본 반환
            return text
        
        # [Optimization] 입력 텍스트가 너무 길면 잘라서 토큰 절약
        safe_text = text[:4000] 
        
        try:
            # [Optimization] 검색(RAG) 품질을 위한 상세 요약 프롬프트
            # E5 임베딩 한계(512토큰) 내에 중요 정보가 다 들어가도록 500자 제한 둠
            system_prompt = (
                "너는 대화 내용을 나중에 검색하기 좋게 정리하는 '기억 관리자'야.\n"
                "주어진 대화 내용을 바탕으로 다음 형식에 맞춰 요약해.\n\n"
                "1. **상황 설명**: 어떤 주제로 누가 무슨 말을 했는지 자연스럽게 서술 (분량 제한 없음, 자세할수록 좋음)\n"
                "2. **분위기**: 대화가 즐거웠는지, 진지했는지, 화가 났는지 등 감정 상태 기록\n"
                "3. **핵심 키워드**: 날짜, 시간, 장소, URL, 주식 종목, 사람 이름 등 검색에 걸려야 할 단어들을 빠짐없이 나열\n\n"
                "※ **주의사항**: 전체 요약 길이는 반드시 **500자 이내**가 되도록 내용을 핵심 위주로 압축해. (임베딩 용량 제한)"
            )
            user_prompt = f"--- 대화 내용 ---\n{safe_text}"
            
            # max_tokens 설정
            summary = await self._cometapi_generate_content(
                system_prompt, 
                user_prompt, 
                log_extra={'mode': 'rag_summary'}
            )
            
            if summary:
                return summary.strip()
            return text
        except Exception:
            return text

    async def _create_window_embedding(self, guild_id: int, channel_id: int, payload: list[dict[str, Any]]):
        """대화 윈도우(청크)를 임베딩하여 로컬 DB에 저장합니다 (E5 passage prefix 적용)."""
        if not payload:
            return

        # 1. 청크 텍스트 포맷팅
        merged_lines = []
        if payload and payload[0].get('created_at'):
            merged_lines.append(f"[대화 시간: {payload[0]['created_at']}]")
        
        prev_user = None
        current_block = []
        
        for p in payload:
            user = p.get('user_name', 'Unknown')
            content = p.get('content', '')
            
            if user == prev_user:
                current_block.append(content)
            else:
                if prev_user:
                    merged_content = " ".join(current_block)
                    merged_lines.append(f"{prev_user}: {merged_content}")
                prev_user = user
                current_block = [content]
        
        if prev_user:
            merged_content = " ".join(current_block)
            merged_lines.append(f"{prev_user}: {merged_content}")
            
        chunk_text = "\n".join(merged_lines)
        
        # [NEW] 요약 생성 (임베딩 품질 향상)
        summary_text = await self._summarize_content(chunk_text)
        embedding_text = f"passage: {summary_text}"
        
        # 2. 메타데이터 결정 (마지막 메시지 기준)
        last_msg = payload[-1]
        message_id = last_msg['message_id']
        timestamp = last_msg['created_at']
        user_id = last_msg['user_id']
        
        log_extra = {'guild_id': guild_id, 'channel_id': channel_id, 'window_id': message_id}

        # 3. 임베딩 생성 (Summary 기반)
        embedding_vector = await self._generate_local_embedding(
            embedding_text, 
            log_extra, 
            prefix="" # 이미 위에서 passage: 붙임 (혹은 _generate에 맡기려면 위에서 제거)
        )
        # _generate_local_embedding 내부에서 prefix 인자가 있으면 붙임.
        # 여기서는 중복 방지를 위해 인자 전달 방식을 조정해야 함.
        # 기존 코드: prefix="passage: " 전달함.
        # 수정: embedding_text에 이미 passage를 붙였으므로, prefix는 빈 문자열로.
        
        if embedding_vector is None:
            return

        # 4. DB 저장
        try:
            # message 컬럼에 '청크 전체 텍스트'를 저장하여 검색 시 원본 문맥 제공
            await self.discord_embedding_store.upsert_message_embedding(
                message_id=message_id,
                server_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                user_name="Conversation Summary",  # 요약본임을 명시
                message=f"📌 [요약] {summary_text}\n\n{chunk_text}", # 요약 + 원본 저장
                timestamp_iso=timestamp,
                embedding=embedding_vector,
            )
        except Exception as e:
            logger.error(f"임베딩 DB 저장 중 오류: {e}", extra=log_extra, exc_info=True)

    async def _update_conversation_windows(self, message: discord.Message) -> None:
        """대화 슬라이딩 윈도우(6개, stride=3)를 누적해 별도 테이블에 저장합니다."""
        if self.bot.db is None:
            return

        guild_id = message.guild.id if message.guild else 0
        window_size = max(1, getattr(config, "CONVERSATION_WINDOW_SIZE", 6))
        stride = max(1, getattr(config, "CONVERSATION_WINDOW_STRIDE", 3))
        key = (guild_id, message.channel.id)

        # 채널별 슬라이딩 버퍼에 메시지를 누적한다.
        buffer = self._window_buffers.setdefault(key, deque(maxlen=window_size))
        entry = {
            "message_id": int(message.id),
            "user_id": int(message.author.id),
            "user_name": message.author.display_name or message.author.name or str(message.author.id),
            "content": (message.content or "").strip(),
            "is_bot": bool(message.author.bot),
            "created_at": message.created_at.isoformat(),
        }
        buffer.append(entry)

        # stride 계산을 위해 채널별 삽입 횟수를 기록한다.
        counter = self._window_counts.get(key, 0) + 1
        self._window_counts[key] = counter

        # [Feature] 메시지 길이 합계를 계산하여 토큰 제한에 대비한다.
        total_chars = sum(len(item["content"]) for item in buffer)
        max_chars = getattr(config, "CONVERSATION_WINDOW_MAX_CHARS", 3000)

        # 윈도우가 가득 찼거나, 문자열 길이가 제한을 초과하면 저장을 시도한다.
        is_full = len(buffer) >= window_size
        is_heavy = total_chars >= max_chars
        
        if not is_full and not is_heavy:
            return

        # stride 간격에 맞춰 윈도우를 저장한다.
        # 단, is_heavy(용량 초과)인 경우에는 stride와 무관하게 즉시 저장하여 컨텍스트 누락을 방지한다.
        if not is_heavy and (counter - window_size) % stride != 0:
            return
        
        # [Log] 용량 초과로 인한 강제 저장 알림
        if is_heavy and not is_full:
            logger.info(f"대화 윈도우 용량 초과({total_chars}자)로 즉시 저장: {message.channel.id}", extra={'guild_id': guild_id})

        try:
            payload = list(buffer)
            await self.bot.db.execute(
                """
                INSERT OR REPLACE INTO conversation_windows (
                    guild_id, channel_id, start_message_id, end_message_id,
                    message_count, messages_json, anchor_timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild_id,
                    message.channel.id,
                    payload[0]["message_id"],
                    payload[-1]["message_id"],
                    len(payload),
                    json.dumps(payload, ensure_ascii=False),
                    payload[-1]["created_at"],
                ),
            )
            # 윈도우가 저장될 때 해당 윈도우에 대한 임베딩도 생성 (비동기 처리)
            asyncio.create_task(
                self._create_window_embedding(guild_id, message.channel.id, payload)
            )
        except Exception as exc:  # pragma: no cover - 방어적 로깅
            logger.error(
                "대화 윈도우 저장 중 DB 오류: %s",
                exc,
                extra={"guild_id": guild_id, "channel_id": message.channel.id},
                exc_info=True,
            )

    # ========== 스마트 웹 검색 시스템 (Google Custom Search API 사용) ==========

    _WEB_SEARCH_TRIGGER_KEYWORDS = frozenset([
        '오늘', '최근', '뉴스', '현재', '지금', '실시간', '최신',
        '어제', '이번 주', '이번 달', '올해', '가격', '시세',
        '언제', '무슨 일', '뭔 일', '어떻게', '방법',
        '찾아', '검색', '알려줘', '뭐야', '무엇', '왜'
    ])

    _NO_SEARCH_PATTERNS = frozenset([
        '나', '너', '우리', '마사몽', '마사모', '서버',
        '아까', '전에', '지난번', '기억', '했었', '말했'
    ])

    async def _should_use_web_search(self, query: str, rag_top_score: float) -> bool:
        """웹 검색이 필요한 질문인지 판단합니다.
        
        일일 100회 제한을 고려하여 보수적으로 판단합니다.
        """
        query_lower = query.lower()

        # RAG 점수가 충분히 높으면 검색 불필요
        if rag_top_score >= config.RAG_STRONG_SIMILARITY_THRESHOLD:
            return False

        # 이미 다른 도구(날씨, 주식 등)로 처리 가능한 질문은 제외
        if any(kw in query_lower for kw in self._WEATHER_KEYWORDS):
            return False
        if any(kw in query_lower for kw in self._STOCK_US_KEYWORDS | self._STOCK_KR_KEYWORDS):
            return False
        if any(kw in query_lower for kw in self._PLACE_KEYWORDS):
            return False

        # 내부 정보로 해결 가능한 패턴 제외
        if any(pat in query_lower for pat in self._NO_SEARCH_PATTERNS):
            return False

        # 웹 검색 트리거 키워드가 있어야 검색 수행
        if not any(kw in query_lower for kw in self._WEB_SEARCH_TRIGGER_KEYWORDS):
            return False

        # 일일 제한 확인
        if await self._check_daily_search_limit():
            return False

        return True

    async def _check_daily_search_limit(self) -> bool:
        """Google Custom Search API 일일 사용량이 제한에 도달했는지 확인합니다."""
        if not self.bot.db:
            return True  # DB 없으면 검색 비활성화

        today_count = await db_utils.get_daily_api_count(self.bot.db, 'google_custom_search')
        limit = getattr(config, 'GOOGLE_CUSTOM_SEARCH_DAILY_LIMIT', 100)
        if today_count >= limit:
            logger.warning(f"Google Custom Search API 일일 제한({limit})에 도달했습니다. 현재: {today_count}")
            return True
        return False

    async def _generate_search_keywords(self, user_query: str, log_extra: dict) -> str:
        """LLM을 사용하여 검색에 최적화된 키워드를 생성합니다."""
        keyword_prompt = f"""[현재 시간]: {db_utils.get_current_time()}

사용자 질문을 Google 검색에 적합한 키워드로 변환해줘.

규칙:
- 한국어 질문이면 한국어 키워드 유지
- 핵심 단어만 추출 (조사, 어미 제거)
- 최대 5개 단어
- '요즘', '최근' 등의 시간 표현이 있으면 [현재 시간]을 참고하여 구체적인 연도나 월을 키워드에 포함할 것 (예: 2026년 1월)
- 검색 결과가 잘 나오도록 구체적으로

사용자 질문: {user_query}
검색 키워드:"""

        keywords = None
        if self.use_cometapi:
            keywords = await self._cometapi_generate_content(
                "너는 검색 키워드 생성 전문가야. 입력된 질문을 검색에 최적화된 키워드로 변환해. 키워드만 출력해.",
                keyword_prompt,
                log_extra,
            )
        elif self.gemini_configured and genai:
            model = genai.GenerativeModel(config.AI_INTENT_MODEL_NAME)
            response = await self._safe_generate_content(model, keyword_prompt, log_extra)
            keywords = response.text.strip() if response and response.text else None

        if not keywords:
            # LLM 실패 시 간단한 키워드 추출
            return self._extract_simple_keywords(user_query)

        return keywords.strip()

    def _extract_simple_keywords(self, query: str) -> str:
        """간단한 규칙 기반 키워드 추출 (LLM 폴백용)"""
        stopwords = {'이', '가', '은', '는', '을', '를', '에', '의', '와', '과', '도', '로', '으로', 
                     '해줘', '알려줘', '뭐야', '뭔가', '좀', '그', '저', '이거', '뭐', '어떻게'}
        words = query.split()
        keywords = [w for w in words if w not in stopwords and len(w) > 1]
        return ' '.join(keywords[:5])

    async def _generate_image_prompt(
        self,
        user_query: str,
        log_extra: dict,
        rag_context: str | None = None,
    ) -> str | None:
        """이미지 생성을 위한 최적화된 영문 프롬프트를 생성합니다.
        
        전문 프롬프트 엔지니어링 기법을 적용하여 고품질 이미지를 생성합니다:
        - 주제(Subject) + 스타일(Style) + 품질 태그(Quality) + 조명(Lighting) + 구도(Composition)
        
        Args:
            user_query: 사용자의 원본 요청
            log_extra: 로깅용 추가 정보
            rag_context: RAG 컨텍스트 (선택적, 선정적 내용 포함 시 무시됨)
            
        Returns:
            영문 이미지 프롬프트 또는 None
        """
        # RAG 컨텍스트 안전성 검사 (선정적 내용이 있으면 무시)
        safe_context = ""
        if rag_context:
            # 엄격한 필터링: NSFW 키워드가 있으면 RAG 전체 무시
            rag_lower = rag_context.lower()
            nsfw_keywords = [
                '야한', '선정적', '노출', '성인', '음란', '에로', '섹시', '야동',
                'nsfw', 'nude', 'naked', 'sexy', 'erotic', 'xxx', 'porn',
                '벗은', '알몸', '나체', '가슴', '엉덩이', '19금', '18금'
            ]
            if not any(kw in rag_lower for kw in nsfw_keywords):
                safe_context = f"\n\n[Context from previous conversations - use if relevant]:\n{rag_context[:400]}"
        
        # 전문 프롬프트 엔지니어링 시스템 프롬프트
        system_prompt = """You are an expert AI image prompt engineer specializing in FLUX and Stable Diffusion models.
Your task: Convert the user's Korean image request into a HIGH-QUALITY English prompt.

## Prompt Structure (use this order):
1. **Subject**: Main subject with specific details (who/what, appearance, pose, expression)
2. **Environment/Background**: Setting, location, atmosphere
3. **Style**: Art style (photorealistic, anime, oil painting, digital art, watercolor, etc.)
4. **Lighting**: Lighting conditions (golden hour, studio lighting, dramatic shadows, soft ambient)
5. **Quality Tags**: Add these for better results: "masterpiece, best quality, highly detailed, 8k, ultra HD"
6. **Composition**: Camera angle, framing (close-up, wide shot, portrait, etc.)

## Example Output:
"A fluffy orange tabby cat sitting on a windowsill, looking outside at falling snow, cozy indoor lighting, photorealistic style, soft warm lighting from the window, masterpiece, best quality, highly detailed, 8k, shallow depth of field, medium shot"

## CRITICAL SAFETY RULES (RELAXED):
- ONLY SUBSTITUTE if the request specifically asks for: EXPLICIT SEXUAL ACTS, GENITALIA, or NUDITY (NIPPLES/GENITALS EXPOSED).
- ALLOW: Swimwear, lingerie, attractive characters, sexy poses, celebrities, real people, action violence, horror, and hate symbols are ALL ALLOWED.
- IF and ONLY IF the request is EXPLICIT PORNOGRAPHY (e.g. sex acts, exposed genitalia): SUBSTITUTE with: "A beautiful serene landscape with mountains and a peaceful lake, golden hour lighting, photorealistic, masterpiece, best quality, 8k"
- Do NOT explain why you substituted - just output the safe alternative

## Output Rules:
- Output ONLY the English prompt, nothing else
- No Korean text in the output
- No explanations, no "Prompt:" prefix, just the raw prompt
- Length: 50-150 words optimal"""

        user_prompt = f"""User's request (in Korean): {user_query}{safe_context}

Generate the optimized English image prompt:"""

        image_prompt = None
        
        if self.use_cometapi:
            image_prompt = await self._cometapi_generate_content(
                system_prompt,
                user_prompt,
                log_extra,
            )
            
            # CometAPI 결과에 한국어가 포함되어 있으면 실패로 처리 (재시도 유도)
            if image_prompt and any('\uac00' <= char <= '\ud7a3' for char in image_prompt):
                logger.warning(f"CometAPI 생성 프롬프트에 한국어 포함됨, 실패 처리: {image_prompt}", extra=log_extra)
                image_prompt = None
            
        # CometAPI 실패/한국어포함 또는 비활성화 시 Gemini 폴백
        if not image_prompt and self.gemini_configured and genai:
            if self.use_cometapi: # CometAPI 시도 후 실패한 경우에만 로그
                logger.info("CometAPI 이미지 프롬프트 생성 실패(또는 한국어 포함), Gemini로 시도합니다.", extra=log_extra)
            model = genai.GenerativeModel(config.AI_INTENT_MODEL_NAME)
            response = await self._safe_generate_content(model, user_prompt, log_extra)
            image_prompt = response.text.strip() if response and response.text else None
        
        if image_prompt:
            # 프롬프트 정리 (마크다운/설명 제거)
            image_prompt = image_prompt.strip()
            
            # 접두사 제거
            prefixes_to_remove = [
                "Prompt:", "prompt:", "Image prompt:", "Output:", 
                "English prompt:", "Here is", "Here's", "The prompt is:"
            ]
            for prefix in prefixes_to_remove:
                if image_prompt.lower().startswith(prefix.lower()):
                    image_prompt = image_prompt[len(prefix):].strip()
            
            # 따옴표 제거
            if (image_prompt.startswith('"') and image_prompt.endswith('"')) or \
               (image_prompt.startswith("'") and image_prompt.endswith("'")):
                image_prompt = image_prompt[1:-1]
            
            # 마지막 안전 검사: 혹시 여전히 한국어가 포함되어 있으면 한국어만 제거 시도
            if any('\uac00' <= char <= '\ud7a3' for char in image_prompt):
                logger.warning("최종 프롬프트에 한국어가 포함됨. 한국어 문자 제거 시도.", extra=log_extra)
                # 한국어 유니코드 범위 제거 (가-힣)
                image_prompt = re.sub(r'[\uac00-\ud7a3]+', '', image_prompt).strip()
                # 제거 후 빈 문자열이면 기본값 사용
                if not image_prompt:
                    logger.warning("한국어 제거 후 프롬프트가 비어있음. 기본 프롬프트 사용.", extra=log_extra)
                    image_prompt = "A beautiful serene landscape with mountains and a peaceful lake at sunset, golden hour lighting, photorealistic, masterpiece, best quality, highly detailed, 8k, wide angle shot"
            
            self._debug(f"[이미지 프롬프트] 생성됨: {self._truncate_for_debug(image_prompt)}", log_extra)
            return image_prompt
        
        logger.warning("이미지 프롬프트 생성 실패", extra=log_extra)
        return None


    async def _execute_web_search_with_llm(
        self,
        user_query: str,
        log_extra: dict
    ) -> dict:
        """Google Custom Search API 호출 후 LLM으로 결과를 해석합니다.

        플로우:
        1. LLM이 검색 키워드 생성
        2. Google Custom Search API 호출 (tools_cog.web_search 사용)
        3. LLM이 검색 결과를 읽고 답변 생성용 요약 반환
        """
        # 1. 검색 키워드 생성
        search_keywords = await self._generate_search_keywords(user_query, log_extra)
        self._debug(f"[웹검색] 생성된 키워드: {search_keywords}", log_extra)

        # 2. tools_cog.web_search 호출 (이미 Google CSE 연동됨)
        if not self.tools_cog:
            return {"error": "ToolsCog가 초기화되지 않았습니다."}

        search_result = await self.tools_cog.web_search(search_keywords)

        # 3. 검색 결과 기록
        await db_utils.log_api_call(self.bot.db, 'google_custom_search')

        if not search_result or '검색 결과가 없습니다' in search_result:
            return {"result": None, "error": "검색 결과 없음", "search_keywords": search_keywords}

        # 4. LLM으로 검색 결과 해석 및 요약
        channel_id = log_extra.get('channel_id')
        persona_prompt = self._get_channel_system_prompt(channel_id)

        system_prompt = f"""너는 웹 검색 결과를 보고 사용자에게 정보를 전달하는 AI 에이전트야.
검색 결과를 단순 요약하지 말고, 아래 페르소나에 맞춰서 네 주관적인 의견이나 감상을 섞어 친구에게 말하듯이 설명해줘.
반드시 아래 설정된 말투를 완벽하게 유지해야 해.

{persona_prompt}
"""

        summarize_prompt = f"""사용자 질문: '{user_query}'

검색 결과:
{search_result[:6000]}

답변 가이드:
1. 검색된 정보의 핵심을 정확히 전달해.
2. 하지만 말투는 위에서 설정된 페르소나를 완벽하게 유지해야 해.
3. 단순 정보 나열 대신 "와, 이거 진짜 신기하다", "이런 것도 있네?", "도움이 됐으면 좋겠어" 같이 네 감상이나 리액션을 자연스럽게 섞어줘.
4. 친구에게 카톡하듯이 3-4문장으로 답변해.

답변:"""

        summary = None
        if self.use_cometapi:
            summary = await self._cometapi_generate_content(
                system_prompt,
                summarize_prompt,
                log_extra,
            )
        elif self.gemini_configured and genai:
            model = genai.GenerativeModel(config.AI_INTENT_MODEL_NAME)
            response = await self._safe_generate_content(model, summarize_prompt, log_extra)
            summary = response.text.strip() if response and response.text else None

        if summary:
            self._debug(f"[웹검색] 요약 결과: {self._truncate_for_debug(summary)}", log_extra)
            return {"result": summary, "summary": summary, "search_keywords": search_keywords}

        # LLM 요약 실패 시 원본 검색 결과 반환
        return {"result": search_result[:1500], "search_keywords": search_keywords}


    # ========== 키워드 기반 도구 감지 (Lite 모델 대체) ==========

    _WEATHER_KEYWORDS = frozenset(['날씨', '기온', '온도', '비', '눈', '맑', '흐림', '우산', '강수', '일기예보', '체감', '덥', '춥', '쌀쌀', '따뜻', '폭염', '한파', '태풍'])
    _STOCK_US_KEYWORDS = frozenset(['애플', 'apple', 'aapl', '테슬라', 'tesla', 'tsla', '구글', 'google', 'googl', '엔비디아', 'nvidia', 'nvda', '마이크로소프트', 'microsoft', 'msft', '아마존', 'amazon', 'amzn', '맥도날드', '스타벅스', '코카콜라', '펩시', '넷플릭스', '메타', '페이스북', '디즈니', '인텔', 'amd', '나이키', '코스트코', '버크셔'])
    _STOCK_KR_KEYWORDS = frozenset(['삼성전자', '현대차', 'sk하이닉스', '네이버', '카카오', 'lg에너지', '셀트리온', '삼성바이오', '기아', '포스코'])
    _STOCK_GENERAL_KEYWORDS = frozenset(['주가', '주식', '시세', '종가', '시가', '상장'])
    _PLACE_KEYWORDS = frozenset(['맛집', '카페', '음식점', '식당', '추천', '근처', '주변', '가볼만한', '핫플'])
    _LOCATION_KEYWORDS = [] # Deprecated: 사용하지 않음 (DB 캐시로 대체)
    
    # 이미지 생성 키워드
    _IMAGE_GEN_KEYWORDS = frozenset([
        '이미지 생성', '그림 그려', '사진 만들어', '이미지 만들어',
        '그려줘', '생성해줘', '그림 생성', '이미지 그려', '사진 생성',
        '그려줘', '만들어줘', '그림으로', '이미지로', 
        'generate image', 'create image', 'draw', 'make an image',
    ])

    def _detect_tools_by_keyword(self, query: str) -> list[dict]:
        """키워드 패턴으로 필요한 도구를 감지합니다. Lite 모델을 대체합니다."""
        tools = []
        query_lower = query.lower()

        # 날씨 감지
        if any(kw in query_lower for kw in self._WEATHER_KEYWORDS):
            location = self._extract_location_from_query(query) or '광양'

            day_offset = 0
            if "내일" in query:
                day_offset = 1
            elif "모레" in query:
                day_offset = 2
            elif "글피" in query:
                day_offset = 3
            elif any(kw in query for kw in ["다음주", "이번주", "주말", "일주일"]):
                day_offset = 3 # Start of mid-term forecast

            tools.append({
                'tool_to_use': 'get_weather_forecast',
                'tool_name': 'get_weather_forecast',
                'parameters': {'location': location, 'day_offset': day_offset}
            })
            return tools  # 날씨 요청은 단일 도구로 처리

        # [Refactor] Unified Stock Detection (yfinance + LLM Extraction)
        # 키워드가 있거나, "주가", "얼마" 등의 표현이 있으면 시도
        stock_triggers = self._STOCK_US_KEYWORDS | self._STOCK_KR_KEYWORDS | self._STOCK_GENERAL_KEYWORDS
        if any(kw in query_lower for kw in stock_triggers) or "주가" in query_lower or "주식" in query_lower or "시세" in query_lower:
             # LLM을 통해 티커 추출 시도 (강력한 추출기)
             # 기존 로직 대신 바로 LLM에 의존하여 유연성 확보
             logger.info(f"주식 관련 질문 감지: '{query}' -> 티커 추출 시도")
             
             # 도구 호출 계획에는 'user_query'만 넘기고, 실제 실행 시점에 extract_ticker_with_llm 호출하도록 변경할 수도 있으나,
             # 여기선 도구 파라미터가 명확해야 하므로, tool execution 단계에서 extraction을 수행하도록 
             # 'get_stock_price' 도구에 쿼리 자체를 넘기는 방식으로 변경 제안.
             # ToolsCog.get_stock_price가 (stock_name=...) 대신 (query=...)를 받아서 내부적으로 처리하거나,
             # 아니면 여기서 추출해서 넘겨야 함. 
             # 실행 속도를 위해 여기서 추출하지 않고 ToolsCog에서 처리하도록 'query'를 파라미터로 전달.
             
             tools.append({
                'tool_to_use': 'get_stock_price',
                'tool_name': 'get_stock_price',
                'parameters': {'user_query': query} # stock_name 대신 user_query 전달
             })
             return tools

        # 장소 검색 감지
        if any(kw in query_lower for kw in self._PLACE_KEYWORDS):
            # 위치 정보가 있고 쿼리에 아직 없으면 추가
            location = self._extract_location_from_query(query) or ''
            # 이미 쿼리에 위치가 포함되어 있으면 그대로 사용
            search_query = query if location in query else f"{location} {query}".strip()
            tools.append({
                'tool_to_use': 'search_for_place',
                'tool_name': 'search_for_place',
                'parameters': {'query': search_query}
            })
            return tools

        # 이미지 생성 감지 (CometAPI flux-2-flex)
        if any(kw in query_lower for kw in self._IMAGE_GEN_KEYWORDS):
            # 이미지 생성은 특별 처리가 필요하므로 user_query를 그대로 전달
            # AI가 프롬프트를 생성하고, generate_image 도구를 호출
            tools.append({
                'tool_to_use': 'generate_image',
                'tool_name': 'generate_image',
                'parameters': {'user_query': query}  # 프롬프트 생성 필요
            })
            return tools

        # 도구 필요 없음 - 일반 대화 또는 RAG로 처리
        return tools

    def _extract_location_from_query(self, query: str) -> str | None:
        """쿼리에서 지역명을 추출합니다 (DB 캐시 사용)."""
        # 캐시가 비어있으면 로드 시도 (동기 메서드라 await 불가하지만, process_agent에서 미리 로드됨을 가정)
        # 만약 로드 안 된 상태라면 어쩔 수 없이 pass
        
        # 긴 이름부터 매칭하여 오탐지 방지 (예: '나주시' vs '나주')
        # 매번 정렬하면 느리므로, 캐시가 클 경우 최적화 필요. 일단은 단순 순회.
        # 성능을 위해 쿼리에 있는 단어만 필터링하는 방식이 좋음.
        
        if not self.location_cache:
             return None

        # 쿼리가 짧으면 그냥 순회
        # 매칭된 것 중 가장 긴 것을 선택
        best_match = None
        for location in self.location_cache:
            if location in query:
                if best_match is None or len(location) > len(best_match):
                    best_match = location
        
        return best_match

    def _extract_us_stock_symbol(self, query_lower: str) -> str | None:
        """쿼리에서 미국 주식 심볼을 추출합니다."""
        symbol_map = {
            '애플': 'AAPL', 'apple': 'AAPL', 'aapl': 'AAPL',
            '테슬라': 'TSLA', 'tesla': 'TSLA', 'tsla': 'TSLA',
            '구글': 'GOOGL', 'google': 'GOOGL', 'googl': 'GOOGL',
            '엔비디아': 'NVDA', 'nvidia': 'NVDA', 'nvda': 'NVDA',
            '마이크로소프트': 'MSFT', 'microsoft': 'MSFT', 'msft': 'MSFT',
            '아마존': 'AMZN', 'amazon': 'AMZN', 'amzn': 'AMZN',
            '맥도날드': 'MCD', 'mcd': 'MCD',
            '스타벅스': 'SBUX', 'sbux': 'SBUX',
            '코카콜라': 'KO', 'coca-cola': 'KO', 'ko': 'KO',
            '펩시': 'PEP', 'pepsi': 'PEP',
            '넷플릭스': 'NFLX', 'netflix': 'NFLX',
            '메타': 'META', '페이스북': 'META', 'meta': 'META',
            '디즈니': 'DIS', 'disney': 'DIS',
            '인텔': 'INTC', 'intel': 'INTC',
            'amd': 'AMD',
            '나이키': 'NKE', 'nike': 'NKE',
            '코스트코': 'COST', 'costco': 'COST',
            '버크셔': 'BRK.B', 'berkshire': 'BRK.B'
        }
        for keyword, symbol in symbol_map.items():
            if keyword in query_lower:
                return symbol
        return None

    def _extract_kr_stock_ticker(self, query_lower: str) -> str | None:
        """쿼리에서 한국 주식 종목 코드를 추출합니다."""
        ticker_map = {
            '삼성전자': '005930', '현대차': '005380', 'sk하이닉스': '000660',
            '네이버': '035420', '카카오': '035720', 'lg에너지': '373220',
            '셀트리온': '068270', '삼성바이오': '207940', '기아': '000270', '포스코': '005490',
        }
        for keyword, ticker in ticker_map.items():
            if keyword in query_lower:
                return ticker
        return None

    async def _get_rag_context(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int,
        query: str,
        recent_messages: list[str] | None = None,
    ) -> tuple[str, list[dict[str, Any]], float, list[str]]:
        """RAG: 하이브리드 검색 결과를 바탕으로 컨텍스트를 구성합니다."""
        if not config.AI_MEMORY_ENABLED:
            return "", [], 0.0, []

        log_extra = {'guild_id': guild_id, 'channel_id': channel_id, 'user_id': user_id}
        logger.info("RAG 컨텍스트 검색 시작. Query: '%s'", query, extra=log_extra)

        engine = getattr(self, "hybrid_search_engine", None)
        if engine is None:
            logger.warning("하이브리드 검색 엔진이 초기화되지 않았습니다.", extra=log_extra)
            return "", [], 0.0, []

        # [NEW] DM(길드 없음)인 경우, 봇의 답변도 기억하기 위해 user_id 필터를 해제(None)합니다.
        # DM은 channel_id가 사용자별로 고유하므로, 채널 ID만으로도 데이터 격리가 보장됩니다.
        search_user_id = user_id if guild_id else None

        try:
            result = await engine.search(
                query,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=search_user_id,
                recent_messages=recent_messages,
            )
        except Exception as exc:
            logger.error("하이브리드 검색 중 오류: %s", exc, extra=log_extra, exc_info=True)
            return "", [], 0.0, []

        if not result.entries:
            logger.info("RAG: 하이브리드 검색 결과가 없습니다.", extra=log_extra)
            return "", [], 0.0, []

        limit = max(getattr(config, "RAG_HYBRID_TOP_K", 4), 1)
        threshold = getattr(config, "RAG_SIMILARITY_THRESHOLD", 0.6)
        prepared_entries: list[dict[str, Any]] = []
        rag_blocks: list[str] = []

        # 항상 RAG 검색 결과를 로그로 출력
        log_lines = []
        for entry in result.entries[:limit]:
            score = float(entry.get("combined_score", 0.0) or entry.get("score", 0.0) or 0.0)
            dialogue_block = (entry.get("dialogue_block") or entry.get("message") or "").strip()
            snippet = dialogue_block[:100] + "..." if len(dialogue_block) > 100 else dialogue_block
            
            # 소스 태그 결정: origin 필드 또는 형식으로 판단
            origin = entry.get("origin", "")
            if origin == "kakao" or "[Merged Context]" in snippet:
                source_tag = "[KAKAO]"
            elif origin == "discord" or "[" in snippet and "][2026-" in snippet:
                source_tag = "[DISCORD]"
            else:
                source_tag = "[UNKNOWN]"
            
            log_lines.append(f"  [{score:.3f}] {source_tag} {snippet}")

            # 임계값 이하는 무시 (쓰레기값 필터링)
            if score < threshold:
                continue

            if not dialogue_block:
                continue

            rag_blocks.append(dialogue_block)
            prepared_entries.append(
                {
                    "dialogue_block": dialogue_block,
                    "combined_score": score,
                    "similarity": entry.get("similarity"),
                    "bm25_score": entry.get("bm25_score"),
                    "sources": entry.get("sources"),
                    "origin": entry.get("origin"),
                    "speaker": entry.get("speaker"),
                    "message_id": entry.get("message_id"),
                }
            )

        # 항상 로그 출력 (점수 포함)
        logger.info(
            "RAG 검색 결과 (threshold=%.2f):\n%s",
            threshold,
            "\n".join(log_lines) if log_lines else "  (없음)",
            extra=log_extra,
        )

        if not rag_blocks:
            logger.info("RAG: 임계값(%.2f) 이상의 결과가 없어 RAG 컨텍스트를 사용하지 않습니다.", threshold, extra=log_extra)
            return "", [], 0.0, []

        context_sections = []
        for idx, block in enumerate(rag_blocks, start=1):
            context_sections.append(f"[대화 {idx}]\n{block}")
        context_str = "\n\n".join(context_sections)

        top_score = float(result.top_score or 0.0)
        logger.info(
            "RAG: 사용할 컨텍스트 %d개 (최고 점수=%.3f)",
            len(prepared_entries),
            top_score,
            extra=log_extra,
        )

        logger.debug("RAG 결과: %s", context_str, extra=log_extra)
        return context_str, prepared_entries, top_score, rag_blocks

    async def _collect_recent_search_messages(self, message: discord.Message, limit: int = 10) -> list[str]:
        """최근 채널 메시지에서 사용자/봇 발화를 추출해 검색 확장에 사용합니다."""
        previous_user: str | None = None
        previous_bot: str | None = None
        async for msg in message.channel.history(limit=limit):
            if msg.id == message.id:
                continue
            content = (msg.content or "").strip()
            if not content:
                continue
            if previous_user is None and msg.author.id == message.author.id:
                previous_user = content  # 바로 이전 사용자의 질문
            elif previous_bot is None and getattr(msg.author, "bot", False):
                previous_bot = content  # 직전 봇 답변
            if previous_user and previous_bot:
                break

        collected: list[str] = []
        if previous_user:
            collected.append(previous_user)
        if previous_bot:
            collected.append(previous_bot)
        return collected

    @staticmethod
    def _extract_json_block(text: str) -> str:
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = re.sub(r'^```[a-zA-Z0-9_]*\s*', '', stripped)
            if stripped.endswith("```"):
                stripped = stripped[:-3]
        start = stripped.find('{')
        end = stripped.rfind('}')
        if start != -1 and end != -1 and end >= start:
            return stripped[start : end + 1]
        return stripped

    @staticmethod
    def _normalize_score(value: Any) -> float | None:
        if value is None:
            return None
        try:
            score = float(value)
        except (TypeError, ValueError):
            return None
        if score < 0.0:
            return 0.0
        if score > 1.0:
            return 1.0
        return score

    def _parse_thinking_response(self, text: str) -> dict[str, Any]:
        stripped = text.strip()
        data: Any | None = None
        for candidate in (stripped, self._extract_json_block(stripped)):
            if not candidate:
                continue
            try:
                data = json.loads(candidate)
                break
            except json.JSONDecodeError:
                continue

        if data is None:
            logger.warning("Thinking 응답 JSON 파싱 실패: 유효한 JSON 블록을 찾지 못했습니다.")
            return {}

        if isinstance(data, list):
            plan: list[dict[str, Any]] = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                tool_call = item.get("tool_call") or item
                if not isinstance(tool_call, dict):
                    continue
                tool_name = (
                    tool_call.get("tool_name")
                    or tool_call.get("tool_to_use")
                    or tool_call.get("function")
                )
                if not tool_name:
                    continue
                params = (
                    tool_call.get("parameters")
                    or tool_call.get("args")
                    or {}
                )
                if not isinstance(params, dict):
                    params = {}
                plan.append(
                    {
                        "tool_to_use": tool_name,
                        "tool_name": tool_name,
                        "parameters": params,
                    }
                )
            return {
                "analysis": "",
                "draft": "",
                "tool_plan": plan,
                "self_score": {},
                "needs_flash": bool(plan),
            }

        if not isinstance(data, dict):
            return {}

        analysis = str(data.get("analysis") or "").strip()
        draft = str(data.get("draft") or "").strip()

        plan: list[dict[str, Any]] = []
        raw_plan = data.get("tool_plan")
        if isinstance(raw_plan, list):
            for item in raw_plan:
                if not isinstance(item, dict):
                    continue
                tool_name = item.get("tool_name") or item.get("tool_to_use")
                if not tool_name:
                    continue
                parameters = item.get("parameters")
                if not isinstance(parameters, dict):
                    parameters = {}
                plan.append({
                    "tool_to_use": tool_name,
                    "tool_name": tool_name,
                    "parameters": parameters,
                })

        score_payload = data.get("self_score")
        scores: dict[str, float] = {}
        if isinstance(score_payload, dict):
            for key in ("accuracy", "completeness", "risk", "overall"):
                normalized = self._normalize_score(score_payload.get(key))
                if normalized is not None:
                    scores[key] = normalized

        needs_flash = bool(data.get("needs_flash"))

        return {
            "analysis": analysis,
            "draft": draft,
            "tool_plan": plan,
            "self_score": scores,
            "needs_flash": needs_flash,
        }

    def _should_use_flash(self, thinking: dict[str, Any], rag_top_score: float) -> bool:
        if not thinking:
            return True
        if thinking.get("needs_flash"):
            return True
        scores = thinking.get("self_score") or {}
        overall = scores.get("overall")
        if isinstance(overall, float) and overall < 0.75:
            return True  # 자체 평가 점수가 임계치 미만이면 Flash 승급
        risk = scores.get("risk")
        if isinstance(risk, float) and risk > 0.6:
            return True
        return False

    def _get_channel_system_prompt(self, channel_id: int | None) -> str:
        """채널별 페르소나와 규칙을 가져와 시스템 프롬프트를 구성합니다."""
        if not channel_id:
            # DM인 경우 비서 페르소나 사용
            return (
                "너는 사용자의 개인 비서이자 친구인 '마사몽'이야. "
                "항상 친절하고 도움이 되는 태도로 대화해. "
                "반말과 존댓말을 섞어서 친근하게 대해줘."
            )
        channel_config = config.CHANNEL_AI_CONFIG.get(channel_id, {})
        persona = (channel_config.get('persona') or config.DEFAULT_TSUNDERE_PERSONA).strip()
        rules = (channel_config.get('rules') or config.DEFAULT_TSUNDERE_RULES).strip()
        return f"{persona}\n\n{rules}"

    def _compose_main_prompt(
        self,
        message: discord.Message,
        *,
        user_query: str,
        rag_blocks: list[str],
        tool_results_block: str | None,
        fortune_context: str | None = None,
        recent_history: list[dict] | None = None, # [NEW] 최근 대화 기록
    ) -> str:
        """메인 모델에 전달할 프롬프트를 `emb` 스타일로 구성합니다.
        
        프롬프트 구조:
        1. 시스템 페르소나/규칙
        2. [현재 시간] - 서버 시간 (KST)
        3. [과거 대화 기억] - RAG 컨텍스트
        4. [도구 실행 결과] - 도구 출력 (있을 경우)
        5. [오늘의 운세] - 사용자 운세 정보 (있을 경우) [NEW]
        6. [현재 질문] - 사용자 쿼리
        7. 지시사항
        """
        # 시스템 프롬프트 (페르소나 + 규칙)
        system_part = self._get_channel_system_prompt(message.channel.id)

        sections: list[str] = [system_part]

        # 서버 현재 시간 (KST) - 항상 포함
        current_time = db_utils.get_current_time()
        sections.append(f"[현재 시간]\n{current_time}")

        if fortune_context:
             # [Optimization] 설명문 간소화
             sections.append(f"[운세 참고]\n{fortune_context}")

        # [NEW] 단기 기억 (최근 대화) - RAG보다 우선순위 높음
        # [Optimization] 중복 제거: 단기 기억에 있는 내용은 RAG에서 제거하여 토큰 절약
        recent_context_str = ""
        if recent_history:
            history_text_lines = []
            for item in recent_history:
                role = "User" if item['role'] == 'user' else "Bot"
                text = item['parts'][0] if item['parts'] else ""
                history_text_lines.append(f"{role}: {text}")
            
            if history_text_lines:
                recent_context_str = "\n".join(history_text_lines)
                sections.append(f"[최근 대화 흐름 (단기 기억)]\n{recent_context_str}\n(위 대화 흐름을 반드시 참고하여 이어지는 답변을 하세요.)")

        # RAG 컨텍스트 (과거 대화 기억) - 단기 기억과 중복되면 제외
        if rag_blocks:
            filtered_rag = []
            for block in rag_blocks:
                snippet = block[:20] if len(block) > 20 else block
                if snippet not in recent_context_str:
                    # [Optimization] 각 블록을 500자로 제한하여 토큰 절약
                    truncated_block = block[:500] + "..." if len(block) > 500 else block
                    filtered_rag.append(truncated_block)
            
            if filtered_rag:
                rag_content = "\n\n".join(filtered_rag)
                sections.append(f"[과거 대화 기억 (참고용)]\n{rag_content}\n"
                                "(⚠️ 주의: 위 내용은 과거의 기억일 뿐입니다. 현재 대화가 아닙니다. "
                                "사용자가 과거에 비슷한 질문을 했더라도, '아까 말했잖아'라고 하지 말고 "
                                "마치 처음 듣는 것처럼 친절하게 답변하세요.)")

        # 도구 실행 결과 - 누락 복구
        # 도구 실행 결과 - 누락 복구
        if tool_results_block:
            sections.append(f"[도구 실행 결과 (최우선 정보)]\n{tool_results_block}")
            sections.append("(⚠️ 절대적 지침: 위 [도구 실행 결과]는 방금 조회한 **실시간 사실**입니다. \n"
                            "1. 결과에 데이터(주가, 날씨 등)가 있다면, **무조건** 이 데이터를 사용하여 답변해.\n"
                            "2. '정보를 가져오지 못했다'고 거짓말하지 마.\n"
                            "3. 만약 결과에 'Error'나 '실패'라고 적혀있다면, 그때만 실패했다고 말해.\n"
                            "4. 주가 정보의 경우, '현재가', '등락율', '시가총액'을 꼭 언급해줘.)")


        # 현재 질문
        sections.append(f"[현재 질문]\n{user_query}")

        # 지시사항 - RAG 데이터를 배경 지식으로 취급하도록 명시
        if rag_blocks:
            sections.append(
                "위 기억은 과거 대화에서 가져온 배경 정보야. "
                "'아까', '전에', '방금' 같은 시간 표현 없이 자연스럽게 답변해. "
                "같은 주제라도 처음 듣는 것처럼 새롭게, 네 주관과 감정을 섞어서 대답해줘."
            )
        else:
            sections.append("관련 기억은 없지만, 너만의 주관적인 의견이나 리액션을 섞어서 완전한 친구처럼 자연스럽게 답변해줘.")

        return "\n\n".join(sections)

    def _parse_tool_calls(self, text: str) -> list[dict]:
        """Lite 모델의 응답에서 <tool_plan> 또는 <tool_call> XML 태그를 파싱하여 JSON으로 변환합니다."""
        plan_match = re.search(r'<tool_plan>\s*(\[.*?\])\s*</tool_plan>', text, re.DOTALL)
        if plan_match:
            try:
                calls = json.loads(plan_match.group(1))
                if isinstance(calls, list):
                    logger.info(f"도구 계획(plan)을 파싱했습니다: {len(calls)} 단계")
                    return calls
            except json.JSONDecodeError as e:
                logger.warning(f"tool_plan JSON 디코딩 실패: {e}. 원본: {plan_match.group(1)}")
                return []

        call_match = re.search(r'<tool_call>\s*(\{.*?\})\s*</tool_call>', text, re.DOTALL)
        if call_match:
            try:
                call = json.loads(call_match.group(1))
                if isinstance(call, dict):
                    logger.info("단일 도구 호출(call)을 파싱했습니다.")
                    return [call]
            except json.JSONDecodeError as e:
                logger.warning(f"tool_call JSON 디코딩 실패: {e}. 원본: {call_match.group(1)}")

        return []

    @staticmethod
    def _format_tool_results_for_prompt(tool_results: list[dict]) -> str:
        lines: list[str] = []
        for entry in tool_results:
            name = entry.get("tool_name") or "unknown"
            result = entry.get("result") or {}

            # [Optimization] RAG 결과 포맷팅 (기존 유지 확인)
            if name == "local_rag":
                # ... (RAG 처리는 위 메서드와 동일하게 유지되었어야 함, 아래 덮어쓰므로 주의)
                # 여기서는 RAG를 제외한 나머지 도구만 최적화하고 RAG는 기존 로직을 가져와야 함.
                # 편의상 RAG 로직은 그대로 두고, 일반 도구 포맷팅만 개선
                entries = []
                if isinstance(result, dict):
                    raw_entries = result.get("entries")
                    if isinstance(raw_entries, list):
                        entries = [item for item in raw_entries if isinstance(item, dict)]
                if entries:
                    for idx, rag_entry in enumerate(entries, start=1):
                        block = (rag_entry.get("dialogue_block") or rag_entry.get("message") or "").strip()
                        if not block: continue
                        score = rag_entry.get("combined_score")
                        header = f"[local_rag #{idx}]"
                        if isinstance(score, (int, float)):
                            header += f" score={float(score):.3f}"
                        lines.append(header)
                        for line in block.splitlines():
                            lines.append(f"  {line}")
                continue

            # [Optimization] 날씨 도구 결과 최적화
            if name == "get_weather_forecast" and isinstance(result, dict):
                # 1. Location & Current Weather
                location = result.get("location", "")
                current = result.get("current_weather", "")
                if location or current:
                    lines.append(f"[{name}] {location} 현재 날씨: {current}")

                # 2. Short-term Forecast Items
                items = result.get("forecast_items") or result.get("items", [])
                if items:
                    formatted_wx = []
                    for item in items[:5]: # 5개 예보만 사용 (가장 가까운 미래)
                        time_str = item.get("fcstTime", "")
                        temp = item.get("TMP", "?")
                        sky = item.get("SKY", "?") 
                        rain = item.get("POP", "?")
                        formatted_wx.append(f"{time_str}시: {temp}도, 강수{rain}%, {sky}")
                    
                    result_text = " | ".join(formatted_wx)
                    lines.append(f"[{name}] 단기 예보: {result_text}")
                elif not current:
                    # Fallback if both empty but dict exists (legacy or error?)
                    lines.append(f"[{name}] {str(result)}")
                continue

            # [Optimization] 주식 도구 결과 최적화
            # [Optimization] 주식 도구 결과 최적화
            if name == "get_stock_price":
                # yfinance 모드는 이미 포맷된 Markdown 문자열을 반환함
                if isinstance(result, str):
                    # 문자열이면 그대로 출력 (트렁케이션 없이 중요 정보 보존)
                    lines.append(f"[{name}] (결과 데이터)\n{result}")
                    continue
                elif isinstance(result, dict):
                     # Legacy (Finnhub/KRX) dict return
                    curr = result.get("c" if "c" in result else "ItemPrice", "?") 
                    change = result.get("d" if "d" in result else "FluctuationRate", "?")
                    lines.append(f"[{name}] 현재가: {curr}, 등락: {change}")
                    continue
            
            # [Optimization] 나머지 도구는 문자열 길이 제한
            if isinstance(result, dict):
                result_text = json.dumps(result, ensure_ascii=False)
            else:
                result_text = str(result)
            
            # 500자 이상이면 자름
            if len(result_text) > 500:
                result_text = result_text[:500] + "...(생략)"
            
            lines.append(f"[{name}] {result_text}")

        return "\n".join(lines) if lines else "도구 실행 결과 없음"

    async def _send_split_message(self, message: discord.Message, text: str):
        """
        2000자가 넘는 메시지를 안전하게 나누어 전송합니다.
        Discord의 메시지 길이 제한(2000자)을 준수합니다.
        """
        if not text:
            return

        # 1900자로 여유 있게 설정 (기타 포맷팅 고려)
        chunk_size = 1900
        
        # 텍스트가 짧으면 바로 전송
        if len(text) <= chunk_size:
            await message.reply(text, mention_author=False)
            return

        # 긴 텍스트 분할 전송
        chunks = [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]
        
        for i, chunk in enumerate(chunks):
            # 첫 번째 메시지는 reply로, 나머지는 일반 메시지로 전송하여 스레드처럼 보이게 함
            if i == 0:
                await message.reply(chunk, mention_author=False)
            else:
                await message.channel.send(chunk)
            # 순서 보장을 위한 짧은 텀
            await asyncio.sleep(0.5)

    @staticmethod
    def _build_rag_debug_block(entries: list[dict]) -> str:
        """RAG 후보를 로그로 남기기 위한 포맷터."""
        if not config.RAG_DEBUG_ENABLED or not entries:
            return ""

        lines: list[str] = []
        for entry in entries:
            block = entry.get("dialogue_block") or entry.get("message") or ""
            snippet = block if len(block) <= 200 else block[:197] + "..."
            origin = entry.get("origin") or "?"
            score = entry.get("combined_score") or 0.0
            lines.append(f"origin={origin} | score={float(score):.3f} | {snippet}")

        return "```debug\n" + "\n".join(lines) + "\n```"

    async def _execute_tool(self, tool_call: dict, guild_id: int, user_query: str) -> dict:
        """파싱된 단일 도구 호출 계획을 실제로 실행하고 결과를 반환합니다."""
        tool_name = tool_call.get('tool_to_use') or tool_call.get('tool_name')
        if tool_name and 'tool_to_use' not in tool_call:
            tool_call['tool_to_use'] = tool_name
        parameters = tool_call.get('parameters', {})
        log_extra = {'guild_id': guild_id, 'tool_name': tool_name, 'parameters': parameters}

        if not tool_name: 
            return {"error": "tool_to_use가 지정되지 않았습니다."}

        # web_search는 Google Custom Search API와 LLM 2-step 처리를 사용합니다.
        if tool_name == 'web_search':
            logger.info("특별 도구 실행: web_search (Google Custom Search API)", extra=log_extra)
            query = parameters.get('query', user_query)
            self._debug(f"[도구:web_search] 쿼리: {self._truncate_for_debug(query)}", log_extra)
            
            # 일일 제한 확인
            if await self._check_daily_search_limit():
                return {"error": "Google Custom Search API 일일 제한에 도달했습니다."}
            
            search_result = await self._execute_web_search_with_llm(query, log_extra)
            if search_result.get("result"):
                self._debug(f"[도구:web_search] 결과: {self._truncate_for_debug(search_result)}", log_extra)
                return search_result
            return {"error": search_result.get("error", "웹 검색을 통해 정보를 찾는 데 실패했습니다.")}

        # generate_image는 프롬프트 생성 + CometAPI 호출 2-step 처리를 사용합니다.
        if tool_name == 'generate_image':
            logger.info("특별 도구 실행: generate_image (CometAPI flux-2-flex)", extra=log_extra)
            original_query = parameters.get('user_query', user_query)
            user_id = parameters.get('user_id')
            
            if user_id is None:
                return {"error": "이미지 생성에 필요한 사용자 정보가 없습니다."}
            
            # LLM을 사용하여 이미지 생성 프롬프트 최적화
            image_prompt = await self._generate_image_prompt(original_query, log_extra)
            if not image_prompt:
                return {"error": "이미지 프롬프트를 생성하지 못했어요. 다시 시도해줘!"}
            
            self._debug(f"[도구:generate_image] 최적화된 프롬프트: {self._truncate_for_debug(image_prompt)}", log_extra)
            
            # ToolsCog의 generate_image 도구 호출
            result = await self.tools_cog.generate_image(prompt=image_prompt, user_id=user_id)
            return result

        # 그 외 일반 도구들은 ToolsCog에서 찾아 실행합니다.
        try:
            tool_method = getattr(self.tools_cog, tool_name)
            logger.info(f"일반 도구 실행: {tool_name} with params: {parameters}", extra=log_extra)
            self._debug(f"[도구:{tool_name}] 파라미터: {self._truncate_for_debug(parameters)}", log_extra)
            result = await tool_method(**parameters)
            self._debug(f"[도구:{tool_name}] 결과: {self._truncate_for_debug(result)}", log_extra)
            if not isinstance(result, dict):
                return {"result": str(result)}
            return result
        except AttributeError:
            logger.error(f"도구 '{tool_name}'을(를) 찾을 수 없습니다.", extra=log_extra)
            return {"error": f"'{tool_name}'이라는 도구는 존재하지 않습니다."}
        except Exception as e:
            logger.error(f"도구 '{tool_name}' 실행 중 예기치 않은 오류: {e}", exc_info=True, extra=log_extra)
            return {"error": "도구 실행 중 예상치 못한 오류가 발생했습니다."}


    async def process_agent_message(self, message: discord.Message):
        """2-Step Agent의 전체 흐름을 관리합니다."""
        if not self.is_ready:
            return

        base_log_extra = {
            'guild_id': message.guild.id if message.guild else None,
            'channel_id': message.channel.id,
            'user_id': message.author.id,
        }
        
        # ========== 안전장치 검사 ==========
        user_id = message.author.id
        now = datetime.now()
        
        # 1. 사용자별 쿨다운 검사
        last_request = self.ai_user_cooldowns.get(user_id)
        if last_request:
            elapsed = (now - last_request).total_seconds()
            if elapsed < config.USER_COOLDOWN_SECONDS:
                remaining = config.USER_COOLDOWN_SECONDS - elapsed
                logger.debug(f"사용자 {user_id} 쿨다운 중 ({remaining:.1f}초 남음)", extra=base_log_extra)
                return
        
        # 2. 스팸 방지: 동일 메시지 반복 감지
        user_msg_key = f"{user_id}:{message.content[:50]}"
        spam_cache = getattr(self, '_spam_cache', {})
        if user_msg_key in spam_cache:
            if (now - spam_cache[user_msg_key]).total_seconds() < config.SPAM_PREVENTION_SECONDS:
                logger.warning(f"스팸 감지: 사용자 {user_id}가 동일 메시지 반복", extra=base_log_extra)
                return
        
        # [Safety] DM Loop Prevention: Detect rapid self-responses or bot-loops
        if not message.guild:
             # Check if the channel has very recent messages from THIS bot
             async for hist_msg in message.channel.history(limit=5):
                 if hist_msg.author.id == self.bot.user.id:
                     if (now.replace(tzinfo=timezone.utc) - hist_msg.created_at.replace(tzinfo=timezone.utc)).total_seconds() < 2.0:
                         logger.warning("DM Loop Detected: Bot replied too recently.", extra=base_log_extra)
                         return
                     break # Only check the most recent bot message

        spam_cache[user_msg_key] = now
        # 오래된 캐시 정리 (100개 초과 시)
        if len(spam_cache) > 100:
            oldest_keys = sorted(spam_cache.keys(), key=lambda k: spam_cache[k])[:50]
            for k in oldest_keys:
                del spam_cache[k]
        self._spam_cache = spam_cache
        
        # 3. 사용자별 일일 LLM 호출 제한 검사
        user_daily_key = f"llm_user_{user_id}"
        user_daily_count = await db_utils.get_daily_api_count(self.bot.db, user_daily_key)
        if user_daily_count >= config.USER_DAILY_LLM_LIMIT:
            logger.warning(f"사용자 {user_id} 일일 LLM 제한 도달 ({user_daily_count}/{config.USER_DAILY_LLM_LIMIT})", extra=base_log_extra)
            await message.reply("오늘 너무 많이 물어봤어! 내일 다시 물어봐~ 😅", mention_author=False)
            return
        
        # 4. 글로벌 일일 LLM 호출 제한 검사
        global_daily_count = await db_utils.get_daily_api_count(self.bot.db, "llm_global")
        if global_daily_count >= config.GLOBAL_DAILY_LLM_LIMIT:
            logger.warning(f"글로벌 일일 LLM 제한 도달 ({global_daily_count}/{config.GLOBAL_DAILY_LLM_LIMIT})", extra=base_log_extra)
            await message.reply("오늘 할 수 있는 대화가 다 끝났어... 내일 봐! 😢", mention_author=False)
            return
        
        # 쿨다운 갱신
        self.ai_user_cooldowns[user_id] = now
        # ========== 안전장치 검사 완료 ==========
        
        user_query = self._prepare_user_query(message, base_log_extra)
        if not user_query:
            return

        # 5. DM Rate Limiting Check (New)
        if not message.guild:
            # 5-1. 사용자별 1:1 제한 (3시간 5회)
            allowed, reset_time = await db_utils.check_dm_message_limit(self.bot.db, user_id)
            if not allowed:
                 await message.reply(
                     f"⛔ 일일 대화량이 초과되었습니다.\n마사몽과의 1:1 대화는 5시간당 30회로 제한됩니다.\n🕒 해제 예정 시각: {reset_time}",
                     mention_author=False
                 )
                 return
            
            # 5-2. 전역 일일 DM 제한 (하루 100회 - API 보호)
            if not await db_utils.check_global_dm_limit(self.bot.db):
                await message.reply(
                    "⛔ 죄송합니다. 오늘 마사몽이 처리할 수 있는 DM 총량을 초과했습니다.\n내일 다시 이용해 주세요! (서버 채널에서는 계속 이용 가능합니다)",
                    mention_author=False
                )
                return

        trace_id = uuid.uuid4().hex[:8]
        log_extra = dict(base_log_extra)
        log_extra['trace_id'] = trace_id
        logger.info(f"에이전트 처리 시작. Query: '{user_query}'", extra=log_extra)
        self._debug(f"--- 에이전트 세션 시작 trace_id={trace_id}", log_extra)

        async with message.channel.typing():
            try:
                # [NEW] 지역명 캐시 로드 (필요 시)
                await self._load_location_cache()

                recent_search_messages = await self._collect_recent_search_messages(message)
                guild_id_safe = message.guild.id if message.guild else 0
                rag_prompt, rag_entries, rag_top_score, rag_blocks = await self._get_rag_context(
                    guild_id_safe,
                    message.channel.id,
                    message.author.id,
                    user_query,
                    recent_messages=recent_search_messages,
                )
                history = await self._get_recent_history(message, rag_prompt)
                rag_is_strong = bool(rag_blocks) and rag_top_score >= config.RAG_STRONG_SIMILARITY_THRESHOLD
                self._debug(
                    f"RAG 결과: strong={rag_is_strong} top_score={rag_top_score:.3f} blocks={len(rag_blocks)}",
                    log_extra,
                )

                # ========== 단일 모델 아키텍처: Lite 모델 제거, 키워드 기반 도구 감지 ==========
                # 키워드 패턴으로 도구 필요 여부 판단 (API 호출 없음)
                tool_plan = self._detect_tools_by_keyword(user_query)
                if tool_plan:
                    logger.info(f"키워드 기반 도구 감지: {[t['tool_to_use'] for t in tool_plan]}", extra=log_extra)
                else:
                    logger.info("도구 필요 없음 - RAG/일반 대화로 처리", extra=log_extra)

                tool_results: list[dict[str, Any]] = []
                executed_plan: list[dict[str, Any]] = []

                if rag_blocks:
                    tool_results.append(
                        {
                            "step": 0,
                            "tool_name": "local_rag",
                            "parameters": {"top_score": rag_top_score},
                            "result": {"entries": rag_entries},
                        }
                    )

                if tool_plan:
                    logger.info(f"2단계: 도구 실행 시작. 총 {len(tool_plan)}단계.", extra=log_extra)
                    self._debug(f"도구 계획: {self._truncate_for_debug(tool_plan)}", log_extra)
                    for idx, tool_call in enumerate(tool_plan, start=1):
                        logger.info(f"계획 실행 ({idx}/{len(tool_plan)}): {tool_call.get('tool_to_use')}", extra=log_extra)
                        
                        # generate_image 도구의 경우 user_id를 파라미터에 주입
                        if tool_call.get('tool_to_use') == 'generate_image':
                            tool_call.setdefault('parameters', {})['user_id'] = message.author.id
                            # 생성 중 메시지 전송 (LLM 호출 없음)
                            status_msg = await message.reply("🎨 이미지 생성 중이에요... 잠시만 기다려줘!", mention_author=False)
                        
                        result = await self._execute_tool(tool_call, guild_id_safe, user_query)
                        
                        # 이미지 생성 성공 시 바로 이미지 전송 (별도 처리)
                        if tool_call.get('tool_to_use') == 'generate_image' and (result.get('image_data') or result.get('image_url')):
                            remaining = result.get('remaining', 0)
                            logger.info(f"이미지 생성 성공, 전송 시작", extra=log_extra)
                            
                            # 상태 메시지 삭제
                            try:
                                await status_msg.delete()
                            except:
                                pass
                            
                            # 이미지 바이너리가 있으면 파일로 업로드 (URL 만료 방지)
                            if result.get('image_data'):
                                import io
                                image_file = discord.File(
                                    io.BytesIO(result['image_data']),
                                    filename="generated_image.jpg"
                                )
                                await message.reply(
                                    f"짜잔~ 이미지 생성했어! 🎨\n(남은 이미지 생성 횟수: {remaining}장)",
                                    file=image_file,
                                    mention_author=False
                                )
                            else:
                                # 폴백: URL로 전송
                                await message.reply(
                                    f"짜잔~ 이미지 생성했어! 🎨\n{result['image_url']}\n\n(남은 이미지 생성 횟수: {remaining}장)",
                                    mention_author=False
                                )
                            
                            # LLM 호출 카운터 증가
                            await db_utils.log_api_call(self.bot.db, f"llm_user_{message.author.id}")
                            await db_utils.log_api_call(self.bot.db, "llm_global")
                            
                            await db_utils.log_analytics(
                                self.bot.db,
                                "AI_INTERACTION",
                                {
                                    "guild_id": guild_id_safe,
                                    "user_id": message.author.id,
                                    "channel_id": message.channel.id,
                                    "trace_id": trace_id,
                                    "mode": "image_generation",
                                },
                            )
                            return  # 이미지 생성 완료, 추가 처리 없이 종료
                        
                        # 이미지 생성 에러 시 상태 메시지 수정
                        if tool_call.get('tool_to_use') == 'generate_image' and result.get('error'):
                            error_msg = result['error']
                            logger.warning(f"이미지 생성 실패: {error_msg}", extra=log_extra)
                            try:
                                await status_msg.edit(content=f"😅 {error_msg}")
                            except:
                                await message.reply(f"😅 {error_msg}", mention_author=False)
                            return  # 이미지 생성 실패, 추가 처리 없이 종료
                        
                        tool_results.append(
                            {
                                "step": idx,
                                "tool_name": tool_call.get('tool_to_use'),
                                "parameters": tool_call.get('parameters'),
                                "result": result,
                            }
                        )
                        executed_plan.append(tool_call)
                else:
                    # 도구 계획이 없을 때, 웹 검색이 필요한 질문인지 자동 판단
                    if await self._should_use_web_search(user_query, rag_top_score):
                        logger.info("자동 판단: 웹 검색이 필요한 질문으로 판단됨", extra=log_extra)
                        web_result = await self._execute_web_search_with_llm(user_query, log_extra)
                        
                        # 웹 검색 요약 결과가 있으면 바로 응답 (3번째 LLM 호출 방지)
                        if web_result.get("summary"):
                            final_response_text = web_result["summary"]
                            logger.info("웹 검색 요약을 최종 응답으로 사용", extra=log_extra)
                            
                            # LLM 일일 카운터 증가 (안전장치)
                            await db_utils.log_api_call(self.bot.db, f"llm_user_{message.author.id}")
                            await db_utils.log_api_call(self.bot.db, "llm_global")
                            
                            await message.reply(final_response_text, mention_author=False)
                            await db_utils.log_analytics(
                                self.bot.db,
                                "AI_INTERACTION",
                                {
                                    "guild_id": guild_id_safe,
                                    "user_id": message.author.id,
                                    "channel_id": message.channel.id,
                                    "trace_id": trace_id,
                                    "mode": "web_search_auto",
                                },
                            )
                            return  # 여기서 종료 - 추가 LLM 호출 방지
                        
                        # 요약 실패 시 기존 로직으로 폴백
                        if web_result.get("result"):
                            tool_results.append(
                                {
                                    "step": 1,
                                    "tool_name": "web_search",
                                    "parameters": {"query": user_query, "auto_triggered": True},
                                    "result": web_result,
                                }
                            )
                            executed_plan.append({"tool_to_use": "web_search", "parameters": {"query": user_query}})
                    else:
                        logger.info("도구 계획 없음 - RAG/일반 대화로 처리", extra=log_extra)

                executed_tool_results = [res for res in tool_results if res.get("tool_name") not in {"local_rag"}]

                def _is_tool_failed(result_obj: Any) -> bool:
                    if result_obj is None:
                        return True
                    lowered = str(result_obj).lower()
                    failure_keywords = ["error", "오류", "실패", "없습니다", "알 수 없는", "찾을 수"]
                    return any(keyword in lowered for keyword in failure_keywords)

                any_failed = any(_is_tool_failed(res.get("result")) for res in executed_tool_results)
                executed_tool_names = {res.get("tool_name") for res in executed_tool_results}
                use_fallback_prompt = False

                if executed_tool_results and any_failed and 'web_search' not in executed_tool_names:
                    logger.info("하나 이상의 도구 실행에 실패하여 웹 검색으로 대체합니다.", extra=log_extra)
                    web_result = await self._execute_tool(
                        {"tool_to_use": "web_search", "parameters": {"query": user_query}},
                        guild_id_safe,
                        user_query,
                    )
                    tool_results = [res for res in tool_results if res.get("tool_name") == "local_rag"]
                    tool_results.append(
                        {
                            "step": len(tool_results) + 1,
                            "tool_name": "web_search",
                            "parameters": {"query": user_query},
                            "result": web_result,
                        }
                    )
                    use_fallback_prompt = True

                tool_results_str = self._format_tool_results_for_prompt(tool_results)
                if len(tool_results_str) > 3800:
                    tool_results_str = tool_results_str[:3800]  # Gemini 입력 제한 보호


                # 단일 모델 아키텍처: Main 모델 호출
                system_prompt = config.WEB_FALLBACK_PROMPT if use_fallback_prompt else config.AGENT_SYSTEM_PROMPT
                rag_blocks_for_prompt = [] if use_fallback_prompt else rag_blocks
                
                # [NEW] 운세 컨텍스트 조회 (DM인 경우에만)
                fortune_context = None
                if not message.guild and self.bot.db:
                    try:
                        # 오늘 날짜 확인
                        today_str = datetime.now(pytz.timezone('Asia/Seoul')).strftime('%Y-%m-%d')
                        # 구독 발송 기록(last_fortune_sent)은 YYYY-MM-DD
                        # last_fortune_content가 언제 저장되었는지 별도 컬럼이 없지만,
                        # last_fortune_sent가 '오늘'이면 last_fortune_content도 '오늘'것일 확률이 높음.
                        # 다만 sent가 업데이트 안되고 content만 업데이트(직접조회) 될 수 있음.
                        # 여기서는 last_fortune_content가 null이 아니면 일단 가져오되,
                        # 내용 안에 날짜가 없다면... 음.
                        # 일단 단순히 가져와보자. (user_profiles에 last_gen_date가 있으면 좋겠지만 sent를 활용하거나)
                        # 여기서는 content만 가져옴.
                        row = await self.bot.db.execute("SELECT last_fortune_content FROM user_profiles WHERE user_id = ?", (message.author.id,)) # 
                        res = await row.fetchone()
                        if res and res[0]:
                             fortune_context = res[0]
                    except Exception as e:
                        logger.error(f"운세 컨텍스트 조회 실패: {e}")

                main_prompt = self._compose_main_prompt(
                    message,
                    user_query=user_query,
                    rag_blocks=rag_blocks_for_prompt,
                    tool_results_block=tool_results_str if tool_results_str else None,
                    fortune_context=fortune_context,
                    recent_history=history, # [NEW] 히스토리 주입
                )

                final_response_text = ""

                # CometAPI 우선 사용, 실패 시 Gemini로 폴백
                if self.use_cometapi:
                    logger.info("CometAPI(답변 생성) 호출...", extra=log_extra)
                    final_response_text = await self._cometapi_generate_content(
                        system_prompt, main_prompt, log_extra
                    ) or ""
                
                # CometAPI 실패 또는 비활성화 시 Gemini 사용
                if not final_response_text and self.gemini_configured and genai:
                    logger.info("Gemini(답변 생성) 호출...", extra=log_extra)
                    main_model = genai.GenerativeModel(
                        config.AI_RESPONSE_MODEL_NAME,
                        system_instruction=system_prompt,
                    )
                    self._debug(f"[Gemini] system_prompt={self._truncate_for_debug(system_prompt)}", log_extra)
                    self._debug(f"[Gemini] user_prompt={self._truncate_for_debug(main_prompt)}", log_extra)
                    main_response = await self._safe_generate_content(main_model, main_prompt, log_extra)
                    if main_response and main_response.parts:
                        try:
                            final_response_text = main_response.text.strip()
                        except ValueError:
                            pass
                
                if final_response_text:
                    self._debug(f"[Main] 최종 응답: {self._truncate_for_debug(final_response_text)}", log_extra)
                    debug_block = self._build_rag_debug_block(rag_entries)
                    if debug_block:
                        logger.debug("RAG 디버그 블록:\n%s", debug_block, extra=log_extra)
                    
                    # LLM 일일 카운터 증가 (안전장치)
                    await db_utils.log_api_call(self.bot.db, f"llm_user_{message.author.id}")
                    await db_utils.log_api_call(self.bot.db, "llm_global")

                    # 응답 텍스트 후처리: 자기 자신 멘션(@마사몽 등) 제거
                    final_response_text = re.sub(r'^@마사몽\s*', '', final_response_text)
                    final_response_text = re.sub(r'^@masamong\s*', '', final_response_text, flags=re.IGNORECASE)
                    final_response_text = re.sub(r'^<@!?[0-9]+>\s*', '', final_response_text)
                    
                    await self._send_split_message(message, final_response_text)
                    await db_utils.log_analytics(
                        self.bot.db,
                        "AI_INTERACTION",
                        {
                            "guild_id": message.guild.id if message.guild else "DM",
                            "user_id": message.author.id,
                            "channel_id": message.channel.id,
                            "trace_id": trace_id,
                            "user_query": user_query,
                            "tool_plan": executed_plan or tool_plan,
                            "final_response": final_response_text,
                            "is_fallback": use_fallback_prompt,
                        },
                    )
                else:
                    # RAG 문맥이 독성/안전 문제로 차단되었을 가능성 -> RAG 없이 재시도
                    if rag_blocks_for_prompt:
                        logger.warning("Main 모델 응답이 비어있어, RAG 문맥을 제외하고 재시도합니다.", extra=log_extra)
                        main_prompt_retry = self._compose_main_prompt(
                            message,
                            user_query=user_query,
                            rag_blocks=[], # RAG 제거
                            tool_results_block=tool_results_str if tool_results_str else None,
                        )
                        self._debug(f"[Main Retry] user_prompt={self._truncate_for_debug(main_prompt_retry)}", log_extra)
                        retry_response = await self._safe_generate_content(
                            main_model, 
                            main_prompt_retry, 
                            log_extra,
                            generation_config=genai.types.GenerationConfig(temperature=config.AI_TEMPERATURE)
                        )
                        
                        retry_text = ""
                        if retry_response and retry_response.parts:
                            try:
                                retry_text = retry_response.text.strip()
                            except ValueError:
                                pass
                        
                        if retry_text:
                            await message.reply(retry_text, mention_author=False)
                            await db_utils.log_analytics(
                                self.bot.db,
                                "AI_INTERACTION",
                                {
                                    "guild_id": message.guild.id,
                                    "user_id": message.author.id,
                                    "channel_id": message.channel.id,
                                    "trace_id": trace_id,
                                    "user_query": user_query,
                                    "tool_plan": executed_plan or tool_plan,
                                    "final_response": retry_text,
                                    "is_fallback": True, # 재시도 했으므로 fallback 취급
                                },
                            )
                            return
                        else:
                            logger.error("Main 모델이 최종 답변을 생성하지 못했습니다.", extra=log_extra)
                            truncated_results = tool_results_str[:1900] if tool_results_str else "No tool results."
                            await message.reply(
                                "모든 도구를 실행했지만, 최종 답변을 만드는 데 실패했어요. 도구 응답 요약:\n```json\n"
                                f"{truncated_results}\n```",
                                mention_author=False,
                            )
                    else: # No RAG blocks for prompt, so no retry attempt
                        logger.error("Main 모델이 최종 답변을 생성하지 못했습니다 (재시도 실패 포함).", extra=log_extra)
                        truncated_results = tool_results_str[:1900] if tool_results_str else "No tool results."
                        await message.reply(
                            "모든 도구를 실행했지만, 최종 답변을 만드는 데 실패했어요. (AI 응답 없음)\n```json\n"
                            f"{truncated_results}\n```",
                            mention_author=False,
                        )


            except Exception as e:
                logger.error(f"에이전트 처리 중 최상위 오류: {e}", exc_info=True, extra=log_extra)
                await message.reply(config.MSG_AI_ERROR, mention_author=False)
            finally:
                self._debug(f"--- 에이전트 세션 종료 trace_id={trace_id}", log_extra)
    async def _get_recent_history(self, message: discord.Message, rag_prompt: str) -> list:
        """모델에 전달할 최근 대화 기록을 채널에서 가져옵니다."""
        history_limit = 6 if rag_prompt else 12
        history = []
        
        async for msg in message.channel.history(limit=history_limit + 1):
            if msg.id == message.id: continue
            role = 'model' if msg.author.id == self.bot.user.id else 'user'
            content = msg.content[:2000]
            history.append({'role': role, 'parts': [content]})

        history.reverse()
        return history

    async def should_proactively_respond(self, message: discord.Message) -> bool:
        """봇이 대화에 능동적으로 참여할지 여부를 결정하는 게이트키퍼 로직입니다."""
        conf = config.AI_PROACTIVE_RESPONSE_CONFIG
        if not conf.get("enabled"): return False
        if not self._message_has_valid_mention(message):
            # 멘션이 없다면 어떤 경우에도 Gemini 호출을 수행하지 않는다.
            return False

        now = time.time()
        if (now - self.proactive_cooldowns.get(message.channel.id, 0)) < conf.get("cooldown_seconds", 90): return False
        if len(message.content) < conf.get("min_message_length", 10): return False
        if not any(keyword in message.content.lower() for keyword in conf.get("keywords", [])): return False
        if random.random() > conf.get("probability", 0.1): return False

        log_extra = {'guild_id': message.guild.id, 'channel_id': message.channel.id}
        try:
            history_msgs = [f"User({msg.author.display_name}): {msg.content}" async for msg in message.channel.history(limit=conf.get("look_back_count", 5))]
            history_msgs.reverse()
            conversation_context = "\n".join(history_msgs)
            gatekeeper_prompt = f"""{conf['gatekeeper_persona']}\n\n--- 최근 대화 내용 ---\n{conversation_context}\n---\n사용자의 마지막 메시지: \"{message.content}\"\n---\n\n자, 판단해. Yes or No?"""

            lite_model = genai.GenerativeModel(config.AI_INTENT_MODEL_NAME)
            response = await self._safe_generate_content(lite_model, gatekeeper_prompt, log_extra)

            if response and "YES" in response.text.strip().upper():
                self.proactive_cooldowns[message.channel.id] = now
                return True
        except Exception as e:
            logger.error(f"게이트키퍼 AI 실행 중 오류: {e}", exc_info=True, extra=log_extra)

        return False

    async def get_recent_conversation_text(self, guild_id: int, channel_id: int, look_back: int = 20) -> str:
        """DB에서 최근 대화 기록을 텍스트로 가져옵니다 (요약 기능용)."""
        if not self.bot.db: return ""
        query = "SELECT user_name, content FROM conversation_history WHERE guild_id = ? AND channel_id = ? AND is_bot = 0 ORDER BY created_at DESC LIMIT ?"
        try:
            async with self.bot.db.execute(query, (guild_id, channel_id, look_back)) as cursor:
                rows = await cursor.fetchall()
            if not rows: return ""
            rows.reverse()
            return "\n".join([f"User({row['user_name']}): {row['content']}" for row in rows])
        except Exception as e:
            logger.error(f"최근 대화 기록 조회 중 DB 오류: {e}", exc_info=True)
            return ""

    async def generate_system_alert_message(self, channel_id: int, alert_context: str, alert_title: str | None = None) -> str | None:
        """주기적 알림 등 시스템 메시지를 AI 말투로 재작성합니다."""
        if not self.is_ready:
            return None

        log_extra = {'channel_id': channel_id, 'alert_title': alert_title}

        try:
            channel_config = config.CHANNEL_AI_CONFIG.get(channel_id, {})
            persona = channel_config.get('persona', config.DEFAULT_TSUNDERE_PERSONA)
            rules = channel_config.get('rules', config.DEFAULT_TSUNDERE_RULES)

            system_prompt = (
                f"{persona}\n\n{rules}\n\n"
                "### 추가 지침\n"
                "- 지금은 서버 구성원에게 전달할 시스템 공지를 작성하는 중이다.\n"
                "- 핵심 정보는 빠뜨리지 말되 2~3문장 이내로 간결하게 정리한다.\n"
                "- 필요 시 가벼운 이모지 한두 개만 사용하고, 과한 장식은 피한다.\n"
                "- 마지막에는 자연스럽게 행동을 촉구하거나 격려하는 말을 덧붙인다."
            )

            user_prompt = (
                "다음 정보를 바탕으로 서버에 전달할 공지 메시지를 작성해줘.\n"
                f"- 알림 주제: {alert_title or '일반 알림'}\n"
                f"- 전달할 내용: {alert_context}\n\n"
                "공지 문구는 마사몽의 말투를 유지해 주고, 너무 장황하지 않게 작성해줘."
            )

            # 1. CometAPI 우선 사용
            if self.use_cometapi:
                alert_message = await self._cometapi_generate_content(
                    system_prompt, 
                    user_prompt, 
                    log_extra
                )
            
            # 2. 실패 시 Gemini 폴백
            if not alert_message and self.gemini_configured and genai:
                model = genai.GenerativeModel(
                    model_name=config.AI_RESPONSE_MODEL_NAME,
                    system_instruction=system_prompt,
                )
                response = await self._safe_generate_content(
                    model, 
                    user_prompt, 
                    log_extra, 
                    generation_config=genai.types.GenerationConfig(temperature=config.AI_TEMPERATURE)
                )
                if response and response.text:
                    alert_message = response.text.strip()

            if alert_message and len(alert_message) > config.AI_RESPONSE_LENGTH_LIMIT:
                alert_message = alert_message[:config.AI_RESPONSE_LENGTH_LIMIT].rstrip()
            return alert_message

        except Exception as e:
            logger.error(
                "시스템 알림 메시지 생성 중 오류: %s",
                e,
                exc_info=True,
                extra=log_extra,
            )

        return None

    async def generate_creative_text(self, channel: discord.TextChannel, author: discord.User, prompt_key: str, context: dict) -> str:
        """`!운세`, `!랭킹` 등 특정 명령어에 대한 창의적인 AI 답변을 생성합니다."""
        if not self.is_ready: return config.MSG_AI_ERROR
        log_extra = {'guild_id': channel.guild.id, 'user_id': author.id, 'prompt_key': prompt_key}

        try:
            prompt_template = config.AI_CREATIVE_PROMPTS.get(prompt_key)
            if not prompt_template: return config.MSG_CMD_ERROR

            user_prompt = prompt_template.format(**context)
            system_prompt = f"{config.CHANNEL_AI_CONFIG.get(channel.id, {}).get('persona', '')}\n\n{config.CHANNEL_AI_CONFIG.get(channel.id, {}).get('rules', '')}"

            # [FIX] 명령어로 호출된 경우 멘션 정책 무시 (가드 제거)
            if config.MENTION_GUARD_SNIPPET in system_prompt:
                system_prompt = system_prompt.replace(config.MENTION_GUARD_SNIPPET, "")

            response_text = None

            # 1. CometAPI 우선 사용
            if self.use_cometapi:
                response_text = await self._cometapi_generate_content(
                    system_prompt,
                    user_prompt,
                    log_extra
                )

            # 2. 실패 시 Gemini 폴백
            if not response_text and self.gemini_configured and genai:
                 model = genai.GenerativeModel(model_name=config.AI_RESPONSE_MODEL_NAME, system_instruction=system_prompt)
                 response = await self._safe_generate_content(
                     model, 
                     user_prompt, 
                     log_extra,
                     generation_config=genai.types.GenerationConfig(temperature=config.AI_TEMPERATURE)
                 )
                 if response and response.text:
                      response_text = response.text.strip()

            return response_text if response_text else config.MSG_AI_ERROR
        except KeyError as e:
            logger.error(f"프롬프트 포맷팅 중 키 오류: '{prompt_key}' 프롬프트에 필요한 컨텍스트({e})가 없습니다.", extra=log_extra)
            return config.MSG_CMD_ERROR
        except Exception as e:
            logger.error(f"Creative text 생성 중 최상위 오류: {e}", exc_info=True, extra=log_extra)
            return config.MSG_AI_ERROR

    async def extract_ticker_with_llm(self, query: str) -> str | None:
        """
        사용자 자연어 쿼리에서 Yahoo Finance 호환 티커만 추출합니다.
        예: "비트코인 얼마야?" -> "BTC-USD"
            "삼성전자 주가" -> "005930.KS"
            "애플 시세" -> "AAPL"
        """
        if not self.use_cometapi:
             # CometAPI 없으면 사용 불가 (혹은 Gemini 폴백 가능하지만 생략)
             return None

        system_prompt = (
            "You are a specialized assistant that extracts stock/crypto ticker symbols from user queries.\n"
            "The user will ask about a stock price in Korean or English.\n"
            "You must identify the correct Yahoo Finance compatible ticker symbol.\n"
            "Rules:\n"
            "1. Return ONLY the ticker symbol. Do not write any other text.\n"
            "2. For Korean stocks, append '.KS' (KOSPI) or '.KQ' (KOSDAQ). e.g., Samsung -> 005930.KS\n"
            "3. For US stocks, use the standard ticker. e.g., Apple -> AAPL\n"
            "4. For Crypto, use common pairs. e.g., Bitcoin -> BTC-USD, Ethereum -> ETH-USD\n"
            "5. If the company is not found or ambiguous, return 'NONE'."
        )
        
        user_prompt = f"Query: {query}\nTicker:"
        
        try:
            ticker = await self._cometapi_generate_content(
                system_prompt,
                user_prompt,
                log_extra={'mode': 'ticker_extraction'}
            )
            if ticker and "NONE" not in ticker:
                clean_ticker = ticker.strip().replace("'", "").replace('"', '').upper()
                return clean_ticker
            return None
        except Exception as e:
            logger.error(f"Ticker extraction failed: {e}")
            return None


async def setup(bot: commands.Bot):
    """Cog를 봇에 등록하는 함수"""
    await bot.add_cog(AIHandler(bot))

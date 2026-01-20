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

from datetime import datetime, timedelta
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
from utils.reranker import Reranker, RerankerConfig

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
    ) -> str | None:
        """CometAPI(OpenAI 호환)를 통해 응답을 생성합니다.

        Args:
            system_prompt: 시스템 프롬프트
            user_prompt: 사용자 프롬프트 (RAG 컨텍스트 포함)
            log_extra: 로깅용 추가 정보

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
                model=config.COMETAPI_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=1024,
                temperature=0.7,
            )

            response_text = completion.choices[0].message.content
            await db_utils.log_api_call(self.bot.db, "cometapi")

            if self.debug_enabled:
                self._debug(f"[CometAPI] 응답: {self._truncate_for_debug(response_text)}", log_extra)

            return response_text.strip() if response_text else None

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
        if not self.is_ready or not config.AI_MEMORY_ENABLED or not message.guild: return
        try:
            channel_config = config.CHANNEL_AI_CONFIG.get(message.channel.id, {})
            if not channel_config.get("allowed", False): return

            await self.bot.db.execute(
                "INSERT INTO conversation_history (message_id, guild_id, channel_id, user_id, user_name, content, is_bot, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message.id,
                    message.guild.id,
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
            logger.error(f"대화 기록 저장 중 DB 오류: {e}", exc_info=True, extra={'guild_id': message.guild.id})

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
        
        # 2. 메타데이터 결정 (마지막 메시지 기준)
        last_msg = payload[-1]
        message_id = last_msg['message_id']
        timestamp = last_msg['created_at']
        user_id = last_msg['user_id']
        
        log_extra = {'guild_id': guild_id, 'channel_id': channel_id, 'window_id': message_id}

        # 3. 임베딩 생성 (passage: prefix 필수)
        embedding_vector = await self._generate_local_embedding(
            chunk_text, 
            log_extra, 
            prefix="passage: "
        )
        if embedding_vector is None:
            return

        # 4. DB 저장
        try:
            # message 컬럼에 '청크 전체 텍스트'를 저장하여 검색 시 문맥을 제공함.
            await self.discord_embedding_store.upsert_message_embedding(
                message_id=message_id,
                server_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                user_name="Conversation Chunk",  # 청크 데이터임을 명시
                message=chunk_text,             # 전체 대화 흐름 저장
                timestamp_iso=timestamp,
                embedding=embedding_vector,
            )
        except Exception as e:
            logger.error(f"임베딩 DB 저장 중 오류: {e}", extra=log_extra, exc_info=True)

    async def _update_conversation_windows(self, message: discord.Message) -> None:
        """대화 슬라이딩 윈도우(6개, stride=3)를 누적해 별도 테이블에 저장합니다."""
        if not message.guild or self.bot.db is None:
            return

        window_size = max(1, getattr(config, "CONVERSATION_WINDOW_SIZE", 6))
        stride = max(1, getattr(config, "CONVERSATION_WINDOW_STRIDE", 3))
        key = (message.guild.id, message.channel.id)

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

        if len(buffer) < window_size:
            return
        # stride 간격에 맞춰 윈도우를 저장한다.
        if (counter - window_size) % stride != 0:
            return

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
                    message.guild.id,
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
                self._create_window_embedding(message.guild.id, message.channel.id, payload)
            )
        except Exception as exc:  # pragma: no cover - 방어적 로깅
            logger.error(
                "대화 윈도우 저장 중 DB 오류: %s",
                exc,
                extra={"guild_id": message.guild.id, "channel_id": message.channel.id},
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
{search_result[:2500]}

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

    _WEATHER_KEYWORDS = frozenset(['날씨', '기온', '온도', '비', '눈', '맑', '흐림', '우산', '강수', '일기예보'])
    _STOCK_US_KEYWORDS = frozenset(['애플', 'apple', 'aapl', '테슬라', 'tesla', 'tsla', '구글', 'google', 'googl', '엔비디아', 'nvidia', 'nvda', '마이크로소프트', 'microsoft', 'msft', '아마존', 'amazon', 'amzn'])
    _STOCK_KR_KEYWORDS = frozenset(['삼성전자', '현대차', 'sk하이닉스', '네이버', '카카오', 'lg에너지', '셀트리온', '삼성바이오', '기아', '포스코'])
    _STOCK_GENERAL_KEYWORDS = frozenset(['주가', '주식', '시세', '종가', '시가', '상장'])
    _PLACE_KEYWORDS = frozenset(['맛집', '카페', '음식점', '식당', '추천', '근처', '주변', '가볼만한', '핫플'])
    _LOCATION_KEYWORDS = ['서울', '부산', '인천', '대구', '광주', '대전', '울산', '세종', '경기', '강원', '충북', '충남', '전북', '전남', '경북', '경남', '제주', '광양', '여수', '순천', '목포', '강남', '홍대', '이태원', '명동']

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

            tools.append({
                'tool_to_use': 'get_weather_forecast',
                'tool_name': 'get_weather_forecast',
                'parameters': {'location': location, 'day_offset': day_offset}
            })
            return tools  # 날씨 요청은 단일 도구로 처리

        # 미국 주식 감지
        if any(kw in query_lower for kw in self._STOCK_US_KEYWORDS):
            symbol = self._extract_us_stock_symbol(query_lower)
            if symbol:
                tools.append({
                    'tool_to_use': 'get_stock_price',
                    'tool_name': 'get_stock_price',
                    'parameters': {'stock_name': symbol}
                })
                return tools

        # 한국 주식 감지
        if any(kw in query_lower for kw in self._STOCK_KR_KEYWORDS) or any(kw in query_lower for kw in self._STOCK_GENERAL_KEYWORDS):
            ticker = self._extract_kr_stock_ticker(query_lower)
            if ticker:
                tools.append({
                    'tool_to_use': 'get_stock_price',
                    'tool_name': 'get_stock_price',
                    'parameters': {'stock_name': ticker}
                })
                return tools

        # 장소 검색 감지
        if any(kw in query_lower for kw in self._PLACE_KEYWORDS):
            # 위치 정보가 있으면 query에 포함시킴 (API는 query만 받음)
            location = self._extract_location_from_query(query) or ''
            search_query = f"{location} {query}".strip() if location else query
            tools.append({
                'tool_to_use': 'search_for_place',
                'tool_name': 'search_for_place',
                'parameters': {'query': search_query}
            })
            return tools

        # 도구 필요 없음 - 일반 대화 또는 RAG로 처리
        return tools

    def _extract_location_from_query(self, query: str) -> str | None:
        """쿼리에서 지역명을 추출합니다."""
        for location in self._LOCATION_KEYWORDS:
            if location in query:
                return location
        return None

    def _extract_us_stock_symbol(self, query_lower: str) -> str | None:
        """쿼리에서 미국 주식 심볼을 추출합니다."""
        symbol_map = {
            '애플': 'AAPL', 'apple': 'AAPL', 'aapl': 'AAPL',
            '테슬라': 'TSLA', 'tesla': 'TSLA', 'tsla': 'TSLA',
            '구글': 'GOOGL', 'google': 'GOOGL', 'googl': 'GOOGL',
            '엔비디아': 'NVDA', 'nvidia': 'NVDA', 'nvda': 'NVDA',
            '마이크로소프트': 'MSFT', 'microsoft': 'MSFT', 'msft': 'MSFT',
            '아마존': 'AMZN', 'amazon': 'AMZN', 'amzn': 'AMZN',
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

        try:
            result = await engine.search(
                query,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
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
            return ""
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
    ) -> str:
        """메인 모델에 전달할 프롬프트를 `emb` 스타일로 구성합니다.
        
        프롬프트 구조:
        1. 시스템 페르소나/규칙
        2. [현재 시간] - 서버 시간 (KST)
        3. [과거 대화 기억] - RAG 컨텍스트
        4. [도구 실행 결과] - 도구 출력 (있을 경우)
        5. [현재 질문] - 사용자 쿼리
        6. 지시사항
        """
        # 시스템 프롬프트 (페르소나 + 규칙)
        system_part = self._get_channel_system_prompt(message.channel.id)

        sections: list[str] = [system_part]

        # 서버 현재 시간 (KST) - 항상 포함
        current_time = db_utils.get_current_time()
        sections.append(f"[현재 시간]\n{current_time}")

        # RAG 컨텍스트 (과거 대화 기억)
        if rag_blocks:
            rag_content = "\n\n".join(rag_blocks)
            sections.append(f"[과거 대화 기억]\n{rag_content}")

        # 도구 실행 결과
        if tool_results_block:
            sections.append(f"[도구 실행 결과]\n{tool_results_block}")

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
            result = entry.get("result")

            if name == "local_rag":
                entries = []
                if isinstance(result, dict):
                    raw_entries = result.get("entries")
                    if isinstance(raw_entries, list):
                        entries = [item for item in raw_entries if isinstance(item, dict)]
                if entries:
                    for idx, rag_entry in enumerate(entries, start=1):
                        block = (rag_entry.get("dialogue_block") or rag_entry.get("message") or "").strip()
                        if not block:
                            continue
                        score = rag_entry.get("combined_score")
                        header = f"[local_rag #{idx}]"
                        if isinstance(score, (int, float)):
                            header += f" score={float(score):.3f}"
                        lines.append(header)
                        for line in block.splitlines():
                            lines.append(f"  {line}")
                continue

            if isinstance(result, dict):
                result_text = json.dumps(result, ensure_ascii=False)
            elif result is None:
                result_text = "(결과 없음)"
            else:
                result_text = str(result)

            lines.append(f"[{name}] {result_text}")

        return "\n".join(lines) if lines else "도구 실행 결과 없음"

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

        trace_id = uuid.uuid4().hex[:8]
        log_extra = dict(base_log_extra)
        log_extra['trace_id'] = trace_id
        logger.info(f"에이전트 처리 시작. Query: '{user_query}'", extra=log_extra)
        self._debug(f"--- 에이전트 세션 시작 trace_id={trace_id}", log_extra)

        async with message.channel.typing():
            try:
                recent_search_messages = await self._collect_recent_search_messages(message)
                rag_prompt, rag_entries, rag_top_score, rag_blocks = await self._get_rag_context(
                    message.guild.id,
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
                        result = await self._execute_tool(tool_call, message.guild.id, user_query)
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
                                    "guild_id": message.guild.id,
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
                        message.guild.id,
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
                main_prompt = self._compose_main_prompt(
                    message,
                    user_query=user_query,
                    rag_blocks=rag_blocks_for_prompt,
                    tool_results_block=tool_results_str if tool_results_str else None,
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
                    
                    await message.reply(final_response_text, mention_author=False)
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
                        retry_response = await self._safe_generate_content(main_model, main_prompt_retry, log_extra)
                        
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

            model = genai.GenerativeModel(
                model_name=config.AI_RESPONSE_MODEL_NAME,
                system_instruction=system_prompt,
            )
            response = await self._safe_generate_content(model, user_prompt, log_extra)
            if response and response.text:
                alert_message = response.text.strip()
                if len(alert_message) > config.AI_RESPONSE_LENGTH_LIMIT:
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

            model = genai.GenerativeModel(model_name=config.AI_RESPONSE_MODEL_NAME, system_instruction=system_prompt)
            response = await self._safe_generate_content(model, user_prompt, log_extra)

            return response.text.strip() if response and response.text else config.MSG_AI_ERROR
        except KeyError as e:
            logger.error(f"프롬프트 포맷팅 중 키 오류: '{prompt_key}' 프롬프트에 필요한 컨텍스트({e})가 없습니다.", extra=log_extra)
            return config.MSG_CMD_ERROR
        except Exception as e:
            logger.error(f"Creative text 생성 중 최상위 오류: {e}", exc_info=True, extra=log_extra)
            return config.MSG_AI_ERROR

async def setup(bot: commands.Bot):
    """Cog를 봇에 등록하는 함수"""
    await bot.add_cog(AIHandler(bot))

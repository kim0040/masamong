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
        self._google_grounding_checked = False
        self._google_grounding_descriptor: dict[str, object] | None = None
        self.discord_embedding_store = DiscordEmbeddingStore(config.DISCORD_EMBEDDING_DB_PATH)
        self.kakao_embedding_store = KakaoEmbeddingStore(
            config.KAKAO_EMBEDDING_DB_PATH,
            config.KAKAO_EMBEDDING_SERVER_MAP,
        ) if config.KAKAO_EMBEDDING_DB_PATH or config.KAKAO_EMBEDDING_SERVER_MAP else None
        self.bm25_manager = BM25IndexManager(config.BM25_DATABASE_PATH) if config.BM25_DATABASE_PATH else None

        reranker: Reranker | None = None
        if config.RAG_RERANKER_MODEL_NAME:
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

    async def _generate_local_embedding(self, content: str, log_extra: dict) -> np.ndarray | None:
        """SentenceTransformer 기반 임베딩을 생성합니다."""
        if not config.AI_MEMORY_ENABLED:
            return None
        if np is None:
            logger.warning("numpy가 설치되어 있지 않아 AI 메모리 기능을 사용할 수 없습니다.", extra=log_extra)
            return None

        embedding = await get_embedding(content)
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
                (message.id, message.guild.id, message.channel.id, message.author.id, message.author.display_name, message.content, message.author.bot, message.created_at.isoformat())
            )
            await self.bot.db.commit()
            if not message.author.bot and message.content.strip():
                asyncio.create_task(self._create_and_save_embedding(message))
        except Exception as e:
            logger.error(f"대화 기록 저장 중 DB 오류: {e}", exc_info=True, extra={'guild_id': message.guild.id})

    async def _create_and_save_embedding(self, message: discord.Message):
        """대화 메시지를 SentenceTransformer 임베딩으로 변환해 별도 DB에 저장합니다."""
        log_extra = {'guild_id': message.guild.id if message.guild else None, 'message_id': message.id}
        embedding_vector = await self._generate_local_embedding(message.content, log_extra)
        if embedding_vector is None:
            return

        try:
            await self.discord_embedding_store.upsert_message_embedding(
                message_id=message.id,
                server_id=message.guild.id,
                channel_id=message.channel.id,
                user_id=message.author.id,
                user_name=message.author.display_name,
                message=message.content,
                timestamp_iso=message.created_at.isoformat(),
                embedding=embedding_vector,
            )
        except Exception as e:
            logger.error(f"임베딩 DB 저장 중 오류: {e}", extra=log_extra, exc_info=True)

    def _discover_google_grounding_tool(self) -> dict[str, object] | None:
        """사용 중인 Gemini 버전에 호환되는 구글 서치 도구를 탐색합니다.

        Returns:
            dict[str, object] | None: `field`/`cls` 정보를 담은 매핑 또는 지원 불가 시 None.
        """
        if not self.gemini_configured:
            return None

        model_name = config.AI_RESPONSE_MODEL_NAME
        
        # 모델 이름에 '1.5'가 포함되어 있는지 여부에 따라 사용할 도구 설정 결정
        if "1.5" in model_name:
            logger.info("Gemini 1.5 모델 감지, 'google_search_retrieval' 도구를 시도합니다.")
            field_name = "google_search_retrieval"
            class_name = "GoogleSearchRetrieval"
        else:
            logger.info("Gemini 2.0+ 모델 감지, 'google_search' 도구를 시도합니다.")
            field_name = "google_search"
            class_name = "GoogleSearch"

        try:
            # getattr을 사용하여 해당 클래스가 존재하는지 확인
            google_cls = getattr(genai.types, class_name, None)
            
            if not google_cls:
                # grounding 서브모듈에서도 찾아보기 (구버전 호환성)
                grounding_module = getattr(genai.types, "grounding", None)
                if grounding_module:
                    google_cls = getattr(grounding_module, class_name, None)

            if not google_cls:
                logger.warning(f"현재 라이브러리에서 '{class_name}' 클래스를 찾을 수 없습니다.")
                return None

            # 도구 생성 테스트
            genai.types.Tool(**{field_name: google_cls()})
            
            logger.info(f"Google Grounding 도구를 '{field_name}' 필드와 '{class_name}' 타입으로 설정했습니다.")
            return {"field": field_name, "cls": google_cls}

        except Exception as exc:
            logger.error(f"Google Grounding 도구 ('{field_name}', '{class_name}') 초기화 검사 실패: {exc}")
            return None

    def _get_google_grounding_tool(self):
        if not self.gemini_configured:
            return None

        if not self._google_grounding_checked:
            self._google_grounding_descriptor = self._discover_google_grounding_tool()
            self._google_grounding_checked = True

        if not self._google_grounding_descriptor:
            return None

        field_name = self._google_grounding_descriptor.get("field")
        google_cls = self._google_grounding_descriptor.get("cls")
        if not field_name or not google_cls:
            return None

        try:
            return genai.types.Tool(**{field_name: google_cls()})
        except Exception as exc:
            logger.error("Google Grounding 도구 인스턴스화 실패: %s", exc)
            return None

    async def _google_grounded_search(self, query: str, log_extra: dict) -> dict | None:
        """Google Search Grounding을 사용해 웹 검색 결과를 생성합니다. (REST API 직접 호출)"""
        # SDK 방식의 불안정성으로 인해 안정적인 REST API 호출로 직접 연결합니다.
        return await self._google_grounded_search_rest(query, log_extra)

    async def _google_grounded_search_rest(self, query: str, log_extra: dict) -> dict | None:
        """google-generativeai SDK에서 Grounding 도구를 제공하지 않을 때 REST API로 호출합니다."""
        if not config.GEMINI_API_KEY:
            logger.warning("Gemini API 키가 없어 Google Grounding REST 호출을 건너뜁니다.", extra=log_extra)
            return None

        if await db_utils.check_api_rate_limit(self.bot.db, 'gemini_response', config.RPM_LIMIT_RESPONSE, config.RPD_LIMIT_RESPONSE):
            logger.warning("Gemini Grounding REST 호출이 Rate Limit에 막혔습니다.", extra=log_extra)
            return None

        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{config.AI_RESPONSE_MODEL_NAME}:generateContent"
        params = {"key": config.GEMINI_API_KEY}
        payload = {
            "contents": [{"role": "user", "parts": [{"text": query}]}],
            "tools": [{"googleSearch": {}}],
            "generationConfig": {"temperature": 0.0},
        }

        if config.GEMINI_SAFETY_SETTINGS:
            payload["safetySettings"] = [
                {"category": category, "threshold": threshold}
                for category, threshold in config.GEMINI_SAFETY_SETTINGS.items()
            ]

        session = http.get_modern_tls_session()
        try:
            logger.info("Google Grounding REST 호출을 실행합니다.", extra=log_extra)
            
            # 재시도 로직 추가
            for attempt in range(3):
                try:
                    response = await asyncio.to_thread(
                        session.post, endpoint, params=params, json=payload, timeout=30  # 타임아웃 30초로 증가
                    )
                    response.raise_for_status()
                    break  # 성공 시 루프 탈출
                except requests.exceptions.ReadTimeout as exc:
                    if attempt >= 2:
                        logger.error("Google Grounding REST 호출이 재시도 후에도 시간 초과되었습니다.", extra=log_extra, exc_info=True)
                        raise exc  # 마지막 시도 실패 시 예외 다시 발생
                    logger.warning(f"Google Grounding REST 호출 시간 초과 (시도 {attempt + 1}/3). {5 * (attempt + 1)}초 후 재시도합니다.", extra=log_extra)
                    await asyncio.sleep(5 * (attempt + 1))
            else: # for-else: break로 탈출하지 못한 경우 (모든 재시도 실패)
                return None

            try:
                data = response.json()
            except ValueError as exc:
                logger.error(f"Google Grounding REST 응답 JSON 파싱 실패: {exc} | 응답: {response.text}", extra=log_extra)
                return None

            await db_utils.log_api_call(self.bot.db, 'gemini_response')

            candidates = data.get("candidates", [])
            candidate = candidates[0] if candidates else {}
            content = candidate.get("content", {})
            parts = content.get("parts", []) if isinstance(content, dict) else []
            texts = [part.get("text", "") for part in parts if isinstance(part, dict) and part.get("text")]
            answer_text = "\n".join(filter(None, (text.strip() for text in texts))).strip()
            metadata = candidate.get("groundingMetadata") or candidate.get("grounding_metadata") or data.get("groundingMetadata")

            if not answer_text:
                logger.warning("Google Grounding REST 호출이 비어있는 응답을 반환했습니다.", extra=log_extra)
                return None

            web_queries = None
            if isinstance(metadata, dict):
                web_queries = metadata.get("webSearchQueries") or metadata.get("web_search_queries")

            return {
                "result": answer_text,
                "grounding_metadata": metadata,
                "web_search_queries": web_queries,
            }

        except requests.exceptions.RequestException as exc:
            logger.error("Google Grounding REST 호출 중 네트워크 오류: %s", exc, extra=log_extra, exc_info=True)
            return None
        finally:
            session.close()

    async def _run_web_search_fallback(self, query: str, log_extra: dict) -> str | None:
        """Gemini Grounding 검색 실패 시 호출되는 폴백 함수. REST API를 직접 호출하여 재시도합니다."""
        logger.info("기본 Gemini Grounding 검색에 실패하여 폴백으로 REST API 검색을 재시도합니다.", extra=log_extra)
        fallback_result = await self._google_grounded_search_rest(query, log_extra)
        if fallback_result and fallback_result.get("result"):
            return fallback_result.get("result")
        return None

    async def _get_rag_context(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int,
        query: str,
    ) -> Tuple[str, list[str], float]:
        """RAG: 하이브리드 검색 결과를 바탕으로 컨텍스트를 구성합니다."""
        if not config.AI_MEMORY_ENABLED:
            return "", [], 0.0

        log_extra = {'guild_id': guild_id, 'channel_id': channel_id, 'user_id': user_id}
        logger.info("RAG 컨텍스트 검색 시작. Query: '%s'", query, extra=log_extra)

        engine = getattr(self, "hybrid_search_engine", None)
        if engine is None:
            logger.warning("하이브리드 검색 엔진이 초기화되지 않았습니다.", extra=log_extra)
            return "", [], 0.0

        try:
            result = await engine.search(
                query,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
            )
        except Exception as exc:
            logger.error("하이브리드 검색 중 오류: %s", exc, extra=log_extra, exc_info=True)
            return "", [], 0.0

        if not result.entries:
            logger.info("RAG: 하이브리드 검색 결과가 없습니다.", extra=log_extra)
            return "", [], 0.0

        top_entries = []
        for entry in result.entries:
            cloned = dict(entry)
            sources = cloned.get("sources")
            if isinstance(sources, set):
                cloned["sources"] = sorted(sources)
            top_entries.append(cloned)

        def _score(entry: dict[str, Any]) -> float:
            return max(
                entry.get("similarity") or 0.0,
                entry.get("bm25_score") or 0.0,
                entry.get("hybrid_score") or 0.0,
                entry.get("rerank_score") or 0.0,
            )

        context_lines: list[str] = []
        for idx, entry in enumerate(top_entries, start=1):
            prefixes: list[str] = []
            origin = entry.get("origin")
            if origin:
                prefixes.append(str(origin))
            speaker = entry.get("speaker")
            if speaker:
                prefixes.append(str(speaker))

            metrics: list[str] = []
            if entry.get("similarity") is not None:
                metrics.append(f"sim={entry['similarity']:.3f}")
            if entry.get("bm25_score") is not None:
                metrics.append(f"bm25={entry['bm25_score']:.3f}")
            if entry.get("hybrid_score") is not None:
                metrics.append(f"hybrid={entry['hybrid_score']:.3f}")
            if entry.get("rerank_score") is not None:
                metrics.append(f"rerank={entry['rerank_score']:.3f}")
            if metrics:
                prefixes.append(" ".join(metrics))

            prefix_block = " ".join(f"[{p}]" for p in prefixes if p)
            message = entry.get("message") or ""
            context_lines.append(f"{idx}. {prefix_block} {message}".strip())

            for ctx_item in entry.get("context_window") or []:
                ctx_speaker = ctx_item.get("user_name") or ctx_item.get("speaker") or "?"
                ctx_message = ctx_item.get("message") or ""
                if ctx_message:
                    context_lines.append(f"    - {ctx_speaker}: {ctx_message}")

        if len(result.query_variants) > 1:
            variant_str = ", ".join(result.query_variants)
            header = f"참고할 만한 과거 대화 내용 (확장 쿼리: {variant_str}):"
        else:
            header = "참고할 만한 과거 대화 내용:"
        context_str = header + "\n" + "\n".join(context_lines)

        top_entry = top_entries[0]
        top_metric = float(_score(top_entry))
        logger.info(
            "RAG: 하이브리드 검색 결과 %d개 (최고 점수=%.3f)",
            len(top_entries),
            top_metric,
            extra=log_extra,
        )

        if config.RAG_DEBUG_ENABLED:
            debug_lines = []
            for entry in top_entries:
                snippet = entry.get("message") or ""
                snippet = snippet if len(snippet) <= 160 else snippet[:157] + "..."
                debug_lines.append(
                    "- origin=%s speaker=%s hybrid=%.3f sim=%.3f bm25=%.3f rerank=%.3f msg=%s"
                    % (
                        entry.get("origin") or "?",
                        entry.get("speaker") or "?",
                        entry.get("hybrid_score") or 0.0,
                        entry.get("similarity") or 0.0,
                        entry.get("bm25_score") or 0.0,
                        entry.get("rerank_score") or 0.0,
                        snippet,
                    )
                )
                for ctx_item in entry.get("context_window") or []:
                    ctx_speaker = ctx_item.get("user_name") or ctx_item.get("speaker") or "?"
                    ctx_message = ctx_item.get("message") or ""
                    ctx_snippet = ctx_message if len(ctx_message) <= 100 else ctx_message[:97] + "..."
                    debug_lines.append(f"    • {ctx_speaker}: {ctx_snippet}")
            logger.info("RAG 디버그 상세:\n%s", "\n".join(debug_lines), extra=log_extra)

        logger.debug("RAG 결과: %s", context_str, extra=log_extra)
        return context_str, top_entries, top_metric

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
                        entries = [entry for entry in raw_entries if isinstance(entry, dict)]
                if entries:
                    lines.append("[local_rag] 아래 대화 내용을 우선 참고해:")
                    for entry in entries:
                        message = entry.get("message") or ""
                        snippet = message if len(message) <= 200 else message[:197] + "..."
                        origin = entry.get("origin") or "?"
                        speaker = entry.get("speaker") or "?"
                        sim = entry.get("similarity") or 0.0
                        db_path = entry.get("db_path") or "-"
                        lines.append(
                            f"  • [{origin} | speaker={speaker} | sim={sim:.3f} | db={db_path}] {snippet}"
                        )
                        ctx_window = entry.get("context_window") or []
                        for ctx in ctx_window:
                            ctx_speaker = ctx.get("user_name") or "?"
                            ctx_msg = ctx.get("message") or ""
                            ctx_snippet = ctx_msg if len(ctx_msg) <= 180 else ctx_msg[:177] + "..."
                            lines.append(f"      - {ctx_speaker}: {ctx_snippet}")
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
            message = entry.get("message") or ""
            snippet = message if len(message) <= 160 else message[:157] + "..."
            origin = entry.get("origin") or "?"
            speaker = entry.get("speaker") or "?"
            sim = entry.get("similarity") or 0.0
            db_path = entry.get("db_path") or "-"
            lines.append(f"origin={origin} | speaker={speaker} | sim={sim:.3f} | db={db_path} | {snippet}")

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

        # web_search는 ToolsCog에 구현된 다른 도구들과 달리, Gemini의 Grounding 기능을 직접 사용합니다.
        if tool_name == 'web_search':
            logger.info("특별 도구 실행: web_search (Google Grounding)", extra=log_extra)
            query = parameters.get('query', user_query)
            self._debug(f"[도구:web_search] 쿼리: {self._truncate_for_debug(query)}", log_extra)
            grounded_payload = await self._google_grounded_search(query, log_extra)
            if grounded_payload and grounded_payload.get("result"):
                self._debug(f"[도구:web_search] 결과: {self._truncate_for_debug(grounded_payload)}", log_extra)
                return grounded_payload
            fallback_result = await self._run_web_search_fallback(query, log_extra)
            if fallback_result:
                self._debug(f"[도구:web_search] 폴백 결과: {self._truncate_for_debug(fallback_result)}", log_extra)
                return {"result": fallback_result}
            return {"error": "Google 검색을 통해 정보를 찾는 데 실패했습니다."}

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
                rag_prompt, rag_entries, rag_top_similarity = await self._get_rag_context(
                    message.guild.id,
                    message.channel.id,
                    message.author.id,
                    user_query,
                )
                history = await self._get_recent_history(message, rag_prompt)
                similarity_threshold = config.RAG_SIMILARITY_THRESHOLD
                strong_similarity_threshold = config.RAG_STRONG_SIMILARITY_THRESHOLD
                rag_is_strong = bool(rag_prompt) and rag_top_similarity >= strong_similarity_threshold
                self._debug(
                    f"RAG 결과: strong={rag_is_strong} top_sim={rag_top_similarity:.3f} entries={len(rag_entries)}",
                    log_extra,
                )

                # --- [1단계] Lite 모델(PM) 호출: 의도 분석 및 계획 수립 --- 
                lite_system_prompt = f"{rag_prompt}\n\n{config.LITE_MODEL_SYSTEM_PROMPT}" if rag_prompt else config.LITE_MODEL_SYSTEM_PROMPT
                lite_model = genai.GenerativeModel(config.AI_INTENT_MODEL_NAME, system_instruction=lite_system_prompt)
                lite_conversation = history + [{'role': 'user', 'parts': [user_query]}]
                self._debug(
                    f"[Lite] system_prompt={self._truncate_for_debug(lite_system_prompt)} conversation={self._truncate_for_debug(lite_conversation)}",
                    log_extra,
                )

                logger.info("1단계: Lite 모델(의도 분석) 호출...", extra=log_extra)
                lite_response = await self._safe_generate_content(lite_model, lite_conversation, log_extra)

                if not lite_response or not lite_response.text:
                    logger.error("Lite 모델이 응답을 생성하지 못했습니다.", extra=log_extra)
                    await message.reply(config.MSG_AI_ERROR, mention_author=False)
                    return

                lite_response_text = lite_response.text.strip()
                logger.info(f"Lite 모델 응답: '{lite_response_text[:150]}...'", extra=log_extra)
                self._debug(f"[Lite] 응답: {self._truncate_for_debug(lite_response_text)}", log_extra)
                
                tool_plan_raw = self._parse_tool_calls(lite_response_text)
                tool_plan: list[dict] = []
                for call in tool_plan_raw:
                    if not isinstance(call, dict):
                        continue
                    normalized = dict(call)
                    if 'tool_to_use' not in normalized and 'tool_name' in normalized:
                        normalized['tool_to_use'] = normalized.get('tool_name')
                    tool_plan.append(normalized)

                if rag_is_strong and tool_plan:
                    filtered_plan = [call for call in tool_plan if call.get('tool_to_use') != 'web_search']
                    if len(filtered_plan) != len(tool_plan):
                        logger.info(
                            "강한 RAG 컨텍스트(최고 유사도=%.3f)로 web_search 단계를 생략합니다.",
                            rag_top_similarity,
                            extra=log_extra,
                        )
                    tool_plan = filtered_plan

                # --- [분기 처리] ---
                # Case 1: 간단한 대화로 판단된 경우 (<conversation_response> 태그 포함)
                if "<conversation_response>" in lite_response_text:
                    logger.info("분기: 간단한 대화로 판단, Main 모델을 호출하여 페르소나 기반으로 응답합니다.", extra=log_extra)
                    channel_config = config.CHANNEL_AI_CONFIG.get(message.channel.id, {})
                    persona = channel_config.get('persona', config.DEFAULT_TSUNDERE_PERSONA)
                    rules = channel_config.get('rules', config.DEFAULT_TSUNDERE_RULES)
                    system_prompt = f"{persona}\n\n{rules}"
                    
                    main_model = genai.GenerativeModel(config.AI_RESPONSE_MODEL_NAME, system_instruction=system_prompt)
                    main_response = await self._safe_generate_content(main_model, user_query, log_extra)

                    if main_response and main_response.text:
                        final_response_text = main_response.text.strip()
                        self._debug(f"[Main] 최종 응답(간단대화): {self._truncate_for_debug(final_response_text)}", log_extra)
                        debug_block = self._build_rag_debug_block(rag_entries)
                        if debug_block:
                            logger.debug("RAG 디버그 블록:\n%s", debug_block, extra=log_extra)
                        await message.reply(final_response_text, mention_author=False)
                        await db_utils.log_analytics(self.bot.db, "AI_INTERACTION", {"guild_id": message.guild.id, "user_id": message.author.id, "channel_id": message.channel.id, "trace_id": trace_id, "user_query": user_query, "tool_plan": [], "final_response": final_response_text, "is_fallback": False})
                    else:
                        logger.error("간단한 대화에 대해 Main 모델이 응답을 생성하지 못했습니다.", extra=log_extra)
                        await message.reply(config.MSG_AI_ERROR, mention_author=False)
                    return

                # Case 2: 도구 계획이 없는 경우 (Lite 모델이 직접 답변)
                if not tool_plan and not rag_prompt:
                    logger.info("분기: 도구 계획이 없으며, Lite 모델의 답변으로 바로 응답합니다.", extra=log_extra)
                    await message.reply(lite_response_text, mention_author=False)
                    await db_utils.log_analytics(self.bot.db, "AI_INTERACTION", {"guild_id": message.guild.id, "user_id": message.author.id, "channel_id": message.channel.id, "trace_id": trace_id, "user_query": user_query, "tool_plan": [], "final_response": lite_response_text, "is_fallback": False})
                    return

                if not tool_plan and rag_prompt:
                    logger.info(
                        "도구 계획이 없지만 강한 RAG 컨텍스트를 활용하여 Main 모델로 진행합니다.",
                        extra=log_extra,
                    )

                # --- [2단계] 도구 실행 ---
                logger.info(f"2단계: 도구 실행 시작. 총 {len(tool_plan)}단계.", extra=log_extra)
                self._debug(f"도구 계획: {self._truncate_for_debug(tool_plan)}", log_extra)
                tool_results = []
                if rag_prompt:
                    tool_results.append(
                        {
                            "step": 0,
                            "tool_name": "local_rag",
                            "parameters": {
                                "similarity_threshold": similarity_threshold,
                                "top_similarity": rag_top_similarity,
                            },
                                "result": {
                                    "entries": [
                                        {
                                            "origin": entry.get("origin"),
                                            "speaker": entry.get("speaker"),
                                            "similarity": entry.get("similarity"),
                                            "message": entry.get("message"),
                                            "db_path": entry.get("db_path"),
                                            "matched_server_id": entry.get("matched_server_id"),
                                            "context_window": entry.get("context_window") or [],
                                        }
                                        for entry in rag_entries
                                    ]
                                },
                            }
                        )

                for i, tool_call in enumerate(tool_plan, start=1):
                    step_num = i if not rag_prompt else i
                    logger.info(f"계획 실행 ({i}/{len(tool_plan)}): {tool_call.get('tool_to_use')}", extra=log_extra)
                    result = await self._execute_tool(tool_call, message.guild.id, user_query)
                    tool_results.append({"step": step_num, "tool_name": tool_call.get('tool_to_use'), "parameters": tool_call.get('parameters'), "result": result})

                # --- [폴백 로직] 도구 실패 시 웹 검색으로 대체 ---
                def is_tool_failed(result): return result is None or any(keyword in str(result).lower() for keyword in ["error", "오류", "실패", "없습니다", "알 수 없는", "찾을 수"])
                executed_tool_results = [res for res in tool_results if res.get("tool_name") not in {"local_rag"}]
                any_failed = any(is_tool_failed(res.get("result")) for res in executed_tool_results)
                use_fallback_prompt = False

                executed_tool_names = {res.get("tool_name") for res in executed_tool_results}
                if executed_tool_results and any_failed and 'web_search' not in executed_tool_names:
                    logger.info("하나 이상의 도구 실행에 실패하여 웹 검색으로 대체합니다.", extra=log_extra)
                    web_search_result = await self._execute_tool({"tool_to_use": "web_search", "parameters": {"query": user_query}}, message.guild.id, user_query)
                    rag_tool_entries = [res for res in tool_results if res.get("tool_name") == "local_rag"]
                    next_step = (rag_tool_entries[-1]["step"] + 1) if rag_tool_entries else 1
                    tool_results = rag_tool_entries + [{"step": next_step, "tool_name": "web_search", "parameters": {"query": user_query}, "result": web_search_result}]
                    use_fallback_prompt = True
                    self._debug("[도구] 웹 검색 폴백 수행", log_extra)

                # --- [3단계] Main 모델 호출: 최종 답변 생성 ---
                logger.info("3단계: Main 모델(답변 생성) 호출...", extra=log_extra)
                tool_results_str = self._format_tool_results_for_prompt(tool_results)

                # 프롬프트 선택: 일반 또는 웹 폴백용
                if use_fallback_prompt:
                    main_system_prompt = config.WEB_FALLBACK_PROMPT.format(user_query=user_query, tool_result=tool_results_str)
                else:
                    main_system_prompt = config.AGENT_SYSTEM_PROMPT.format(user_query=user_query, tool_result=tool_results_str)
                
                # 페르소나 적용
                channel_config = config.CHANNEL_AI_CONFIG.get(message.channel.id, {})
                persona = channel_config.get('persona', config.DEFAULT_TSUNDERE_PERSONA)
                rules = channel_config.get('rules', config.DEFAULT_TSUNDERE_RULES)
                main_system_prompt = f"{persona}\n\n{rules}\n\n{main_system_prompt}"
                if rag_prompt:
                    main_system_prompt = (
                        f"{main_system_prompt}\n\n### Local Conversation Context "
                        f"(최고 유사도 {rag_top_similarity:.3f})\n{rag_prompt}"
                    )

                main_model = genai.GenerativeModel(config.AI_RESPONSE_MODEL_NAME, system_instruction=main_system_prompt)
                main_prompt_parts = [f"사용자 질문: {user_query}"]
                if rag_prompt:
                    main_prompt_parts.append(
                        f"로컬 RAG 컨텍스트 (최고 유사도 {rag_top_similarity:.3f}):\n{rag_prompt}"
                    )
                main_prompt_parts.append(
                    "위 자료를 최우선으로 참고해서 사실에 근거한 답장을 반말로 작성해. 자료에 없는 내용은 모른다고 솔직하게 말하고, 억지 추측은 하지 마."
                )
                main_prompt = "\n\n".join(main_prompt_parts)
                self._debug(f"[Main] system_prompt={self._truncate_for_debug(main_system_prompt)}", log_extra)
                self._debug(f"[Main] user_prompt={self._truncate_for_debug(main_prompt)}", log_extra)
                main_response = await self._safe_generate_content(main_model, main_prompt, log_extra)

                if main_response and main_response.text:
                    final_response_text = main_response.text.strip()
                    self._debug(f"[Main] 최종 응답: {self._truncate_for_debug(final_response_text)}", log_extra)
                    debug_block = self._build_rag_debug_block(rag_entries)
                    if debug_block:
                        logger.debug("RAG 디버그 블록:\n%s", debug_block, extra=log_extra)
                    await message.reply(final_response_text, mention_author=False)
                    await db_utils.log_analytics(self.bot.db, "AI_INTERACTION", {"guild_id": message.guild.id, "user_id": message.author.id, "channel_id": message.channel.id, "trace_id": trace_id, "user_query": user_query, "tool_plan": tool_plan, "final_response": final_response_text, "is_fallback": use_fallback_prompt})
                else:
                    logger.error("Main 모델이 최종 답변을 생성하지 못했습니다.", extra=log_extra)
                    # 메시지 길이 제한 오류 방지를 위해 tool_results_str를 3800자로 자릅니다.
                    truncated_results = tool_results_str[:3800]
                    await message.reply(f"모든 도구를 실행했지만, 최종 답변을 만드는 데 실패했어요. 여기 결과라도 확인해보세요.\n```json\n{truncated_results}\n```", mention_author=False)

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

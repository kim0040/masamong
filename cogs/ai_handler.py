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
import config

# 지원 종료된 legacy SDK는 명시적으로 direct Gemini fallback을 켠
# 인스턴스에서만 불러온다. 기본 CometAPI 레인은 신규 google-genai/OpenAI
# client를 사용하므로 평상시 시작 시간과 경고를 늘리지 않는다.
if config.GEMINI_API_KEY and config.ALLOW_DIRECT_GEMINI_FALLBACK:
    try:
        import google.generativeai as genai
    except ModuleNotFoundError:  # pragma: no cover
        genai = None
else:
    genai = None

# 신규 Google GenAI SDK (for CometAPI/FastModel)
try:
    from google import genai as google_genai
except ImportError:
    google_genai = None

# CometAPI용 OpenAI 호환 클라이언트
try:
    from openai import AsyncOpenAI, APITimeoutError
except ImportError:  # pragma: no cover
    AsyncOpenAI = None
    APITimeoutError = None

from datetime import datetime, timedelta, timezone
import asyncio
from dataclasses import dataclass, field
import pytz
import re
from typing import Dict, Any, Tuple
import aiosqlite
import random
import time
import json
import io
import uuid
from logger_config import logger
from utils import db as db_utils
from utils.constants import DM_LIMIT_COUNT, DM_LIMIT_WINDOW_HOURS
from utils.llm_client import LLMClient
from utils.intent_analyzer import IntentAnalyzer
from utils.tool_health import ToolTemporarilyUnavailable
from utils.rag_manager import RAGManager
from utils.discord_helpers import (
    DiscordProgress,
    normalize_discord_text,
    split_message_chunks,
)
from utils.embeddings import (
    DiscordEmbeddingStore,
    KakaoEmbeddingStore,
)
from utils.hybrid_search import HybridSearchEngine
from utils.rag_policy import should_construct_bm25_manager
from utils.reranker import Reranker, RerankerConfig
from utils.privacy_consent import (
    CONSENT_GRANTED,
    FORTUNE_SCOPE,
    get_policy,
)
from .ai_prompting import AIPromptMixin
from .ai_tool_runtime import AIToolRuntimeMixin

# 저사양 기본 경로에서는 FTS5 모듈을 import하지 않습니다. 테스트가 이름을
# monkeypatch할 수 있게 속성만 둡니다.
BM25IndexManager = None

KST = pytz.timezone('Asia/Seoul')
FORTUNE_CONSENT_POLICY = get_policy(FORTUNE_SCOPE)


@dataclass
class _QueuedAIRequest:
    """한 번만 소비되는 Discord AI 요청과 재사용할 접수 메시지."""

    message: discord.Message
    enqueued_at: float
    notice: discord.Message | None = None
    status_claimed: bool = False
    notice_ready: asyncio.Event = field(default_factory=asyncio.Event)


class AIHandler(AIPromptMixin, AIToolRuntimeMixin, commands.Cog):
    """AI 에이전트 워크플로우를 통합 관리하는 Cog입니다.

    - Lite/Flash Gemini 모델을 사용해 의도 분석과 응답 생성을 수행합니다.
    - `ToolsCog`와 협력해 외부 API 호출, 후처리, 오류 복구를 담당합니다.
    - 대화 저장소(RAG)를 구축해 장기 기억과 능동형 제안을 지원합니다.
    """

    # 메인 user prompt의 선택 컨텍스트는 아래 순서대로 예산을 받는다.
    # 현재 질문과 도구 결과는 이 예산과 무관하게 먼저 자리를 예약한다.
    _RECENT_HISTORY_PROMPT_MAX_CHARS = 4_000
    _CONTEXT_DIGEST_PROMPT_MAX_CHARS = 1_200
    _FORTUNE_PROMPT_MAX_CHARS = 1_200
    _RAG_PROMPT_MAX_CHARS = 5_000
    _PROMPT_OMISSION_MARKER = "\n…(문자 예산에 맞춰 일부 생략)…\n"
    _USER_COOLDOWN_MAX_ENTRIES = 4_096
    _PROACTIVE_COOLDOWN_MAX_ENTRIES = 2_048
    _EMOJI_CACHE_MAX_GUILDS = 512

    def __init__(self, bot: commands.Bot):
        """AIHandler 초기화 — LLM 클라이언트, 임베딩 스토어, 검색 엔진 등 코어 컴포넌트를 설정합니다."""
        self.bot = bot
        self.tools_cog = bot.get_cog('ToolsCog')
        self.ai_user_cooldowns: Dict[int, datetime] = {}
        self.proactive_cooldowns: Dict[int, float] = {}
        # 뉴스 출처 리액션 캐시: {메시지ID: [URL, ...]} — 📰 리액션 클릭 시 출처 표시.
        # 프로세스 수명 동안만 필요한 UI 상태이며 상한을 둬 장기 실행 시 누적을 막는다.
        self._news_source_cache: Dict[int, list[str]] = {}
        # 같은 메시지에서 빠른 추가/취소 이벤트가 겹쳐도 최종 반응 수와
        # 본문 표시 상태가 어긋나지 않도록 메시지별로 직렬화한다.
        self._news_source_locks: Dict[int, asyncio.Lock] = {}
        # 서버별 말투 폴백 캐시: {guild_id: 그 서버에 설정된 채널 config}
        # 설정을 새로 만들지 않고 이미 있는 값을 재사용만 하므로,
        # 다른 서버의 guild_id로는 절대 조회되지 않는다.
        self._guild_channel_config_cache: Dict[int, dict] = {}
        self.gemini_configured = False
        self.api_call_lock = asyncio.Lock()
        # [저사양 보호] 전역 AI 처리 동시성 제한.
        # 저사양 서버에서 동시 유저가 몰리면 임베딩 인코딩 + LLM 호출이 동시에 폭주해
        # CPU 스래싱/메모리 스파이크가 발생한다. 동시 처리 수를 제한해 백프레셔를 건다.
        _ai_max_concurrent = max(1, int(getattr(config, "AI_MAX_CONCURRENT_PROCESSING", 3)))
        self.ai_processing_semaphore = asyncio.Semaphore(_ai_max_concurrent)
        self._ai_worker_count = _ai_max_concurrent
        self.ai_request_queue: asyncio.Queue[_QueuedAIRequest] = asyncio.Queue(
            maxsize=max(
                _ai_max_concurrent,
                int(getattr(config, "AI_QUEUE_MAX_SIZE", 8)),
            )
        )
        self._ai_queue_workers: list[asyncio.Task] = []
        self._ai_queue_start_lock = asyncio.Lock()
        self._ai_queue_closing = False
        self._ai_active_requests = 0
        # General 저사양 프로필은 AI 답변 자체는 사용할 수 있지만 로컬 RAG는
        # 끈 채 시작한다. 이때 사용되지 않을 저장소/검색/리랭커 객체까지 미리
        # 만들지 않는다. 운영 중 플래그를 바꾸는 설정은 지원하지 않으므로
        # Masamo의 활성 경로는 기존과 동일하게 한 번만 구성된다.
        self.rag_enabled = bool(
            config.AI_MEMORY_ENABLED and config.EMBEDDING_ENABLED
        )
        self.discord_embedding_store = None
        self.kakao_embedding_store = None
        self.bm25_manager = None
        self.reranker = None
        self.hybrid_search_engine = None

        if self.rag_enabled:
            self.discord_embedding_store = DiscordEmbeddingStore(
                config.DISCORD_EMBEDDING_DB_PATH
            )
            self.kakao_embedding_store = KakaoEmbeddingStore(
                config.KAKAO_EMBEDDING_DB_PATH,
                config.KAKAO_EMBEDDING_SERVER_MAP,
            ) if (
                config.KAKAO_MEMORY_ENABLED
                and (
                    config.KAKAO_EMBEDDING_DB_PATH
                    or config.KAKAO_EMBEDDING_SERVER_MAP
                )
            ) else None
            self.bm25_manager = None
            if should_construct_bm25_manager(config.BM25_DATABASE_PATH):
                from database.bm25_index import BM25IndexManager as _BM25IndexManager

                globals()["BM25IndexManager"] = _BM25IndexManager
                self.bm25_manager = _BM25IndexManager(config.BM25_DATABASE_PATH)

            if config.RERANK_ENABLED and config.RAG_RERANKER_MODEL_NAME:
                reranker_config = RerankerConfig(
                    model_name=config.RAG_RERANKER_MODEL_NAME,
                    device=config.RAG_RERANKER_DEVICE,
                    score_threshold=config.RAG_RERANKER_SCORE_THRESHOLD,
                )
                self.reranker = Reranker(reranker_config)
            self.hybrid_search_engine = HybridSearchEngine(
                self.discord_embedding_store,
                self.kakao_embedding_store,
                self.bm25_manager,
                reranker=self.reranker,
            )
        else:
            logger.info(
                "로컬 RAG 비활성: embedding store/search/reranker 구성을 건너뜁니다."
            )
        self.debug_enabled = config.AI_DEBUG_ENABLED
        self._debug_log_len = getattr(config, "AI_DEBUG_LOG_MAX_LEN", 400)
        self.llm_client = LLMClient(db=self.bot.db)
        self.intent_analyzer = IntentAnalyzer(db=self.bot.db, llm_client=self.llm_client, tools_cog=self.tools_cog)
        self.use_cometapi = self.llm_client.use_cometapi
        self.gemini_configured = self.llm_client.gemini_configured
        self.rag_manager = RAGManager(
            db=self.bot.db,
            embedding_store=self.discord_embedding_store,
            hybrid_search_engine=self.hybrid_search_engine,
            reranker=self.reranker,
            llm_client=self.llm_client,
            bot=self.bot,
        )

        logger.info(
            "LLM 레인 구성: routing=%s, main=%s",
            [f"{t['provider']}:{t['model']}" for t in self.llm_client.get_lane_targets("routing")] or ["none"],
            [f"{t['provider']}:{t['model']}" for t in self.llm_client.get_lane_targets("main")] or ["none"],
        )

        if self.gemini_configured and not config.ALLOW_DIRECT_GEMINI_FALLBACK:
            logger.info("Gemini direct fallback이 비활성화되어 레인(primary/fallback) 경로만 사용합니다.")
        if not self.use_cometapi and not self.llm_client.can_use_direct_gemini():
            logger.warning("사용 가능한 LLM 제공자가 없습니다. LLM 레인 키/엔드포인트 또는 Gemini fallback 설정을 확인하세요.")
        
        # [NEW] Location Cache from DB
        # [NEW] Emoji Cache: {guild_id: (formatted_list, timestamp)}
        self._emoji_cache: Dict[int, Tuple[list[str], float]] = {}

    # 이전 응답에서 제거할 레거시 안내 문구.
    NEWS_SOURCE_FOOTER = "\n\n📰 *뉴스 리액션을 누르면 출처를 확인할 수 있어!*"
    NEWS_SOURCE_SECTION = "\n\n📰 **뉴스 출처**\n"
    NEWS_SOURCE_CACHE_MAX = 512

    @classmethod
    def _format_web_source_footer(
        cls,
        source_urls: list[str],
        *,
        max_sources: int = 5,
        max_chars: int | None = None,
    ) -> str:
        """Discord 자동 임베드를 억제한 짧은 출처 목록을 만듭니다."""
        seen: set[str] = set()
        lines: list[str] = []
        char_budget = (
            max(0, min(int(max_chars), 2_000))
            if max_chars is not None
            else None
        )
        for raw_url in source_urls or []:
            url = str(raw_url or "").strip()
            if not re.match(r"^https?://", url, flags=re.IGNORECASE):
                continue
            if url in seen:
                continue
            # Discord 메시지 하나보다 긴 추적 URL은 UI를 망가뜨리고 어차피
            # 표시할 수 없다. 정상적인 기사 URL에는 충분한 여유를 둔다.
            if len(url) > 800:
                continue
            seen.add(url)
            candidate_lines = [*lines, f"{len(lines) + 1}. <{url}>"]
            candidate = cls.NEWS_SOURCE_SECTION + "\n".join(candidate_lines)
            if char_budget is not None and len(candidate) > char_budget:
                continue
            lines = candidate_lines
            if len(lines) >= max(1, min(int(max_sources), 8)):
                break
        if not lines:
            return ""
        return cls.NEWS_SOURCE_SECTION + "\n".join(lines)

    async def _register_news_source_reaction(
        self,
        messages: list[discord.Message],
        source_urls: list[str],
    ) -> discord.Message | None:
        """웹 답변 메시지 하나에 bounded 출처 캐시와 봇 📰 반응을 등록한다."""
        if not messages:
            return None
        valid_urls: list[str] = []
        seen: set[str] = set()
        for raw_url in source_urls or []:
            url = str(raw_url or "").strip()
            if (
                url in seen
                or len(url) > 800
                or not re.match(r"^https?://", url, flags=re.IGNORECASE)
            ):
                continue
            seen.add(url)
            valid_urls.append(url)
            if len(valid_urls) >= 5:
                break
        if not valid_urls:
            return None

        # 분할 응답 중 가장 짧은 조각을 골라 사용자가 반응했을 때 출처를
        # 같은 메시지에 안전하게 덧붙일 공간을 최대화한다.
        anchor = min(
            messages,
            key=lambda item: len(str(getattr(item, "content", "") or "")),
        )
        available = 2_000 - len(str(getattr(anchor, "content", "") or ""))
        if not self._format_web_source_footer(valid_urls, max_chars=available):
            logger.warning(
                "뉴스 출처 반응 등록 생략: 메시지 여유 공간 부족. message_id=%s",
                getattr(anchor, "id", None),
            )
            return None

        message_id = int(anchor.id)
        self._news_source_cache[message_id] = valid_urls
        self._news_source_locks.setdefault(message_id, asyncio.Lock())
        while len(self._news_source_cache) > self.NEWS_SOURCE_CACHE_MAX:
            oldest_message_id = next(iter(self._news_source_cache))
            self._news_source_cache.pop(oldest_message_id, None)
            self._news_source_locks.pop(oldest_message_id, None)
        try:
            await anchor.add_reaction("📰")
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            self._news_source_cache.pop(message_id, None)
            self._news_source_locks.pop(message_id, None)
            logger.warning(
                "뉴스 출처 📰 반응 등록 실패. message_id=%s",
                message_id,
                exc_info=True,
            )
            return None
        return anchor

    @staticmethod
    def _contextualize_web_query(
        query: str,
        user_query: str,
        history: list[dict] | None,
    ) -> str:
        """짧은 후속 검색에 같은 사용자의 직전 발화를 LLM 호출 없이 보강합니다."""
        current = re.sub(r"\s+", " ", str(query or user_query or "")).strip()
        if not current:
            return ""
        followup_signal = (
            len(current) <= 32
            or any(
                token in current.lower()
                for token in ("그거", "그건", "그게", "이거", "그럼", "가격은", "일정은", "왜")
            )
        )
        if not followup_signal:
            return current

        for item in reversed(history or []):
            if item.get("role") != "user" or not item.get("is_current_user"):
                continue
            parts = item.get("parts") or []
            previous = parts[0] if isinstance(parts, list) and parts else ""
            previous = re.sub(r"\s+", " ", str(previous)).strip()
            if previous and previous != current:
                return f"{previous[:220]}\n후속 질문: {current}"[:320]
        return current

    def _ensure_intent_analyzer(self) -> IntentAnalyzer:
        """부분 초기화된 테스트 인스턴스에서도 의도 분석기를 사용할 수 있게 보장합니다."""
        analyzer = getattr(self, "intent_analyzer", None)
        if analyzer is not None:
            return analyzer

        class _HandlerLLMClientAdapter:
            def __init__(self, handler: "AIHandler"):
                self.handler = handler

            @property
            def use_cometapi(self) -> bool:
                llm_client = getattr(self.handler, "llm_client", None)
                return bool(
                    getattr(self.handler, "use_cometapi", False)
                    or getattr(llm_client, "use_cometapi", False)
                )

            async def fast_generate_text(
                self,
                prompt: str,
                model: str | None,
                log_extra: dict,
                *,
                trace_key: str = "cometapi_fast",
                max_tokens: int | None = None,
            ) -> str | None:
                return await self.handler._cometapi_fast_generate_text(
                    prompt,
                    model,
                    log_extra,
                    trace_key=trace_key,
                    max_tokens=max_tokens,
                )

        analyzer = IntentAnalyzer(
            db=getattr(getattr(self, "bot", None), "db", None),
            llm_client=_HandlerLLMClientAdapter(self),
            tools_cog=getattr(self, "tools_cog", None),
        )
        self.intent_analyzer = analyzer
        return analyzer

    @property
    def is_ready(self) -> bool:
        """AI 핸들러가 모든 의존성(Gemini, DB, ToolsCog)을 포함하여 준비되었는지 확인합니다."""
        has_llm_provider = bool(self.use_cometapi or self._can_use_direct_gemini())
        return has_llm_provider and self.bot.db is not None and self.tools_cog is not None

    async def cog_load(self) -> None:
        """Cog가 활성화되면 설정된 수만큼 bounded FIFO worker를 시작합니다."""
        await self._ensure_ai_queue_workers()

    async def cog_unload(self) -> None:
        """reload/종료 시 worker와 미처리 접수 표시를 정리합니다."""
        await self.close_ai_queue()

    async def _ensure_ai_queue_workers(self) -> None:
        if self._ai_queue_closing or self._ai_queue_workers:
            return
        async with self._ai_queue_start_lock:
            if self._ai_queue_closing or self._ai_queue_workers:
                return
            self._ai_queue_workers = [
                asyncio.create_task(
                    self._ai_queue_worker(index),
                    name=f"masamong-ai-queue-{index}",
                )
                for index in range(self._ai_worker_count)
            ]
            logger.info(
                "AI FIFO 대기열 worker 시작: workers=%d capacity=%d",
                self._ai_worker_count,
                self.ai_request_queue.maxsize,
                extra={
                    "event": "ai_queue_started",
                    "outcome": "succeeded",
                    "queue_capacity": self.ai_request_queue.maxsize,
                    "active_count": 0,
                },
            )

    @staticmethod
    async def _delete_unclaimed_queue_notice(
        request: _QueuedAIRequest,
    ) -> None:
        if request.notice is None or request.status_claimed:
            return
        try:
            await request.notice.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    async def enqueue_message(self, message: discord.Message) -> bool:
        """메시지를 bounded FIFO에 한 번 넣고 즉시 반환합니다.

        실행 worker 수가 CPU 동시성이고, Queue의 maxsize가 대기 메모리 상한이다.
        provider 재시도나 LLM 호출은 이 단계에서 수행하지 않는다.
        """
        if self._ai_queue_closing:
            return False
        await self._ensure_ai_queue_workers()

        guild_id = message.guild.id if message.guild else None
        log_extra = {
            "guild_id": guild_id,
            "channel_id": message.channel.id,
            "user_id": message.author.id,
        }
        ahead = self._ai_active_requests + self.ai_request_queue.qsize()
        if ahead:
            notice_text = (
                f"⏳ 요청을 대기열에 넣었어요. 앞의 {ahead}개 요청이 끝나면 "
                "자동으로 시작할게요."
            )
        else:
            notice_text = "⏳ 요청을 접수했어요. 곧 확인할게요."

        request = _QueuedAIRequest(
            message=message,
            enqueued_at=time.monotonic(),
        )
        # Discord 전송을 await하기 전에 자리를 원자적으로 확보한다. 그렇지
        # 않으면 동시에 들어온 모든 on_message task가 비어 있는 큐를 보고 접수
        # 메시지 전송에서 대기해 bounded queue 상한을 우회할 수 있다.
        try:
            self.ai_request_queue.put_nowait(request)
        except asyncio.QueueFull:
            logger.warning(
                "AI FIFO 대기열이 가득 차 요청을 받지 못했습니다.",
                extra={
                    **log_extra,
                    "event": "ai_queue_rejected",
                    "outcome": "rejected",
                    "failure_kind": "queue_full",
                    "queue_depth": self.ai_request_queue.qsize(),
                    "queue_capacity": self.ai_request_queue.maxsize,
                    "active_count": self._ai_active_requests,
                },
            )
            try:
                await message.channel.send(
                    "지금 대기 중인 요청이 너무 많아요. 잠시 뒤에 다시 불러주세요.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            return False

        logger.info(
            "AI 요청을 FIFO 대기열에 넣었습니다.",
            extra={
                **log_extra,
                "event": "ai_queue_enqueued",
                "outcome": "queued",
                "stage": "queued",
                "queue_depth": self.ai_request_queue.qsize(),
                "queue_capacity": self.ai_request_queue.maxsize,
                "active_count": self._ai_active_requests,
            },
        )
        try:
            request.notice = await message.channel.send(
                notice_text,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            # 접수 표시가 실패해도 기존 초기 상태 전송 경로에서 한 번 더
            # 시도할 수 있으므로 요청 자체는 큐에 보존한다.
            pass
        finally:
            request.notice_ready.set()

        if self._ai_queue_closing and request.notice is not None:
            try:
                await request.notice.edit(
                    content=(
                        "봇이 재시작되어 이 요청은 처리하지 않았어요. "
                        "다시 한 번 불러주세요."
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        return True

    async def _ai_queue_worker(self, worker_index: int) -> None:
        """FIFO 항목을 정확히 한 번 처리하는 장기 실행 worker."""
        while True:
            request = await self.ai_request_queue.get()
            acquired = False
            active_counted = False
            try:
                await request.notice_ready.wait()
                self._ai_active_requests += 1
                active_counted = True
                queue_wait_ms = max(
                    0,
                    round((time.monotonic() - request.enqueued_at) * 1000),
                )
                message = request.message
                log_extra = {
                    "guild_id": message.guild.id if message.guild else None,
                    "channel_id": message.channel.id,
                    "user_id": message.author.id,
                }
                await self.ai_processing_semaphore.acquire()
                acquired = True
                logger.info(
                    "AI FIFO 요청 처리를 시작합니다. worker=%d",
                    worker_index,
                    extra={
                        **log_extra,
                        "event": "ai_queue_dequeued",
                        "outcome": "processing",
                        "stage": "dequeued",
                        "queue_wait_ms": queue_wait_ms,
                        "queue_depth": self.ai_request_queue.qsize(),
                        "queue_capacity": self.ai_request_queue.maxsize,
                        "active_count": self._ai_active_requests,
                    },
                )
                await self.process_agent_message(
                    message,
                    queue_request=request,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - worker 생존 경계
                message = request.message
                log_extra = {
                    "guild_id": message.guild.id if message.guild else None,
                    "channel_id": message.channel.id,
                    "user_id": message.author.id,
                }
                logger.error(
                    "AI FIFO worker 처리 중 오류: %s",
                    type(exc).__name__,
                    exc_info=True,
                    extra={
                        **log_extra,
                        "event": "ai_queue_item_failed",
                        "outcome": "failed",
                        "stage": "queue_worker",
                        "error_kind": type(exc).__name__,
                    },
                )
            finally:
                if acquired:
                    self.ai_processing_semaphore.release()
                if active_counted:
                    self._ai_active_requests = max(
                        0,
                        self._ai_active_requests - 1,
                    )
                await self._delete_unclaimed_queue_notice(request)
                self.ai_request_queue.task_done()

    async def close_ai_queue(self) -> None:
        """worker를 취소하고 아직 시작하지 않은 항목을 중복 호출 없이 폐기합니다."""
        if self._ai_queue_closing:
            return
        self._ai_queue_closing = True

        workers = list(self._ai_queue_workers)
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self._ai_queue_workers.clear()

        pending: list[_QueuedAIRequest] = []
        while True:
            try:
                request = self.ai_request_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            pending.append(request)
            self.ai_request_queue.task_done()

        async def _mark_interrupted(request: _QueuedAIRequest) -> None:
            if request.notice is None or request.status_claimed:
                return
            try:
                await request.notice.edit(
                    content=(
                        "봇이 재시작되어 이 요청은 처리하지 않았어요. "
                        "다시 한 번 불러주세요."
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException,
            ):
                pass

        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *(_mark_interrupted(item) for item in pending)
                    ),
                    timeout=3.0,
                )
            except asyncio.TimeoutError:
                pass
        logger.info(
            "AI FIFO 대기열 종료: discarded=%d",
            len(pending),
            extra={
                "event": "ai_queue_stopped",
                "outcome": "succeeded",
                "queue_depth": 0,
                "queue_capacity": self.ai_request_queue.maxsize,
                "active_count": 0,
            },
        )

    def _can_use_direct_gemini(self) -> bool:
        return self.llm_client.can_use_direct_gemini()

    @staticmethod
    def _normalize_provider(provider: Any) -> str:
        """LLM 제공자 식별자를 소문자 문자열로 정규화합니다."""
        return LLMClient.normalize_provider(provider)

    @staticmethod
    def _strip_mention_guard(text: Any) -> str:
        """프롬프트 텍스트에서 멘션 가드 스니펫을 제거합니다."""
        return LLMClient.strip_mention_guard(text)

    def _get_lane_targets(self, lane: str, *, model_override: str | None = None) -> list[dict[str, str]]:
        """지정된 레인(라우팅/메인)의 LLM 타깃 목록을 조회합니다."""
        return self.llm_client.get_lane_targets(lane, model_override=model_override)

    def _get_openai_client(self, base_url: str, api_key: str) -> Any | None:
        """캐시된 OpenAI 호환 클라이언트를 반환하거나 새로 생성합니다."""
        return self.llm_client.get_openai_client(base_url, api_key)

    def _get_gemini_compat_client(self, base_url: str, api_key: str) -> Any | None:
        """캐시된 Gemini 호환 클라이언트를 반환하거나 새로 생성합니다."""
        return self.llm_client.get_gemini_compat_client(base_url, api_key)

    async def _call_main_lane_target(
        self,
        target,
        *,
        system_prompt,
        user_prompt,
        log_extra,
        max_tokens,
        reasoning_effort_override: str | None = None,
    ):
        """시스템/사용자 프롬프트로 단일 메인 레인 LLM 타겟을 호출합니다."""
        return await self.llm_client.call_main_lane_target(
            target, system_prompt=system_prompt, user_prompt=user_prompt,
            log_extra=log_extra, max_tokens=max_tokens,
            reasoning_effort_override=reasoning_effort_override,
        )

    async def _call_routing_lane_target(
        self,
        target,
        *,
        prompt,
        log_extra,
        max_tokens: int | None = None,
    ):
        """단일 라우팅 레인 LLM 타겟을 호출하여 프롬프트 응답을 반환합니다."""
        return await self.llm_client.call_routing_lane_target(
            target,
            prompt=prompt,
            log_extra=log_extra,
            max_tokens=max_tokens,
        )

    def _debug(self, message: str, log_extra: dict[str, Any] | None = None) -> None:
        """디버그 설정이 켜진 경우에만 메시지를 기록합니다."""
        self.llm_client.debug(message, log_extra)

    def _truncate_for_debug(self, value: Any) -> str:
        """긴 문자열을 로그용으로 잘라냅니다."""
        return self.llm_client.truncate_for_debug(value)

    def _format_prompt_debug(self, prompt: Any) -> str:
        """프롬프트를 JSON 또는 일반 문자열로 축약합니다."""
        return self.llm_client.format_prompt_debug(prompt)

    async def _load_location_cache(self):
        """DB에서 지역명 데이터를 로드하여 캐싱합니다."""
        await self._ensure_intent_analyzer()._load_location_cache()

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

    @staticmethod
    def _build_interaction_analytics(
        *,
        message: discord.Message,
        trace_id: str,
        user_query: str,
        final_response: str,
        tool_plan: list[dict[str, Any]] | None,
        response_message_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """기본적으로 원문 없이 AI 상호작용 메타데이터만 저장합니다."""
        tool_names: list[str] = []
        for item in tool_plan or []:
            if not isinstance(item, dict):
                continue
            name = (
                item.get("tool_name")
                or item.get("tool_to_use")
                or item.get("name")
                or item.get("tool")
            )
            if name:
                tool_names.append(str(name))
        details: dict[str, Any] = {
            # analytics_log.guild_id는 TiDB에서 BIGINT이므로 DM은 문자열 sentinel
            # 대신 NULL로 저장한다.
            "guild_id": message.guild.id if message.guild else None,
            "user_id": message.author.id,
            "channel_id": message.channel.id,
            "trace_id": trace_id,
            "user_query_chars": len(user_query),
            "final_response_chars": len(final_response),
            "tools": list(dict.fromkeys(tool_names)),
        }
        if response_message_ids is not None:
            details.update(
                {
                    "response_message_count": len(response_message_ids),
                    "response_message_ids": list(response_message_ids)[:8],
                }
            )
        if config.ANALYTICS_STORE_CONTENT:
            details.update(
                {
                    "user_query": user_query,
                    "tool_plan": tool_plan or [],
                    "final_response": final_response,
                }
            )
        return details

    async def _record_delivered_response_messages(
        self,
        response_messages: list[discord.Message],
        log_extra: dict[str, Any],
    ) -> list[int]:
        """최종 전송 메시지를 응답 흐름과 분리된 기억 저장소에 기록합니다."""
        recorder = getattr(
            getattr(self, "rag_manager", None),
            "record_delivered_bot_messages",
            None,
        )
        if not callable(recorder) or not response_messages:
            return []
        try:
            return await recorder(response_messages)
        except Exception as exc:  # pragma: no cover - 응답 전송과 기억 저장 격리
            logger.error(
                "전송된 봇 응답 기억 기록 실패: %s",
                exc,
                exc_info=True,
                extra={
                    **log_extra,
                    "event": "assistant_response_memory_failed",
                    "failure_kind": type(exc).__name__,
                },
            )
            return []

    async def _deliver_single_image_result(
        self,
        *,
        message: discord.Message,
        status_msg: discord.Message,
        progress: DiscordProgress,
        image_payload: dict[str, Any],
        log_extra: dict[str, Any],
        delivered_messages: list[discord.Message] | None = None,
    ) -> str:
        """이미지 단독 결과를 디스코드에 정확히 한 번 전송합니다."""
        image_error = str(image_payload.get("error") or "").strip()
        image_data = image_payload.get("image_data")
        await progress.stop()

        if image_error:
            # image_error는 이미 완결된 안내 문장이다. 접두어를 덧붙이지 않는다.
            response_text = f"😅 {image_error}"
            edited = await status_msg.edit(content=response_text)
            if delivered_messages is not None:
                delivered_messages.append(edited or status_msg)
            return response_text

        if not image_data:
            response_text = (
                "그림이 제대로 나오지 않았어요. "
                "잠시 뒤에 다시 부탁해주세요."
            )
            edited = await status_msg.edit(content=response_text)
            if delivered_messages is not None:
                delivered_messages.append(edited or status_msg)
            return response_text

        extension = {
            "image/png": "png",
            "image/webp": "webp",
            "image/jpeg": "jpg",
        }.get(
            str(image_payload.get("mime_type") or "").casefold(),
            "png",
        )
        remaining = max(
            0,
            int(image_payload.get("remaining") or 0),
        )
        image_file = discord.File(
            io.BytesIO(image_data),
            filename=f"masamong_image.{extension}",
        )
        response_text = (
            "🎨 요청한 이미지를 한 장으로 완성했어요.\n"
            f"남은 생성 횟수: {remaining}회"
        )
        sent_message = await message.channel.send(
            content=response_text,
            file=image_file,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if delivered_messages is not None:
            delivered_messages.append(sent_message)
        try:
            await status_msg.delete()
        except Exception:
            logger.debug(
                "이미지 완료 후 상태 메시지 삭제 실패",
                exc_info=True,
                extra=log_extra,
            )
        logger.info(
            "최종 이미지 1장 전송 완료. bytes=%d",
            len(image_data),
            extra=log_extra,
        )
        return response_text

    async def get_ai_completion(
        self,
        prompt: str,
        system_role: str = "도움이 되는 친절한 보조원",
        model: str | None = None
    ) -> str | None:
        return await self.llm_client.get_ai_completion(prompt, system_role, model)

    async def _safe_generate_content(self, model, prompt, log_extra, generation_config=None):
        return await self.llm_client.safe_generate_content(model, prompt, log_extra, generation_config)

    def _looks_like_prompt_leakage(self, response_text: str) -> bool:
        """시스템/내부 지시문 유출로 보이는 응답을 선별 차단합니다."""
        return self.llm_client.looks_like_prompt_leakage(response_text)

    async def _cometapi_generate_content(
        self,
        system_prompt: str,
        user_prompt: str,
        log_extra: dict,
        model: str | None = None,
        *,
        stop_on_bounded_failure: bool = False,
        reasoning_effort_override: str | None = None,
    ) -> str | None:
        """메인 레인(primary/fallback)을 통해 응답을 생성합니다.

        Rate Limit 확인 → 프롬프트 길이 제한 → Primary/Fallback 순차 호출 → 응답 반환.
        """
        return await self.llm_client.generate_content(
            system_prompt,
            user_prompt,
            log_extra,
            model,
            raise_on_bounded_failure=stop_on_bounded_failure,
            reasoning_effort_override=reasoning_effort_override,
        )

    async def _cometapi_fast_generate_text(
        self,
        prompt: str,
        model: str | None,
        log_extra: dict,
        *,
        trace_key: str = "cometapi_fast",
        max_tokens: int | None = None,
    ) -> str | None:
        """라우팅 레인 Fast 모델을 통해 텍스트를 생성합니다."""
        llm_client = getattr(self, "llm_client", None)
        if llm_client is not None:
            return await llm_client.fast_generate_text(
                prompt,
                model,
                log_extra,
                trace_key=trace_key,
                max_tokens=max_tokens,
            )

        targets = self._get_lane_targets("routing", model_override=model)
        for target in targets:
            response_text = await self._call_routing_lane_target(
                target,
                prompt=prompt,
                log_extra=log_extra,
                max_tokens=max_tokens,
            )
            if response_text:
                return str(response_text).strip()
        return None

    async def _generate_local_embedding(self, content: str, log_extra: dict, prefix: str = "") -> Any | None:
        """SentenceTransformer 기반 임베딩을 생성합니다."""
        return await self.rag_manager._generate_local_embedding(content, log_extra, prefix)

    @staticmethod
    def _estimate_window_tokens(text: str) -> int:
        """윈도우 저장 판단용 경량 토큰 추정치."""
        return RAGManager._estimate_window_tokens(text)

    async def _embedding_token_limit(self) -> int:
        """임베딩 입력에 사용할 안전 토큰 한계를 반환합니다."""
        return await self.rag_manager._embedding_token_limit()

    async def add_message_to_history(self, message: discord.Message):
        """AI 허용 채널의 메시지를 대화 기록 DB에 저장합니다.

        Args:
            message (discord.Message): Discord 원본 메시지.

        Notes:
            메시지가 충분히 길면 임베딩 생성을 비동기 태스크로 예약합니다.
        """
        return await self.rag_manager.add_message_to_history(message)

    async def _summarize_content(self, text: str) -> str:
        """긴 텍스트를 임베딩용으로 요약합니다. DeepSeek 모델을 사용하여 검색 품질을 최적화합니다."""
        return await self.rag_manager._summarize_content(text)

    async def _create_window_embedding(self, guild_id: int, channel_id: int, payload: list[dict[str, Any]]):
        """대화 윈도우를 구조화 메모리 유닛으로 정제해 저장합니다."""
        return await self.rag_manager._create_window_embedding(guild_id, channel_id, payload)

    async def _update_conversation_windows(self, message: discord.Message) -> None:
        """대화 슬라이딩 윈도우(6개, stride=3)를 누적해 별도 테이블에 저장합니다."""
        return await self.rag_manager._update_conversation_windows(message)

    # ========== 뉴스/실시간 정보 검색 (DuckDuckGo RAG) ==========

    async def _generate_image_prompt(
        self,
        user_query: str,
        log_extra: dict,
        rag_context: str | None = None,
        interpreted_query: str | None = None,
    ) -> str | None:
        """원문과 관련 기억을 보존한 단일 이미지 프롬프트를 만듭니다.

        현재 이미지 모델은 한국어를 직접 지원하므로 번역 LLM을 한 번 더
        호출하지 않는다. 번역·재서술 과정에서 사용자의 대상을 바꾸는 문제와
        지연을 피하고, 라우터의 해석은 원문을 보조하는 힌트로만 사용한다.
        """
        from utils.constants import contains_nsfw

        request = re.sub(r"\s+", " ", str(user_query or "")).strip()
        if not request:
            return None
        request = self._clip_prompt_text(request, 1_200)

        # 안전하지 않은 요청을 무관한 풍경으로 조용히 바꿔 생성하지 않는다.
        # 최종 provider 호출 직전의 안전 검사에서 명시적인 실패로 안내한다.
        if contains_nsfw(request):
            logger.warning(
                "이미지 생성 요청에 로컬 선차단 표현이 있습니다. chars=%d",
                len(request),
                extra=log_extra,
            )
            return request

        prompt_sections = [
            "Create exactly one final, cohesive image from the request below.",
            f"[Authoritative user request]\n{request}",
        ]

        interpreted = re.sub(
            r"\s+",
            " ",
            str(interpreted_query or ""),
        ).strip()
        if (
            interpreted
            and interpreted.casefold() != request.casefold()
            and not contains_nsfw(interpreted)
        ):
            prompt_sections.append(
                "[Optional conversation-resolution hint]\n"
                + self._clip_prompt_text(interpreted, 500)
            )

        context = str(rag_context or "").strip()
        if context and not contains_nsfw(context):
            prompt_sections.append(
                "[Related memory from this Discord scope]\n"
                + self._clip_prompt_text(context, 800)
            )

        prompt_sections.append(
            "[Output requirements]\n"
            "- Return one final image only.\n"
            "- Use one unified composition. Do not make a collage, split screen, "
            "diptych, triptych, contact sheet, comparison grid, or multiple variants.\n"
            "- The authoritative request overrides the optional hint and memory.\n"
            "- Use memory only when it clearly describes the requested subject; "
            "ignore unrelated facts and preferences.\n"
            "- Do not infer sensitive traits or claim a real likeness when visual "
            "traits were never stated; use a clearly imaginative interpretation.\n"
            "- Preserve the requested style, subject, setting, and wording. If text "
            "inside the image was requested, render it in the requested language."
        )
        image_prompt = "\n\n".join(prompt_sections)
        self._debug(
            f"[이미지 프롬프트] 준비됨: {self._truncate_for_debug(image_prompt)}",
            log_extra,
        )
        return image_prompt


    async def _refine_search_query_with_llm(self, query: str, history: list, log_extra: dict) -> str:
        """대화 히스토리를 바탕으로 사용자의 모호한 질문을 명확한 검색어로 정제합니다."""
        # 히스토리 텍스트 변환 (최근 3개)
        history_text = ""
        if history:
            h_lines = []
            for h in history[-3:]:
                role = "U" if h['role'] == 'user' else "M"
                content = h['parts'][0] if isinstance(h['parts'], list) else str(h['parts'])
                h_lines.append(f"{role}: {content}")
            history_text = "\n".join(h_lines)

        prompt = (
            "당신은 검색 쿼리 최적화 전문가입니다. 이전 대화 맥락을 바탕으로 사용자의 현재 질문을 "
            "단독 검색이 가능한 명확한 검색어로 변환하세요. 다른 설명 없이 정제된 검색어만 출력하세요.\n\n"
            "예시:\n"
            "Context: U: 이란 이스라엘 전쟁 소식 알려줘 | M: (답변)\n"
            "User: 군비는 얼마나 썼대?\n"
            "Result: 이란 이스라엘 전쟁 군비 지출액\n\n"
            f"--- Current Context ---\n{history_text}\n"
            f"User Message: {query}\n"
            "Result:"
        )
        try:
            if not self.use_cometapi:
                return query
            refined = await self._cometapi_fast_generate_text(
                prompt,
                None,
                log_extra,
                trace_key="cometapi_fast_refine",
            )
            return refined if refined else query
        except Exception as e:
            logger.warning(f"쿼리 정제 실패: {e}")
            return query


    # Keyword / pattern sets moved to IntentAnalyzer (see utils/intent_analyzer.py)

    def _is_smalltalk_only_query(self, query: str) -> bool:
        """외부 도구 호출이 불필요한 인사/잡담성 질문인지 판별합니다."""
        return self._ensure_intent_analyzer()._is_smalltalk_only_query(query)

    def _has_explicit_web_search_intent(self, query: str) -> bool:
        """질문이 명시적으로 외부 웹 탐색을 요구하는지 판별합니다."""
        return self._ensure_intent_analyzer()._has_explicit_web_search_intent(query)

    def _looks_like_external_fact_query(self, query: str) -> bool:
        """
        웹에서 사실 확인이 필요한 질의인지 휴리스틱으로 판별합니다.
        (명시적 웹검색 키워드가 없어도 외부 정보가 필요한 질문을 놓치지 않기 위한 보정)
        """
        return self._ensure_intent_analyzer()._looks_like_external_fact_query(query)

    def _is_realtime_web_query(self, query: str) -> bool:
        """질의에 실시간 웹 검색이 필요한지 여부를 판단합니다."""
        return self._ensure_intent_analyzer()._is_realtime_web_query(query)

    def _looks_like_finance_query(self, query: str) -> bool:
        """회사명 단독 언급 오탐을 줄이기 위해 금융 의도 문맥까지 함께 확인합니다."""
        return self._ensure_intent_analyzer()._looks_like_finance_query(query)

    @staticmethod
    def _normalize_realtime_web_query(query: str) -> str:
        """실시간 질의에서 과거 연/월 오염 토큰을 제거하고 현재 날짜 앵커를 부여합니다."""
        return IntentAnalyzer._normalize_realtime_web_query(query)

    def _has_tool_keyword_signal(self, query: str) -> bool:
        """질문에 도구 호출이 필요한 명시적 신호가 있는지 판별합니다."""
        return self._ensure_intent_analyzer()._has_tool_keyword_signal(query)

    @staticmethod
    def _auto_web_search_scope_key(message: discord.Message) -> int:
        """자동 웹검색 쿨다운 범위 키를 산출합니다.

        서버/채널 또는 DM 단위로 쿨다운을 적용할 스코프를 반환합니다.
        """
        return IntentAnalyzer._auto_web_search_scope_key(message)

    def _can_run_auto_web_search(self, message: discord.Message, query: str, log_extra: dict | None = None) -> bool:
        """
        자동 웹검색(도구 계획이 없을 때의 fallback) 실행 가능 여부를 판단합니다.
        명시적 웹검색 요청은 쿨다운을 적용하지 않습니다.
        """
        return self._ensure_intent_analyzer()._can_run_auto_web_search(message, query, log_extra)

    def _mark_auto_web_search_used(self, message: discord.Message) -> None:
        """자동 웹 검색 사용 시점을 기록하여 스코프별 쿨다운을 갱신합니다."""
        return self._ensure_intent_analyzer()._mark_auto_web_search_used(message)

    def _sanitize_tool_plan(
        self,
        query: str,
        tool_plan: list[dict],
        *,
        rag_top_score: float,
        log_extra: dict | None = None,
        trust_llm: bool = False,
    ) -> list[dict]:
        """LLM 도구 계획을 운영 정책(과도한 웹검색 방지) 기준으로 보정합니다."""
        return self._ensure_intent_analyzer()._sanitize_tool_plan(
            query, tool_plan, rag_top_score=rag_top_score, log_extra=log_extra, trust_llm=trust_llm,
        )

    async def _should_use_web_search(self, query: str, rag_top_score: float, history: list = None) -> bool:
        """외부 정보 탐색(뉴스/웹/블로그/문서) 필요 여부를 판단합니다."""
        return await self._ensure_intent_analyzer()._should_use_web_search(query, rag_top_score, history)

    async def _detect_tools_by_llm(self, query: str, log_extra: dict, history: list = None) -> list[dict]:
        """사용자의 의도와 대화 맥락을 분석하여 가장 적합한 도구와 최적화된 검색 파라미터를 결정합니다."""
        return await self._ensure_intent_analyzer()._detect_tools_by_llm(query, log_extra, history)

    async def _route_tools(
        self,
        query: str,
        log_extra: dict,
        history: list = None,
        *,
        conversation_scope: str = "",
    ):
        """키워드가 아닌 의미 기반 라우팅 결과와 장기기억 필요 여부를 반환합니다."""
        return await self._ensure_intent_analyzer().route_tools(
            query,
            log_extra,
            history,
            conversation_scope=conversation_scope,
        )

    @staticmethod
    def _should_search_memory(routing_decision: Any) -> bool:
        """명시 기억 요청은 깊게, 라우터 장애 시에만 얕게 검색합니다.

        정상 의미 라우터는 특정 인물·과거 합의까지 포함해 장기기억 필요 여부를
        한 번에 판정한다. 그 결과가 신뢰 가능한데도 모든 무도구 대화에서 RAG를
        다시 실행하면 인사·일반 지식에도 로컬 임베딩과 TiDB 조회가 발생한다.
        provider 장애로 제한 fallback을 쓴 경우에만 기존 얕은 검색을 안전망으로
        남긴다. 실제 주입 여부는 관련성 게이트가 다시 검증한다.
        """
        if bool(getattr(routing_decision, "needs_memory", False)):
            return True
        if not bool(
            getattr(config, "RAG_PASSIVE_NO_TOOL_SEARCH_ENABLED", True)
        ):
            return False
        if bool(getattr(routing_decision, "plan", None)):
            return False
        return getattr(routing_decision, "source", None) != "llm"

    @staticmethod
    def _select_final_history(
        history: list[dict[str, Any]],
        routing_decision: Any,
    ) -> list[dict[str, Any]]:
        """라우터가 읽은 짧은 최근 문맥이 최종 모델 앞에서 사라지지 않게 합니다."""
        if getattr(routing_decision, "context_digest", ""):
            limit = max(
                1,
                int(getattr(config, "AI_CONTEXT_RECENT_TURNS", 8)),
            )
        else:
            # 최종 프롬프트 빌더가 최근 대화에 별도 문자 예산(기본 4,000자)을
            # 적용한다. 여기서 RAG 여부만으로 8/12개까지 먼저 잘라내면 짧은
            # 13~24번째 메시지는 압축도 검색도 되지 않는 공백이 생긴다.
            limit = max(
                1,
                int(
                    getattr(
                        config,
                        "AI_CONTEXT_SOURCE_HISTORY_LIMIT",
                        max(
                            config.HISTORY_LIMIT_WITH_RAG,
                            config.HISTORY_LIMIT_WITHOUT_RAG,
                        ),
                    )
                ),
            )
        return history[-limit:]

    def _detect_tools_by_keyword(self, query: str) -> list[dict]:
        """키워드 기반 도구 감지 (LLM 실패 시 fallback)."""
        return self._ensure_intent_analyzer()._detect_tools_by_keyword(query)

    @staticmethod
    def _build_finance_news_query(query: str) -> str:
        """금융 질문을 웹 검색 친화 쿼리로 보정합니다."""
        return IntentAnalyzer._build_finance_news_query(query)

    def _extract_location_from_query(self, query: str) -> str | None:
        """쿼리에서 지역명을 추출합니다 (DB 캐시 사용)."""
        return self._ensure_intent_analyzer()._extract_location_from_query(query)

    async def _get_rag_context(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int,
        query: str,
        recent_messages: list[str] | None = None,
        *,
        deep_search: bool = False,
    ) -> tuple[str, list[dict[str, Any]], float, list[str]]:
        """RAG: 하이브리드 검색 결과를 바탕으로 컨텍스트를 구성합니다."""
        if not getattr(
            self,
            "rag_enabled",
            bool(config.AI_MEMORY_ENABLED and config.EMBEDDING_ENABLED),
        ):
            return "", [], 0.0, []

        log_extra = {'guild_id': guild_id, 'channel_id': channel_id, 'user_id': user_id}
        logger.info(
            "RAG 컨텍스트 검색 시작. query_chars=%d",
            len(query),
            extra=log_extra,
        )

        engine = getattr(self, "hybrid_search_engine", None)
        if engine is None:
            logger.warning("하이브리드 검색 엔진이 초기화되지 않았습니다.", extra=log_extra)
            return "", [], 0.0, []

        # RAG 스코프 정책:
        # - guild(channel) 기본: 채널 전체 맥락을 회수
        # - guild(user): 요청자 본인 메시지만 회수
        # - DM: 채널 ID 자체가 사용자별로 분리되므로 user 필터를 두지 않음
        rag_scope = getattr(config, "RAG_GUILD_SCOPE", "channel")
        if guild_id and rag_scope == "user":
            search_user_id = user_id
        else:
            search_user_id = None

        try:
            result = await engine.search(
                query,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=search_user_id,
                memory_user_id=user_id,
                recent_messages=recent_messages,
                deep_search=deep_search,
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
        selected_source_counts: dict[str, int] = {}

        # INFO 로그에는 대화 원문을 넣지 않고 점수/출처/식별자만 남긴다.
        log_lines = []
        for entry in result.entries[:limit]:
            score = float(entry.get("combined_score", 0.0) or entry.get("score", 0.0) or 0.0)
            try:
                entry_threshold = float(entry.get("acceptance_threshold", threshold))
            except (TypeError, ValueError):
                entry_threshold = threshold
            entry_threshold = max(-1.0, min(1.0, entry_threshold))
            dialogue_block = (entry.get("dialogue_block") or entry.get("message") or "").strip()
            # 소스 태그 결정: origin 필드 또는 형식으로 판단
            origin = str(entry.get("origin", "")).lower()
            if origin == "kakao":
                source_tag = "[KAKAO]"
            elif origin == "discord":
                source_tag = "[DISCORD]"
            else:
                source_tag = "[UNKNOWN]"

            log_lines.append(
                f"  [{score:.3f}/≥{entry_threshold:.3f}] "
                f"{source_tag} message_id={entry.get('message_id') or '-'}"
            )

            # 검색 엔진은 저장소 유형별로 서로 다른 임계값을 적용한다.
            # 구조화 메모리(기본 0.50)를 통과한 결과를 여기서 전역 0.60으로
            # 다시 잘라내면 누적 메모리가 사실상 사용되지 않으므로, 후보가
            # 생성될 때 기록한 임계값을 그대로 존중한다.
            if score < entry_threshold:
                continue

            if not dialogue_block:
                continue

            rag_blocks.append(dialogue_block)
            source_key = source_tag.strip("[]").lower()
            selected_source_counts[source_key] = (
                selected_source_counts.get(source_key, 0) + 1
            )
            prepared_entries.append(
                {
                    "dialogue_block": dialogue_block,
                    "combined_score": score,
                    "similarity": entry.get("similarity"),
                    "bm25_score": entry.get("bm25_score"),
                    "sources": entry.get("sources"),
                    "acceptance_threshold": entry_threshold,
                    "origin": entry.get("origin"),
                    "source_label": entry.get("source_label"),
                    "speaker": entry.get("speaker"),
                    "message_id": entry.get("message_id"),
                }
            )

        # 항상 로그 출력 (점수 포함)
        logger.info(
            "RAG 검색 결과 (threshold=%.2f, selected_sources=%s):\n%s",
            threshold,
            selected_source_counts or {},
            "\n".join(log_lines) if log_lines else "  (없음)",
            extra={
                **log_extra,
                "event": "rag_retrieval_completed",
                "candidate_count": len(result.entries),
                "selected_count": len(prepared_entries),
                "selected_source_counts": selected_source_counts,
            },
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

        logger.debug(
            "RAG 컨텍스트 구성 완료. entries=%d context_chars=%d",
            len(prepared_entries),
            len(context_str),
            extra=log_extra,
        )
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
    def _recent_search_messages_from_history(
        history: list[dict] | None,
    ) -> list[str]:
        """이미 읽은 최근 대화에서 RAG 검색 확장용 발화를 재사용한다."""
        previous_user: str | None = None
        previous_bot: str | None = None
        for item in reversed(history or []):
            if not isinstance(item, dict):
                continue
            parts = item.get("parts") or []
            content = parts[0] if isinstance(parts, list) and parts else ""
            content = str(content or "").strip()
            if not content:
                continue
            role = item.get("role")
            if (
                previous_user is None
                and role == "user"
                and item.get("is_current_user")
            ):
                previous_user = content
            elif previous_bot is None and role == "model":
                previous_bot = content
            if previous_user and previous_bot:
                break
        result: list[str] = []
        if previous_user:
            result.append(previous_user)
        if previous_bot:
            result.append(previous_bot)
        return result

    @staticmethod
    def _extract_json_block(text: str) -> str:
        """LLM 응답 텍스트에서 JSON 블록을 추출합니다."""
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
        """점수 값을 float 또는 None으로 정규화합니다."""
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
        """Thinking 모델 응답을 구조화된 dict로 파싱합니다."""
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
        """Flash/소형 모델을 사용해야 하는지 판단합니다."""
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

    def _guild_scoped_channel_config(self, guild_id: int) -> dict:
        """같은 서버에 이미 설정된 채널의 말투를 서버 기본값으로 물려줍니다.

        말투는 ``prompts.json``에 채널 단위로만 저장된다. 관리자가 ``!관리``로
        아직 등록하지 않은 채널의 응답을 켜면 그 채널만 전역 기본 말투로
        답해, 같은 서버 안에서 마사몽의 인격이 갈라졌다.

        설정 파일을 바꾸거나 새 말투를 만들지 않는다. 이미 그 서버에 설정된
        채널의 persona/rules를 그대로 재사용할 뿐이라 운영자가 지정한 값이
        무시되거나 사라지지 않는다. 조회 조건이 ``guild_id`` 일치이므로 다른
        서버의 말투는 어떤 경로로도 섞이지 않는다.
        """
        cached = self._guild_channel_config_cache.get(guild_id)
        if cached:
            return cached
        get_channel = getattr(self.bot, "get_channel", None)
        if not callable(get_channel):
            return {}
        for configured_id, meta in config.CHANNEL_AI_CONFIG.items():
            channel = get_channel(int(configured_id))
            channel_guild_id = getattr(getattr(channel, "guild", None), "id", None)
            if channel_guild_id is None:
                # 아직 Discord 캐시가 채워지지 않은 채널은 건너뛴다. 실패를
                # 캐시하면 기동 직후 조회가 영구히 기본값으로 굳는다.
                continue
            if int(channel_guild_id) == int(guild_id):
                self._guild_channel_config_cache[guild_id] = meta
                return meta
        return {}

    @staticmethod
    def _successful_tool_result(
        tool_results: list[dict[str, Any]],
        tool_name: str,
    ) -> dict[str, Any] | None:
        """지정 도구의 실제 성공 결과만 반환합니다."""
        for entry in tool_results:
            if entry.get("tool_name") != tool_name:
                continue
            result = entry.get("result")
            if not isinstance(result, dict) or result.get("error"):
                continue
            if tool_name == "web_search":
                if (
                    str(result.get("context") or result.get("result") or "").strip()
                    and bool(result.get("source_urls"))
                ):
                    return result
                continue
            if tool_name == "get_market_snapshot":
                if result.get("status") == "success" and result.get("indices"):
                    return result
                continue
            if result:
                return result
        return None

    @classmethod
    def _has_verified_external_evidence(
        cls,
        tool_results: list[dict[str, Any]],
    ) -> bool:
        return any(
            cls._successful_tool_result(tool_results, name) is not None
            for name in (
                "web_search",
                "get_market_snapshot",
                "get_stock_price",
                "get_weather_forecast",
                "search_for_place",
            )
        )

    @staticmethod
    def _final_reasoning_progress_text(reasoning_level: Any) -> str:
        """최종 모델의 실제 추론 수준에 맞춰 Discord 진행 문구를 고릅니다."""
        if str(reasoning_level or "").strip().lower() == "high":
            return (
                "🧠 마사몽이 여러 내용을 살펴보며 "
                "조금 더 오래 고민 중이에요..."
            )
        return "✍️ 수집한 정보를 바탕으로 답변을 작성 중이에요..."

    @staticmethod
    def _extract_significant_numbers(text: str) -> list[float]:
        """금융 답변의 지수·가격·비율처럼 검증할 가치가 큰 수치를 추출합니다."""
        values: list[float] = []
        pattern = re.compile(
            r"(?<![\w])([+-]?\d[\d,]*(?:\.\d+)?)"
            r"(\s*(?:%|포인트|p\b|원\b|달러|usd\b|krw\b|조\b|억\b))?",
            re.IGNORECASE,
        )
        for match in pattern.finditer(str(text or "")):
            raw = match.group(1)
            unit = str(match.group(2) or "").strip()
            try:
                value = float(raw.replace(",", ""))
            except ValueError:
                continue
            # 날짜의 월/일, 목록 번호, "2분기" 같은 작은 정수는 제외한다.
            if unit or "." in raw or "," in raw or abs(value) >= 100:
                values.append(value)
        return values

    @classmethod
    def _looks_like_hypothetical_calculation(cls, query_text: str) -> bool:
        """단가·기준을 사용자가 직접 제시한 가정 계산 요청인지 봅니다.

        "지금", "현재", "실시간"처럼 실제 시세를 가리키는 표현이 함께 있으면
        가정 계산으로 보지 않는다. 조회가 필요한 질문을 가정으로 오인하면
        지어낸 수치가 그대로 나가기 때문이다.
        """
        text = str(query_text or "").casefold()
        if not text:
            return False
        if re.search(r"(?:지금|현재|오늘|실시간|시세|주가)", text):
            return False
        premise = re.search(
            r"(?:라 ?치면|라고 ?치면|라 ?하면|라고 ?하면|이라면|기준으로|가정하[면고])",
            text,
        )
        asks_amount = re.search(r"(?:얼마|계산|총액|합계)", text)
        return bool(premise and asks_amount)

    @staticmethod
    def _split_message_chunks(text: str, chunk_size: int = 1900) -> list[str]:
        """Discord 메시지 제한보다 작은 단위로 텍스트를 나눕니다."""
        return split_message_chunks(text, chunk_size=chunk_size)

    async def _send_split_message(self, message: discord.Message, text: str):
        """
        2000자가 넘는 메시지를 안전하게 나누어 전송합니다.
        Discord의 메시지 길이 제한(2000자)을 준수합니다.
        """
        for chunk in self._split_message_chunks(text):
            await message.channel.send(
                chunk,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            # 순서 보장을 위한 짧은 텀
            await asyncio.sleep(0.5)

    async def _edit_status_with_split_response(
        self,
        status_msg: discord.Message,
        text: str,
        *,
        chunk_size: int = 1900,
    ) -> list[discord.Message]:
        """진행 상태 메시지를 최종 응답으로 바꾸되, 길면 후속 메시지로 나눠 보냅니다."""
        chunks = self._split_message_chunks(text, chunk_size=chunk_size)
        if not chunks:
            return []

        edited_status = await status_msg.edit(
            content=chunks[0],
            allowed_mentions=discord.AllowedMentions.none(),
        )
        sent_messages = [edited_status or status_msg]
        for chunk in chunks[1:]:
            sent_messages.append(
                await status_msg.channel.send(
                    chunk,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            )
            await asyncio.sleep(0.5)
        return sent_messages

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

    @staticmethod
    def _log_agent_execution_outcome(
        log_extra: dict[str, Any],
        *,
        started_at: float,
        outcome: str,
        stage: str,
        tool_count: int,
        error_kind: str | None = None,
    ) -> None:
        """한 AI 요청이 반드시 하나의 종료 레코드를 남기게 합니다."""
        duration_ms = round(
            max(0.0, time.monotonic() - float(started_at)) * 1000
        )
        terminal_extra = {
            **log_extra,
            "event": "agent_completed",
            "outcome": str(outcome or "unknown")[:64],
            "stage": str(stage or "unknown")[:64],
            "duration_ms": duration_ms,
            "tool_count": max(0, int(tool_count)),
        }
        if error_kind:
            terminal_extra["error_kind"] = str(error_kind)[:128]
        logger.info(
            "에이전트 처리 종료: outcome=%s stage=%s "
            "duration_ms=%d tool_count=%d error_kind=%s",
            terminal_extra["outcome"],
            terminal_extra["stage"],
            duration_ms,
            terminal_extra["tool_count"],
            terminal_extra.get("error_kind") or "none",
            extra=terminal_extra,
        )


    async def _fortune_context_with_consent(self, user_id: int) -> str | None:
        """현재 운세 동의가 유지된 DM 사용자에게만 저장 컨텍스트를 반환한다."""
        if not getattr(self.bot, "db", None):
            return None
        try:
            async with self.bot.db.execute(
                """
                SELECT up.last_fortune_content
                FROM user_profiles AS up
                JOIN privacy_consents AS pc
                  ON pc.user_id = up.user_id
                 AND pc.scope = ?
                 AND pc.policy_version = ?
                 AND pc.notice_hash = ?
                 AND pc.status = ?
                 AND pc.granted_at IS NOT NULL
                 AND pc.withdrawn_at IS NULL
                WHERE up.user_id = ?
                """,
                (
                    FORTUNE_CONSENT_POLICY.scope,
                    FORTUNE_CONSENT_POLICY.version,
                    FORTUNE_CONSENT_POLICY.notice_hash,
                    CONSENT_GRANTED,
                    int(user_id),
                ),
            ) as cursor:
                row = await cursor.fetchone()
        except Exception:
            # 동의 저장소 장애 때문에 일반 대화 자체를 막지는 않되, 개인정보
            # 컨텍스트는 절대 fail-open으로 주입하지 않는다.
            logger.error(
                "운세 개인정보 컨텍스트 조회 실패: user_id=%s",
                user_id,
                exc_info=True,
            )
            return None
        return str(row[0]) if row and row[0] else None

    async def process_agent_message(
        self,
        message: discord.Message,
        *,
        queue_request: _QueuedAIRequest | None = None,
    ):
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
        # 메인 Discord 경로는 이미 bounded FIFO에 들어온 요청이다. 여기서 처리
        # 시작 시각 기준 쿨다운을 다시 적용하면, 오래 기다린 서로 다른 질문도 앞
        # 요청 직후 worker가 꺼내는 순간 조용히 버려진다. 큐 경로는 모든 항목을
        # 한 번씩 처리하고, 반복 문장 방지와 기존 사용자/전역 provider quota로
        # burst 비용을 제한한다. 큐를 거치지 않는 내부 호환 경로만 기존 쿨다운을
        # 유지한다.
        if queue_request is None:
            last_request = self.ai_user_cooldowns.get(user_id)
            if last_request:
                elapsed = (now - last_request).total_seconds()
                if elapsed < config.USER_COOLDOWN_SECONDS:
                    remaining = config.USER_COOLDOWN_SECONDS - elapsed
                    logger.debug(
                        "사용자 %s 쿨다운 중 (%.1f초 남음)",
                        user_id,
                        remaining,
                        extra=base_log_extra,
                    )
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
        
        # 3. 사용자별/글로벌 일일 LLM 호출 제한 검사
        # 원격 TiDB 왕복을 줄이기 위해 두 카운터를 단일 GROUP BY SELECT로 읽는다.
        user_daily_key = f"llm_user_{user_id}"
        daily_counts = await db_utils.get_daily_api_counts(
            self.bot.db,
            (user_daily_key, "llm_global"),
        )
        user_daily_count = daily_counts.get(user_daily_key, 0)
        global_daily_count = daily_counts.get("llm_global", 0)
        if user_daily_count >= config.USER_DAILY_LLM_LIMIT:
            logger.warning(f"사용자 {user_id} 일일 LLM 제한 도달 ({user_daily_count}/{config.USER_DAILY_LLM_LIMIT})", extra=base_log_extra)
            await message.channel.send("오늘은 좀 많이 물어봤어요! 내일 다시 찾아와주세요 😅")
            return
        
        if global_daily_count >= config.GLOBAL_DAILY_LLM_LIMIT:
            logger.warning(f"글로벌 일일 LLM 제한 도달 ({global_daily_count}/{config.GLOBAL_DAILY_LLM_LIMIT})", extra=base_log_extra)
            await message.channel.send("오늘 할 수 있는 대화를 다 썼어요. 내일 다시 봬요 😢")
            return
        
        # 쿨다운 갱신
        self.ai_user_cooldowns[user_id] = now
        if len(self.ai_user_cooldowns) > self._USER_COOLDOWN_MAX_ENTRIES:
            oldest_users = sorted(
                self.ai_user_cooldowns,
                key=self.ai_user_cooldowns.__getitem__,
            )[
                : len(self.ai_user_cooldowns)
                - self._USER_COOLDOWN_MAX_ENTRIES
            ]
            for stale_user_id in oldest_users:
                self.ai_user_cooldowns.pop(stale_user_id, None)
        # ========== 안전장치 검사 완료 ==========
        
        user_query = self._prepare_user_query(message, base_log_extra)
        if not user_query:
            return

        # 5. DM 사용자·전역 제한을 한 트랜잭션으로 함께 예약한다.
        if not message.guild:
            allowed, reason, reset_time = await db_utils.reserve_dm_message(
                self.bot.db,
                user_id,
            )
            if not allowed:
                if reason == "user_limit":
                    await message.channel.send(
                        "⛔ 지금은 DM 대화 한도까지 왔어요.\n"
                        f"1:1 대화는 {DM_LIMIT_WINDOW_HOURS}시간에 "
                        f"{DM_LIMIT_COUNT}번까지예요.\n"
                        f"🕒 다시 쓸 수 있는 시각: {reset_time}"
                    )
                elif reason == "global_limit":
                    await message.channel.send(
                        "⛔ 오늘 DM으로 나눌 수 있는 대화를 다 썼어요.\n"
                        "내일 다시 찾아와주세요. 서버 채널에서는 그대로 이야기할 수 있어요."
                    )
                else:
                    await message.channel.send(
                        "남은 대화 횟수를 확인하지 못해서 아직 시작하지 않았어요. "
                        "잠시 뒤에 다시 불러주세요."
                    )
                return

        trace_id = uuid.uuid4().hex[:8]
        log_extra = dict(base_log_extra)
        log_extra['trace_id'] = trace_id
        request_started_at = time.monotonic()
        logger.info(
            "에이전트 처리 시작. query_chars=%d",
            len(user_query),
            extra={
                **log_extra,
                "event": "agent_started",
                "stage": "accepted",
            },
        )
        self._debug(f"--- 에이전트 세션 시작 trace_id={trace_id}", log_extra)

        # 초기 상태는 즉시 표시하고 이후 단계는 낮은 빈도로 합쳐 갱신한다.
        # Discord 기본 입력 중 애니메이션과 12초 heartbeat로 살아 있음을
        # 알리되, 단계가 빠르게 바뀔 때 edit 요청을 연속으로 보내지 않는다.
        initial_progress_text = "🤔 질문을 확인하고 있어요..."
        try:
            if queue_request is not None and queue_request.notice is not None:
                try:
                    status_msg = await queue_request.notice.edit(
                        content=initial_progress_text,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except (
                    discord.NotFound,
                    discord.Forbidden,
                    discord.HTTPException,
                ):
                    status_msg = await message.channel.send(
                        initial_progress_text,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            else:
                status_msg = await message.channel.send(
                    initial_progress_text,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            if queue_request is not None:
                queue_request.status_claimed = True
            progress = await DiscordProgress(
                status_msg,
                initial_text=initial_progress_text,
            ).start()
        except Exception as exc:
            error_kind = type(exc).__name__
            logger.warning(
                "AI 초기 상태 메시지 전송 실패: error_kind=%s",
                error_kind,
                exc_info=True,
                extra={
                    **log_extra,
                    "event": "discord_delivery_failed",
                    "outcome": "failed",
                    "stage": "initial_status",
                    "error_kind": error_kind,
                },
            )
            self._log_agent_execution_outcome(
                log_extra,
                started_at=request_started_at,
                outcome="failed",
                stage="initial_status",
                tool_count=0,
                error_kind=error_kind,
            )
            return

        terminal_outcome = "failed"
        terminal_stage = "routing"
        terminal_error_kind: str | None = None
        executed_tool_count = 0

        try:
            # 1단계: 분석 및 도구 계획 수립
            await progress.update(
                "🔎 질문 의도를 파악하고 필요한 자료를 검토 중이에요..."
            )
            
            # [NEW] 지역명 캐시 로드 (필요 시)
            await self._load_location_cache()

            guild_id_safe = message.guild.id if message.guild else 0

            # Discord REST history를 한 번만 읽고 도구 라우팅·후속 검색·최종
            # 프롬프트에 함께 사용한다. 이전에는 검색 확장과 답변 맥락이 각각
            # history()를 호출해 같은 네트워크 왕복을 중복했다.
            terminal_stage = "context"
            history = await self._get_recent_history(message, "")

            # 정상 경로는 routing lane이 자연어 의미와 대화 흐름을 읽는다.
            # 키워드 목록은 provider 장애 시의 제한된 비상 fallback에만 사용한다.
            terminal_stage = "routing"
            routing_decision = await self._route_tools(
                user_query,
                log_extra,
                history=history,
                conversation_scope=(
                    str(message.guild.name)
                    if message.guild and getattr(message.guild, "name", None)
                    else ""
                ),
            )
            logger.info(
                "에이전트 라우팅 완료: source=%s tools=%d "
                "needs_memory=%s shared_history=%s reasoning=%s",
                routing_decision.source,
                len(routing_decision.plan or []),
                bool(routing_decision.needs_memory),
                bool(
                    getattr(
                        routing_decision,
                        "references_shared_history",
                        False,
                    )
                ),
                routing_decision.reasoning_level or "default",
                extra={
                    **log_extra,
                    "event": "agent_routing_completed",
                    "outcome": "succeeded",
                    "stage": "routing",
                    "route_source": routing_decision.source,
                    "tool_count": len(routing_decision.plan or []),
                    "needs_memory": bool(routing_decision.needs_memory),
                    "shared_history_ref": bool(
                        getattr(
                            routing_decision,
                            "references_shared_history",
                            False,
                        )
                    ),
                    "reasoning_level": (
                        routing_decision.reasoning_level or "default"
                    ),
                },
            )

            rag_prompt = ""
            rag_entries: list[dict[str, Any]] = []
            rag_top_score = 0.0
            rag_blocks: list[str] = []
            if self._should_search_memory(routing_decision):
                terminal_stage = "memory"
                recent_search_messages = self._recent_search_messages_from_history(
                    history
                )
                (
                    rag_prompt,
                    rag_entries,
                    rag_top_score,
                    rag_blocks,
                ) = await self._get_rag_context(
                    guild_id_safe,
                    message.channel.id,
                    message.author.id,
                    user_query,
                    recent_messages=recent_search_messages,
                    deep_search=bool(routing_decision.needs_memory),
                )

            history = self._select_final_history(
                history,
                routing_decision,
            )

            raw_tool_plan = routing_decision.plan
            llm_decision_trusted = routing_decision.source == "llm"
            tool_plan = self._sanitize_tool_plan(
                user_query,
                raw_tool_plan,
                rag_top_score=rag_top_score,
                log_extra=log_extra,
                trust_llm=llm_decision_trusted,
            )
            
            tool_results: list[dict[str, Any]] = []
            executed_plan: list[dict[str, Any]] = []

            if rag_blocks:
                tool_results.append({
                    "step": 0,
                    "tool_name": "local_rag",
                    "parameters": {"top_score": rag_top_score},
                    "result": {"entries": rag_entries},
                })

            if tool_plan:
                terminal_stage = "tools"
                step_label = f"{len(tool_plan)}단계" if len(tool_plan) > 1 else ""
                tool_names_kr = {
                    "web_search": "웹 검색",
                    "get_weather_forecast": "날씨 조회",
                    "get_market_snapshot": "시장 지수 확인",
                    "get_stock_price": "주가 조회",
                    "search_for_place": "장소 검색",
                    "generate_image": "이미지 생성",
                }
                first_tool = tool_plan[0].get('tool_to_use', '')
                first_label = tool_names_kr.get(first_tool, first_tool)
                await progress.update(
                    f"🔍 {first_label} 정보를 가져오는 중이에요... {step_label}"
                )
                logger.info(f"2단계: 도구 실행 시작. 총 {len(tool_plan)}단계.", extra=log_extra)
                
                for idx, tool_call in enumerate(tool_plan, start=1):
                    tool_name = tool_call.get('tool_to_use')
                    if tool_name == "web_search":
                        parameters = tool_call.setdefault("parameters", {})
                        parameters["query"] = self._contextualize_web_query(
                            parameters.get("query") or user_query,
                            user_query,
                            history,
                        )
                    tool_label = tool_names_kr.get(tool_name, tool_name)
                    step_progress = (
                        f"({idx}/{len(tool_plan)})"
                        if len(tool_plan) > 1
                        else ""
                    )
                    image_lock = getattr(
                        self.tools_cog,
                        "_image_generation_lock",
                        None,
                    )
                    if (
                        tool_name == "generate_image"
                        and image_lock is not None
                        and image_lock.locked()
                    ):
                        await progress.update(
                            "🎨 앞선 그림이 끝나길 기다리는 중이에요... "
                            f"{step_progress}"
                        )
                    else:
                        await progress.update(
                            f"🔍 {tool_label} 진행 중이에요... {step_progress}"
                        )

                    tool_started_at = time.monotonic()
                    result = await self._execute_tool(
                        tool_call,
                        guild_id_safe,
                        user_query,
                        channel_id=message.channel.id,
                        user_id=message.author.id,
                        rag_context=rag_prompt,
                    )
                    self._log_tool_execution_outcome(
                        tool_name,
                        result,
                        log_extra,
                        duration_ms=round(
                            max(0.0, time.monotonic() - tool_started_at)
                            * 1000
                        ),
                        step=idx,
                        step_count=len(tool_plan),
                    )

                    tool_results.append({
                        "step": idx,
                        "tool_name": tool_name,
                        "parameters": tool_call.get('parameters'),
                        "result": result,
                    })
                    executed_plan.append(tool_call)
                    executed_tool_count += 1

            # 현재가 도구가 정확한 티커를 찾지 못했거나 공급자 장애로 근거를
            # 만들지 못한 경우, 같은 요청 안에서 공개 웹 검색을 딱 한 번만
            # 사용한다. 이 경로는 재귀하지 않으며 메시지당 도구 호출 상한에도
            # 포함된다.
            stock_failure = next(
                (
                    entry
                    for entry in tool_results
                    if entry.get("tool_name") == "get_stock_price"
                    and isinstance(entry.get("result"), dict)
                    and entry["result"].get("error")
                ),
                None,
            )
            has_web_result = any(
                entry.get("tool_name") == "web_search"
                for entry in tool_results
            )
            max_tool_calls = min(
                3,
                max(1, int(getattr(config, "AGENT_MAX_TOOL_CALLS", 3))),
            )
            if (
                stock_failure
                and not has_web_result
                and bool(routing_decision.requires_external_evidence)
                and executed_tool_count < max_tool_calls
            ):
                await progress.update(
                    "🌐 정확한 종목과 공개 시세 자료를 한 번 더 확인 중이에요..."
                )
                contextual_query = self._contextualize_web_query(
                    user_query,
                    user_query,
                    history,
                )
                fallback_query = (
                    self._ensure_intent_analyzer()._build_finance_lookup_query(
                        contextual_query
                    )
                )
                tool_started_at = time.monotonic()
                web_result = await self._execute_web_search_raw(
                    fallback_query,
                    log_extra,
                    depth_hint="fast",
                )
                fallback_step = executed_tool_count + 1
                self._log_tool_execution_outcome(
                    "web_search",
                    web_result,
                    log_extra,
                    duration_ms=round(
                        max(0.0, time.monotonic() - tool_started_at) * 1000
                    ),
                    step=fallback_step,
                    step_count=fallback_step,
                )
                fallback_call = {
                    "tool_to_use": "web_search",
                    "tool_name": "web_search",
                    "parameters": {
                        "query": fallback_query,
                        "depth": "fast",
                    },
                    "auto": "stock_fallback",
                }
                tool_results.append(
                    {
                        "step": fallback_step,
                        "tool_name": "web_search",
                        "parameters": fallback_call["parameters"],
                        "result": web_result,
                    }
                )
                executed_plan.append(fallback_call)
                executed_tool_count += 1
                logger.info(
                    "주가 도구 실패 후 bounded web_search 폴백 완료. "
                    "failure_kind=%s",
                    stock_failure["result"].get("failure_kind"),
                    extra=log_extra,
                )

            # 의미 라우터가 정상적으로 "도구 없음"을 결정했다면 키워드 규칙으로
            # 뒤집지 않는다. provider 장애 fallback에서만 기존 자동 검색을
            # 최후 수단으로 사용한다.
            if (
                not tool_plan
                and not llm_decision_trusted
                and await self._should_use_web_search(
                    user_query,
                    rag_top_score,
                    history=history,
                )
            ):
                if self._can_run_auto_web_search(message, user_query, log_extra):
                    await progress.update(
                        "🌐 웹에서 최신 정보를 검색하고 요약 중이에요..."
                    )

                    # 같은 사용자의 짧은 후속 질문을 무과금 규칙으로 먼저 보강합니다.
                    refined_query = self._contextualize_web_query(
                        user_query,
                        user_query,
                        history,
                    )
                    if history and getattr(config, "WEB_SEARCH_REFINE_WITH_LLM", False):
                        refined_query = await self._refine_search_query_with_llm(
                            refined_query,
                            history,
                            log_extra,
                        )
                        logger.info(
                            "자동 웹검색 쿼리 정제 완료. before_chars=%d after_chars=%d",
                            len(user_query),
                            len(refined_query),
                            extra=log_extra,
                        )

                    tool_started_at = time.monotonic()
                    web_result = await self._execute_web_search_raw(
                        refined_query,
                        log_extra,
                    )
                    self._log_tool_execution_outcome(
                        "web_search",
                        web_result,
                        log_extra,
                        duration_ms=round(
                            max(0.0, time.monotonic() - tool_started_at)
                            * 1000
                        ),
                        step=1,
                        step_count=1,
                    )
                    self._mark_auto_web_search_used(message)
                    tool_results.append(
                        {
                            "step": 1,
                            "tool_name": "web_search",
                            "parameters": {"query": refined_query},
                            "result": web_result,
                        }
                    )
                    executed_plan.append(
                        {
                            "tool_to_use": "web_search",
                            "parameters": {"query": refined_query},
                            "auto": True,
                        }
                    )
                    executed_tool_count += 1

            # 이미지 생성 단독 요청은 이미 provider가 최종 결과를 만들었으므로
            # 답변용 LLM을 한 번 더 호출하지 않는다. 추가 호출은 이미지 내용과
            # 무관한 문장을 만들고 전송을 수십 초 늦출 뿐이다.
            non_local_tool_results = [
                res for res in tool_results
                if res.get("tool_name") != "local_rag"
            ]
            intent_analyzer = self._ensure_intent_analyzer()
            semantic_request = (
                f"{user_query}\n{routing_decision.intent or ''}"
            ).strip()
            finance_request = intent_analyzer._looks_like_finance_query(
                semantic_request
            )
            requires_external_evidence = bool(
                getattr(
                    routing_decision,
                    "requires_external_evidence",
                    False,
                )
            )
            market_snapshot_result = self._successful_tool_result(
                non_local_tool_results,
                "get_market_snapshot",
            )
            stock_quote_result = self._successful_tool_result(
                non_local_tool_results,
                "get_stock_price",
            )
            guarded_response = ""
            terminal_stage = "answer"
            if (
                requires_external_evidence
                and not self._has_verified_external_evidence(
                    non_local_tool_results
                )
            ):
                guarded_response = (
                    self._format_market_snapshot_fallback(
                        None,
                        note="",
                    )
                    if finance_request
                    # 페르소나를 거치지 않는 공통 고정 문구라 말투를 특정 채널에
                    # 맞출 수 없다. 다만 "모르는 걸 지어내긴 싫어서 여기까지만
                    # 할게요"는 사용자가 요구하지도 않은 원칙을 선언하는 훈계조라
                    # 거부처럼 읽혔다. 못 찾았다는 사실만 한 문장으로 남긴다.
                    else "이건 자료를 확인해야 하는데 지금 못 찾았어요. 잠시 뒤에 다시 물어봐 주세요."
                )
                logger.warning(
                    "검증 필수 요청의 외부 자료가 없어 답변 생성을 fail-closed 처리합니다.",
                    extra=log_extra,
                )
            if (
                len(non_local_tool_results) == 1
                and non_local_tool_results[0].get("tool_name")
                == "generate_image"
                and isinstance(
                    non_local_tool_results[0].get("result"),
                    dict,
                )
            ):
                image_payload = non_local_tool_results[0]["result"]
                terminal_stage = "delivery"
                image_response_messages: list[discord.Message] = []
                final_image_response = (
                    await self._deliver_single_image_result(
                        message=message,
                        status_msg=status_msg,
                        progress=progress,
                        image_payload=image_payload,
                        log_extra=log_extra,
                        delivered_messages=image_response_messages,
                    )
                )
                image_response_message_ids = (
                    await self._record_delivered_response_messages(
                        image_response_messages,
                        log_extra,
                    )
                )

                terminal_stage = "analytics"
                await db_utils.log_api_call(
                    self.bot.db,
                    f"llm_user_{message.author.id}",
                )
                await db_utils.log_api_call(
                    self.bot.db,
                    "llm_global",
                )
                await db_utils.log_analytics(
                    self.bot.db,
                    "AI_INTERACTION",
                    self._build_interaction_analytics(
                        message=message,
                        trace_id=trace_id,
                        user_query=user_query,
                        final_response=final_image_response,
                        tool_plan=executed_plan or tool_plan,
                        response_message_ids=image_response_message_ids,
                    ),
                )
                terminal_outcome = "succeeded"
                terminal_stage = "completed"
                return

            # 답변 작성 단계
            await progress.update(
                self._final_reasoning_progress_text(
                    getattr(routing_decision, "reasoning_level", None)
                )
            )

            # 도구 결과에서 출처 URL 추출
            source_urls_to_cache = []
            for res in tool_results:
                if res.get("tool_name") in {
                    "web_search",
                    "get_market_snapshot",
                    "get_stock_price",
                } and isinstance(res.get("result"), dict):
                    urls = res["result"].get("source_urls") or res["result"].get("urls")
                    if urls:
                        source_urls_to_cache.extend(urls)

            # 도구 결과 포맷팅 및 프롬프트 구성
            tool_results_str = self._format_tool_results_for_prompt(tool_results)
            system_prompt = self._compose_main_system_prompt(
                message,
                user_query=user_query,
            )
            
            # [NEW] 운세 컨텍스트 조회
            fortune_context = None
            if (
                not message.guild
                and self.bot.db
                and bool(
                    getattr(
                        routing_decision,
                        "needs_fortune_context",
                        False,
                    )
                )
            ):
                fortune_context = await self._fortune_context_with_consent(
                    message.author.id
                )

            main_prompt = self._compose_main_prompt(
                message,
                user_query=user_query,
                rag_blocks=rag_blocks,
                tool_results_block=tool_results_str if tool_results_str else None,
                fortune_context=fortune_context,
                recent_history=history,
                context_digest=routing_decision.context_digest,
            )

            # 답변 생성. 도구 성공 결과는 메인 모델이 채널 말투·최근 대화·
            # 기억과 함께 대화로 녹인다. 검증 자료가 아예 없을 때만 고정
            # 문구로 LLM을 건너뛴다.
            final_response_text = guarded_response
            web_only_summary = ""
            if (
                len(non_local_tool_results) == 1
                and non_local_tool_results[0].get("tool_name") == "web_search"
                and isinstance(non_local_tool_results[0].get("result"), dict)
                and non_local_tool_results[0]["result"].get("summary")
            ):
                web_only_summary = str(non_local_tool_results[0]["result"]["summary"]).strip()

            if final_response_text:
                logger.info(
                    "검증 안전 응답을 사용해 최종 답변 LLM 호출을 생략합니다.",
                    extra=log_extra,
                )
            else:
                if self.use_cometapi:
                    final_response_text = await self._cometapi_generate_content(
                        system_prompt,
                        main_prompt,
                        log_extra,
                        stop_on_bounded_failure=True,
                        reasoning_effort_override=(
                            routing_decision.reasoning_level or None
                        ),
                    ) or ""

                if not final_response_text and self._can_use_direct_gemini():
                    main_model = genai.GenerativeModel(config.AI_RESPONSE_MODEL_NAME, system_instruction=system_prompt)
                    main_response = await self._safe_generate_content(main_model, main_prompt, log_extra)
                    if main_response:
                        final_response_text = main_response.text.strip()

                if not final_response_text and web_only_summary:
                    final_response_text = web_only_summary
                    logger.info(
                        "메인 답변 생성 실패로 웹 검색 요약을 폴백합니다.",
                        extra=log_extra,
                    )

            if final_response_text:
                # 멘션 제거 및 후처리
                final_response_text = re.sub(r'^@마사몽\s*|^@masamong\s*|^<@!?[0-9]+>\s*', '', final_response_text, flags=re.IGNORECASE)
                final_response_text = normalize_discord_text(final_response_text)
                final_response_text = self._replace_unexecuted_lookup_promise(
                    final_response_text,
                    has_external_evidence=self._has_verified_external_evidence(
                        non_local_tool_results
                    ),
                    creative_response=(
                        self._ensure_intent_analyzer()._looks_like_creative_query(
                            getattr(routing_decision, "intent", "")
                        )
                    ),
                )

                if finance_request and not guarded_response:
                    evidence_text = self._format_tool_results_for_prompt(
                        non_local_tool_results
                    )
                    unsupported_numbers = self._unsupported_finance_numbers(
                        final_response_text,
                        evidence_text,
                        user_query,
                    )
                    if unsupported_numbers:
                        logger.error(
                            "금융 답변의 근거 없는 수치를 차단합니다. count=%d",
                            len(unsupported_numbers),
                            extra=log_extra,
                        )
                        final_response_text = self._format_verified_quote_fallback(
                            stock_quote_result,
                            market_snapshot_result,
                            query=user_query,
                            note=(
                                "뉴스 요약에서 원자료로 확인되지 않는 수치가 감지되어 "
                                "해당 내용은 제외했어요."
                            ),
                        )
                
                # 이미지 생성 결과가 있으면 Discord 파일로 전송
                image_result = next((res for res in tool_results if res.get("tool_name") == "generate_image"), None)
                if image_result and isinstance(image_result.get("result"), dict):
                    img_data = image_result["result"].get("image_data")
                    img_url = image_result["result"].get("image_url")
                    if img_data:
                        try:
                            terminal_stage = "delivery"
                            await progress.stop()
                            extension = {
                                "image/png": "png",
                                "image/webp": "webp",
                                "image/jpeg": "jpg",
                            }.get(
                                str(
                                    image_result["result"].get("mime_type") or ""
                                ).casefold(),
                                "png",
                            )
                            image_file = discord.File(
                                io.BytesIO(img_data),
                                filename=f"generated.{extension}",
                            )
                            chunks = self._split_message_chunks(
                                final_response_text
                            ) or ["이미지를 생성했습니다."]
                            image_messages = [
                                await message.channel.send(
                                    content=chunks[0],
                                    file=image_file,
                                    allowed_mentions=discord.AllowedMentions.none(),
                                )
                            ]
                            for chunk in chunks[1:]:
                                image_messages.append(
                                    await message.channel.send(
                                        chunk,
                                        allowed_mentions=discord.AllowedMentions.none(),
                                    )
                                )
                            image_response_message_ids = (
                                await self._record_delivered_response_messages(
                                    image_messages,
                                    log_extra,
                                )
                            )
                            try:
                                await status_msg.delete()
                            except (
                                discord.NotFound,
                                discord.Forbidden,
                                discord.HTTPException,
                            ):
                                logger.debug(
                                    "AI 상태 메시지를 삭제하지 못했습니다.",
                                    exc_info=True,
                                    extra=log_extra,
                                )
                            # 분석 데이터 로깅 (순차 실행: 단일 커넥션 공유)
                            terminal_stage = "analytics"
                            await db_utils.log_api_call(self.bot.db, f"llm_user_{message.author.id}")
                            await db_utils.log_api_call(self.bot.db, "llm_global")
                            await db_utils.log_analytics(
                                self.bot.db,
                                "AI_INTERACTION",
                                self._build_interaction_analytics(
                                    message=message,
                                    trace_id=trace_id,
                                    user_query=user_query,
                                    final_response=final_response_text,
                                    tool_plan=executed_plan or tool_plan,
                                    response_message_ids=(
                                        image_response_message_ids
                                    ),
                                ),
                            )
                            terminal_outcome = "succeeded"
                            terminal_stage = "completed"
                            return
                        except Exception as img_exc:
                            logger.error(f"이미지 전송 실패: {img_exc}", extra=log_extra)
                    elif img_url:
                        final_response_text += f"\n\n🖼️ {img_url}"
                
                # [Progress Update] 최종 답변으로 편집. 웹 출처는 본문에 항상
                # 노출하지 않고 봇이 단 📰 반응을 사용자가 눌렀을 때만 표시한다.
                terminal_stage = "delivery"
                await progress.stop()
                response_messages = await self._edit_status_with_split_response(
                    status_msg,
                    final_response_text,
                    # 출처 footer가 같은 메시지에 들어갈 여유를 확보한다.
                    chunk_size=1_400 if source_urls_to_cache else 1_900,
                )
                response_message_ids = (
                    await self._record_delivered_response_messages(
                        response_messages,
                        log_extra,
                    )
                )
                if source_urls_to_cache:
                    await self._register_news_source_reaction(
                        response_messages,
                        source_urls_to_cache,
                    )
                
                # 분석 데이터 로깅 (순차 실행: 단일 커넥션 공유)
                terminal_stage = "analytics"
                await db_utils.log_api_call(self.bot.db, f"llm_user_{message.author.id}")
                await db_utils.log_api_call(self.bot.db, "llm_global")
                await db_utils.log_analytics(
                    self.bot.db,
                    "AI_INTERACTION",
                    self._build_interaction_analytics(
                        message=message,
                        trace_id=trace_id,
                        user_query=user_query,
                        final_response=final_response_text,
                        tool_plan=executed_plan or tool_plan,
                        response_message_ids=response_message_ids,
                    ),
                )
                terminal_outcome = "succeeded"
                terminal_stage = "completed"
            else:
                terminal_outcome = "empty_response"
                terminal_stage = "delivery"
                await progress.stop()
                await status_msg.edit(content="미안해요, 답을 만들다 막혔어요. 잠시 뒤에 다시 물어봐 주세요 😢")

        except Exception as e:
            terminal_outcome = "failed"
            terminal_error_kind = type(e).__name__
            logger.error(f"에이전트 처리 중 최상위 오류: {e}", exc_info=True, extra=log_extra)
            await progress.stop()
            try:
                await status_msg.edit(content=config.MSG_AI_ERROR)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                try:
                    await message.channel.send(config.MSG_AI_ERROR)
                except (
                    discord.NotFound,
                    discord.Forbidden,
                    discord.HTTPException,
                ):
                    logger.warning(
                        "AI 오류 안내 메시지 전송 실패",
                        exc_info=True,
                        extra=log_extra,
                    )
        finally:
            await progress.stop()
            self._log_agent_execution_outcome(
                log_extra,
                started_at=request_started_at,
                outcome=terminal_outcome,
                stage=terminal_stage,
                tool_count=executed_tool_count,
                error_kind=terminal_error_kind,
            )
            self._debug(f"--- 에이전트 세션 종료 trace_id={trace_id}", log_extra)
    async def _get_recent_history(self, message: discord.Message, rag_prompt: str) -> list:
        """모델에 전달할 최근 대화 기록을 채널에서 가져옵니다."""
        history_limit = (
            config.HISTORY_LIMIT_WITH_RAG
            if rag_prompt
            else max(
                config.HISTORY_LIMIT_WITHOUT_RAG,
                int(
                    getattr(
                        config,
                        "AI_CONTEXT_SOURCE_HISTORY_LIMIT",
                        24,
                    )
                ),
            )
        )
        history = []
        
        # FIFO에서 잠시 기다린 요청도 "그 메시지가 작성된 시점 이전"의 문맥만
        # 읽는다. 처리 대기 중 뒤에 올라온 메시지를 과거 맥락처럼 섞지 않는다.
        async for msg in message.channel.history(
            limit=history_limit,
            before=message,
        ):
            role = 'model' if msg.author.id == self.bot.user.id else 'user'
            content = msg.content[:config.MAX_MESSAGE_CHARS]
            
            # [NEW] 이전 답변에서 뉴스 출처 안내 문구 제거 (모델이 따라하는 것 방지)
            if role == 'model' and self.NEWS_SOURCE_FOOTER.strip() in content:
                content = content.replace(self.NEWS_SOURCE_FOOTER, "").replace(self.NEWS_SOURCE_FOOTER.strip(), "").strip()

            if not content:
                continue
            speaker = (
                "Masamong"
                if role == "model"
                else self._clip_prompt_text(
                    str(getattr(msg.author, "display_name", "unknown")),
                    80,
                    keep="head",
                )
            )
            history.append(
                {
                    'role': role,
                    'parts': [content],
                    'speaker': speaker,
                    'is_current_user': (
                        role == "user" and msg.author.id == message.author.id
                    ),
                }
            )

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
                if (
                    len(self.proactive_cooldowns)
                    > self._PROACTIVE_COOLDOWN_MAX_ENTRIES
                ):
                    oldest_channels = sorted(
                        self.proactive_cooldowns,
                        key=self.proactive_cooldowns.__getitem__,
                    )[
                        : len(self.proactive_cooldowns)
                        - self._PROACTIVE_COOLDOWN_MAX_ENTRIES
                    ]
                    for stale_channel_id in oldest_channels:
                        self.proactive_cooldowns.pop(stale_channel_id, None)
                return True
        except Exception as e:
            logger.error(f"게이트키퍼 AI 실행 중 오류: {e}", exc_info=True, extra=log_extra)

        return False

    @staticmethod
    def _normalize_summary_text(text: str) -> str:
        """요약 입력용 텍스트의 공백/개행을 정규화합니다."""
        return re.sub(r"\s+", " ", (text or "")).strip()

    @staticmethod
    def _truncate_summary_text(text: str, limit: int) -> str:
        """문자 수 제한을 넘는 요약 입력 라인을 안전하게 자릅니다."""
        if len(text) <= limit:
            return text
        if limit <= 3:
            return text[:limit]
        return text[: limit - 3].rstrip() + "..."

    @staticmethod
    def _sample_evenly(items: list[dict[str, Any]], target: int) -> list[dict[str, Any]]:
        """리스트 전체 구간을 고르게 대표하는 항목 샘플을 선택합니다."""
        if target <= 0 or not items:
            return []
        if len(items) <= target:
            return items
        if target == 1:
            return [items[-1]]

        total = len(items)
        step = (total - 1) / float(target - 1)
        indices: list[int] = []
        for i in range(target):
            idx = int(round(i * step))
            if indices and idx <= indices[-1]:
                idx = min(indices[-1] + 1, total - 1)
            indices.append(idx)
        return [items[idx] for idx in indices]

    def _merge_rows_to_turns(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """연속 발화자를 하나의 turn으로 병합해 요약 입력 토큰을 줄입니다."""
        turns: list[dict[str, Any]] = []
        for row in rows:
            content = self._normalize_summary_text(row.get("content", ""))
            if not content:
                continue

            speaker = str(row.get("user_name") or "Unknown")
            user_id_raw = row.get("user_id")
            user_id: int | None
            try:
                user_id = int(user_id_raw) if user_id_raw is not None else None
            except (TypeError, ValueError):
                user_id = None
            created_at = str(row.get("created_at") or "")
            is_bot = bool(row.get("is_bot"))
            speaker_key = f"user:{user_id}" if user_id is not None else f"name:{speaker.lower()}"

            if turns and turns[-1]["speaker_key"] == speaker_key:
                turns[-1]["content"] = f"{turns[-1]['content']} {content}".strip()
                turns[-1]["is_bot"] = turns[-1]["is_bot"] or is_bot
            else:
                turns.append(
                    {
                        "speaker": speaker,
                        "speaker_key": speaker_key,
                        "user_id": user_id,
                        "content": content,
                        "created_at": created_at,
                        "is_bot": is_bot,
                    }
                )
        return turns

    @staticmethod
    def _build_speaker_disambiguation(turns: list[dict[str, Any]]) -> dict[str, set[str]]:
        """동일 닉네임이 여러 사용자에 매핑되는지 계산합니다."""
        buckets: dict[str, set[str]] = {}
        for turn in turns:
            if turn.get("is_bot"):
                continue
            name = str(turn.get("speaker") or "Unknown").strip() or "Unknown"
            key = str(turn.get("speaker_key") or name.lower())
            buckets.setdefault(name, set()).add(key)
        return buckets

    @staticmethod
    def _resolve_speaker_label(turn: dict[str, Any], disambiguation: dict[str, set[str]]) -> str:
        """요약 표시용 화자 라벨을 생성합니다."""
        if turn.get("is_bot"):
            return "마사몽"

        base_name = str(turn.get("speaker") or "Unknown").strip() or "Unknown"
        keys = disambiguation.get(base_name, set())
        if len(keys) <= 1:
            return base_name

        user_id = turn.get("user_id")
        if user_id is None:
            return f"{base_name}(구분필요)"
        return f"{base_name}({str(user_id)[-4:]})"

    def _build_summary_context_from_turns(self, turns: list[dict[str, Any]]) -> str:
        """긴 대화를 압축해 [이전 맥락]+[최신 대화] 형태의 입력으로 변환합니다."""
        if not turns:
            return ""

        recent_turn_count = max(1, int(getattr(config, "SUMMARY_RECENT_TURNS", 12)))
        older_turn_count = max(0, int(getattr(config, "SUMMARY_OLDER_TURNS", 8)))
        recent_line_chars = max(40, int(getattr(config, "SUMMARY_RECENT_LINE_CHARS", 180)))
        older_line_chars = max(30, int(getattr(config, "SUMMARY_OLDER_LINE_CHARS", 90)))
        max_chars = max(800, int(getattr(config, "SUMMARY_MAX_CONTEXT_CHARS", 3200)))

        recent_turns = turns[-recent_turn_count:]
        older_turns = turns[:-recent_turn_count]
        older_samples = self._sample_evenly(older_turns, older_turn_count)
        speaker_disambiguation = self._build_speaker_disambiguation(turns)

        def _format_line(turn: dict[str, Any], *, limit: int) -> str:
            speaker = self._resolve_speaker_label(turn, speaker_disambiguation)
            content = self._truncate_summary_text(str(turn.get("content") or ""), limit)
            return f"- {speaker}: {content}"

        older_lines = [_format_line(turn, limit=older_line_chars) for turn in older_samples]
        recent_lines = [_format_line(turn, limit=recent_line_chars) for turn in recent_turns]

        def _render() -> str:
            sections: list[str] = []
            if older_lines:
                sections.append("[이전 맥락(압축)]\n" + "\n".join(older_lines))
            if recent_lines:
                sections.append("[최신 대화]\n" + "\n".join(recent_lines))
            return "\n\n".join(sections)

        context_text = _render()
        while len(context_text) > max_chars and older_lines:
            older_lines.pop(0)
            context_text = _render()

        while len(context_text) > max_chars and len(recent_lines) > 4:
            recent_lines.pop(0)
            context_text = _render()

        if len(context_text) > max_chars:
            context_text = self._truncate_summary_text(context_text, max_chars)

        return context_text

    async def get_recent_conversation_text(
        self,
        guild_id: int,
        channel_id: int,
        look_back: int = 20,
        *,
        max_chars: int | None = None,
        include_bot: bool = True,
        after_message_id: int | None = None,
    ) -> str:
        """요약 기능용 최근 대화를 읽어 압축된 컨텍스트 문자열로 반환합니다."""
        if not self.bot.db:
            return ""

        look_back = max(1, look_back)
        effective_max_chars = max_chars if max_chars is not None else getattr(config, "SUMMARY_MAX_CONTEXT_CHARS", 3200)

        query_parts = [
            "SELECT message_id, user_id, user_name, content, is_bot, created_at",
            "FROM conversation_history",
            "WHERE guild_id = ? AND channel_id = ?",
        ]
        params: list[int] = [int(guild_id), int(channel_id)]
        if after_message_id is not None:
            query_parts.append("AND message_id > ?")
            params.append(int(after_message_id))
        query_parts.append("ORDER BY created_at DESC, message_id DESC LIMIT ?")
        params.append(int(look_back))
        query = " ".join(query_parts)

        try:
            async with self.bot.db.execute(query, tuple(params)) as cursor:
                rows = await cursor.fetchall()
            if not rows:
                return ""

            rows.reverse()
            materialized_rows = [dict(row) for row in rows]
            if not include_bot:
                materialized_rows = [row for row in materialized_rows if not bool(row.get("is_bot"))]
            if not materialized_rows:
                return ""

            turns = self._merge_rows_to_turns(materialized_rows)
            context_text = self._build_summary_context_from_turns(turns)
            return self._truncate_summary_text(context_text, max(800, int(effective_max_chars)))
        except Exception as e:
            logger.error(f"최근 대화 기록 조회 중 DB 오류: {e}", exc_info=True)
            return ""

    async def get_latest_conversation_message_id(self, guild_id: int, channel_id: int) -> int | None:
        """채널의 최신 message_id를 반환합니다."""
        if not self.bot.db:
            return None
        query = (
            "SELECT message_id FROM conversation_history "
            "WHERE guild_id = ? AND channel_id = ? "
            "ORDER BY created_at DESC, message_id DESC LIMIT 1"
        )
        try:
            async with self.bot.db.execute(query, (int(guild_id), int(channel_id))) as cursor:
                row = await cursor.fetchone()
            if not row:
                return None
            value = row["message_id"] if isinstance(row, aiosqlite.Row) else row[0]
            return int(value)
        except Exception as e:
            logger.error(f"최신 메시지 ID 조회 중 DB 오류: {e}", exc_info=True)
            return None

    async def count_recent_conversation_messages(
        self,
        guild_id: int,
        channel_id: int,
        *,
        after_message_id: int | None = None,
        include_bot: bool = True,
    ) -> int:
        """요약 기준 범위 내 메시지 개수를 반환합니다."""
        if not self.bot.db:
            return 0

        query_parts = [
            "SELECT COUNT(1) AS cnt FROM conversation_history",
            "WHERE guild_id = ? AND channel_id = ?",
        ]
        params: list[int] = [int(guild_id), int(channel_id)]
        if not include_bot:
            query_parts.append("AND is_bot = 0")
        if after_message_id is not None:
            query_parts.append("AND message_id > ?")
            params.append(int(after_message_id))
        query = " ".join(query_parts)

        try:
            async with self.bot.db.execute(query, tuple(params)) as cursor:
                row = await cursor.fetchone()
            if not row:
                return 0
            value = row["cnt"] if isinstance(row, aiosqlite.Row) else row[0]
            return int(value or 0)
        except Exception as e:
            logger.error(f"최근 대화 개수 조회 중 DB 오류: {e}", exc_info=True)
            return 0

    async def generate_system_alert_message(self, channel_id: int, alert_context: str, alert_title: str | None = None) -> str | None:
        """일상 알림을 해당 채널이 속한 서버의 말투로만 재작성합니다.

        지진 등 공통 재난 경보는 이 메서드를 호출하지 않고 고정 문구로
        전송한다. 일반 알림도 ``channel.guild.id``를 반드시 함께 사용해 다른
        서버의 DB 페르소나가 섞일 여지를 없앤다.
        """
        if not self.is_ready:
            return None

        channel = self.bot.get_channel(int(channel_id))
        guild = getattr(channel, "guild", None)
        guild_id = int(guild.id) if guild is not None else None
        log_extra = {
            'guild_id': guild_id,
            'channel_id': channel_id,
            'alert_title': alert_title,
        }

        try:
            system_prompt = (
                f"{self._get_channel_system_prompt(channel_id, guild_id=guild_id)}\n\n"
                "### 추가 지침\n"
                "- 지금은 서버 구성원에게 전달할 시스템 공지를 작성하는 중이다.\n"
                "- 핵심 정보는 빠뜨리지 말되 2~3문장 이내로 간결하게 정리한다.\n"
                "- 필요 시 가벼운 이모지 한두 개만 사용하고, 과한 장식은 피한다.\n"
                "- 마지막에는 자연스럽게 행동을 촉구하거나 격려하는 말을 덧붙인다.\n"
                "- 절대로 @everyone, @here, <@&역할ID> 같은 멘션 태그를 사용하지 않는다. "
                "메시지에 멘션을 포함하면 안 된다.\n\n"
                f"{config.MODEL_STYLE_FIDELITY_PROMPT}"
            )

            user_prompt = (
                "다음 정보를 바탕으로 서버에 전달할 공지 메시지를 작성해줘.\n"
                f"- 알림 주제: {alert_title or '일반 알림'}\n"
                f"- 전달할 내용: {alert_context}\n\n"
                "공지 문구는 마사몽의 말투를 유지해 주고, 너무 장황하지 않게 작성해줘."
            )

            alert_message = None

            # 1. CometAPI 우선 사용
            if self.use_cometapi:
                alert_message = await self._cometapi_generate_content(
                    system_prompt,
                    user_prompt,
                    log_extra,
                    stop_on_bounded_failure=True,
                )

            # 2. 실패 시 Gemini 폴백(옵션)
            if not alert_message and self._can_use_direct_gemini():
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
        guild = getattr(channel, "guild", None)
        guild_id = int(guild.id) if guild is not None else None
        log_extra = {
            'guild_id': guild_id,
            'channel_id': int(channel.id),
            'user_id': author.id,
            'prompt_key': prompt_key,
        }

        try:
            prompt_template = config.AI_CREATIVE_PROMPTS.get(prompt_key)
            if not prompt_template: return config.MSG_CMD_ERROR

            user_prompt = prompt_template.format(**context)
            system_prompt = self._get_channel_system_prompt(
                int(channel.id),
                guild_id=guild_id,
            )
            system_prompt = (
                f"{system_prompt}\n\n"
                f"{config.MODEL_STYLE_FIDELITY_PROMPT}"
            )

            # [FIX] 명령어로 호출된 경우 멘션 정책 무시 (가드 제거)
            if config.MENTION_GUARD_SNIPPET in system_prompt:
                system_prompt = system_prompt.replace(config.MENTION_GUARD_SNIPPET, "")

            response_text = None

            # 1. CometAPI 우선 사용
            if self.use_cometapi:
                response_text = await self._cometapi_generate_content(
                    system_prompt,
                    user_prompt,
                    log_extra,
                    stop_on_bounded_failure=True,
                )

            # 2. 실패 시 Gemini 폴백(옵션)
            if not response_text and self._can_use_direct_gemini():
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
        """호환용 별칭. 실제 심볼 확정은 Yahoo 검색이 담당합니다."""
        return await self.extract_finance_search_term_with_llm(query)



    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """사용자가 📰를 더하면 캐시한 뉴스 출처를 같은 메시지에 표시합니다."""
        # 봇 자신의 리액션은 무시
        if payload.user_id == self.bot.user.id:
            return
        # 📰 이모지 외 무시
        if str(payload.emoji) != "📰":
            return
        # 캐시에 없으면 무시 (웹 검색 결과 아님)
        source_urls = self._news_source_cache.get(payload.message_id)
        if not source_urls:
            return
        lock = self._news_source_locks.setdefault(
            int(payload.message_id),
            asyncio.Lock(),
        )
        async with lock:
            try:
                channel = self.bot.get_channel(payload.channel_id)
                if not channel:
                    # DM 채널도 캐시될 수 있으므로 API 조회로 보완한다.
                    try:
                        channel = await self.bot.fetch_channel(payload.channel_id)
                    except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
                        logger.debug(
                            "뉴스 출처 채널 조회 실패. channel_id=%s error=%s",
                            payload.channel_id,
                            exc,
                        )
                        return

                if not channel:
                    return

                msg = await channel.fetch_message(payload.message_id)

                # 이미 출처가 포함되어 있는지 확인 (더블 체크)
                if self.NEWS_SOURCE_SECTION in msg.content:
                    return
                source_text = self._format_web_source_footer(
                    source_urls,
                    max_chars=2_000 - len(msg.content),
                )
                if not source_text:
                    logger.warning(
                        "뉴스 출처 표시 생략: 메시지 길이 여유 없음. message_id=%s",
                        payload.message_id,
                    )
                    return
                await msg.edit(content=msg.content + source_text)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
                logger.warning(
                    "뉴스 출처 반응 처리 실패. message_id=%s error=%s",
                    payload.message_id,
                    exc,
                )
            except Exception:
                logger.exception(
                    "뉴스 출처 반응 처리 중 예상하지 못한 오류. message_id=%s",
                    payload.message_id,
                )

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        """마지막 사용자 📰 반응이 제거되면 메시지에서 출처를 다시 숨깁니다."""
        if str(payload.emoji) != "📰":
            return

        # 캐시에 있는 메시지인지 확인
        if payload.message_id not in self._news_source_cache:
            return

        lock = self._news_source_locks.setdefault(
            int(payload.message_id),
            asyncio.Lock(),
        )
        async with lock:
            try:
                channel = self.bot.get_channel(payload.channel_id)
                if not channel:
                    try:
                        channel = await self.bot.fetch_channel(payload.channel_id)
                    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                        return

                if not channel:
                    return

                msg = await channel.fetch_message(payload.message_id)

                # 봇이 미리 붙인 반응 하나만 남았거나 반응 자체가 사라졌다면
                # 출처 섹션을 제거한다. 다른 사용자가 누른 상태면 유지한다.
                newspaper_reaction = discord.utils.get(msg.reactions, emoji="📰")
                if newspaper_reaction is None or newspaper_reaction.count <= 1:
                    if self.NEWS_SOURCE_SECTION in msg.content:
                        new_content = msg.content.split(
                            self.NEWS_SOURCE_SECTION,
                            1,
                        )[0]
                        await msg.edit(content=new_content)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
                logger.debug(
                    "뉴스 출처 숨기기 실패. message_id=%s error=%s",
                    payload.message_id,
                    exc,
                )
            except Exception:
                logger.exception(
                    "뉴스 출처 숨기기 중 예상하지 못한 오류. message_id=%s",
                    payload.message_id,
                )


async def setup(bot: commands.Bot):
    """Cog를 봇에 등록하는 함수"""
    await bot.add_cog(AIHandler(bot))

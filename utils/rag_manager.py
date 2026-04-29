# -*- coding: utf-8 -*-
"""RAG(Retrieval-Augmented Generation), 임베딩, 대화 메모리 관리를 전담하는 매니저 클래스입니다.

대화 기록 저장, 임베딩 생성, 슬라이딩 윈도우 기반 구조화 메모리 관리 등의 기능을 제공합니다.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any, Dict

import re

import discord
import aiosqlite
import numpy as np

import config
from logger_config import logger
from utils.embeddings import (
    count_embedding_tokens,
    get_embedding,
    get_embedding_token_limit,
    trim_text_to_embedding_token_limit,
)
from utils.memory_units import build_storage_text, build_structured_memory_units


class RAGManager:
    """RAG 파이프라인 및 대화 메모리 관리를 전담하는 클래스입니다.

    대화 기록 저장, 임베딩 생성, 슬라이딩 윈도우 기반 구조화 메모리 유닛 생성/저장,
    텍스트 요약 등 AI 장기 기억에 필요한 모든 기능을 제공합니다.
    """

    def __init__(
        self,
        *,
        db: aiosqlite.Connection | None = None,
        embedding_store,
        hybrid_search_engine,
        reranker,
        llm_client,
        bot,
    ):
        """RAGManager 초기화.

        Args:
            db: 봇의 메인 데이터베이스 연결.
            embedding_store: Discord 임베딩 저장소 (DiscordEmbeddingStore).
            hybrid_search_engine: 하이브리드 검색 엔진.
            reranker: 리랭커 인스턴스 (또는 None).
            llm_client: LLM 클라이언트 인스턴스.
            bot: Discord 봇 인스턴스 (add_message_to_history에서 필요한 채널/길드 정보 접근용).
        """
        self.db = db
        self.embedding_store = embedding_store
        self.hybrid_search_engine = hybrid_search_engine
        self.reranker = reranker
        self.llm_client = llm_client
        self.bot = bot
        self._window_buffers: dict[tuple[int, int], deque[dict[str, Any]]] = {}
        self._window_counts: dict[tuple[int, int], int] = {}
        self._embedding_token_limit_cache: int | None = None

    @property
    def use_cometapi(self) -> bool:
        """LLMClient에서 CometAPI 사용 여부를 가져옵니다."""
        return bool(self.llm_client and self.llm_client.use_cometapi)

    async def _generate_local_embedding(self, content: str, log_extra: dict, prefix: str = "") -> np.ndarray | None:
        """SentenceTransformer 기반 임베딩을 생성합니다."""
        if not config.AI_MEMORY_ENABLED:
            return None

        embedding = await get_embedding(content, prefix=prefix)
        if embedding is None:
            logger.error("임베딩 생성 실패", extra=log_extra)
        return embedding

    @staticmethod
    def _estimate_window_tokens(text: str) -> int:
        """윈도우 저장 판단용 경량 토큰 추정치."""
        return len(re.findall(r"\S+", str(text or "")))

    async def _embedding_token_limit(self) -> int:
        """임베딩 입력에 사용할 안전 토큰 한계를 반환합니다."""
        if self._embedding_token_limit_cache is not None:
            return self._embedding_token_limit_cache
        reserve = max(8, int(getattr(config, "CONVERSATION_WINDOW_TOKEN_RESERVE", 32)))
        limit = await get_embedding_token_limit(reserve_tokens=reserve)
        self._embedding_token_limit_cache = max(32, int(limit))
        return self._embedding_token_limit_cache

    async def add_message_to_history(self, message: discord.Message):
        """AI 허용 채널의 메시지를 대화 기록 DB에 저장합니다.

        Args:
            message (discord.Message): Discord 원본 메시지.

        Notes:
            메시지가 충분히 길면 임베딩 생성을 비동기 태스크로 예약합니다.
        """
        if self.db is None or not config.AI_MEMORY_ENABLED:
            return

        guild_id = message.guild.id if message.guild else 0

        # Guild인 경우에만 채널 화이트리스트 체크
        if message.guild:
            try:
                channel_config = config.CHANNEL_AI_CONFIG.get(message.channel.id, {})
                if not channel_config.get("allowed", False):
                    return
            except AttributeError:
                pass  # message.channel has no id? rare.

        try:
            storage_text = build_storage_text(
                message.content or "",
                attachment_count=len(getattr(message, "attachments", [])),
                embed_count=len(getattr(message, "embeds", [])),
                sticker_count=len(getattr(message, "stickers", [])),
            )
            if not storage_text:
                return
            await self.db.execute(
                "INSERT INTO conversation_history (message_id, guild_id, channel_id, user_id, user_name, content, is_bot, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message.id,
                    guild_id,
                    message.channel.id,
                    message.author.id,
                    message.author.display_name,
                    storage_text,
                    message.author.bot,
                    message.created_at.isoformat(),
                ),
            )
            await self._update_conversation_windows(message)
            await self.db.commit()
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

            summary = await self.llm_client.generate_content(
                system_prompt,
                user_prompt,
                log_extra={'mode': 'rag_summary'},
            )

            if summary:
                return summary.strip()
            return text
        except Exception as e:
            logger.warning("대화 내용 요약 중 오류 발생, 원본 텍스트 반환: %s", e)
            return text

    async def _create_window_embedding(self, guild_id: int, channel_id: int, payload: list[dict[str, Any]]):
        """대화 윈도우를 구조화 메모리 유닛으로 정제해 저장합니다."""
        if not payload:
            return

        memory_units = build_structured_memory_units(
            payload,
            channel_id=channel_id,
            max_summary_chars=getattr(config, "STRUCTURED_MEMORY_MAX_SUMMARY_CHARS", 320),
            max_context_chars=getattr(config, "STRUCTURED_MEMORY_MAX_CONTEXT_CHARS", 1200),
            user_turn_min_chars=getattr(config, "STRUCTURED_USER_MEMORY_MIN_CHARS", 12),
        )
        if not memory_units:
            return

        for unit in memory_units:
            log_extra = {
                'guild_id': guild_id,
                'channel_id': channel_id,
                'window_id': unit.anchor_message_id,
                'memory_scope': unit.memory_scope,
                'memory_type': unit.memory_type,
            }
            memory_text_for_embedding = unit.memory_text
            summary_text_for_storage = unit.summary_text
            token_limit = await self._embedding_token_limit()
            input_text = f"passage: {memory_text_for_embedding}"
            token_count = await count_embedding_tokens(input_text)
            if token_count > token_limit:
                logger.info(
                    "구조화 메모리 토큰 초과 감지: %s > %s (scope=%s). 요약 전환 시도",
                    token_count,
                    token_limit,
                    unit.memory_scope,
                    extra=log_extra,
                )
                summary_source = unit.raw_context or unit.memory_text
                summarized = await self._summarize_content(summary_source)
                if summarized:
                    summary_text_for_storage = summarized
                    memory_text_for_embedding = summarized
                    token_count = await count_embedding_tokens(f"passage: {memory_text_for_embedding}")

                if token_count > token_limit:
                    trimmed = await trim_text_to_embedding_token_limit(
                        memory_text_for_embedding,
                        token_limit,
                    )
                    if trimmed:
                        memory_text_for_embedding = trimmed
                        token_count = await count_embedding_tokens(f"passage: {memory_text_for_embedding}")

            embedding_vector = await self._generate_local_embedding(
                f"passage: {memory_text_for_embedding}",
                log_extra,
                prefix="",
            )
            if embedding_vector is None:
                continue

            try:
                await self.embedding_store.upsert_memory_entry(
                    memory_id=unit.memory_id,
                    anchor_message_id=unit.anchor_message_id,
                    server_id=guild_id,
                    channel_id=channel_id,
                    owner_user_id=unit.owner_user_id,
                    owner_user_name=unit.owner_user_name,
                    memory_scope=unit.memory_scope,
                    memory_type=unit.memory_type,
                    summary_text=summary_text_for_storage,
                    memory_text=memory_text_for_embedding,
                    raw_context=unit.raw_context,
                    source_message_ids=unit.source_message_ids,
                    speaker_names=unit.speaker_names,
                    keywords=unit.keywords,
                    timestamp_iso=unit.timestamp_iso,
                    embedding=embedding_vector,
                )
            except Exception as e:
                logger.error("구조화 메모리 저장 중 오류: %s", e, extra=log_extra, exc_info=True)

    async def _update_conversation_windows(self, message: discord.Message) -> None:
        """대화 슬라이딩 윈도우(6개, stride=3)를 누적해 별도 테이블에 저장합니다."""
        if self.db is None:
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
            "content": build_storage_text(
                message.content or "",
                attachment_count=len(getattr(message, "attachments", [])),
                embed_count=len(getattr(message, "embeds", [])),
                sticker_count=len(getattr(message, "stickers", [])),
            ),
            "is_bot": bool(message.author.bot),
            "created_at": message.created_at.isoformat(),
        }
        if not entry["content"]:
            return
        entry["token_estimate"] = self._estimate_window_tokens(entry["content"])
        buffer.append(entry)

        # stride 계산을 위해 채널별 삽입 횟수를 기록한다.
        counter = self._window_counts.get(key, 0) + 1
        self._window_counts[key] = counter

        # [Feature] 토큰 기반 윈도우 저장 기준.
        total_tokens = sum(int(item.get("token_estimate") or 0) for item in buffer)
        configured_max_tokens = int(getattr(config, "CONVERSATION_WINDOW_MAX_TOKENS", 0))
        fallback_max_tokens = max(64, int(getattr(config, "LOCAL_EMBEDDING_MAX_TOKENS", 512)))
        max_tokens = configured_max_tokens if configured_max_tokens > 0 else max(
            64,
            fallback_max_tokens - int(getattr(config, "CONVERSATION_WINDOW_TOKEN_RESERVE", 32)),
        )

        # 비정상 장문 보호용 문자 기준(2차 안전장치)
        total_chars = sum(len(item["content"]) for item in buffer)
        max_chars = int(getattr(config, "CONVERSATION_WINDOW_MAX_CHARS", 3000))

        # 윈도우가 가득 찼거나, 토큰/문자열 길이가 제한을 초과하면 저장을 시도한다.
        is_full = len(buffer) >= window_size
        is_heavy = total_tokens >= max_tokens or total_chars >= max_chars

        if not is_full and not is_heavy:
            return

        # stride 간격에 맞춰 윈도우를 저장한다.
        # 단, is_heavy(용량 초과)인 경우에는 stride와 무관하게 즉시 저장하여 컨텍스트 누락을 방지한다.
        if not is_heavy and (counter - window_size) % stride != 0:
            return

        # [Log] 용량 초과로 인한 강제 저장 알림
        if is_heavy and not is_full:
            logger.info(
                "대화 윈도우 용량 초과(tokens=%s/%s, chars=%s/%s)로 즉시 저장: %s",
                total_tokens,
                max_tokens,
                total_chars,
                max_chars,
                message.channel.id,
                extra={'guild_id': guild_id},
            )

        try:
            payload = list(buffer)
            await self.db.execute(
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

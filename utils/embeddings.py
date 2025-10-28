# -*- coding: utf-8 -*-
"""로컬 임베딩 모델과 벡터 저장소 관리를 담당하는 유틸리티 모듈."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Iterable

import aiosqlite
import numpy as np

try:  # pragma: no cover - optional dependency guard
    from sentence_transformers import SentenceTransformer
except ModuleNotFoundError:  # pragma: no cover
    SentenceTransformer = None  # type: ignore

import config
from logger_config import logger

_MODEL: SentenceTransformer | None = None
_MODEL_LOCK = asyncio.Lock()


async def _load_model() -> SentenceTransformer:
    """SentenceTransformer 모델을 비동기적으로 로드합니다."""
    global _MODEL
    if _MODEL is not None:
        return _MODEL

    async with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL

        loop = asyncio.get_running_loop()
        model_name = getattr(config, "LOCAL_EMBEDDING_MODEL_NAME", "BM-K/KoSimCSE-roberta")
        device = getattr(config, "LOCAL_EMBEDDING_DEVICE", None)

        if SentenceTransformer is None:
            raise RuntimeError(
                "sentence-transformers 패키지가 설치되어 있지 않습니다. `pip install sentence-transformers`로 설치 후 다시 시도하세요."
            )

        def _sync_load():
            load_kwargs = {"device": device} if device else {}
            return SentenceTransformer(model_name, **load_kwargs)

        logger.info("로컬 임베딩 모델 로드 시작: %s", model_name)
        _MODEL = await loop.run_in_executor(None, _sync_load)
        logger.info("로컬 임베딩 모델 로드 완료: %s", model_name)
        return _MODEL


async def get_embedding(text: str) -> np.ndarray | None:
    """문자열을 임베딩 벡터(float32)로 변환합니다."""
    if not text:
        return None

    model = await _load_model()
    normalize = getattr(config, "LOCAL_EMBEDDING_NORMALIZE", True)
    loop = asyncio.get_running_loop()

    def _sync_encode() -> np.ndarray:
        vector = model.encode(text, normalize_embeddings=normalize)
        if not isinstance(vector, np.ndarray):
            vector = np.asarray(vector)
        return vector.astype(np.float32)

    try:
        return await loop.run_in_executor(None, _sync_encode)
    except Exception as exc:  # pragma: no cover - encode() 내부 오류 방지용
        logger.error("임베딩 생성 중 오류 발생: %s", exc, exc_info=True)
        return None


class DiscordEmbeddingStore:
    """Discord 대화 임베딩을 SQLite 파일로 관리하는 저장소."""

    _CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS discord_chat_embeddings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id TEXT UNIQUE,
        server_id TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        user_name TEXT,
        message TEXT,
        timestamp TEXT,
        embedding BLOB NOT NULL
    );
    """
    _CREATE_INDEX_SQL = (
        "CREATE INDEX IF NOT EXISTS idx_discord_embeddings_scuid ON discord_chat_embeddings (server_id, channel_id, user_id)",
        "CREATE INDEX IF NOT EXISTS idx_discord_embeddings_timestamp ON discord_chat_embeddings (timestamp DESC)"
    )

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self._init_lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        """DB 파일이 존재하지 않으면 생성하고 스키마를 준비합니다."""
        if self._initialized:
            return

        async with self._init_lock:
            if self._initialized:
                return

            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA journal_mode=WAL;")
                await db.execute(self._CREATE_TABLE_SQL)
                for sql in self._CREATE_INDEX_SQL:
                    await db.execute(sql)
                await db.commit()
            self._initialized = True
            logger.info("Discord 임베딩 DB 초기화 완료: %s", self.db_path)

    async def upsert_message_embedding(
        self,
        message_id: int,
        server_id: int,
        channel_id: int,
        user_id: int,
        user_name: str,
        message: str,
        timestamp_iso: str,
        embedding: np.ndarray,
    ) -> None:
        """메시지의 임베딩을 저장하거나 갱신합니다."""
        await self.initialize()
        embedding_bytes = np.asarray(embedding, dtype=np.float32).tobytes()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO discord_chat_embeddings (
                    message_id, server_id, channel_id, user_id, user_name, message, timestamp, embedding
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    user_name = excluded.user_name,
                    message = excluded.message,
                    timestamp = excluded.timestamp,
                    embedding = excluded.embedding
                """,
                (
                    str(message_id),
                    str(server_id),
                    str(channel_id),
                    str(user_id),
                    user_name,
                    message,
                    timestamp_iso,
                    embedding_bytes,
                ),
            )
            await db.commit()

    async def fetch_recent_embeddings(
        self,
        server_id: int,
        channel_id: int,
        user_id: int | None = None,
        limit: int = 200,
    ) -> list[aiosqlite.Row]:
        """지정한 범위의 최신 임베딩 레코드를 반환합니다."""
        await self.initialize()
        query = (
            "SELECT message_id, user_id, user_name, message, timestamp, embedding "
            "FROM discord_chat_embeddings WHERE server_id = ? AND channel_id = ?"
        )
        params: list[str | int] = [str(server_id), str(channel_id)]
        if user_id is not None:
            query += " AND user_id = ?"
            params.append(str(user_id))
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(int(limit))

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
        return rows

    async def delete_embeddings(self, message_ids: Iterable[int]) -> None:
        """지정한 메시지 ID 목록의 임베딩을 삭제합니다."""
        ids = [str(mid) for mid in message_ids]
        if not ids:
            return
        await self.initialize()
        placeholders = ",".join("?" for _ in ids)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"DELETE FROM discord_chat_embeddings WHERE message_id IN ({placeholders})",
                ids,
            )
            await db.commit()

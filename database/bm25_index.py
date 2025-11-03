# -*- coding: utf-8 -*-
"""FTS5 기반 BM25 인덱스를 관리하고 검색하는 헬퍼 모듈."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Optional

import aiosqlite

from logger_config import logger

_FTS_TABLE_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS conversation_bm25 USING fts5(
    content,
    guild_id UNINDEXED,
    channel_id UNINDEXED,
    user_id UNINDEXED,
    user_name,
    created_at,
    message_id UNINDEXED,
    tokenize='unicode61 remove_diacritics 2'
);
"""

_TRIGGER_INSERT_SQL = """
CREATE TRIGGER IF NOT EXISTS conversation_history_bm25_ai
AFTER INSERT ON conversation_history
BEGIN
    INSERT INTO conversation_bm25(
        rowid, content, guild_id, channel_id, user_id, user_name, created_at, message_id
    ) VALUES (
        NEW.message_id,
        NEW.content,
        NEW.guild_id,
        NEW.channel_id,
        NEW.user_id,
        NEW.user_name,
        NEW.created_at,
        NEW.message_id
    );
END;
"""

_TRIGGER_UPDATE_SQL = """
CREATE TRIGGER IF NOT EXISTS conversation_history_bm25_au
AFTER UPDATE ON conversation_history
BEGIN
    INSERT INTO conversation_bm25(conversation_bm25, rowid) VALUES('delete', OLD.message_id);
    INSERT INTO conversation_bm25(
        rowid, content, guild_id, channel_id, user_id, user_name, created_at, message_id
    ) VALUES (
        NEW.message_id,
        NEW.content,
        NEW.guild_id,
        NEW.channel_id,
        NEW.user_id,
        NEW.user_name,
        NEW.created_at,
        NEW.message_id
    );
END;
"""

_TRIGGER_DELETE_SQL = """
CREATE TRIGGER IF NOT EXISTS conversation_history_bm25_ad
AFTER DELETE ON conversation_history
BEGIN
    INSERT INTO conversation_bm25(conversation_bm25, rowid) VALUES('delete', OLD.message_id);
END;
"""

_CONTEXT_FETCH_SQL = """
SELECT message_id, user_name, content
FROM conversation_history
WHERE channel_id = ?
  AND created_at BETWEEN ? AND ?
ORDER BY created_at
LIMIT ?
"""


@dataclass(frozen=True)
class BM25SearchResult:
    """BM25 검색 결과를 표현하는 단순 자료 구조."""

    message_id: int
    guild_id: int
    channel_id: int
    user_id: int
    user_name: str
    content: str
    created_at: str
    bm25_score: float
    context_window: List[dict[str, Any]]


class BM25IndexManager:
    """대화 히스토리에 대한 FTS5 기반 BM25 검색을 처리합니다."""

    def __init__(self, db_path: str, context_minutes: int = 10, context_limit: int = 6):
        self.db_path = Path(db_path)
        self.context_minutes = max(1, context_minutes)
        self.context_limit = max(1, context_limit)
        self._init_lock = asyncio.Lock()
        self._initialized = False

    async def ensure_index(self) -> None:
        """FTS5 인덱스 및 트리거를 생성하고 동기화합니다."""
        if self._initialized:
            return

        async with self._init_lock:
            if self._initialized:
                return

            if not self.db_path.exists():
                logger.warning("BM25 인덱스를 위한 DB 파일이 존재하지 않습니다: %s", self.db_path)
                self._initialized = True
                return

            try:
                async with aiosqlite.connect(self.db_path) as db:
                    # FTS 테이블과 동기화 트리거를 준비하고, 기존 데이터를 재색인한다.
                    await db.execute(_FTS_TABLE_SQL)
                    await db.execute(_TRIGGER_INSERT_SQL)
                    await db.execute(_TRIGGER_UPDATE_SQL)
                    await db.execute(_TRIGGER_DELETE_SQL)
                    await db.execute("INSERT INTO conversation_bm25(conversation_bm25) VALUES('rebuild');")
                    await db.commit()
                logger.info("BM25 FTS 인덱스 초기화 완료: %s", self.db_path)
            except aiosqlite.Error as exc:
                logger.error("BM25 인덱스 초기화 중 오류: %s", exc, exc_info=True)
            finally:
                self._initialized = True

    async def search(
        self,
        query: str,
        *,
        guild_id: int | None = None,
        channel_id: int | None = None,
        limit: int = 20,
    ) -> list[BM25SearchResult]:
        """BM25 기반 텍스트 검색을 수행합니다."""
        await self.ensure_index()
        if not query.strip():
            return []
        if not self.db_path.exists():
            return []

        normalized_query = self._normalize_query(query)
        filters: list[str] = []
        params: list[Any] = [normalized_query]
        if guild_id is not None:
            filters.append("guild_id = ?")
            params.append(str(guild_id))
        if channel_id is not None:
            filters.append("channel_id = ?")
            params.append(str(channel_id))

        # MATCH 구문은 파라미터화된 자리표시자를 사용해야 SQL 인젝션을 방지할 수 있다.
        where_clause = "WHERE conversation_bm25 MATCH ?"
        if filters:
            where_clause += " AND " + " AND ".join(filters)

        query_sql = f"""
            SELECT
                message_id,
                guild_id,
                channel_id,
                user_id,
                user_name,
                content,
                created_at,
                bm25(conversation_bm25, 1.2, 0.75) AS score
            FROM conversation_bm25
            {where_clause}
            ORDER BY score ASC
            LIMIT ?
        """
        params.append(int(limit))

        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(query_sql, params) as cursor:
                    rows = await cursor.fetchall()
                results: list[BM25SearchResult] = []
                for row in rows:
                    window = await self._build_context_window(
                        db,
                        channel_id=row["channel_id"],
                        center_ts=row["created_at"],
                    )
                    result = BM25SearchResult(
                        message_id=int(row["message_id"]),
                        guild_id=int(row["guild_id"]),
                        channel_id=int(row["channel_id"]),
                        user_id=int(row["user_id"]),
                        user_name=str(row["user_name"] or ""),
                        content=str(row["content"] or ""),
                        created_at=str(row["created_at"] or ""),
                        bm25_score=float(row["score"]),
                        context_window=window,
                    )
                    results.append(result)
                return results
        except aiosqlite.Error as exc:
            logger.error("BM25 검색 중 오류: %s", exc, exc_info=True)
            return []

    async def _build_context_window(
        self,
        db: aiosqlite.Connection,
        *,
        channel_id: int,
        center_ts: str,
    ) -> list[dict[str, Any]]:
        """검색 결과 주변의 대화를 시간 기반으로 수집합니다."""
        if not center_ts:
            return []

        try:
            async with db.execute(
                _CONTEXT_FETCH_SQL,
                (
                    int(channel_id),
                    self._shift_timestamp(center_ts, -self.context_minutes),
                    self._shift_timestamp(center_ts, self.context_minutes),
                    self.context_limit,
                ),
            ) as cursor:
                rows = await cursor.fetchall()
        except aiosqlite.Error:
            return []

        window: list[dict[str, Any]] = []
        for row in rows:
            window.append(
                {
                    "message_id": row["message_id"],
                    "user_name": row["user_name"],
                    "message": row["content"],
                }
            )
        return window

    async def fetch_context(
        self,
        *,
        channel_id: int,
        center_timestamp: str,
    ) -> list[dict[str, Any]]:
        """외부 모듈에서 사용할 수 있도록 컨텍스트 윈도우를 노출합니다."""
        await self.ensure_index()
        if not center_timestamp or not self.db_path.exists():
            return []
        try:
            async with aiosqlite.connect(self.db_path) as db:
                return await self._build_context_window(
                    db,
                    channel_id=channel_id,
                    center_ts=center_timestamp,
                )
        except aiosqlite.Error:
            return []

    def _shift_timestamp(self, timestamp_iso: str, minutes: int) -> str:
        """ISO 포맷 문자열에 대해 분 단위 이동을 수행합니다."""
        from datetime import datetime, timedelta

        try:
            dt = datetime.fromisoformat(timestamp_iso.replace("Z", "+00:00"))
        except ValueError:
            return timestamp_iso
        shifted = dt + timedelta(minutes=minutes)
        return shifted.isoformat()

    def _normalize_query(self, query: str) -> str:
        """FTS 쿼리에 사용할 문자열을 간단히 정규화합니다."""
        tokens: list[str] = []
        for raw in query.split():
            # FTS5 특수문자(따옴표, 콜론 등)는 제거해 한 단어로 만든다.
            stripped = raw.strip().strip('"\'')
            stripped = stripped.replace('"', " ").replace("'", " ").replace(":", " ")
            # 알파벳/숫자/한글 외 문자를 공백으로 치환한다.
            normalized = []
            for char in stripped:
                if char.isalnum() or '가' <= char <= '힣':
                    normalized.append(char)
                else:
                    normalized.append(" ")
            candidate = "".join(part for part in "".join(normalized).split() if part)
            if candidate:
                tokens.append(candidate)
        if not tokens:
            return ""
        # 특수 명령으로 해석되지 않도록 각 토큰을 따옴표로 감싼 OR 쿼리로 변환한다.
        return " OR ".join(f'"{token}"' for token in tokens)


async def bulk_rebuild(db_path: str) -> None:
    """대화 기록 전체를 대상으로 BM25 인덱스를 재구축합니다."""
    manager = BM25IndexManager(db_path)
    await manager.ensure_index()
    if not Path(db_path).exists():
        return
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute("INSERT INTO conversation_bm25(conversation_bm25) VALUES('rebuild');")
            await db.commit()
        logger.info("BM25 인덱스 재구축 완료: %s", db_path)
    except aiosqlite.Error as exc:
        logger.error("BM25 인덱스 재구축 실패: %s", exc, exc_info=True)

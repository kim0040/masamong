# -*- coding: utf-8 -*-
"""로컬 임베딩 모델과 벡터 저장소 관리를 담당하는 유틸리티 모듈."""

from __future__ import annotations

import asyncio
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

import aiosqlite

# numpy/torch 기반 의존성은 저사양 서버에서는 설치하지 않을 수 있으므로, ImportError를 허용한다.
try:
    import numpy as np  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    np = None  # type: ignore

try:  # pragma: no cover - optional dependency guard
    from sentence_transformers import SentenceTransformer
except ModuleNotFoundError:  # pragma: no cover
    SentenceTransformer = None  # type: ignore

import config
from logger_config import logger

_MODEL: SentenceTransformer | None = None
_MODEL_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class _KakaoTableMeta:
    table_name: str
    text_column: str
    embedding_column: str
    timestamp_column: Optional[str] = None
    speaker_column: Optional[str] = None


async def _load_model() -> SentenceTransformer:
    """SentenceTransformer 모델을 비동기적으로 로드합니다."""
    if SentenceTransformer is None:
        raise RuntimeError(
            "sentence-transformers 패키지가 설치되어 있지 않습니다. `pip install sentence-transformers`로 설치 후 다시 시도하세요."
        )
    if np is None:
        raise RuntimeError(
            "numpy 패키지가 설치되어 있지 않습니다. AI 메모리 기능을 사용하려면 `pip install numpy`로 설치하세요."
        )

    global _MODEL
    if _MODEL is not None:
        return _MODEL

    async with _MODEL_LOCK:
        if _MODEL is not None:
            return _MODEL

        loop = asyncio.get_running_loop()
        model_name = getattr(config, "LOCAL_EMBEDDING_MODEL_NAME", "BM-K/KoSimCSE-roberta")
        device = getattr(config, "LOCAL_EMBEDDING_DEVICE", None)

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
    if np is None:
        logger.warning("numpy가 설치되지 않아 임베딩을 생성할 수 없습니다. `AI_MEMORY_ENABLED` 값을 확인하세요.")
        return None
    if SentenceTransformer is None:
        logger.warning("sentence-transformers가 설치되지 않아 임베딩 생성을 건너뜁니다.")
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
        if np is None:
            raise RuntimeError("numpy가 설치되어 있지 않아 임베딩을 저장할 수 없습니다.")
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


class KakaoEmbeddingStore:
    """여러 카카오 채팅방 임베딩 DB를 읽어오는 헬퍼."""

    def __init__(self, default_db_path: str | None, server_map: Dict[str, Dict[str, str]]):
        self.default_db_path = Path(default_db_path) if default_db_path else None
        self.server_map: Dict[str, Dict[str, Any]] = {}
        for raw_server_id, meta in (server_map or {}).items():
            server_id = str(raw_server_id)
            db_path = meta.get("db_path") if isinstance(meta, dict) else None
            if not db_path:
                continue
            self.server_map[server_id] = {
                "path": Path(db_path),
                "label": meta.get("label", "") if isinstance(meta, dict) else "",
            }

        self._table_meta_cache: Dict[Path, Optional[_KakaoTableMeta]] = {}
        self._table_meta_lock = asyncio.Lock()
        self._vector_extension_candidates = self._build_vector_extension_candidates()
        self._vector_extension_warning_logged = False

    async def fetch_recent_embeddings(self, server_ids: Iterable[str], limit: int = 200) -> list[Dict[str, Any]]:
        """서버 ID 후보 목록에 해당하는 Kakao 임베딩 레코드를 읽어옵니다."""
        targets: list[tuple[Path, str, str]] = []
        seen_paths: set[Path] = set()

        for candidate in server_ids:
            if not candidate:
                continue
            meta = self.server_map.get(str(candidate))
            if not meta:
                continue
            path = meta["path"]
            if path in seen_paths:
                continue
            if not path.exists():
                logger.warning("Kakao 임베딩 DB 파일을 찾을 수 없습니다: %s", path)
                continue
            seen_paths.add(path)
            targets.append((path, meta.get("label", ""), str(candidate)))

        if not targets and self.default_db_path and self.default_db_path.exists():
            targets.append((self.default_db_path, "", "default"))

        results: list[Dict[str, Any]] = []
        for path, label, matched_id in targets:
            rows = await self._fetch_from_path(path, label, limit)
            for row in rows:
                row.setdefault("label", label or path.stem)
                row.setdefault("matched_server_id", matched_id)
                row.setdefault("db_path", str(path))
                results.append(row)
        return results

    def _build_vector_extension_candidates(self) -> list[str]:
        candidates: list[str] = []
        raw = getattr(config, "KAKAO_VECTOR_EXTENSION", None)
        if isinstance(raw, str) and raw.strip():
            candidates.append(raw.strip())
        elif isinstance(raw, (list, tuple)):
            for item in raw:
                if isinstance(item, str) and item.strip():
                    candidates.append(item.strip())

        candidates.extend(["vec0", "vector0"])

        deduped: list[str] = []
        for candidate in candidates:
            if candidate not in deduped:
                deduped.append(candidate)
        return deduped

    async def _load_vector_extension(self, db: aiosqlite.Connection) -> bool:
        if not self._vector_extension_candidates:
            return False

        try:
            await db.enable_load_extension(True)
        except AttributeError:
            pass
        except aiosqlite.Error as exc:
            logger.warning("SQLite 확장 로딩을 활성화하지 못했습니다: %s", exc)
            return False

        last_error: Exception | None = None
        for candidate in self._vector_extension_candidates:
            try:
                await db.execute("SELECT load_extension(?)", (candidate,))
                return True
            except aiosqlite.Error as exc:
                last_error = exc
                continue

        if not self._vector_extension_warning_logged:
            details = f"{self._vector_extension_candidates}"
            if last_error is not None:
                logger.warning("Kakao 임베딩 벡터 확장 로딩 실패(%s): %s", details, last_error)
            else:
                logger.warning("Kakao 임베딩 벡터 확장을 로드할 후보가 없습니다: %s", details)
            self._vector_extension_warning_logged = True
        return False

    async def _fetch_from_path(self, path: Path, label: str, limit: int) -> list[Dict[str, Any]]:
        try:
            async with aiosqlite.connect(path) as db:
                await self._load_vector_extension(db)
                db.row_factory = aiosqlite.Row
                table_meta = await self._get_or_detect_table_meta(path, db)
                if table_meta is None:
                    logger.warning("Kakao 임베딩 테이블 구조를 식별하지 못했습니다: %s", path)
                    return []

                select_parts = [
                    f"{table_meta.text_column} AS message",
                    f"{table_meta.embedding_column} AS embedding",
                ]
                if table_meta.timestamp_column:
                    select_parts.append(f"{table_meta.timestamp_column} AS timestamp")
                if table_meta.speaker_column:
                    select_parts.append(f"{table_meta.speaker_column} AS speaker")

                order_column = table_meta.timestamp_column or "rowid"
                query = (
                    f"SELECT {', '.join(select_parts)} FROM {table_meta.table_name} "
                    f"ORDER BY {order_column} DESC LIMIT ?"
                )

                async with db.execute(query, (limit,)) as cursor:
                    return [dict(row) for row in await cursor.fetchall()]
        except aiosqlite.Error as exc:
            logger.error("Kakao 임베딩 DB 읽기 중 오류: %s", exc, exc_info=True)
        return []

    async def _get_or_detect_table_meta(
        self,
        path: Path,
        connection: aiosqlite.Connection | None = None,
    ) -> Optional[_KakaoTableMeta]:
        cached = self._table_meta_cache.get(path)
        if cached is not None:
            return cached

        async with self._table_meta_lock:
            cached = self._table_meta_cache.get(path)
            if cached is not None:
                return cached

            if connection is None:
                try:
                    async with aiosqlite.connect(path) as db:
                        meta = await self._detect_table_meta(db)
                except aiosqlite.Error as exc:
                    logger.error("Kakao 임베딩 DB 구조 확인 중 오류: %s", exc, exc_info=True)
                    meta = None
            else:
                meta = await self._detect_table_meta(connection)

            self._table_meta_cache[path] = meta
            return meta

    async def _detect_table_meta(self, db: aiosqlite.Connection) -> Optional[_KakaoTableMeta]:
        try:
            async with db.execute(
                "SELECT name, type FROM sqlite_master "
                "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'"
            ) as cursor:
                raw_candidates = [(row[0], row[1]) for row in await cursor.fetchall()]

            # Prefer views (예: kakao_message_embeddings) before physical tables.
            candidates = sorted(
                raw_candidates,
                key=lambda item: 0 if (item[1] or "").lower() == "view" else 1,
            )

            for table_name, _ in candidates:
                columns = await self._fetch_column_info(db, table_name)
                if not columns:
                    continue

                lc_map = {name.lower(): name for name, _ in columns}
                column_types = {name.lower(): (col_type or "").upper() for name, col_type in columns}

                text_col = self._pick_column(
                    lc_map,
                    ["message", "content", "text", "body"],
                    column_types,
                    {"TEXT", "CHAR", "CLOB", "VARCHAR"},
                )
                embedding_col = self._pick_column(
                    lc_map,
                    ["embedding", "embedding_vector", "vector"],
                    column_types,
                    {"BLOB", "REAL", "FLOAT", "DOUBLE"},
                )

                if not text_col or not embedding_col:
                    continue

                timestamp_col = self._pick_column(
                    lc_map,
                    ["timestamp", "created_at", "datetime", "sent_at", "time"],
                    column_types,
                    {"TEXT", "CHAR", "NUMERIC", "DATE", "DATETIME", "INT"},
                )
                speaker_col = self._pick_column(
                    lc_map,
                    ["user_name", "username", "sender", "author", "speaker", "nickname"],
                    column_types,
                    {"TEXT", "CHAR", "CLOB", "VARCHAR"},
                )

                return _KakaoTableMeta(
                    table_name=table_name,
                    text_column=text_col,
                    embedding_column=embedding_col,
                    timestamp_column=timestamp_col,
                    speaker_column=speaker_col,
                )
        except aiosqlite.Error as exc:
            logger.error("Kakao 임베딩 테이블 구조 감지 실패: %s", exc, exc_info=True)
        return None

    async def _fetch_column_info(self, db: aiosqlite.Connection, table_name: str) -> list[tuple[str, str]]:
        async with db.execute(f"PRAGMA table_info('{table_name}')") as cursor:
            rows = await cursor.fetchall()
        return [(row[1], row[2]) for row in rows]

    @staticmethod
    def _pick_column(
        lc_map: Dict[str, str],
        candidates: list[str],
        column_types: Optional[Dict[str, str]] = None,
        allowed_type_hints: Optional[set[str]] = None,
    ) -> Optional[str]:
        def _type_matches(name_lower: str) -> bool:
            if not allowed_type_hints or not column_types:
                return True
            col_type = column_types.get(name_lower, "")
            if not col_type:
                return True
            for hint in allowed_type_hints:
                if hint in col_type:
                    return True
            return False

        for candidate in candidates:
            if candidate in lc_map:
                if _type_matches(candidate):
                    return lc_map[candidate]
        # try contains match just in case
        for name_lower, original in lc_map.items():
            for candidate in candidates:
                if candidate in name_lower and _type_matches(name_lower):
                    return original
        return None

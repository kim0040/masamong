# -*- coding: utf-8 -*-
"""로컬 임베딩 모델과 벡터 저장소 관리를 담당하는 유틸리티 모듈."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

import json
import aiosqlite
from database.compat_db import TiDBSettings

# numpy/torch 기반 의존성은 저사양 서버에서는 설치하지 않을 수 있으므로, ImportError를 허용한다.
try:
    import numpy as np  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    np = None  # type: ignore

try:  # pragma: no cover - optional dependency guard
    from sentence_transformers import SentenceTransformer
except ModuleNotFoundError:  # pragma: no cover
    SentenceTransformer = None  # type: ignore

try:
    import pymysql
except ModuleNotFoundError:  # pragma: no cover
    pymysql = None  # type: ignore

import config
from logger_config import logger

_MODEL: SentenceTransformer | None = None
_MODEL_LOCK = asyncio.Lock()
_WHITESPACE_TOKEN_RE = re.compile(r"\S+")


def _build_tidb_settings() -> TiDBSettings | None:
    """config 값을 바탕으로 TiDB 연결 설정 객체를 생성합니다."""
    if not (config.TIDB_HOST and config.TIDB_USER):
        return None
    return TiDBSettings(
        host=config.TIDB_HOST,
        port=config.TIDB_PORT,
        user=config.TIDB_USER,
        password=config.TIDB_PASSWORD or "",
        database=config.TIDB_NAME,
        ssl_ca=config.TIDB_SSL_CA,
        ssl_verify_identity=config.TIDB_SSL_VERIFY_IDENTITY,
    )


def _resolve_local_model_path(model_name: str) -> Path | None:
    """Hugging Face 캐시에서 모델 snapshot 경로를 찾아 반환합니다."""
    if not model_name:
        return None
    candidate = Path(model_name).expanduser()
    if candidate.exists():
        return candidate
    if "/" not in model_name:
        return None

    repo_dir = Path.home() / ".cache" / "huggingface" / "hub" / f"models--{model_name.replace('/', '--')}"
    snapshots_dir = repo_dir / "snapshots"
    if not snapshots_dir.exists():
        return None

    main_ref = repo_dir / "refs" / "main"
    if main_ref.exists():
        revision = main_ref.read_text(encoding="utf-8").strip()
        if revision:
            target = snapshots_dir / revision
            if target.exists():
                return target

    snapshot_dirs = [p for p in snapshots_dir.iterdir() if p.is_dir()]
    if not snapshot_dirs:
        return None
    snapshot_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return snapshot_dirs[0]


def _vector_literal(vector: "np.ndarray") -> str:
    """numpy 배열을 SQL 벡터 리터럴 문자열로 직렬화합니다."""
    values = np.asarray(vector, dtype=np.float32).tolist()
    return "[" + ",".join(f"{float(item):.8f}" for item in values) + "]"


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
        local_files_only = bool(getattr(config, "LOCAL_EMBEDDING_LOCAL_FILES_ONLY", False))

        def _sync_load():
            load_kwargs = {"device": device} if device else {}
            target_model = model_name
            if local_files_only:
                load_kwargs["local_files_only"] = True
                os.environ["HF_HUB_OFFLINE"] = "1"
                os.environ["TRANSFORMERS_OFFLINE"] = "1"
                resolved_path = _resolve_local_model_path(model_name)
                if resolved_path is not None:
                    target_model = str(resolved_path)
                    logger.info("로컬 임베딩 캐시 snapshot 경로 사용: %s", target_model)
            try:
                return SentenceTransformer(target_model, **load_kwargs)
            except TypeError:
                # 구버전 sentence-transformers는 local_files_only 인자를 지원하지 않을 수 있음
                if local_files_only:
                    raise RuntimeError(
                        "현재 sentence-transformers 버전은 LOCAL_EMBEDDING_LOCAL_FILES_ONLY를 지원하지 않습니다. "
                        "패키지를 업데이트하거나 모델을 사전 캐시한 뒤 옵션을 끄세요."
                    )
                load_kwargs.pop("local_files_only", None)
                return SentenceTransformer(target_model, **load_kwargs)

        logger.info("로컬 임베딩 모델 로드 시작: %s", model_name)
        _MODEL = await loop.run_in_executor(None, _sync_load)
        logger.info("로컬 임베딩 모델 로드 완료: %s", model_name)
        return _MODEL


def _fallback_token_count(text: str) -> int:
    """토크나이저 접근이 불가능할 때 공백 기반으로 대략적인 토큰 수를 추정합니다."""
    return len(_WHITESPACE_TOKEN_RE.findall(text or ""))


async def get_embedding_token_limit(*, reserve_tokens: int = 32) -> int:
    """임베딩 모델 max_seq_length를 기준으로 안전 토큰 한계를 반환합니다."""
    fallback_limit = max(64, int(getattr(config, "LOCAL_EMBEDDING_MAX_TOKENS", 512)))
    reserve = max(0, int(reserve_tokens))
    usable_fallback = max(32, fallback_limit - reserve)

    try:
        model = await _load_model()
    except Exception:
        return usable_fallback

    raw_max_len = getattr(model, "max_seq_length", None)
    try:
        max_len = int(raw_max_len)
    except (TypeError, ValueError):
        max_len = fallback_limit
    if max_len <= 0:
        max_len = fallback_limit

    return max(32, max_len - reserve)


async def count_embedding_tokens(text: str, *, add_special_tokens: bool = True) -> int:
    """임베딩 모델 토크나이저 기준 토큰 수를 계산합니다."""
    payload = str(text or "").strip()
    if not payload:
        return 0

    try:
        model = await _load_model()
    except Exception:
        return _fallback_token_count(payload)

    tokenizer = getattr(model, "tokenizer", None)
    if tokenizer is None:
        return _fallback_token_count(payload)

    def _sync_count() -> int:
        encoded = tokenizer(
            payload,
            add_special_tokens=add_special_tokens,
            truncation=False,
            return_attention_mask=False,
            return_token_type_ids=False,
        )
        input_ids = encoded.get("input_ids") if isinstance(encoded, dict) else None
        if input_ids is None:
            return _fallback_token_count(payload)
        if isinstance(input_ids, list) and input_ids and isinstance(input_ids[0], list):
            return len(input_ids[0])
        return len(input_ids)

    try:
        loop = asyncio.get_running_loop()
        return int(await loop.run_in_executor(None, _sync_count))
    except Exception:
        return _fallback_token_count(payload)


async def trim_text_to_embedding_token_limit(text: str, token_limit: int) -> str:
    """토큰 수가 한계를 넘으면 모델 토크나이저 기준으로 안전하게 축약합니다."""
    payload = str(text or "").strip()
    limit = max(8, int(token_limit))
    if not payload:
        return ""

    try:
        model = await _load_model()
    except Exception:
        approx_tokens = _fallback_token_count(payload)
        if approx_tokens <= limit:
            return payload
        ratio = max(0.1, min(1.0, limit / max(1, approx_tokens)))
        return payload[: max(32, int(len(payload) * ratio))].strip()

    tokenizer = getattr(model, "tokenizer", None)
    if tokenizer is None:
        return payload

    def _sync_trim() -> str:
        encoded = tokenizer(
            payload,
            add_special_tokens=True,
            truncation=False,
            return_attention_mask=False,
            return_token_type_ids=False,
        )
        input_ids = encoded.get("input_ids") if isinstance(encoded, dict) else None
        if not isinstance(input_ids, list):
            return payload
        if input_ids and isinstance(input_ids[0], list):
            ids = list(input_ids[0])
        else:
            ids = list(input_ids)
        if len(ids) <= limit:
            return payload
        trimmed_ids = ids[:limit]
        return tokenizer.decode(trimmed_ids, skip_special_tokens=True).strip()

    try:
        loop = asyncio.get_running_loop()
        trimmed = await loop.run_in_executor(None, _sync_trim)
        return trimmed or payload
    except Exception:
        return payload


async def get_embedding(text: str, prefix: str = "") -> np.ndarray | None:
    """문자열을 임베딩 벡터(float32)로 변환합니다.
    
    Args:
        text: 임베딩할 텍스트
        prefix: 모델에 따른 접두사 (예: "query: ", "passage: ")
    """
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

    # E5 모델의 경우 접두사 추가
    final_text = f"{prefix}{text}"

    def _sync_encode() -> np.ndarray:
        # [Safe] Truncation 인자를 제거 (모델이 지원하지 않음, 기본값에 맡김)
        vector = model.encode(final_text, normalize_embeddings=normalize)
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
    _CREATE_MEMORY_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS discord_memory_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        memory_id TEXT UNIQUE,
        anchor_message_id TEXT NOT NULL,
        server_id TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        owner_user_id TEXT,
        owner_user_name TEXT,
        memory_scope TEXT NOT NULL,
        memory_type TEXT NOT NULL,
        summary_text TEXT NOT NULL,
        memory_text TEXT NOT NULL,
        raw_context TEXT,
        source_message_ids TEXT,
        speaker_names TEXT,
        keyword_json TEXT,
        timestamp TEXT,
        embedding BLOB NOT NULL
    );
    """
    _CREATE_MEMORY_INDEX_SQL = (
        "CREATE INDEX IF NOT EXISTS idx_discord_memory_scope ON discord_memory_entries (server_id, channel_id, memory_scope, owner_user_id)",
        "CREATE INDEX IF NOT EXISTS idx_discord_memory_timestamp ON discord_memory_entries (timestamp DESC)",
    )

    def __init__(self, db_path: str):
        """Discord 임베딩 저장소를 초기화합니다.

        Args:
            db_path: 임베딩을 저장할 SQLite DB 파일 경로.
        """
        self.db_path = Path(db_path)
        self.backend = getattr(config, "DISCORD_EMBEDDING_BACKEND", "sqlite")
        self.tidb_table = getattr(config, "DISCORD_EMBEDDING_TIDB_TABLE", "discord_chat_embeddings")
        self._tidb_settings = _build_tidb_settings()
        self._init_lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        """DB 파일이 존재하지 않으면 생성하고 스키마를 준비합니다."""
        if self._initialized:
            return

        async with self._init_lock:
            if self._initialized:
                return

            if self.backend == "tidb":
                await self._initialize_tidb()
                self._initialized = True
                logger.info("Discord 임베딩 TiDB 초기화 완료: %s", self.tidb_table)
                return

            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("PRAGMA journal_mode=WAL;")
                await db.execute(self._CREATE_TABLE_SQL)
                for sql in self._CREATE_INDEX_SQL:
                    await db.execute(sql)
                await db.execute(self._CREATE_MEMORY_TABLE_SQL)
                for sql in self._CREATE_MEMORY_INDEX_SQL:
                    await db.execute(sql)
                await db.commit()
            self._initialized = True
            logger.info("Discord 임베딩 DB 초기화 완료: %s", self.db_path)

    async def _initialize_tidb(self) -> None:
        """TiDB에 Discord 임베딩 테이블과 메모리 엔트리 테이블을 생성합니다."""
        create_sql = f"""
        CREATE TABLE IF NOT EXISTS {self.tidb_table} (
            id BIGINT PRIMARY KEY AUTO_RANDOM,
            message_id VARCHAR(64) NOT NULL,
            server_id VARCHAR(64) NOT NULL,
            channel_id VARCHAR(64) NOT NULL,
            user_id VARCHAR(64) NOT NULL,
            user_name VARCHAR(255),
            message MEDIUMTEXT,
            timestamp VARCHAR(64),
            embedding BLOB NOT NULL,
            UNIQUE KEY uq_discord_embeddings_message (message_id),
            KEY idx_discord_embeddings_scuid (server_id, channel_id, user_id),
            KEY idx_discord_embeddings_timestamp (timestamp)
        )
        """
        await asyncio.to_thread(self._tidb_exec, create_sql, ())
        create_memory_sql = f"""
        CREATE TABLE IF NOT EXISTS discord_memory_entries (
            id BIGINT PRIMARY KEY AUTO_RANDOM,
            memory_id VARCHAR(191) NOT NULL,
            anchor_message_id VARCHAR(64) NOT NULL,
            server_id VARCHAR(64) NOT NULL,
            channel_id VARCHAR(64) NOT NULL,
            owner_user_id VARCHAR(64),
            owner_user_name VARCHAR(255),
            memory_scope VARCHAR(32) NOT NULL,
            memory_type VARCHAR(64) NOT NULL,
            summary_text MEDIUMTEXT NOT NULL,
            memory_text MEDIUMTEXT NOT NULL,
            raw_context MEDIUMTEXT,
            source_message_ids MEDIUMTEXT,
            speaker_names MEDIUMTEXT,
            keyword_json MEDIUMTEXT,
            timestamp VARCHAR(64),
            embedding BLOB NOT NULL,
            UNIQUE KEY uq_discord_memory_entries_memory_id (memory_id),
            KEY idx_discord_memory_scope (server_id, channel_id, memory_scope, owner_user_id),
            KEY idx_discord_memory_timestamp (timestamp)
        )
        """
        await asyncio.to_thread(self._tidb_exec, create_memory_sql, ())

    def _tidb_exec(self, query: str, params: tuple[Any, ...], *, fetch: bool = False) -> list[dict[str, Any]] | None:
        """TiDB 연결을 열고 SQL을 실행한 후 연결을 닫습니다.

        Args:
            query: 실행할 SQL 쿼리.
            params: 쿼리 파라미터 튜플.
            fetch: SELECT 결과를 반환할지 여부.
        """
        if pymysql is None or self._tidb_settings is None:
            raise RuntimeError("TiDB 연결 정보 또는 PyMySQL 패키지가 없습니다.")
        conn = pymysql.connect(**self._tidb_settings.to_connect_kwargs())
        try:
            with conn.cursor() as cursor:
                cursor.execute(query, params)
                rows = cursor.fetchall() if fetch and cursor.description is not None else None
            conn.commit()
            return rows
        finally:
            conn.close()

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
        if self.backend == "tidb":
            query = f"""
                INSERT INTO {self.tidb_table} (
                    message_id, server_id, channel_id, user_id, user_name, message, timestamp, embedding
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    user_name = VALUES(user_name),
                    message = VALUES(message),
                    timestamp = VALUES(timestamp),
                    embedding = VALUES(embedding)
            """
            await asyncio.to_thread(
                self._tidb_exec,
                query,
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
            return

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
        if self.backend == "tidb":
            query = (
                f"SELECT message_id, user_id, user_name, message, timestamp, embedding "
                f"FROM {self.tidb_table} WHERE server_id = %s AND channel_id = %s"
            )
            params: list[str | int] = [str(server_id), str(channel_id)]
            if user_id is not None:
                query += " AND user_id = %s"
                params.append(str(user_id))
            query += " ORDER BY timestamp DESC LIMIT %s"
            params.append(int(limit))
            rows = await asyncio.to_thread(self._tidb_exec, query, tuple(params), fetch=True)
            return rows or []

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

    async def upsert_memory_entry(
        self,
        *,
        memory_id: str,
        anchor_message_id: int,
        server_id: int,
        channel_id: int,
        owner_user_id: int | None,
        owner_user_name: str,
        memory_scope: str,
        memory_type: str,
        summary_text: str,
        memory_text: str,
        raw_context: str,
        source_message_ids: list[int],
        speaker_names: list[str],
        keywords: list[str],
        timestamp_iso: str,
        embedding: np.ndarray,
    ) -> None:
        """메모리 엔트리를 저장하거나 갱신합니다."""
        await self.initialize()
        if np is None:
            raise RuntimeError("numpy가 설치되어 있지 않아 임베딩을 저장할 수 없습니다.")
        embedding_bytes = np.asarray(embedding, dtype=np.float32).tobytes()
        source_json = json.dumps([int(item) for item in source_message_ids], ensure_ascii=False)
        speakers_json = json.dumps(list(speaker_names), ensure_ascii=False)
        keywords_json = json.dumps(list(keywords), ensure_ascii=False)
        owner_user_id_str = str(owner_user_id) if owner_user_id is not None else None

        if self.backend == "tidb":
            query = """
                INSERT INTO discord_memory_entries (
                    memory_id, anchor_message_id, server_id, channel_id, owner_user_id, owner_user_name,
                    memory_scope, memory_type, summary_text, memory_text, raw_context, source_message_ids,
                    speaker_names, keyword_json, timestamp, embedding
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    owner_user_id = VALUES(owner_user_id),
                    owner_user_name = VALUES(owner_user_name),
                    memory_scope = VALUES(memory_scope),
                    memory_type = VALUES(memory_type),
                    summary_text = VALUES(summary_text),
                    memory_text = VALUES(memory_text),
                    raw_context = VALUES(raw_context),
                    source_message_ids = VALUES(source_message_ids),
                    speaker_names = VALUES(speaker_names),
                    keyword_json = VALUES(keyword_json),
                    timestamp = VALUES(timestamp),
                    embedding = VALUES(embedding)
            """
            await asyncio.to_thread(
                self._tidb_exec,
                query,
                (
                    memory_id,
                    str(anchor_message_id),
                    str(server_id),
                    str(channel_id),
                    owner_user_id_str,
                    owner_user_name,
                    memory_scope,
                    memory_type,
                    summary_text,
                    memory_text,
                    raw_context,
                    source_json,
                    speakers_json,
                    keywords_json,
                    timestamp_iso,
                    embedding_bytes,
                ),
            )
            return

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO discord_memory_entries (
                    memory_id, anchor_message_id, server_id, channel_id, owner_user_id, owner_user_name,
                    memory_scope, memory_type, summary_text, memory_text, raw_context, source_message_ids,
                    speaker_names, keyword_json, timestamp, embedding
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    owner_user_id = excluded.owner_user_id,
                    owner_user_name = excluded.owner_user_name,
                    memory_scope = excluded.memory_scope,
                    memory_type = excluded.memory_type,
                    summary_text = excluded.summary_text,
                    memory_text = excluded.memory_text,
                    raw_context = excluded.raw_context,
                    source_message_ids = excluded.source_message_ids,
                    speaker_names = excluded.speaker_names,
                    keyword_json = excluded.keyword_json,
                    timestamp = excluded.timestamp,
                    embedding = excluded.embedding
                """,
                (
                    memory_id,
                    str(anchor_message_id),
                    str(server_id),
                    str(channel_id),
                    owner_user_id_str,
                    owner_user_name,
                    memory_scope,
                    memory_type,
                    summary_text,
                    memory_text,
                    raw_context,
                    source_json,
                    speakers_json,
                    keywords_json,
                    timestamp_iso,
                    embedding_bytes,
                ),
            )
            await db.commit()

    async def fetch_recent_memory_entries(
        self,
        *,
        server_id: int,
        channel_id: int,
        user_id: int | None = None,
        limit: int = 200,
    ) -> list[aiosqlite.Row]:
        """지정한 범위의 최신 메모리 엔트리를 반환합니다."""
        await self.initialize()
        user_id_str = str(user_id) if user_id is not None else None
        if self.backend == "tidb":
            if user_id_str is not None:
                query = """
                    SELECT memory_id,
                           anchor_message_id AS message_id,
                           owner_user_id AS user_id,
                           owner_user_name AS user_name,
                           memory_text AS message,
                           summary_text,
                           raw_context,
                           source_message_ids,
                           speaker_names,
                           keyword_json,
                           timestamp,
                           embedding,
                           memory_scope,
                           memory_type
                    FROM discord_memory_entries
                    WHERE server_id = %s
                      AND channel_id = %s
                      AND (memory_scope = 'channel' OR (memory_scope = 'user' AND owner_user_id = %s))
                    ORDER BY CASE WHEN memory_scope = 'user' THEN 0 ELSE 1 END, timestamp DESC
                    LIMIT %s
                """
                params: tuple[Any, ...] = (str(server_id), str(channel_id), user_id_str, int(limit))
            else:
                query = """
                    SELECT memory_id,
                           anchor_message_id AS message_id,
                           owner_user_id AS user_id,
                           owner_user_name AS user_name,
                           memory_text AS message,
                           summary_text,
                           raw_context,
                           source_message_ids,
                           speaker_names,
                           keyword_json,
                           timestamp,
                           embedding,
                           memory_scope,
                           memory_type
                    FROM discord_memory_entries
                    WHERE server_id = %s
                      AND channel_id = %s
                      AND memory_scope = 'channel'
                    ORDER BY timestamp DESC
                    LIMIT %s
                """
                params = (str(server_id), str(channel_id), int(limit))
            rows = await asyncio.to_thread(self._tidb_exec, query, params, fetch=True)
            return rows or []

        if user_id_str is not None:
            query = """
                SELECT memory_id,
                       anchor_message_id AS message_id,
                       owner_user_id AS user_id,
                       owner_user_name AS user_name,
                       memory_text AS message,
                       summary_text,
                       raw_context,
                       source_message_ids,
                       speaker_names,
                       keyword_json,
                       timestamp,
                       embedding,
                       memory_scope,
                       memory_type
                FROM discord_memory_entries
                WHERE server_id = ?
                  AND channel_id = ?
                  AND (memory_scope = 'channel' OR (memory_scope = 'user' AND owner_user_id = ?))
                ORDER BY CASE WHEN memory_scope = 'user' THEN 0 ELSE 1 END, timestamp DESC
                LIMIT ?
            """
            params: list[str | int] = [str(server_id), str(channel_id), user_id_str, int(limit)]
        else:
            query = """
                SELECT memory_id,
                       anchor_message_id AS message_id,
                       owner_user_id AS user_id,
                       owner_user_name AS user_name,
                       memory_text AS message,
                       summary_text,
                       raw_context,
                       source_message_ids,
                       speaker_names,
                       keyword_json,
                       timestamp,
                       embedding,
                       memory_scope,
                       memory_type
                FROM discord_memory_entries
                WHERE server_id = ?
                  AND channel_id = ?
                  AND memory_scope = 'channel'
                ORDER BY timestamp DESC
                LIMIT ?
            """
            params = [str(server_id), str(channel_id), int(limit)]
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
        return rows

    async def clear_memory_entries(self) -> None:
        """모든 메모리 엔트리를 삭제합니다."""
        await self.initialize()
        if self.backend == "tidb":
            await asyncio.to_thread(self._tidb_exec, "DELETE FROM discord_memory_entries", ())
            return
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM discord_memory_entries")
            await db.commit()

    async def delete_memory_entries(self, memory_ids: Iterable[str]) -> None:
        """지정한 memory_id 목록에 해당하는 메모리 엔트리를 삭제합니다."""
        ids = [str(item) for item in memory_ids if str(item).strip()]
        if not ids:
            return
        await self.initialize()
        if self.backend == "tidb":
            placeholders = ",".join(["%s"] * len(ids))
            query = f"DELETE FROM discord_memory_entries WHERE memory_id IN ({placeholders})"
            await asyncio.to_thread(self._tidb_exec, query, tuple(ids))
            return
        placeholders = ",".join("?" for _ in ids)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"DELETE FROM discord_memory_entries WHERE memory_id IN ({placeholders})",
                ids,
            )
            await db.commit()

    async def count_memory_entries(
        self,
        *,
        server_id: int | None = None,
        channel_id: int | None = None,
    ) -> int:
        """조건에 맞는 메모리 엔트리 개수를 반환합니다."""
        await self.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if server_id is not None:
            clauses.append("server_id = ?")
            params.append(str(server_id))
        if channel_id is not None:
            clauses.append("channel_id = ?")
            params.append(str(channel_id))
        where_sql = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        if self.backend == "tidb":
            sql = f"SELECT COUNT(*) AS cnt FROM discord_memory_entries{where_sql}".replace("?", "%s")
            row = await asyncio.to_thread(self._tidb_exec, sql, tuple(params), fetch=True)
            return int((row or [{}])[0].get("cnt", 0))

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(f"SELECT COUNT(*) FROM discord_memory_entries{where_sql}", params) as cursor:
                row = await cursor.fetchone()
        return int(row[0] if row else 0)

    async def delete_embeddings(self, message_ids: Iterable[int]) -> None:
        """지정한 메시지 ID 목록의 임베딩을 삭제합니다."""
        ids = [str(mid) for mid in message_ids]
        if not ids:
            return
        await self.initialize()
        if self.backend == "tidb":
            placeholders = ",".join(["%s"] * len(ids))
            query = f"DELETE FROM {self.tidb_table} WHERE message_id IN ({placeholders})"
            await asyncio.to_thread(self._tidb_exec, query, tuple(ids))
            return

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
        """Kakao 임베딩 저장소를 초기화합니다.

        Args:
            default_db_path: 기본 DB 파일 경로.
            server_map: 서버 ID별 DB 경로/방 키 매핑 정보.
        """
        self.backend = getattr(config, "KAKAO_STORE_BACKEND", "local")
        self.tidb_table = getattr(config, "KAKAO_TIDB_TABLE", "kakao_chunks")
        self._tidb_settings = _build_tidb_settings()
        self.default_db_path = Path(default_db_path) if default_db_path else None
        self.server_map: Dict[str, Dict[str, Any]] = {}
        for raw_server_id, meta in (server_map or {}).items():
            server_id = str(raw_server_id)
            db_path = meta.get("db_path") if isinstance(meta, dict) else None
            room_key = ""
            if isinstance(meta, dict):
                room_key = str(meta.get("room_key") or "").strip()
            if not room_key and db_path:
                room_key = Path(db_path).name
            if not db_path and not room_key:
                continue
            self.server_map[server_id] = {
                "path": Path(db_path) if db_path else None,
                "label": meta.get("label", "") if isinstance(meta, dict) else "",
                "room_key": room_key,
            }

        self._table_meta_cache: Dict[Path, Optional[_KakaoTableMeta]] = {}
        self._table_meta_lock = asyncio.Lock()
        self._vector_extension_candidates = self._build_vector_extension_candidates()
        self._vector_extension_warning_logged = False
        self._window_size = 3

        # Numpy Backend Cache: Path -> (vectors, metadata_list)
        self._numpy_cache: Dict[Path, Any] = {}
        self._numpy_lock = asyncio.Lock()

    async def _ensure_numpy_backend(self, path: Path) -> bool:
        """경로가 numpy 파일을 포함한 디렉터리인지 확인하고 필요 시 로드합니다."""
        """Check if path is a directory with numpy files and load them if needed."""
        if not path.is_dir():
            return False
            
        if path in self._numpy_cache:
            return True
            
        async with self._numpy_lock:
            if path in self._numpy_cache:
                return True
                
            vec_path = path / "vectors.npy"
            meta_path = path / "metadata.json"
            
            if not vec_path.exists() or not meta_path.exists():
                return False
                
            try:
                if np is None:
                    logger.warning("Numpy required for offline embeddings at %s", path)
                    return False
                    
                # Load in thread to avoid blocking loop
                loop = asyncio.get_running_loop()
                vectors, metadata = await loop.run_in_executor(None, self._load_numpy_files, vec_path, meta_path)
                
                self._numpy_cache[path] = (vectors, metadata)
                logger.info(f"Loaded offline embeddings from {path}: {len(metadata)} items")
                return True
            except Exception as e:
                logger.error(f"Failed to load offline embeddings from {path}: {e}")
                return False

    @staticmethod
    def _load_numpy_files(vec_path: Path, meta_path: Path):
        """numpy 벡터 파일과 JSON 메타데이터 파일을 로드합니다."""
        vectors = np.load(vec_path)
        with open(meta_path, 'r', encoding='utf-8') as f:
            metadata = json.load(f)
        return vectors, metadata

    async def fetch_recent_embeddings(
        self,
        server_ids: Iterable[str],
        limit: int = 200,
        query_vector: "np.ndarray" | None = None,
    ) -> list[Dict[str, Any]]:
        """서버 ID 후보 목록에 해당하는 Kakao 임베딩 레코드를 읽어옵니다."""
        if self.backend == "tidb":
            return await self._fetch_remote_embeddings(server_ids, limit=limit, query_vector=query_vector)

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
        if query_vector is not None:
            per_target_limit = max(1, min(int(limit), 50))
        else:
            per_target_limit = max(1, int(limit))
        for path, label, matched_id in targets:
            if query_vector is not None:
                rows = await self._vector_search(path, per_target_limit, query_vector)
            else:
                rows = await self._fetch_from_path(path, label, per_target_limit)
            for row in rows:
                row.setdefault("label", label or path.stem)
                row.setdefault("matched_server_id", matched_id)
                row.setdefault("db_path", str(path))
                results.append(row)
        return results

    async def _fetch_remote_embeddings(
        self,
        server_ids: Iterable[str],
        *,
        limit: int,
        query_vector: "np.ndarray" | None,
    ) -> list[Dict[str, Any]]:
        """TiDB에서 방 키 기준으로 임베딩 레코드를 읽어옵니다."""
        if pymysql is None or self._tidb_settings is None:
            logger.warning("TiDB Kakao 저장소를 사용할 수 없습니다.")
            return []

        targets: list[tuple[str, str, str]] = []
        seen_room_keys: set[str] = set()
        for candidate in server_ids:
            meta = self.server_map.get(str(candidate))
            if not meta:
                continue
            room_key = str(meta.get("room_key") or "").strip()
            if not room_key or room_key in seen_room_keys:
                continue
            seen_room_keys.add(room_key)
            targets.append((room_key, meta.get("label", ""), str(candidate)))

        results: list[Dict[str, Any]] = []
        per_target_limit = max(1, min(int(limit), 50)) if query_vector is not None else max(1, int(limit))
        for room_key, label, matched_id in targets:
            if query_vector is not None:
                rows = await asyncio.to_thread(self._remote_vector_search, room_key, query_vector, per_target_limit)
            else:
                rows = await asyncio.to_thread(self._remote_recent_fetch, room_key, per_target_limit)
            for row in rows:
                row.setdefault("label", label or room_key)
                row.setdefault("matched_server_id", matched_id)
                row.setdefault("db_path", f"tidb:{room_key}")
                results.append(row)
        return results

    def _remote_recent_fetch(self, room_key: str, limit: int) -> list[Dict[str, Any]]:
        """TiDB에서 특정 방의 최신 레코드를 가져옵니다."""
        conn = pymysql.connect(**self._tidb_settings.to_connect_kwargs())
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT chunk_id AS message_id,
                           text_long AS message,
                           start_date AS timestamp,
                           'Merged Context' AS speaker
                    FROM {self.tidb_table}
                    WHERE room_key = %s
                    ORDER BY start_date DESC, chunk_id DESC
                    LIMIT %s
                    """,
                    (room_key, int(limit)),
                )
                rows = cursor.fetchall()
            return [dict(row, context_window=[]) for row in rows]
        finally:
            conn.close()

    def _remote_vector_search(self, room_key: str, query_vector: "np.ndarray", limit: int) -> list[Dict[str, Any]]:
        """TiDB에서 벡터 유사도 기반으로 레코드를 검색합니다."""
        vector_literal = _vector_literal(query_vector)
        sql = f"""
            SELECT chunk_id AS message_id,
                   text_long AS message,
                   start_date AS timestamp,
                   'Merged Context' AS speaker,
                   VEC_COSINE_DISTANCE(embedding, %s) AS distance,
                   1.0 - VEC_COSINE_DISTANCE(embedding, %s) AS score
            FROM {self.tidb_table}
            WHERE room_key = %s
            ORDER BY VEC_COSINE_DISTANCE(embedding, %s) ASC
            LIMIT %s
        """
        conn = pymysql.connect(**self._tidb_settings.to_connect_kwargs())
        try:
            with conn.cursor() as cursor:
                cursor.execute(sql, (vector_literal, vector_literal, room_key, vector_literal, int(limit)))
                rows = cursor.fetchall()
            return [dict(row, context_window=[]) for row in rows]
        finally:
            conn.close()

    def _build_vector_extension_candidates(self) -> list[str]:
        """config와 기본값을 바탕으로 SQLite 벡터 확장 후보 목록을 생성합니다."""
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
        """SQLite 벡터 확장을 로드합니다. 성공 시 True, 실패 시 False를 반환합니다."""
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
        """SQLite 또는 numpy 백엔드에서 최신 임베딩 레코드를 읽어옵니다."""
        # 1. Numpy Backend Check
        if await self._ensure_numpy_backend(path):
            return self._fetch_from_numpy(path, limit)

        # 2. SQLite Backend
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

    async def _vector_search(
        self,
        path: Path,
        limit: int,
        query_vector: "np.ndarray",
    ) -> list[Dict[str, Any]]:
        """로컬 DB에서 벡터 유사도 기반으로 레코드를 검색합니다."""
        if np is None:
            logger.debug("numpy 미설치로 Kakao 벡터 검색을 건너뜁니다: %s", path)
            return []

        try:
            vector_blob = query_vector.astype(np.float32).tobytes()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Kakao 벡터 검색용 쿼리 벡터 직렬화 실패: %s", exc)
            return []

        try:
            # 1. Numpy Backend
            if await self._ensure_numpy_backend(path):
                return self._vector_search_numpy(path, query_vector, limit)

            async with aiosqlite.connect(path) as db:
                await self._load_vector_extension(db)
                db.row_factory = aiosqlite.Row
                query = (
                    "SELECT m.id AS message_id, "
                    "       m.message AS message, "
                    "       v.embedding AS embedding, "
                    "       m.timestamp AS timestamp, "
                    "       m.user_name AS speaker, "
                    "       v.distance AS distance "
                    "FROM vss_kakao AS v "
                    "JOIN kakao_messages AS m ON m.id = v.message_id "
                    "WHERE v.embedding MATCH ? AND v.k = ? "
                    "ORDER BY v.distance ASC LIMIT ?"
                )
                top_k = max(1, int(limit))
                async with db.execute(query, (vector_blob, top_k, top_k)) as cursor:
                    rows = [dict(row) for row in await cursor.fetchall()]

                for row in rows:
                    message_id = row.get("message_id")
                    if not isinstance(message_id, int):
                        continue
                    row["context_window"] = await self._fetch_message_window(db, message_id)
                return rows
        except aiosqlite.Error as exc:
            logger.error("Kakao 벡터 검색 중 오류: %s", exc, exc_info=True)
        return []

        window_rows: list[dict[str, str]] = []
        for row in rows:
            window_rows.append(
                {
                    "id": row[0],
                    "user_name": row[1],
                    "message": row[2],
                }
            )
        return window_rows

    def _fetch_from_numpy(self, path: Path, limit: int) -> list[Dict[str, Any]]:
        """numpy 백엔드에서 최근 N개 아이템을 반환합니다."""
        """Fetch recent items from numpy store (just returns last N items)."""
        vectors, metadata = self._numpy_cache[path]
        
        # Taking last N items
        slice_start = max(0, len(metadata) - limit)
        recent_meta = metadata[slice_start:]
        recent_meta.reverse() # Newest first
        
        results = []
        for m in recent_meta:
            results.append({
                "message_id": m.get("id"),
                "message": m.get("text", ""),
                "timestamp": m.get("start_date", ""),
                "speaker": "Merged Context", # Offline chunks don't have single speaker
                "embedding": None, # Not strictly needed for display
                "context_window": [], # Already chunked
            })
        return results

    def _vector_search_numpy(self, path: Path, query_vector: np.ndarray, limit: int) -> list[Dict[str, Any]]:
        """numpy 백엔드에서 코사인 유사도 기반으로 Top-K 벡터 검색을 수행합니다."""
        vectors, metadata = self._numpy_cache[path]
        
        # Cosine Similarity: (A . B) / (|A| * |B|)
        # encoding normalize_embeddings=True used in generation, so |V| should be ~1
        # query_vector also likely normalized if from get_embedding
        
        norm_q = np.linalg.norm(query_vector)
        # norm_v is pre-calculated or assumed 1 if normalized during save.
        # But let's compute to be safe or assume optimized script.
        # For speed in python, we'll assume vectors are normalized or just do dot if we trust it.
        # Let's do full cosine for safety.
        
        norm_v = np.linalg.norm(vectors, axis=1)
        norm_v[norm_v == 0] = 1e-10
        
        similarities = np.dot(vectors, query_vector) / (norm_v * norm_q)
        
        # Top K
        top_indices = np.argsort(similarities)[::-1][:limit]
        
        results = []
        for idx in top_indices:
            score = similarities[idx]
            meta = metadata[idx]
            
            results.append({
                "message_id": meta.get("id"),
                "message": meta.get("text", ""),
                "timestamp": meta.get("start_date", ""),
                "speaker": "Merged Context",
                "distance": 1.0 - score, # Convert similarity to distance-like for compatibility
                "score": float(score),
                "context_window": [], # It's already a chunk
            })
            
        return results

    async def _get_or_detect_table_meta(
        self,
        path: Path,
        connection: aiosqlite.Connection | None = None,
    ) -> Optional[_KakaoTableMeta]:
        """캐시된 테이블 메타정보를 반환하거나, 없으면 DB에서 감지 후 캐싱합니다."""
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
        """SQLite DB 스키마를 분석하여 텍스트/임베딩/타임스탬프/발화자 컬럼을 감지합니다."""
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
        """지정한 테이블의 컬럼명과 타입을 조회합니다."""
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
        """후보 컬럼명 중 테이블에 존재하고 타입이 일치하는 컬럼을 선택합니다."""
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

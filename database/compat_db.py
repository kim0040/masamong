# -*- coding: utf-8 -*-
"""SQLite/TiDB 겸용 비동기 DB 호환 레이어."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from typing import Any, Iterable

try:
    import pymysql
    from pymysql.cursors import DictCursor
except ModuleNotFoundError:  # pragma: no cover
    pymysql = None  # type: ignore
    DictCursor = None  # type: ignore

import aiosqlite


class CompatDBError(aiosqlite.Error):
    """TiDB 에러를 aiosqlite 스타일로 감싸기 위한 기본 예외."""


class CompatOperationalError(CompatDBError, aiosqlite.OperationalError):
    """연결/실행 계열 운영 오류."""


class CompatRow:
    """SQLite Row와 유사하게 int/str 인덱싱을 모두 지원하는 행 객체."""

    def __init__(self, data: dict[str, Any]):
        self._mapping = dict(data)
        self._columns = list(data.keys())
        self._values = [data[name] for name in self._columns]

    def __getitem__(self, key: int | str) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return self._mapping[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def get(self, key: str, default: Any = None) -> Any:
        return self._mapping.get(key, default)

    def keys(self):
        return self._mapping.keys()

    def items(self):
        return self._mapping.items()

    def values(self):
        return self._mapping.values()

    def as_dict(self) -> dict[str, Any]:
        return dict(self._mapping)


class BufferedCursor:
    """결과를 메모리에 버퍼링한 비동기 커서."""

    def __init__(self, rows: list[CompatRow], rowcount: int = 0, lastrowid: int | None = None):
        self._rows = rows
        self._index = 0
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    async def fetchone(self) -> CompatRow | None:
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    async def fetchall(self) -> list[CompatRow]:
        if self._index == 0:
            self._index = len(self._rows)
            return list(self._rows)
        remaining = self._rows[self._index :]
        self._index = len(self._rows)
        return remaining

    async def __aenter__(self) -> "BufferedCursor":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class QueryHandle:
    """`await db.execute(...)` 와 `async with db.execute(...)` 를 모두 지원."""

    def __init__(self, db: "TiDBConnection", query: str, params: Iterable[Any] | None = None):
        self._db = db
        self._query = query
        self._params = tuple(params or ())
        self._cursor: BufferedCursor | None = None

    async def _ensure(self) -> BufferedCursor:
        if self._cursor is None:
            self._cursor = await self._db._execute_buffered(self._query, self._params)
        return self._cursor

    def __await__(self):
        return self._ensure().__await__()

    async def __aenter__(self) -> BufferedCursor:
        return await self._ensure()

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


@dataclass(frozen=True)
class TiDBSettings:
    host: str
    port: int
    user: str
    password: str
    database: str
    ssl_ca: str | None = None
    ssl_verify_identity: bool = True

    @classmethod
    def from_env(cls) -> "TiDBSettings":
        return cls(
            host=os.environ.get("MASAMONG_DB_HOST", "").strip(),
            port=int(os.environ.get("MASAMONG_DB_PORT", "4000")),
            user=os.environ.get("MASAMONG_DB_USER", "").strip(),
            password=os.environ.get("MASAMONG_DB_PASSWORD", ""),
            database=os.environ.get("MASAMONG_DB_NAME", "masamong").strip() or "masamong",
            ssl_ca=os.environ.get("MASAMONG_DB_SSL_CA", "").strip() or None,
            ssl_verify_identity=os.environ.get("MASAMONG_DB_SSL_VERIFY_IDENTITY", "true").strip().lower() in {"1", "true", "yes", "on"},
        )

    def to_connect_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "database": self.database,
            "charset": "utf8mb4",
            "autocommit": False,
            "cursorclass": DictCursor,
        }
        if self.ssl_ca:
            kwargs["ssl"] = {"ca": self.ssl_ca}
        return kwargs


_INSERT_OR_IGNORE_RE = re.compile(r"INSERT\s+OR\s+IGNORE", re.IGNORECASE)
_INSERT_OR_REPLACE_RE = re.compile(r"INSERT\s+OR\s+REPLACE", re.IGNORECASE)


def rewrite_sql_for_tidb(query: str) -> str:
    """현재 코드의 SQLite 문법 일부를 TiDB 문법으로 치환."""
    sql = _INSERT_OR_IGNORE_RE.sub("INSERT IGNORE", query)
    sql = _INSERT_OR_REPLACE_RE.sub("REPLACE", sql)
    sql = sql.replace("datetime('now')", "CURRENT_TIMESTAMP(6)")
    sql = sql.replace('datetime("now")', "CURRENT_TIMESTAMP(6)")
    return sql.replace("?", "%s")


def split_sql_script(script: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    for raw_line in script.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("--"):
            continue
        current.append(raw_line)
        if line.endswith(";"):
            statements.append("\n".join(current).strip().rstrip(";"))
            current = []
    if current:
        statements.append("\n".join(current).strip().rstrip(";"))
    return [stmt for stmt in statements if stmt]


class TiDBConnection:
    """단일 PyMySQL 연결을 aiosqlite 스타일로 감싼 어댑터."""

    def __init__(self, settings: TiDBSettings):
        self.settings = settings
        self.row_factory = aiosqlite.Row
        self._conn: Any = None
        self._lock = asyncio.Lock()
        self.backend = "tidb"

    async def connect(self) -> "TiDBConnection":
        if pymysql is None:
            raise CompatOperationalError("PyMySQL 패키지가 필요합니다.")
        self._conn = await asyncio.to_thread(pymysql.connect, **self.settings.to_connect_kwargs())
        return self

    async def _ensure_connected(self) -> None:
        if self._conn is None:
            await self.connect()
            return
        try:
            await asyncio.to_thread(self._conn.ping, True)
        except Exception as exc:  # pragma: no cover
            raise CompatOperationalError(str(exc)) from exc

    async def _execute_buffered(self, query: str, params: Iterable[Any] | None = None) -> BufferedCursor:
        await self._ensure_connected()
        sql = rewrite_sql_for_tidb(query)
        bind = tuple(params or ())
        async with self._lock:
            try:
                return await asyncio.to_thread(self._execute_sync, sql, bind)
            except Exception as exc:  # pragma: no cover
                raise CompatOperationalError(str(exc)) from exc

    def _execute_sync(self, sql: str, params: tuple[Any, ...]) -> BufferedCursor:
        assert self._conn is not None
        with self._conn.cursor() as cursor:
            cursor.execute(sql, params)
            rows: list[CompatRow] = []
            if cursor.description is not None:
                raw_rows = cursor.fetchall()
                rows = [CompatRow(row) for row in raw_rows]
            return BufferedCursor(rows, rowcount=cursor.rowcount, lastrowid=cursor.lastrowid)

    def execute(self, query: str, params: Iterable[Any] | None = None) -> QueryHandle:
        return QueryHandle(self, query, params)

    async def executemany(self, query: str, seq_of_params: Iterable[Iterable[Any]]) -> None:
        await self._ensure_connected()
        sql = rewrite_sql_for_tidb(query)
        values = [tuple(item) for item in seq_of_params]
        async with self._lock:
            try:
                await asyncio.to_thread(self._executemany_sync, sql, values)
            except Exception as exc:  # pragma: no cover
                raise CompatOperationalError(str(exc)) from exc

    def _executemany_sync(self, sql: str, values: list[tuple[Any, ...]]) -> None:
        assert self._conn is not None
        with self._conn.cursor() as cursor:
            cursor.executemany(sql, values)

    async def executescript(self, script: str) -> None:
        for statement in split_sql_script(script):
            await self.execute(statement)

    async def commit(self) -> None:
        if self._conn is None:
            return
        async with self._lock:
            try:
                await asyncio.to_thread(self._conn.commit)
            except Exception as exc:  # pragma: no cover
                raise CompatOperationalError(str(exc)) from exc

    async def rollback(self) -> None:
        if self._conn is None:
            return
        async with self._lock:
            try:
                await asyncio.to_thread(self._conn.rollback)
            except Exception as exc:  # pragma: no cover
                raise CompatOperationalError(str(exc)) from exc

    async def close(self) -> None:
        if self._conn is None:
            return
        async with self._lock:
            await asyncio.to_thread(self._conn.close)
        self._conn = None


async def connect_main_db(backend: str, *, sqlite_path: str | None = None, tidb_settings: TiDBSettings | None = None):
    """환경에 따라 SQLite 또는 TiDB 연결을 생성한다."""
    backend_norm = (backend or "sqlite").strip().lower()
    if backend_norm == "tidb":
        settings = tidb_settings or TiDBSettings.from_env()
        return await TiDBConnection(settings).connect()
    if not sqlite_path:
        raise CompatOperationalError("SQLite 경로가 필요합니다.")
    conn = await aiosqlite.connect(sqlite_path)
    conn.row_factory = aiosqlite.Row
    return conn


async def get_table_columns(db: Any, table_name: str) -> list[str]:
    """백엔드에 따라 테이블 컬럼 목록을 반환한다."""
    backend = getattr(db, "backend", "sqlite")
    if backend == "tidb":
        async with db.execute(
            """
            SELECT COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
            """,
            (table_name,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async with db.execute(f"PRAGMA table_info({table_name})") as cursor:
        rows = await cursor.fetchall()
    return [row[1] for row in rows]

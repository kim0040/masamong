# -*- coding: utf-8 -*-
"""SQLite/TiDB 겸용 비동기 DB 호환 레이어."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import ssl
import time
import weakref
from dataclasses import dataclass
from typing import Any, Iterable

try:
    import pymysql
    from pymysql.cursors import DictCursor
except ModuleNotFoundError:  # pragma: no cover
    pymysql = None  # type: ignore
    DictCursor = None  # type: ignore

import aiosqlite


logger = logging.getLogger(__name__)

# 분류되지 않은 오류가 같은 연결에서 이 횟수만큼 연속 발생하면 연결을 폐기한다.
_UNCLASSIFIED_FAILURE_LIMIT = 5

# 재연결이 실패했을 때 다음 시도까지의 최소 대기(초)와 상한. DB가 완전히 내려간
# 동안 여러 background loop가 매 주기마다 TCP 연결을 새로 시도하면 원격 TiDB의
# 연결 수/RU를 불필요하게 소모하므로 지수 백오프로 시도 간격을 넓힌다.
_RECONNECT_BACKOFF_BASE_SECONDS = 2.0
_RECONNECT_BACKOFF_MAX_SECONDS = 60.0


class CompatDBError(aiosqlite.Error):
    """TiDB 에러를 aiosqlite 스타일로 감싸기 위한 기본 예외."""


class CompatOperationalError(CompatDBError, aiosqlite.OperationalError):
    """연결/실행 계열 운영 오류."""


class CompatRow:
    """SQLite Row와 유사하게 int/str 인덱싱을 모두 지원하는 행 객체."""

    def __init__(self, data: dict[str, Any]):
        """주어진 딕셔너리로 행 객체를 초기화합니다.

        Args:
            data: 컬럼명→값 매핑
        """
        self._mapping = dict(data)
        self._columns = list(data.keys())
        self._values = [data[name] for name in self._columns]

    def __getitem__(self, key: int | str) -> Any:
        """정수 인덱스 또는 컬럼명 문자열로 값에 접근합니다."""
        if isinstance(key, int):
            return self._values[key]
        return self._mapping[key]

    def __iter__(self):
        """값 목록에 대한 이터레이터를 반환합니다."""
        return iter(self._values)

    def __len__(self) -> int:
        """컬럼 개수를 반환합니다."""
        return len(self._values)

    def get(self, key: str, default: Any = None) -> Any:
        """딕셔너리 스타일로 값에 접근하며, 키가 없으면 default를 반환합니다."""
        return self._mapping.get(key, default)

    def keys(self):
        """컬럼명 키 뷰를 반환합니다."""
        return self._mapping.keys()

    def items(self):
        """(컬럼명, 값) 쌍의 아이템 뷰를 반환합니다."""
        return self._mapping.items()

    def values(self):
        """값 뷰를 반환합니다."""
        return self._mapping.values()

    def as_dict(self) -> dict[str, Any]:
        """행 데이터를 일반 dict로 변환합니다."""
        return dict(self._mapping)


class BufferedCursor:
    """결과를 메모리에 버퍼링한 비동기 커서."""

    def __init__(self, rows: list[CompatRow], rowcount: int = 0, lastrowid: int | None = None):
        """버퍼링된 행 목록, rowcount, lastrowid로 커서를 초기화합니다."""
        self._rows = rows
        self._index = 0
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    async def fetchone(self) -> CompatRow | None:
        """버퍼에서 다음 행 하나를 반환하거나, 더 이상 없으면 None을 반환합니다."""
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    async def fetchall(self) -> list[CompatRow]:
        """버퍼에서 남은 모든 행을 리스트로 반환합니다."""
        if self._index == 0:
            self._index = len(self._rows)
            return list(self._rows)
        remaining = self._rows[self._index :]
        self._index = len(self._rows)
        return remaining

    async def __aenter__(self) -> "BufferedCursor":
        """비동기 컨텍스트 매니저 진입."""
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        """비동기 컨텍스트 매니저 종료 (예외를 전파)."""
        return False


class QueryHandle:
    """`await db.execute(...)` 와 `async with db.execute(...)` 를 모두 지원."""

    def __init__(self, db: "TiDBConnection", query: str, params: Iterable[Any] | None = None):
        """TiDB 연결, SQL 쿼리, 파라미터로 쿼리 핸들을 초기화합니다."""
        self._db = db
        self._query = query
        self._params = tuple(params or ())
        self._cursor: BufferedCursor | None = None

    async def _ensure(self) -> BufferedCursor:
        """실행되지 않았다면 지연 실행하고, 버퍼링된 커서를 반환합니다."""
        if self._cursor is None:
            self._cursor = await self._db._execute_buffered(self._query, self._params)
        return self._cursor

    def __await__(self):
        """`await db.execute(...)` 호출을 지원합니다."""
        return self._ensure().__await__()

    async def __aenter__(self) -> BufferedCursor:
        """`async with db.execute(...)` 진입 시 실행하고 커서를 반환합니다."""
        return await self._ensure()

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        """비동기 컨텍스트 매니저 종료."""
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
    require_tls: bool = False
    connect_timeout: int = 10
    read_timeout: int = 30
    write_timeout: int = 30
    conn_max_lifetime_seconds: int = 600

    @classmethod
    def from_env(cls) -> "TiDBSettings":
        """환경 변수에서 TiDB 접속 설정을 읽어 TiDBSettings 인스턴스를 생성합니다."""
        ssl_ca = os.environ.get("MASAMONG_DB_SSL_CA", "").strip() or None
        strict_remote = os.environ.get(
            "MASAMONG_DB_STRICT_REMOTE_ONLY", "false"
        ).strip().lower() in {"1", "true", "yes", "on"}
        require_tls = strict_remote or os.environ.get(
            "MASAMONG_DB_REQUIRE_TLS", "false"
        ).strip().lower() in {"1", "true", "yes", "on"}
        ssl_verify_identity = os.environ.get(
            "MASAMONG_DB_SSL_VERIFY_IDENTITY", "true"
        ).strip().lower() in {"1", "true", "yes", "on"}
        if strict_remote and not ssl_verify_identity:
            raise ValueError(
                "strict remote 모드에서는 TLS hostname 검증을 끌 수 없습니다."
            )

        # 예전에는 미설정 시 "masamong"으로 폴백했다. 그 이름은 운영 중인 Masamo
        # 인스턴스의 실제 DB라서, 프로필 없이 실행된 코드가 조용히 운영 데이터를
        # 대상으로 삼을 수 있었다. 대상 DB는 항상 명시하게 한다.
        database = os.environ.get("MASAMONG_DB_NAME", "").strip()
        if not database:
            raise ValueError(
                "MASAMONG_DB_NAME이 없어 TiDB 대상 DB를 결정할 수 없습니다. "
                "운영 DB로 암묵적으로 폴백하지 않으므로 대상 DB를 명시하세요."
            )

        return cls(
            host=os.environ.get("MASAMONG_DB_HOST", "").strip(),
            port=int(os.environ.get("MASAMONG_DB_PORT", "4000")),
            user=os.environ.get("MASAMONG_DB_USER", "").strip(),
            password=os.environ.get("MASAMONG_DB_PASSWORD", ""),
            database=database,
            ssl_ca=ssl_ca,
            ssl_verify_identity=ssl_verify_identity,
            require_tls=require_tls,
            connect_timeout=max(1, int(os.environ.get("MASAMONG_DB_CONNECT_TIMEOUT", "10"))),
            read_timeout=max(1, int(os.environ.get("MASAMONG_DB_READ_TIMEOUT", "30"))),
            write_timeout=max(1, int(os.environ.get("MASAMONG_DB_WRITE_TIMEOUT", "30"))),
            conn_max_lifetime_seconds=max(60, int(os.environ.get("MASAMONG_DB_CONN_MAX_LIFETIME_SECONDS", "600"))),
        )

    def to_connect_kwargs(self) -> dict[str, Any]:
        """PyMySQL 연결을 위한 키워드 인자 딕셔너리를 생성합니다."""
        kwargs: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "database": self.database,
            "charset": "utf8mb4",
            "autocommit": False,
            "cursorclass": DictCursor,
            "connect_timeout": int(self.connect_timeout),
            "read_timeout": int(self.read_timeout),
            "write_timeout": int(self.write_timeout),
        }
        if self.require_tls and not self.ssl_ca:
            raise ValueError("TLS 필수 모드인데 MASAMONG_DB_SSL_CA가 없습니다.")
        if self.ssl_ca:
            if not os.path.isfile(self.ssl_ca):
                raise ValueError(f"TiDB CA 파일을 찾을 수 없습니다: {self.ssl_ca}")
            kwargs["ssl"] = {
                "ca": self.ssl_ca,
                "check_hostname": bool(self.ssl_verify_identity),
                "verify_mode": ssl.CERT_REQUIRED,
            }
        return kwargs


_INSERT_OR_IGNORE_RE = re.compile(r"INSERT\s+OR\s+IGNORE", re.IGNORECASE)
_INSERT_OR_REPLACE_RE = re.compile(r"INSERT\s+OR\s+REPLACE", re.IGNORECASE)
_READ_ONLY_RETRY_KEYWORDS = frozenset(
    {"SELECT", "SHOW", "DESCRIBE", "DESC"}
)


def _leading_sql_keyword(sql: str) -> str:
    """선행 공백/주석을 제외한 첫 SQL 키워드를 보수적으로 반환한다."""
    remaining = str(sql or "").lstrip()
    while remaining:
        if remaining.startswith("--") or remaining.startswith("#"):
            newline = remaining.find("\n")
            if newline < 0:
                return ""
            remaining = remaining[newline + 1 :].lstrip()
            continue
        if remaining.startswith("/*"):
            end = remaining.find("*/", 2)
            if end < 0:
                return ""
            remaining = remaining[end + 2 :].lstrip()
            continue
        break
    match = re.match(r"([A-Za-z]+)", remaining)
    return match.group(1).upper() if match else ""


def _is_safe_read_retry(sql: str) -> bool:
    """연결 단절 후 새 트랜잭션에서 재실행해도 데이터 쓰기가 없는 문장인지 판별한다."""
    return _leading_sql_keyword(sql) in _READ_ONLY_RETRY_KEYWORDS


def rewrite_sql_for_tidb(query: str) -> str:
    """현재 코드의 SQLite 문법 일부를 TiDB 문법으로 치환."""
    sql = _INSERT_OR_IGNORE_RE.sub("INSERT IGNORE", query)
    sql = _INSERT_OR_REPLACE_RE.sub("REPLACE", sql)
    sql = sql.replace("datetime('now')", "CURRENT_TIMESTAMP(6)")
    sql = sql.replace('datetime("now")', "CURRENT_TIMESTAMP(6)")
    return sql.replace("?", "%s")


def split_sql_script(script: str) -> list[str]:
    """SQL 스크립트를 개별 구문(statement) 목록으로 분할합니다.

    주석(`--`)과 빈 줄은 무시하며, 세미콜론(`;`)을 기준으로 구문을 나눕니다.

    Args:
        script: 분할할 SQL 스크립트 문자열

    Returns:
        개별 SQL 구문 리스트
    """
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
        """TiDB 연결 설정을 받아 어댑터를 초기화합니다."""
        self.settings = settings
        self.row_factory = aiosqlite.Row
        self._conn: Any = None
        # ``_lock``은 PyMySQL 패킷 단위 직렬화만 담당한다. autocommit=False인
        # 단일 연결에서 execute와 commit 사이에 다른 Discord task가 끼어들면
        # 서로의 쓰기를 함께 commit/rollback할 수 있으므로, 논리 트랜잭션 전체를
        # 별도 gate로 묶는다.
        self._lock = asyncio.Lock()
        self._transaction_gate = asyncio.Lock()
        self._transaction_owner: asyncio.Task[Any] | None = None
        self._transaction_owner_callbacks: weakref.WeakSet[
            asyncio.Task[Any]
        ] = weakref.WeakSet()
        self.backend = "tidb"
        self._connected_at_monotonic: float | None = None
        self._transaction_dirty = False
        # 연결이 더 이상 쓸 수 없다고 판정된 상태. 미확정 쓰기가 남아 있어도
        # 서버 쪽 세션은 이미 사라졌으므로 다음 사용 시점에 반드시 재연결한다.
        self._conn_broken = False
        # 분류되지 않은 형태의 장애로 같은 연결이 계속 실패할 때를 대비한
        # 백스톱. 연속 실패가 임계값을 넘으면 연결을 폐기 대상으로 본다.
        self._consecutive_failures = 0
        # 재연결 실패 시 지수 백오프 상태.
        self._reconnect_failures = 0
        self._reconnect_blocked_until: float | None = None

    async def connect(self) -> "TiDBConnection":
        """PyMySQL 연결을 생성하고 연결 시각을 기록합니다."""
        if pymysql is None:
            raise CompatOperationalError("PyMySQL 패키지가 필요합니다.")
        self._conn = await asyncio.to_thread(pymysql.connect, **self.settings.to_connect_kwargs())
        self._connected_at_monotonic = time.monotonic()
        self._transaction_dirty = False
        self._conn_broken = False
        self._consecutive_failures = 0
        return self

    def _is_connection_stale(self) -> bool:
        """연결 유지 시간이 최대 수명을 초과했는지 확인합니다."""
        if self._connected_at_monotonic is None:
            return False
        return (time.monotonic() - self._connected_at_monotonic) >= float(self.settings.conn_max_lifetime_seconds)

    @staticmethod
    def _is_retryable_disconnect(exc: Exception) -> bool:
        """예외 타입/메시지/코드로 재연결 가능한 연결 끊김인지 판별합니다.

        PyMySQL은 소켓이 이미 닫힌 연결에 명령을 보내면 코드도 메시지도 없는
        ``InterfaceError(0, "")``를 던진다. 코드 집합만 검사하면 이 예외가 걸러지지
        않아 죽은 연결을 영구히 재사용하게 되므로 타입으로 먼저 판정한다.
        """
        # InterfaceError는 드라이버 레벨에서 연결을 더 쓸 수 없다는 신호다.
        if pymysql is not None and isinstance(exc, pymysql.err.InterfaceError):
            return True
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return True
        msg = str(exc).lower()
        if any(
            token in msg
            for token in (
                "lost connection",
                "server has gone away",
                "connection was killed",
                "connection reset",
                "broken pipe",
                "already closed",
                "connection is closed",
            )
        ):
            return True
        code = None
        if getattr(exc, "args", None):
            try:
                code = int(exc.args[0])
            except Exception:
                code = None
        return code in {2006, 2013, 2055}

    def _note_failure(self, exc: Exception) -> bool:
        """실행 실패를 기록하고 연결 폐기가 필요한지 판정합니다.

        분류된 연결 끊김이면 즉시 폐기 대상으로 표시한다. 분류되지 않은
        오류라도 같은 연결에서 연속으로 누적되면 알 수 없는 형태의 단절로 보고
        폐기 대상으로 승격한다(미래의 새로운 wedge 형태에 대한 백스톱).
        """
        self._consecutive_failures += 1
        if self._is_retryable_disconnect(exc):
            self._conn_broken = True
            return True
        if self._consecutive_failures >= _UNCLASSIFIED_FAILURE_LIMIT:
            logger.warning(
                "동일 TiDB 연결에서 분류되지 않은 오류가 %d회 연속 발생해 "
                "연결을 폐기 대상으로 표시합니다: %s",
                self._consecutive_failures,
                exc,
            )
            self._conn_broken = True
        return False

    def _note_success(self) -> None:
        """정상 실행 시 연속 실패 카운터를 초기화합니다."""
        self._consecutive_failures = 0

    def _reconnect_sync(self) -> None:
        """기존 연결을 닫고 새 PyMySQL 연결을 동기적으로 생성합니다."""
        if pymysql is None:
            raise CompatOperationalError("PyMySQL 패키지가 필요합니다.")
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = pymysql.connect(**self.settings.to_connect_kwargs())
        self._connected_at_monotonic = time.monotonic()
        self._transaction_dirty = False
        self._conn_broken = False
        self._consecutive_failures = 0

    async def _reconnect_locked(self) -> None:
        """지수 백오프를 적용해 재연결합니다.

        반드시 ``self._lock``을 획득한 상태에서만 호출해야 한다. 재연결이
        연속 실패하면 다음 시도까지 대기 구간을 두어, DB가 내려간 동안 여러
        background loop가 매 주기 새 TCP 연결을 시도하는 것을 막는다.
        """
        now = time.monotonic()
        blocked_until = self._reconnect_blocked_until
        if blocked_until is not None and now < blocked_until:
            raise CompatOperationalError(
                "TiDB 재연결 백오프 중입니다. "
                f"{blocked_until - now:.1f}초 후 다시 시도합니다."
            )
        try:
            await asyncio.to_thread(self._reconnect_sync)
        except Exception as exc:
            self._reconnect_failures += 1
            delay = min(
                _RECONNECT_BACKOFF_MAX_SECONDS,
                _RECONNECT_BACKOFF_BASE_SECONDS * (2 ** (self._reconnect_failures - 1)),
            )
            self._reconnect_blocked_until = time.monotonic() + delay
            self._conn_broken = True
            logger.warning(
                "TiDB 재연결 실패(%d회 연속). %.1f초 후 재시도합니다: %s",
                self._reconnect_failures,
                delay,
                exc,
            )
            raise CompatOperationalError(str(exc)) from exc
        self._reconnect_failures = 0
        self._reconnect_blocked_until = None

    async def _ensure_connected_locked(self) -> None:
        """연결 상태 점검/재연결.

        주의: 반드시 `self._lock`을 획득한 상태에서만 호출해야 한다.
        같은 연결 객체에서 ping/query/commit이 동시에 실행되면
        PyMySQL packet sequence 오류가 발생할 수 있어 임계구역으로 묶는다.

        성능: 매 쿼리마다 `ping()`으로 서버 왕복을 추가하지 않는다(원격 TiDB에서
        쿼리당 왕복이 2배가 되어 지연/RU 비용이 커진다). 최대 수명을 초과했을 때만
        선제적으로 재연결하고, 그 사이에 끊긴 연결은 실행 시점의 재시도 경로
        (`_is_retryable_disconnect` 기반 재연결)에서 처리한다. 결과가 불확실한
        쓰기는 재실행하지 않고, 진행 중 트랜잭션이 없는 읽기만 1회 재실행한다.
        """
        if self._conn is None:
            await self.connect()
            return
        # 폐기 대상으로 표시된 연결은 미확정 쓰기가 남아 있어도 재연결한다.
        # 서버 세션이 이미 사라져 트랜잭션도 함께 유실된 상태이므로, dirty를
        # 이유로 재연결을 미루면 죽은 연결을 영구히 붙들게 된다.
        if self._conn_broken:
            if self._transaction_dirty:
                logger.warning(
                    "끊어진 TiDB 연결을 교체합니다. 미확정 트랜잭션은 서버 세션과 "
                    "함께 유실되었으므로 호출자가 작업을 재시도해야 합니다."
                )
            await self._reconnect_locked()
            return
        if self._is_connection_stale() and not self._transaction_dirty:
            await self._reconnect_locked()
            return

    async def _enter_transaction_gate(self, *, starts_transaction: bool) -> bool:
        """현재 task에 단일 연결 사용권을 부여한다.

        읽기는 한 문장 동안만 gate를 잡고, 쓰기는 명시적 commit/rollback까지
        소유권을 유지한다. 반환값은 이 호출이 새로 gate를 획득했는지 나타낸다.
        """
        task = asyncio.current_task()
        if task is not None and self._transaction_owner is task:
            return False

        await self._transaction_gate.acquire()
        if starts_transaction and task is not None:
            self._transaction_owner = task
            # scheduler/background loop처럼 같은 task가 계속 쓰기를 수행해도
            # done callback을 트랜잭션마다 누적하지 않는다.
            if task not in self._transaction_owner_callbacks:
                self._transaction_owner_callbacks.add(task)
                task.add_done_callback(self._handle_transaction_owner_done)
        return True

    def _release_transaction_gate(self, task: asyncio.Task[Any] | None) -> None:
        """소유권과 gate를 함께 해제한다."""
        if task is not None and self._transaction_owner is task:
            self._transaction_owner = None
        if self._transaction_gate.locked():
            self._transaction_gate.release()

    def _handle_transaction_owner_done(self, task: asyncio.Task[Any]) -> None:
        """commit 없이 끝난 task의 미확정 쓰기를 비동기로 되돌린다."""
        self._transaction_owner_callbacks.discard(task)
        if self._transaction_owner is not task:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - 이벤트 루프 종료 구간
            return
        loop.create_task(self._rollback_abandoned_transaction(task))

    async def _rollback_abandoned_transaction(
        self,
        owner: asyncio.Task[Any],
    ) -> None:
        if self._transaction_owner is not owner:
            return
        try:
            async with self._lock:
                if self._conn is not None and self._transaction_dirty:
                    try:
                        await asyncio.to_thread(self._conn.rollback)
                    except Exception:
                        logger.exception(
                            "종료된 task의 TiDB 트랜잭션 롤백에 실패했습니다."
                        )
                self._transaction_dirty = False
        finally:
            if self._transaction_owner is owner:
                self._release_transaction_gate(owner)

    async def _execute_buffered(self, query: str, params: Iterable[Any] | None = None) -> BufferedCursor:
        """SQL을 TiDB 문법으로 변환 후 실행하고 BufferedCursor로 결과를 반환합니다.

        연결 끊김 감지 시 재연결하되, 쓰기는 중복 반영 위험 때문에 재실행하지 않습니다.
        """
        sql = rewrite_sql_for_tidb(query)
        bind = tuple(params or ())
        retry_safe = _is_safe_read_retry(sql)
        acquired_gate = await self._enter_transaction_gate(
            starts_transaction=not retry_safe
        )
        try:
            async with self._lock:
                await self._ensure_connected_locked()
                # 실행 도중 오류가 나더라도 서버가 문장의 일부 또는 DDL의 implicit
                # commit을 반영했을 수 있다. 성공 뒤가 아니라 실행 전에 dirty로
                # 표시해야 다음 호출의 선제 재연결로 미확정 트랜잭션을 버리지 않는다.
                if not retry_safe:
                    self._transaction_dirty = True
                try:
                    result = await asyncio.to_thread(self._execute_sync, sql, bind)
                    self._note_success()
                    return result
                except Exception as exc:
                    if self._note_failure(exc):
                        transaction_was_dirty = self._transaction_dirty
                        await self._reconnect_locked()
                        if retry_safe and not transaction_was_dirty:
                            try:
                                result = await asyncio.to_thread(
                                    self._execute_sync,
                                    sql,
                                    bind,
                                )
                            except Exception as retry_exc:  # pragma: no cover
                                self._note_failure(retry_exc)
                                raise CompatOperationalError(str(retry_exc)) from retry_exc
                            self._note_success()
                            return result
                        raise CompatOperationalError(
                            "연결 단절 시 쓰기 또는 진행 중 트랜잭션은 결과가 불확실하여 "
                            "자동 재실행하지 않았습니다."
                        ) from exc
                    raise CompatOperationalError(str(exc)) from exc
        finally:
            # 읽기는 문장 종료와 함께 해제한다. 쓰기는 commit/rollback까지 현재
            # task가 소유하며, task 자체가 끝나면 done callback이 rollback한다.
            if retry_safe and acquired_gate:
                self._release_transaction_gate(None)

    def _execute_sync(self, sql: str, params: tuple[Any, ...]) -> BufferedCursor:
        """PyMySQL 커서로 SQL을 동기 실행하고 결과를 BufferedCursor로 래핑합니다."""
        assert self._conn is not None
        with self._conn.cursor() as cursor:
            cursor.execute(sql, params)
            rows: list[CompatRow] = []
            if cursor.description is not None:
                raw_rows = cursor.fetchall()
                rows = [CompatRow(row) for row in raw_rows]
            return BufferedCursor(rows, rowcount=cursor.rowcount, lastrowid=cursor.lastrowid)

    def execute(self, query: str, params: Iterable[Any] | None = None) -> QueryHandle:
        """aiosqlite 호환 `await/async with db.execute()` 인터페이스를 제공합니다."""
        return QueryHandle(self, query, params)

    async def executemany(self, query: str, seq_of_params: Iterable[Iterable[Any]]) -> None:
        """여러 행을 한 번에 실행합니다. (INSERT 다중행 등)"""
        sql = rewrite_sql_for_tidb(query)
        values = [tuple(item) for item in seq_of_params]
        if not values:
            return
        await self._enter_transaction_gate(starts_transaction=True)
        async with self._lock:
            await self._ensure_connected_locked()
            # 드라이버가 큰 batch를 여러 문장으로 나눈 뒤 후반부에서 실패할 수
            # 있으므로, 일부 행이 반영됐을 가능성까지 보수적으로 추적한다.
            self._transaction_dirty = True
            try:
                await asyncio.to_thread(self._executemany_sync, sql, values)
                self._note_success()
            except Exception as exc:  # pragma: no cover
                if self._note_failure(exc):
                    await self._reconnect_locked()
                    raise CompatOperationalError(
                        "연결 단절 시 executemany 결과가 불확실하여 자동 재실행하지 않았습니다."
                    ) from exc
                raise CompatOperationalError(str(exc)) from exc

    def _executemany_sync(self, sql: str, values: list[tuple[Any, ...]]) -> None:
        """여러 행을 동기적으로 일괄 실행합니다."""
        assert self._conn is not None
        with self._conn.cursor() as cursor:
            cursor.executemany(sql, values)

    async def executescript(self, script: str) -> None:
        """세미콜론으로 구분된 SQL 스크립트 전체를 순차 실행합니다."""
        for statement in split_sql_script(script):
            await self.execute(statement)

    async def commit(self) -> None:
        """현재 트랜잭션을 커밋합니다. 연결 끊김 시 CompatOperationalError를 발생시킵니다."""
        if self._conn is None:
            return
        task = asyncio.current_task()
        acquired_gate = await self._enter_transaction_gate(starts_transaction=False)
        try:
            async with self._lock:
                await self._ensure_connected_locked()
                try:
                    await asyncio.to_thread(self._conn.commit)
                    self._transaction_dirty = False
                    self._note_success()
                except Exception as exc:  # pragma: no cover
                    if self._note_failure(exc):
                        await self._reconnect_locked()
                        raise CompatOperationalError(
                            "커밋 중 연결이 끊어져 결과가 불확실합니다. 멱등성 키나 "
                            "read-back 확인 없이 작업 전체를 자동 재시도하면 안 됩니다."
                        ) from exc
                    raise CompatOperationalError(str(exc)) from exc
        finally:
            if self._transaction_owner is task:
                self._release_transaction_gate(task)
            elif acquired_gate:
                self._release_transaction_gate(None)

    async def rollback(self) -> None:
        """현재 트랜잭션을 롤백합니다."""
        if self._conn is None:
            return
        task = asyncio.current_task()
        acquired_gate = await self._enter_transaction_gate(starts_transaction=False)
        try:
            async with self._lock:
                await self._ensure_connected_locked()
                try:
                    await asyncio.to_thread(self._conn.rollback)
                    self._transaction_dirty = False
                    self._note_success()
                except Exception as exc:  # pragma: no cover
                    if self._note_failure(exc):
                        # 연결이 끊긴 시점에 서버 세션과 트랜잭션이 함께 사라졌다.
                        # 롤백의 목적은 이미 달성됐으므로 연결만 새로 만들고
                        # 정상 반환한다. 여기서 예외를 올리면 호출자의 정리
                        # 경로가 매 주기 CRITICAL 로그를 남기게 된다.
                        await self._reconnect_locked()
                        logger.info(
                            "연결이 끊긴 상태의 롤백 요청입니다. 트랜잭션은 서버 "
                            "세션과 함께 이미 폐기되어 새 연결로 교체했습니다."
                        )
                        return
                    raise CompatOperationalError(str(exc)) from exc
        finally:
            if self._transaction_owner is task:
                self._release_transaction_gate(task)
            elif acquired_gate:
                self._release_transaction_gate(None)

    async def close(self) -> None:
        """TiDB 연결을 안전하게 종료합니다."""
        if self._conn is None:
            return
        task = asyncio.current_task()
        acquired_gate = await self._enter_transaction_gate(starts_transaction=False)
        try:
            async with self._lock:
                await asyncio.to_thread(self._conn.close)
            self._conn = None
            self._connected_at_monotonic = None
            self._transaction_dirty = False
        finally:
            if self._transaction_owner is task:
                self._release_transaction_gate(task)
            elif acquired_gate:
                self._release_transaction_gate(None)


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

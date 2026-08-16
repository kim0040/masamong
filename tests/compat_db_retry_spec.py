import asyncio
import time

import pymysql
import pytest

from database.compat_db import (
    BufferedCursor,
    CompatOperationalError,
    TiDBConnection,
    TiDBSettings,
    _is_safe_read_retry,
)


def _connection() -> TiDBConnection:
    db = TiDBConnection(
        TiDBSettings(
            host="db.example",
            port=4000,
            user="bot",
            password="secret",
            database="masamong",
            conn_max_lifetime_seconds=1,
        )
    )
    db._conn = object()
    db._connected_at_monotonic = time.monotonic()
    return db


def test_retry_classifier_is_conservative():
    assert _is_safe_read_retry(" /* health */ SELECT 1") is True
    assert _is_safe_read_retry("-- comment\nSHOW TABLES") is True
    assert _is_safe_read_retry("INSERT INTO logs VALUES (1)") is False
    assert _is_safe_read_retry("WITH rows AS (SELECT 1) SELECT * FROM rows") is False
    assert _is_safe_read_retry("EXPLAIN ANALYZE DELETE FROM logs") is False


@pytest.mark.asyncio
async def test_clean_select_disconnect_is_retried_once(monkeypatch):
    db = _connection()
    attempts = 0
    reconnects = 0

    def fake_execute(_sql, _params):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError(2013, "Lost connection to server")
        return BufferedCursor([])

    def fake_reconnect():
        nonlocal reconnects
        reconnects += 1
        db._conn = object()
        db._connected_at_monotonic = time.monotonic()
        db._transaction_dirty = False

    monkeypatch.setattr(db, "_execute_sync", fake_execute)
    monkeypatch.setattr(db, "_reconnect_sync", fake_reconnect)

    await db._execute_buffered("SELECT 1")

    assert attempts == 2
    assert reconnects == 1


@pytest.mark.asyncio
async def test_write_disconnect_is_not_automatically_retried(monkeypatch):
    db = _connection()
    attempts = 0
    reconnects = 0

    def fake_execute(_sql, _params):
        nonlocal attempts
        attempts += 1
        raise OSError(2013, "Lost connection to server")

    def fake_reconnect():
        nonlocal reconnects
        reconnects += 1
        db._conn = object()
        db._connected_at_monotonic = time.monotonic()
        db._transaction_dirty = False

    monkeypatch.setattr(db, "_execute_sync", fake_execute)
    monkeypatch.setattr(db, "_reconnect_sync", fake_reconnect)

    with pytest.raises(CompatOperationalError, match="자동 재실행하지"):
        await db._execute_buffered(
            "INSERT INTO analytics_log (event_type) VALUES (%s)",
            ("message",),
        )

    assert attempts == 1
    assert reconnects == 1


@pytest.mark.asyncio
async def test_select_is_not_retried_after_uncommitted_write(monkeypatch):
    db = _connection()
    db._transaction_dirty = True
    attempts = 0

    def fake_execute(_sql, _params):
        nonlocal attempts
        attempts += 1
        raise OSError(2013, "Lost connection to server")

    monkeypatch.setattr(db, "_execute_sync", fake_execute)
    monkeypatch.setattr(
        db,
        "_reconnect_sync",
        lambda: setattr(db, "_transaction_dirty", False),
    )

    with pytest.raises(CompatOperationalError, match="진행 중 트랜잭션"):
        await db._execute_buffered("SELECT LAST_INSERT_ID()")

    assert attempts == 1


@pytest.mark.asyncio
async def test_executemany_disconnect_is_not_automatically_retried(monkeypatch):
    db = _connection()
    attempts = 0

    def fake_executemany(_sql, _values):
        nonlocal attempts
        attempts += 1
        raise OSError(2013, "Lost connection to server")

    monkeypatch.setattr(db, "_executemany_sync", fake_executemany)
    monkeypatch.setattr(
        db,
        "_reconnect_sync",
        lambda: setattr(db, "_transaction_dirty", False),
    )

    with pytest.raises(CompatOperationalError, match="executemany"):
        await db.executemany(
            "INSERT INTO api_call_log (api_type) VALUES (%s)",
            [("weather",), ("finance",)],
        )

    assert attempts == 1


@pytest.mark.asyncio
async def test_non_disconnect_write_error_keeps_transaction_dirty(monkeypatch):
    db = _connection()

    def partially_applied_write(_sql, _params):
        raise RuntimeError("later batch failed")

    monkeypatch.setattr(db, "_execute_sync", partially_applied_write)

    with pytest.raises(CompatOperationalError, match="later batch failed"):
        await db._execute_buffered("UPDATE guild_settings SET ai_enabled = 1")

    assert db._transaction_dirty is True


@pytest.mark.asyncio
async def test_dirty_transaction_is_committed_before_stale_reconnect(monkeypatch):
    class FakeConnection:
        def __init__(self):
            self.commit_calls = 0

        def commit(self):
            self.commit_calls += 1

    db = _connection()
    underlying = FakeConnection()
    db._conn = underlying
    monkeypatch.setattr(
        db,
        "_execute_sync",
        lambda _sql, _params: BufferedCursor([], rowcount=1),
    )

    await db._execute_buffered("UPDATE guild_settings SET ai_enabled = 1")
    assert db._transaction_dirty is True

    db._connected_at_monotonic = 0.0
    monkeypatch.setattr(
        db,
        "_reconnect_sync",
        lambda: pytest.fail("dirty transaction must not be replaced before commit"),
    )

    await db.commit()

    assert underlying.commit_calls == 1
    assert db._transaction_dirty is False


@pytest.mark.asyncio
async def test_concurrent_task_cannot_enter_between_write_and_commit(monkeypatch):
    class FakeConnection:
        def commit(self):
            return None

    db = _connection()
    db._conn = FakeConnection()
    events: list[str] = []
    first_write_done = asyncio.Event()
    allow_commit = asyncio.Event()

    def fake_execute(sql, _params):
        events.append(sql)
        return BufferedCursor([], rowcount=1)

    monkeypatch.setattr(db, "_execute_sync", fake_execute)

    async def writer():
        await db._execute_buffered("UPDATE guild_settings SET ai_enabled = 1")
        first_write_done.set()
        await allow_commit.wait()
        await db.commit()

    async def reader():
        await first_write_done.wait()
        await db._execute_buffered("SELECT 1")

    writer_task = asyncio.create_task(writer())
    reader_task = asyncio.create_task(reader())
    await first_write_done.wait()
    await asyncio.sleep(0)

    assert events == ["UPDATE guild_settings SET ai_enabled = 1"]
    allow_commit.set()
    await asyncio.gather(writer_task, reader_task)
    assert events == [
        "UPDATE guild_settings SET ai_enabled = 1",
        "SELECT 1",
    ]


@pytest.mark.asyncio
async def test_abandoned_write_is_rolled_back_and_gate_released(monkeypatch):
    class FakeConnection:
        def __init__(self):
            self.rollback_calls = 0

        def rollback(self):
            self.rollback_calls += 1

    db = _connection()
    underlying = FakeConnection()
    db._conn = underlying
    monkeypatch.setattr(
        db,
        "_execute_sync",
        lambda _sql, _params: BufferedCursor([], rowcount=1),
    )

    async def abandoned_writer():
        await db._execute_buffered("UPDATE guild_settings SET ai_enabled = 1")

    await asyncio.create_task(abandoned_writer())
    for _ in range(20):
        if underlying.rollback_calls and db._transaction_owner is None:
            break
        await asyncio.sleep(0)

    assert underlying.rollback_calls == 1
    assert db._transaction_dirty is False
    assert db._transaction_owner is None
    assert db._transaction_gate.locked() is False


@pytest.mark.asyncio
async def test_transaction_owner_close_releases_gate(monkeypatch):
    class FakeConnection:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    db = _connection()
    underlying = FakeConnection()
    db._conn = underlying
    monkeypatch.setattr(
        db,
        "_execute_sync",
        lambda _sql, _params: BufferedCursor([], rowcount=1),
    )

    await db._execute_buffered("UPDATE guild_settings SET ai_enabled = 1")
    assert db._transaction_owner is asyncio.current_task()
    assert db._transaction_gate.locked() is True

    await db.close()

    assert underlying.close_calls == 1
    assert db._conn is None
    assert db._transaction_dirty is False
    assert db._transaction_owner is None
    assert db._transaction_gate.locked() is False


@pytest.mark.asyncio
async def test_repeated_transactions_register_one_task_done_callback(monkeypatch):
    class FakeConnection:
        def commit(self):
            return None

    db = _connection()
    db._conn = FakeConnection()
    monkeypatch.setattr(
        db,
        "_execute_sync",
        lambda _sql, _params: BufferedCursor([], rowcount=1),
    )

    for _ in range(10):
        await db._execute_buffered("UPDATE guild_settings SET ai_enabled = 1")
        await db.commit()

    assert list(db._transaction_owner_callbacks) == [asyncio.current_task()]
    assert db._transaction_owner is None
    assert db._transaction_gate.locked() is False


# --- 2026-08-10 운영 장애 회귀 테스트 -------------------------------------
# 죽은 소켓에 명령을 보낼 때 PyMySQL이 던지는 InterfaceError(0, "")가
# 재시도 분류에서 누락돼, 미확정 트랜잭션(_transaction_dirty)이 남은 연결이
# 영구히 교체되지 않았다. 봇이 6일간 모든 DB 작업에 실패한 원인이다.


def test_dead_socket_interface_error_is_retryable():
    assert TiDBConnection._is_retryable_disconnect(pymysql.err.InterfaceError(0, "")) is True
    assert TiDBConnection._is_retryable_disconnect(BrokenPipeError()) is True
    assert TiDBConnection._is_retryable_disconnect(OSError(2013, "Lost connection")) is True
    # 연결과 무관한 오류는 여전히 재시도 대상이 아니어야 한다.
    assert TiDBConnection._is_retryable_disconnect(ValueError("bad param")) is False
    assert TiDBConnection._is_retryable_disconnect(OSError(1062, "Duplicate entry")) is False


@pytest.mark.asyncio
async def test_dead_socket_with_dirty_transaction_is_replaced(monkeypatch):
    """미확정 쓰기가 남아 있어도 죽은 연결은 다음 사용 시점에 교체된다."""
    db = _connection()
    db._transaction_dirty = True
    reconnects = 0

    def fake_reconnect():
        nonlocal reconnects
        reconnects += 1
        db._conn = object()
        db._connected_at_monotonic = time.monotonic()
        db._transaction_dirty = False
        db._conn_broken = False
        db._consecutive_failures = 0

    def dead_socket(_sql, _params):
        raise pymysql.err.InterfaceError(0, "")

    monkeypatch.setattr(db, "_execute_sync", dead_socket)
    monkeypatch.setattr(db, "_reconnect_sync", fake_reconnect)

    # 첫 호출은 죽은 소켓을 만나 연결을 폐기 대상으로 표시하고 재연결한다.
    with pytest.raises(CompatOperationalError):
        await db._execute_buffered("SELECT 1")

    assert reconnects == 1
    assert db._conn_broken is False


@pytest.mark.asyncio
async def test_rollback_on_dead_socket_recovers_without_raising(monkeypatch):
    """끊긴 연결의 롤백은 새 연결로 교체하고 조용히 성공한다."""

    class DeadConnection:
        def rollback(self):
            raise pymysql.err.InterfaceError(0, "")

    db = _connection()
    db._conn = DeadConnection()
    db._transaction_dirty = True
    reconnects = 0

    def fake_reconnect():
        nonlocal reconnects
        reconnects += 1
        db._conn = object()
        db._connected_at_monotonic = time.monotonic()
        db._transaction_dirty = False
        db._conn_broken = False

    monkeypatch.setattr(db, "_reconnect_sync", fake_reconnect)

    await db.rollback()

    assert reconnects == 1
    assert db._transaction_dirty is False
    assert db._transaction_gate.locked() is False


@pytest.mark.asyncio
async def test_unclassified_repeated_failures_mark_connection_broken(monkeypatch):
    """분류되지 않은 오류라도 연속 누적되면 연결을 폐기 대상으로 승격한다."""
    db = _connection()
    monkeypatch.setattr(
        db,
        "_execute_sync",
        lambda _sql, _params: (_ for _ in ()).throw(RuntimeError("weird driver state")),
    )

    for _ in range(4):
        with pytest.raises(CompatOperationalError):
            await db._execute_buffered("SELECT 1")
        assert db._conn_broken is False

    with pytest.raises(CompatOperationalError):
        await db._execute_buffered("SELECT 1")
    assert db._conn_broken is True


@pytest.mark.asyncio
async def test_reconnect_failures_back_off_instead_of_hammering(monkeypatch):
    """재연결이 실패하면 즉시 재시도하지 않고 대기 구간을 둔다."""
    db = _connection()
    attempts = 0

    def failing_reconnect():
        nonlocal attempts
        attempts += 1
        raise OSError("connection refused")

    monkeypatch.setattr(db, "_reconnect_sync", failing_reconnect)
    db._conn_broken = True

    with pytest.raises(CompatOperationalError):
        await db._execute_buffered("SELECT 1")
    assert attempts == 1

    # 백오프 구간 안에서는 실제 재연결을 시도하지 않는다.
    with pytest.raises(CompatOperationalError, match="백오프"):
        await db._execute_buffered("SELECT 1")
    assert attempts == 1

    # 대기가 끝나면 다시 시도한다.
    db._reconnect_blocked_until = time.monotonic() - 0.01
    with pytest.raises(CompatOperationalError):
        await db._execute_buffered("SELECT 1")
    assert attempts == 2


@pytest.mark.asyncio
async def test_successful_execute_resets_failure_counter(monkeypatch):
    """정상 실행 후에는 연속 실패 카운터가 초기화된다."""
    db = _connection()
    calls = 0

    def flaky(_sql, _params):
        nonlocal calls
        calls += 1
        if calls <= 3:
            raise RuntimeError("transient")
        return BufferedCursor([])

    monkeypatch.setattr(db, "_execute_sync", flaky)

    for _ in range(3):
        with pytest.raises(CompatOperationalError):
            await db._execute_buffered("SELECT 1")
    assert db._consecutive_failures == 3

    await db._execute_buffered("SELECT 1")
    assert db._consecutive_failures == 0
    assert db._conn_broken is False


@pytest.mark.asyncio
async def test_production_wedge_sequence_recovers(monkeypatch):
    """2026-08-10 운영 장애의 정확한 순서를 재현하고 자력 복구를 확인한다.

    운영 설정은 ``read_timeout=30`` + ``connect_timeout=10``인데 호출자의
    tick timeout은 35초였다. 그래서 실제로 일어난 일은 다음과 같다.

    1) 쓰기가 read timeout으로 실패한다 (2013).
    2) `_execute_buffered`가 재연결을 시작하지만, 30+10 > 35 이므로
       호출자의 ``asyncio.wait_for``가 재연결 도중 task를 취소한다.
    3) 죽은 연결과 ``_transaction_dirty=True``가 그대로 남는다.
    4) 이후 모든 쿼리가 같은 죽은 소켓을 재사용해 InterfaceError(0, "")로
       영구히 실패했다. 선제 재연결도 dirty 가드에 막혀 동작하지 않았다.

    수정 후에는 4)에서 연결이 교체되어 정상 동작해야 한다.
    """
    state = {"alive": True, "allow_reconnect": False, "first_write_done": False}

    class Connection:
        def rollback(self):
            if not state["alive"]:
                raise pymysql.err.InterfaceError(0, "")

    db = _connection()
    db._conn = Connection()

    def execute(_sql, _params):
        if not state["alive"]:
            # 죽은 소켓에 명령을 보낼 때 PyMySQL이 던지는 예외.
            raise pymysql.err.InterfaceError(0, "")
        if not state["first_write_done"]:
            state["first_write_done"] = True
            state["alive"] = False  # read timeout으로 서버가 세션을 버린다
            raise pymysql.err.OperationalError(
                2013,
                "Lost connection to MySQL server during query (The read operation timed out)",
            )
        return BufferedCursor([])

    def reconnect():
        if not state["allow_reconnect"]:
            # 2) wait_for가 재연결 도중 취소시킨 상황을 모사한다.
            raise asyncio.CancelledError()
        state["alive"] = True
        db._conn = Connection()
        db._connected_at_monotonic = time.monotonic()
        db._transaction_dirty = False
        db._conn_broken = False
        db._consecutive_failures = 0

    monkeypatch.setattr(db, "_execute_sync", execute)
    monkeypatch.setattr(db, "_reconnect_sync", reconnect)

    # 1)~3) 쓰기 실패 후 재연결이 취소되어 죽은 연결이 남는다.
    with pytest.raises(asyncio.CancelledError):
        await db._execute_buffered(
            "INSERT INTO analytics_log (event_type) VALUES (%s)", ("message",)
        )
    assert db._transaction_dirty is True
    assert db._conn_broken is True

    # 호출자의 정리 경로도 죽은 소켓을 만난다. 예외를 던지지 않아야 한다.
    state["allow_reconnect"] = True
    await db.rollback()
    assert db._transaction_dirty is False

    # 4) 다음 주기의 쿼리는 정상 동작한다 (기존에는 영구 실패했다).
    state["alive"] = True
    await db._execute_buffered("SELECT 1")
    assert db._conn_broken is False

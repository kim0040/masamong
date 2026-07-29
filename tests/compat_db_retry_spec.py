import asyncio
import time

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

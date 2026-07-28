import aiosqlite
import pytest

import database.bm25_index as bm25_module
from database.bm25_index import BM25IndexManager


@pytest.mark.asyncio
async def test_bm25_triggers_follow_insert_update_and_delete(tmp_path):
    db_path = tmp_path / "history.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE conversation_history (
                message_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT,
                content TEXT,
                created_at TEXT
            )
            """
        )
        await db.commit()

    manager = BM25IndexManager(str(db_path))
    await manager.ensure_index()

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO conversation_history (
                message_id, guild_id, channel_id, user_id, user_name, content, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (1, 10, 20, 30, "tester", "alpha phrase", "2026-01-01T00:00:00+00:00"),
        )
        await db.commit()
        async with db.execute(
            "SELECT rowid, message_id, content FROM conversation_bm25"
        ) as cursor:
            assert await cursor.fetchall() == [(1, 1, "alpha phrase")]

    assert len(await manager.search("alpha", guild_id=10, channel_id=20)) == 1

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE conversation_history SET content = ? WHERE message_id = ?",
            ("beta phrase", 1),
        )
        await db.commit()
        async with db.execute(
            "SELECT rowid, message_id, content FROM conversation_bm25"
        ) as cursor:
            assert await cursor.fetchall() == [(1, 1, "beta phrase")]

    assert await manager.search("alpha", guild_id=10, channel_id=20) == []
    assert len(await manager.search("beta", guild_id=10, channel_id=20)) == 1

    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM conversation_history WHERE message_id = ?", (1,))
        await db.commit()
        async with db.execute("SELECT COUNT(*) FROM conversation_bm25") as cursor:
            assert (await cursor.fetchone())[0] == 0

    assert await manager.search("beta", guild_id=10, channel_id=20) == []


@pytest.mark.asyncio
async def test_initialization_failure_is_retried_on_next_call(tmp_path, monkeypatch):
    db_path = tmp_path / "history.db"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE conversation_history (
                message_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT,
                content TEXT,
                created_at TEXT
            )
            """
        )
        await db.commit()

    real_connect = bm25_module.aiosqlite.connect
    attempts = 0

    def flaky_connect(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise aiosqlite.OperationalError("database is temporarily locked")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(bm25_module.aiosqlite, "connect", flaky_connect)
    manager = BM25IndexManager(str(db_path))

    await manager.ensure_index()
    assert manager._initialized is False

    await manager.ensure_index()
    assert manager._initialized is True
    assert attempts == 2


@pytest.mark.asyncio
async def test_punctuation_only_query_returns_without_fts_match(tmp_path, monkeypatch):
    db_path = tmp_path / "history.db"
    db_path.touch()
    manager = BM25IndexManager(str(db_path))
    manager._initialized = True

    def unexpected_connect(*_args, **_kwargs):
        pytest.fail("빈 정규화 쿼리는 SQLite MATCH를 실행하면 안 됩니다.")

    monkeypatch.setattr(bm25_module.aiosqlite, "connect", unexpected_connect)

    assert await manager.search("*** :::") == []


@pytest.mark.asyncio
async def test_bulk_rebuild_does_not_run_a_second_full_rebuild(monkeypatch):
    ensure_calls = 0

    class FakeManager:
        def __init__(self, db_path):
            assert db_path == "history.db"

        async def ensure_index(self):
            nonlocal ensure_calls
            ensure_calls += 1

    monkeypatch.setattr(bm25_module, "BM25IndexManager", FakeManager)

    await bm25_module.bulk_rebuild("history.db")

    assert ensure_calls == 1

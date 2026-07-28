import aiosqlite
import pytest

from database.bm25_index import BM25IndexManager
from utils import db as db_utils


async def _create_conversation_tables(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE conversation_history (
            message_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            user_name TEXT NOT NULL,
            content TEXT NOT NULL,
            is_bot BOOLEAN NOT NULL,
            created_at TEXT NOT NULL,
            embedding BLOB
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE conversation_history_archive (
            message_id INTEGER PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            user_name TEXT NOT NULL,
            content TEXT NOT NULL,
            is_bot BOOLEAN NOT NULL,
            created_at TEXT NOT NULL,
            embedding BLOB
        )
        """
    )
    await db.execute("CREATE TABLE commit_probe (value INTEGER NOT NULL)")
    await db.executemany(
        """
        INSERT INTO conversation_history (
            message_id, guild_id, channel_id, user_id, user_name,
            content, is_bot, created_at, embedding
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (1, 10, 20, 30, "old-user", "old searchable text", 0, "2026-01-01", None),
            (2, 10, 20, 31, "new-user", "new searchable text", 0, "2026-01-02", None),
        ),
    )
    await db.commit()


def _enable_one_row_archive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        db_utils.config,
        "RAG_ARCHIVING_CONFIG",
        {
            "enabled": True,
            "history_limit": 1,
            "batch_size": 1,
        },
    )


@pytest.mark.asyncio
async def test_archive_delete_keeps_content_fts_index_consistent(tmp_path, monkeypatch):
    db_path = tmp_path / "archive-with-bm25.db"
    async with aiosqlite.connect(db_path) as db:
        await _create_conversation_tables(db)

    manager = BM25IndexManager(str(db_path))
    await manager.ensure_index()
    _enable_one_row_archive(monkeypatch)

    async with aiosqlite.connect(db_path) as db:
        await db_utils.archive_old_conversations(db)

        async with db.execute(
            "SELECT message_id FROM conversation_history ORDER BY message_id"
        ) as cursor:
            assert await cursor.fetchall() == [(2,)]
        async with db.execute(
            "SELECT message_id FROM conversation_history_archive ORDER BY message_id"
        ) as cursor:
            assert await cursor.fetchall() == [(1,)]
        async with db.execute(
            "SELECT rowid, message_id, content FROM conversation_bm25 ORDER BY rowid"
        ) as cursor:
            assert await cursor.fetchall() == [(2, 2, "new searchable text")]


@pytest.mark.asyncio
async def test_archive_rolls_back_insert_when_delete_trigger_fails(tmp_path, monkeypatch):
    db_path = tmp_path / "archive-rollback.db"
    _enable_one_row_archive(monkeypatch)

    async with aiosqlite.connect(db_path) as db:
        await _create_conversation_tables(db)
        await db.execute(
            """
            CREATE TRIGGER reject_conversation_delete
            BEFORE DELETE ON conversation_history
            BEGIN
                SELECT RAISE(ABORT, 'simulated delete trigger failure');
            END
            """
        )
        await db.commit()

        await db_utils.archive_old_conversations(db)

        async with db.execute(
            "SELECT message_id FROM conversation_history ORDER BY message_id"
        ) as cursor:
            assert await cursor.fetchall() == [(1,), (2,)]
        async with db.execute(
            "SELECT message_id FROM conversation_history_archive"
        ) as cursor:
            assert await cursor.fetchall() == []

        # A later, unrelated commit must not make the preceding archive INSERT
        # durable after its paired DELETE failed.
        await db.execute("INSERT INTO commit_probe (value) VALUES (1)")
        await db.commit()

    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT message_id FROM conversation_history_archive"
        ) as cursor:
            assert await cursor.fetchall() == []
        async with db.execute("SELECT value FROM commit_probe") as cursor:
            assert await cursor.fetchall() == [(1,)]

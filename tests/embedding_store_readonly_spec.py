import sqlite3

import numpy as np
import pytest

import config
from utils.embeddings import DiscordEmbeddingStore


def _use_sqlite(monkeypatch, *, auto_migrate: bool) -> None:
    monkeypatch.setattr(config, "DISCORD_EMBEDDING_BACKEND", "sqlite")
    monkeypatch.setattr(config, "DISCORD_EMBEDDING_TIDB_TABLE", "discord_chat_embeddings")
    monkeypatch.setattr(config, "AUTO_MIGRATE", auto_migrate)


@pytest.mark.asyncio
async def test_read_only_missing_sqlite_store_does_not_create_file(
    tmp_path,
    monkeypatch,
):
    _use_sqlite(monkeypatch, auto_migrate=True)
    db_path = tmp_path / "missing.db"
    store = DiscordEmbeddingStore(str(db_path), read_only=True)

    with pytest.raises(RuntimeError, match="DB 파일이 없습니다"):
        await store.initialize()

    assert not db_path.exists()


@pytest.mark.asyncio
async def test_auto_migrate_false_rejects_incomplete_existing_schema(
    tmp_path,
    monkeypatch,
):
    _use_sqlite(monkeypatch, auto_migrate=False)
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as db:
        db.execute(
            """
            CREATE TABLE discord_chat_embeddings (
                message_id TEXT UNIQUE,
                server_id TEXT,
                channel_id TEXT,
                user_id TEXT,
                message TEXT,
                timestamp TEXT,
                embedding BLOB
            )
            """
        )
        db.execute(
            """
            CREATE TABLE discord_memory_entries (
                memory_id TEXT UNIQUE
            )
            """
        )

    store = DiscordEmbeddingStore(str(db_path))

    with pytest.raises(RuntimeError, match="필수 컬럼"):
        await store.initialize()


@pytest.mark.asyncio
async def test_auto_migrate_false_rejects_missing_unique_constraints(
    tmp_path,
    monkeypatch,
):
    _use_sqlite(monkeypatch, auto_migrate=False)
    db_path = tmp_path / "no-unique.db"
    schema = (
        DiscordEmbeddingStore._CREATE_TABLE_SQL.replace(
            "message_id TEXT UNIQUE",
            "message_id TEXT",
        )
        + DiscordEmbeddingStore._CREATE_MEMORY_TABLE_SQL.replace(
            "memory_id TEXT UNIQUE",
            "memory_id TEXT",
        )
    )
    with sqlite3.connect(db_path) as db:
        db.executescript(schema)

    store = DiscordEmbeddingStore(str(db_path))

    with pytest.raises(RuntimeError, match="UNIQUE"):
        await store.initialize()


@pytest.mark.asyncio
async def test_auto_migrate_false_allows_writes_without_becoming_read_only(
    tmp_path,
    monkeypatch,
):
    _use_sqlite(monkeypatch, auto_migrate=True)
    db_path = tmp_path / "existing.db"
    await DiscordEmbeddingStore(str(db_path)).initialize()

    monkeypatch.setattr(config, "AUTO_MIGRATE", False)
    store = DiscordEmbeddingStore(str(db_path))
    await store.upsert_message_embedding(
        message_id=1,
        server_id=2,
        channel_id=3,
        user_id=4,
        user_name="tester",
        message="stored",
        timestamp_iso="2026-07-27T00:00:00+00:00",
        embedding=np.zeros(4, dtype=np.float32),
    )

    rows = await store.fetch_recent_embeddings(2, 3)
    assert len(rows) == 1
    assert rows[0]["message"] == "stored"


@pytest.mark.asyncio
async def test_read_only_store_blocks_all_public_mutations(
    tmp_path,
    monkeypatch,
):
    _use_sqlite(monkeypatch, auto_migrate=True)
    db_path = tmp_path / "readonly.db"
    await DiscordEmbeddingStore(str(db_path)).initialize()
    store = DiscordEmbeddingStore(str(db_path), read_only=True)
    vector = np.zeros(4, dtype=np.float32)

    assert await store.fetch_recent_embeddings(2, 3) == []
    with pytest.raises(RuntimeError, match="read-only"):
        await store.upsert_message_embedding(
            message_id=1,
            server_id=2,
            channel_id=3,
            user_id=4,
            user_name="tester",
            message="must not persist",
            timestamp_iso="2026-07-27T00:00:00+00:00",
            embedding=vector,
        )
    with pytest.raises(RuntimeError, match="read-only"):
        await store.upsert_memory_entry(
            memory_id="memory-1",
            anchor_message_id=1,
            server_id=2,
            channel_id=3,
            owner_user_id=4,
            owner_user_name="tester",
            memory_scope="user",
            memory_type="conversation",
            summary_text="summary",
            memory_text="memory",
            raw_context="raw",
            source_message_ids=[1],
            speaker_names=["tester"],
            keywords=["test"],
            timestamp_iso="2026-07-27T00:00:00+00:00",
            embedding=vector,
        )
    with pytest.raises(RuntimeError, match="read-only"):
        await store.clear_memory_entries()
    with pytest.raises(RuntimeError, match="read-only"):
        await store.delete_memory_entries([])
    with pytest.raises(RuntimeError, match="read-only"):
        await store.delete_embeddings([])


@pytest.mark.asyncio
async def test_tidb_read_only_initialization_runs_schema_selects_only(
    monkeypatch,
):
    monkeypatch.setattr(config, "DISCORD_EMBEDDING_BACKEND", "tidb")
    monkeypatch.setattr(config, "DISCORD_EMBEDDING_TIDB_TABLE", "custom_embeddings")
    monkeypatch.setattr(config, "AUTO_MIGRATE", True)
    store = DiscordEmbeddingStore("unused.db", read_only=True)
    queries = []

    def fake_tidb_exec(query, params, *, fetch=False):
        normalized = " ".join(query.split())
        queries.append(normalized)
        assert fetch is True
        assert normalized.startswith("SELECT")
        if "information_schema.TABLES" in normalized:
            return [
                {"TABLE_NAME": "custom_embeddings"},
                {"TABLE_NAME": "discord_memory_entries"},
            ]
        if "information_schema.COLUMNS" in normalized:
            return [
                {"TABLE_NAME": table_name, "COLUMN_NAME": column_name}
                for table_name, columns in {
                    "custom_embeddings": store._REQUIRED_MESSAGE_COLUMNS,
                    "discord_memory_entries": store._REQUIRED_MEMORY_COLUMNS,
                }.items()
                for column_name in columns
            ]
        if "information_schema.STATISTICS" in normalized:
            return [
                {
                    "TABLE_NAME": "custom_embeddings",
                    "INDEX_NAME": "uq_message_id",
                    "COLUMN_NAME": "message_id",
                    "SEQ_IN_INDEX": 1,
                },
                {
                    "TABLE_NAME": "discord_memory_entries",
                    "INDEX_NAME": "uq_memory_id",
                    "COLUMN_NAME": "memory_id",
                    "SEQ_IN_INDEX": 1,
                },
            ]
        raise AssertionError(f"unexpected query: {normalized}")

    monkeypatch.setattr(store, "_tidb_exec", fake_tidb_exec)

    await store.initialize()

    assert len(queries) == 3
    with pytest.raises(RuntimeError, match="read-only"):
        await store.delete_embeddings([1])

from __future__ import annotations

from types import SimpleNamespace

import aiosqlite
import pytest

import config
from cogs.fun_cog import FunCog
from scripts import apply_summary_state_schema as migration


@pytest.mark.parametrize("backend", ["sqlite", "tidb"])
def test_summary_schema_is_single_additive_create(backend):
    statement = migration.schema_statement(backend)
    assert statement.upper().startswith(
        "CREATE TABLE IF NOT EXISTS CHANNEL_SUMMARY_STATE"
    )
    for forbidden in ("DELETE ", "DROP ", "TRUNCATE ", "ALTER "):
        assert forbidden not in statement.upper()


@pytest.mark.asyncio
async def test_sqlite_summary_state_upsert_preserves_other_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_BACKEND", "sqlite")
    path = tmp_path / "summary.db"
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(migration.schema_statement("sqlite"))
        await db.execute("CREATE TABLE existing_data (id INTEGER PRIMARY KEY, value TEXT)")
        await db.execute("INSERT INTO existing_data VALUES (1, 'keep')")
        await db.commit()

        bot = SimpleNamespace(db=db)
        cog = FunCog.__new__(FunCog)
        cog.bot = bot
        cog.summary_cache = {}
        cog.keyword_cooldowns = {}
        assert await cog._persist_summary_state(1, 2, 10, "첫 요약") is True
        assert await cog._persist_summary_state(1, 2, 20, "갱신 요약") is True

        loaded = await cog._load_summary_state(1, 2)
        async with db.execute("SELECT value FROM existing_data WHERE id = 1") as cursor:
            existing = await cursor.fetchone()
        async with db.execute("SELECT COUNT(*) FROM channel_summary_state") as cursor:
            count = (await cursor.fetchone())[0]

    assert loaded is not None
    assert loaded.anchor_message_id == 20
    assert loaded.summary_text == "갱신 요약"
    assert existing[0] == "keep"
    assert count == 1

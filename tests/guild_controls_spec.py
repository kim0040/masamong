"""최고 관리자 서버 제어의 additive schema와 인스턴스 격리."""

import aiosqlite
import pytest

import config
from scripts import apply_guild_controls_schema as migration
from utils.guild_controls import (
    get_guild_control,
    load_guild_controls,
    set_channel_enabled,
    set_guild_ai_enabled,
)


@pytest.mark.parametrize("backend", ["sqlite", "tidb"])
def test_guild_controls_schema_is_single_additive_create(backend):
    statement = migration.schema_statement(backend)
    assert statement.upper().startswith(
        "CREATE TABLE IF NOT EXISTS BOT_GUILD_CONTROLS"
    )
    for forbidden in ("DELETE ", "DROP ", "TRUNCATE ", "ALTER ", "UPDATE "):
        assert forbidden not in statement.upper()


@pytest.mark.asyncio
async def test_guild_controls_are_isolated_by_instance(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_BACKEND", "sqlite")
    path = tmp_path / "controls.db"
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(migration.schema_statement("sqlite"))
        await db.commit()

        monkeypatch.setattr(config, "INSTANCE_NAME", "masamo")
        await set_guild_ai_enabled(
            db,
            guild_id=10,
            enabled=False,
            changed_by=1,
        )
        await set_channel_enabled(
            db,
            guild_id=10,
            channel_id=20,
            enabled=True,
            changed_by=1,
        )
        masamo = await get_guild_control(db, 10)

        monkeypatch.setattr(config, "INSTANCE_NAME", "general")
        general = await get_guild_control(db, 10)
        await set_channel_enabled(
            db,
            guild_id=10,
            channel_id=30,
            enabled=False,
            changed_by=2,
        )
        general_cache = await load_guild_controls(db)

        monkeypatch.setattr(config, "INSTANCE_NAME", "masamo")
        masamo_cache = await load_guild_controls(db)
        async with db.execute(
            "SELECT COUNT(*) FROM bot_guild_controls"
        ) as cursor:
            row_count = (await cursor.fetchone())[0]

    assert masamo.ai_enabled is False
    assert masamo.enabled_channels == frozenset({20})
    assert general.ai_enabled is True
    assert general.enabled_channels == frozenset()
    assert general_cache[10].disabled_channels == frozenset({30})
    assert masamo_cache[10].enabled_channels == frozenset({20})
    assert row_count == 2

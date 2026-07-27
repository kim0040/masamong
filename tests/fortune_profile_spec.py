from types import SimpleNamespace

import aiosqlite
import pytest

import config
from cogs.fortune_cog import FortuneCog


@pytest.mark.asyncio
async def test_profile_reregistration_preserves_subscription_state(tmp_path):
    db = await aiosqlite.connect(tmp_path / "fortune.db")
    try:
        await db.execute(
            """
            CREATE TABLE user_profiles (
                user_id INTEGER PRIMARY KEY,
                birth_date TEXT,
                birth_time TEXT,
                gender TEXT,
                birth_place TEXT,
                subscription_active BOOLEAN DEFAULT 0,
                subscription_time TEXT,
                last_fortune_sent TEXT,
                pending_payload TEXT,
                last_fortune_content TEXT,
                created_at TEXT
            )
            """
        )
        await db.execute(
            """
            INSERT INTO user_profiles (
                user_id, birth_date, birth_time, gender, birth_place,
                subscription_active, subscription_time, last_fortune_sent,
                pending_payload, last_fortune_content, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                7,
                "1990-01-01",
                "07:00",
                "M",
                "서울",
                1,
                "08:30",
                "2026-07-26",
                "pending",
                "previous fortune",
                "2026-01-01",
            ),
        )
        await db.commit()

        cog = FortuneCog.__new__(FortuneCog)
        cog.bot = SimpleNamespace(db=db)
        await cog._save_user_profile(7, "1991-02-02", "09:15", "F", "부산")

        async with db.execute(
            """
            SELECT birth_date, birth_time, gender, birth_place,
                   subscription_active, subscription_time, last_fortune_sent,
                   pending_payload, last_fortune_content
            FROM user_profiles
            WHERE user_id = ?
            """,
            (7,),
        ) as cursor:
            row = await cursor.fetchone()

        assert tuple(row) == (
            "1991-02-02",
            "09:15",
            "F",
            "부산",
            1,
            "08:30",
            "2026-07-26",
            "pending",
            "previous fortune",
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_auto_migrate_false_skips_fortune_runtime_ddl(monkeypatch):
    class _FailIfUsedDB:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("AUTO_MIGRATE=false must not execute schema DDL")

    class _Bot:
        db = _FailIfUsedDB()

        async def wait_until_ready(self):
            return None

    monkeypatch.setattr(config, "AUTO_MIGRATE", False)
    cog = FortuneCog.__new__(FortuneCog)
    cog.bot = _Bot()
    cog._ready = False

    await cog._ensure_db_schema()

    assert cog._ready is True

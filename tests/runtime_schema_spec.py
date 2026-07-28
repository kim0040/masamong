from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import discord
from discord.ext import commands
import pytest

import config
from main import ReMasamongBot, _missing_startup_cogs


ROOT = Path(__file__).resolve().parents[1]


def test_explicit_profile_rejects_any_partially_loaded_cog(monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_EXPLICIT_PROFILE", True)
    monkeypatch.setattr(config, "REQUIRED_COGS", frozenset({"tools_cog"}))

    missing = _missing_startup_cogs(
        {"tools_cog", "events"},
        {"tools_cog", "events", "fortune_cog"},
    )

    assert missing == ["fortune_cog"]


def test_legacy_profile_only_requires_configured_cogs(monkeypatch):
    monkeypatch.setattr(config, "REQUIRE_EXPLICIT_PROFILE", False)
    monkeypatch.setattr(config, "REQUIRED_COGS", frozenset({"tools_cog"}))

    missing = _missing_startup_cogs(
        {"tools_cog"},
        {"tools_cog", "fortune_cog"},
    )

    assert missing == []


async def _bot_with_schema(schema_sql: str) -> ReMasamongBot:
    bot = ReMasamongBot(command_prefix="!", intents=discord.Intents.none())
    bot.db = await aiosqlite.connect(":memory:")
    await bot.db.executescript(schema_sql)
    await bot.db.commit()
    return bot


@pytest.mark.asyncio
async def test_explicit_profile_accepts_complete_runtime_schema():
    schema_sql = (ROOT / "database" / "schema.sql").read_text(encoding="utf-8")
    bot = await _bot_with_schema(schema_sql)
    statements: list[str] = []
    await bot.db.set_trace_callback(statements.append)
    try:
        await bot._verify_runtime_schema()
        catalog_queries = [
            statement
            for statement in statements
            if "FROM sqlite_master" in statement
        ]
        assert len(catalog_queries) == 1
    finally:
        await bot.db.close()


@pytest.mark.asyncio
async def test_explicit_profile_rejects_missing_deferred_runtime_table():
    schema_sql = (ROOT / "database" / "schema.sql").read_text(encoding="utf-8")
    bot = await _bot_with_schema(schema_sql)
    try:
        await bot.db.execute("DROP TABLE analytics_log")
        await bot.db.commit()
        with pytest.raises(RuntimeError, match="analytics_log"):
            await bot._verify_runtime_schema()
    finally:
        await bot.db.close()


@pytest.mark.asyncio
async def test_explicit_profile_rejects_stale_fortune_schema():
    schema_sql = (ROOT / "database" / "schema.sql").read_text(encoding="utf-8")
    schema_sql = schema_sql.replace(
        "    birth_place TEXT, -- 출생지\n",
        "",
    )
    bot = await _bot_with_schema(schema_sql)
    try:
        with pytest.raises(RuntimeError, match="birth_place"):
            await bot._verify_runtime_schema()
    finally:
        await bot.db.close()


@pytest.mark.asyncio
async def test_legacy_profile_keeps_deferred_table_compatibility(monkeypatch):
    schema_sql = (ROOT / "database" / "schema.sql").read_text(encoding="utf-8")
    bot = await _bot_with_schema(schema_sql)
    try:
        await bot.db.execute("DROP TABLE analytics_log")
        await bot.db.commit()
        monkeypatch.setattr(config, "REQUIRE_EXPLICIT_PROFILE", False)
        await bot._verify_runtime_schema()
    finally:
        await bot.db.close()


@pytest.mark.asyncio
async def test_bot_identity_mismatch_stops_before_database_setup(monkeypatch):
    bot = ReMasamongBot(command_prefix="!", intents=discord.Intents.none())
    bot._connection.user = SimpleNamespace(id=111)
    monkeypatch.setattr(config, "EXPECTED_DISCORD_BOT_USER_ID", 222)
    try:
        with pytest.raises(RuntimeError, match="Discord bot identity"):
            await bot.setup_hook()
        assert bot.db is None
    finally:
        await bot.close()


@pytest.mark.asyncio
async def test_shutdown_unloads_cogs_before_closing_database(monkeypatch):
    events: list[str] = []

    class FakeDB:
        async def close(self):
            events.append("db")

    async def fake_bot_close(self):
        events.append("cogs")

    monkeypatch.setattr(commands.Bot, "close", fake_bot_close)
    bot = ReMasamongBot(command_prefix="!", intents=discord.Intents.none())
    bot.db = FakeDB()

    await bot.close()

    assert events == ["cogs", "db"]
    assert bot.db is None


@pytest.mark.asyncio
async def test_on_message_routes_static_prefix_without_prefix_lookup():
    bot = ReMasamongBot(command_prefix=config.COMMAND_PREFIX, intents=discord.Intents.none())
    bot.process_commands = AsyncMock()
    bot.get_prefix = AsyncMock(side_effect=AssertionError("unexpected prefix lookup"))
    message = SimpleNamespace(
        author=SimpleNamespace(bot=False, id=123),
        guild=None,
        channel=SimpleNamespace(id=456),
        content=f"{config.COMMAND_PREFIX}도움",
    )
    try:
        await bot.on_message(message)
        bot.process_commands.assert_awaited_once_with(message)
        bot.get_prefix.assert_not_awaited()
    finally:
        await bot.close()


@pytest.mark.asyncio
async def test_kakao_storage_is_required_only_for_kakao_profiles(monkeypatch):
    # general은 Kakao 기억을 쓰지 않으므로 그 저장소를 요구하면
    # "General에는 Kakao가 없다"는 경계가 스키마에서 깨진다.
    schema_sql = (ROOT / "database" / "schema.sql").read_text(encoding="utf-8")
    monkeypatch.setattr(config, "REQUIRE_EXPLICIT_PROFILE", True)
    monkeypatch.setattr(config, "DB_BACKEND", "tidb")

    async def _run_with(kakao_enabled: bool) -> set[str]:
        monkeypatch.setattr(config, "KAKAO_MEMORY_ENABLED", kakao_enabled)
        bot = await _bot_with_schema(schema_sql)
        requested: set[str] = set()

        async def _fake_existing_tables(table_names):
            requested.update(str(name) for name in table_names)
            return set(requested)

        # 컬럼 검증은 이 테스트의 관심사가 아니므로 항상 충족시킨다.
        # 그래야 아래 호출이 조용히 실패하지 않고, 요구된 테이블 집합만 남는다.
        async def _fake_get_table_columns(db, table_name):
            return [
                "id", "guild_id", "ai_enabled", "ai_allowed_channels",
                "persona_text", "language", "user_id", "birth_date",
                "birth_time", "gender", "birth_place", "subscription_active",
                "subscription_time", "pending_payload", "last_fortune_sent",
                "last_fortune_content", "created_at", "message_id",
                "server_id", "channel_id", "user_name", "message", "timestamp",
                "embedding", "memory_id", "anchor_message_id", "owner_user_id",
                "owner_user_name", "memory_scope", "memory_type",
                "summary_text", "memory_text", "raw_context",
                "source_message_ids", "speaker_names", "keyword_json",
                "room_key", "source_room_label", "chunk_id", "session_id",
                "start_date", "message_count", "summary", "text_long",
            ]

        monkeypatch.setattr(bot, "_existing_tables", _fake_existing_tables)
        monkeypatch.setattr("main.get_table_columns", _fake_get_table_columns)
        try:
            await bot._verify_runtime_schema()
        finally:
            await bot.db.close()
        return requested

    assert "kakao_chunks" in await _run_with(True)
    assert "kakao_chunks" not in await _run_with(False)

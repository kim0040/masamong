import discord
import pytest

import config
from main import ReMasamongBot


@pytest.mark.asyncio
async def test_static_guild_settings_mode_never_reads_stale_database_rows(
    monkeypatch,
):
    class _FailIfUsedDB:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("static mode must not query guild_settings")

    monkeypatch.setattr(config, "GUILD_SETTINGS_MODE", "static")
    bot = ReMasamongBot(
        command_prefix="!",
        intents=discord.Intents.none(),
    )
    bot.db = _FailIfUsedDB()
    bot._guild_settings_cache = {
        123: {
            "ai_enabled": False,
            "ai_allowed_channels": {456},
            "persona_text": "stale persona",
        }
    }
    try:
        await bot._load_guild_settings_cache()
        assert bot._guild_settings_cache == {}
    finally:
        bot.db = None
        await bot.close()

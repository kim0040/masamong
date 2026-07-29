# -*- coding: utf-8 -*-
"""최고 관리자 패널에서만 호출하는 제한된 서버 설정 서비스."""

from __future__ import annotations

import asyncio

from discord.ext import commands

from logger_config import logger
from utils.guild_controls import (
    GuildControl,
    get_guild_control,
    set_channel_enabled,
    set_guild_ai_enabled,
)


class SettingsCog(commands.Cog):
    """공개 slash command 없이 서버 AI/채널 override만 제공합니다."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._write_lock = asyncio.Lock()
        logger.info("최고 관리자 전용 SettingsCog가 초기화되었습니다.")

    async def snapshot(self, guild_id: int) -> GuildControl:
        cached = getattr(self.bot, "_guild_control_cache", {}).get(int(guild_id))
        if cached is not None:
            return cached
        return await get_guild_control(self.bot.db, int(guild_id))

    async def set_ai_enabled(
        self,
        *,
        guild_id: int,
        enabled: bool,
        changed_by: int,
    ) -> GuildControl:
        async with self._write_lock:
            updated = await set_guild_ai_enabled(
                self.bot.db,
                guild_id=guild_id,
                enabled=enabled,
                changed_by=changed_by,
            )
            self.bot.update_guild_control_cache(guild_id, updated)
            return updated

    async def set_channel_enabled(
        self,
        *,
        guild_id: int,
        channel_id: int,
        enabled: bool,
        changed_by: int,
    ) -> GuildControl:
        async with self._write_lock:
            updated = await set_channel_enabled(
                self.bot.db,
                guild_id=guild_id,
                channel_id=channel_id,
                enabled=enabled,
                changed_by=changed_by,
            )
            self.bot.update_guild_control_cache(guild_id, updated)
            return updated


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SettingsCog(bot))

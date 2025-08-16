# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
from typing import Dict

from logger_config import logger

class LoggingCog(commands.Cog):
    """서버별 로깅 채널에 메시지 기록을 남기는 기능"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._log_channel_cache: Dict[int, discord.TextChannel | None] = {}

    async def _get_log_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        """서버(길드)에서 'logs' 채널을 찾아 캐시하고 반환합니다."""
        if guild.id in self._log_channel_cache:
            return self._log_channel_cache[guild.id]

        log_channel = discord.utils.get(guild.text_channels, name="logs")

        if log_channel:
            logger.info(f"서버 '{guild.name}'에서 'logs' 채널을 찾았습니다 (ID: {log_channel.id}).")
            self._log_channel_cache[guild.id] = log_channel
        else:
            # 채널이 없으면 None을 캐시하여 반복적인 검색을 방지
            self._log_channel_cache[guild.id] = None
            logger.warning(f"서버 '{guild.name}'에서 'logs' 채널을 찾을 수 없습니다.")

        return log_channel

    async def log_message(self, message: discord.Message):
        """지정된 메시지를 해당 서버의 로그 채널에 임베드 형태로 기록합니다."""
        if not message.guild:
            return

        log_channel = await self._get_log_channel(message.guild)
        if not log_channel:
            return

        # 봇이 로그 채널에 메시지를 보낼 권한이 있는지 확인
        if not log_channel.permissions_for(message.guild.me).send_messages:
            logger.warning(f"서버 '{message.guild.name}'의 'logs' 채널({log_channel.id})에 메시지를 보낼 권한이 없습니다.")
            # 권한이 없으면 캐시에서 제거하여 다음번에 다시 확인하도록 함
            self._log_channel_cache.pop(message.guild.id, None)
            return

        embed = discord.Embed(
            description=message.content,
            color=discord.Color.default()
        )
        embed.set_author(
            name=f"{message.author.display_name} ({message.author.id})",
            icon_url=message.author.display_avatar.url
        )
        embed.add_field(name="채널", value=message.channel.mention, inline=True)
        embed.add_field(name="메시지 링크", value=f"[이동하기]({message.jump_url})", inline=True)
        embed.set_footer(text=f"메시지 ID: {message.id}")
        embed.timestamp = message.created_at

        try:
            await log_channel.send(embed=embed)
        except discord.errors.Forbidden:
            logger.warning(f"서버 '{message.guild.name}'의 'logs' 채널({log_channel.id})에 메시지를 보낼 권한이 없습니다 (Forbidden).")
            self._log_channel_cache.pop(message.guild.id, None)
        except Exception as e:
            logger.error(f"로그 채널에 메시지 전송 중 오류 발생: {e}", exc_info=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(LoggingCog(bot))

# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
import re
import random
import time
import pytz
from datetime import datetime

import config
from logger_config import logger
from utils import db as db_utils
from .ai_handler import AIHandler
from .weather_cog import WeatherCog
from .fun_cog import FunCog
from .activity_cog import ActivityCog

from collections import deque

class EventListeners(commands.Cog):
    """Discord 이벤트 리스너 (모든 상호작용의 시작점)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ai_handler: AIHandler | None = None
        self.weather_cog: WeatherCog | None = None
        self.fun_cog: FunCog | None = None
        self.activity_cog: ActivityCog | None = None
        # 중복 이벤트 처리를 방지하기 위한 최근 명령어 ID 저장소
        self.processed_command_ids = deque(maxlen=100)

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info(f'봇 준비 완료: {self.bot.user.name} (ID: {self.bot.user.id})')
        
        # 의존성 주입은 main.py의 setup_hook에서 처리하므로 여기서는 각 Cog를 가져오기만 함.
        self.ai_handler = self.bot.get_cog('AIHandler')
        self.weather_cog = self.bot.get_cog('WeatherCog')
        self.fun_cog = self.bot.get_cog('FunCog')
        self.activity_cog = self.bot.get_cog('ActivityCog')

        if not all([self.ai_handler, self.weather_cog, self.fun_cog, self.activity_cog]):
            logger.warning("일부 Cog를 찾을 수 없습니다. 의존성 주입이 완벽하지 않을 수 있습니다.")

        # 날씨 Cog의 주기적 작업 시작
        if self.weather_cog:
            self.weather_cog.setup_and_start_loops()

    async def _handle_keyword_triggers(self, message: discord.Message) -> bool:
        """키워드를 감지하여 상호작용을 처리합니다."""
        if not self.fun_cog or not config.FUN_KEYWORD_TRIGGERS.get("enabled"):
            return False
        if self.fun_cog.is_on_cooldown(message.channel.id):
            return False

        msg_content = message.content.lower()
        context_log = f"[{message.guild.name}/{message.channel.name}]"

        for trigger_type, keywords in config.FUN_KEYWORD_TRIGGERS.get("triggers", {}).items():
            for keyword in keywords:
                if keyword in msg_content:
                    logger.info(f"{context_log} FunCog '{trigger_type}' 키워드 감지 ('{keyword}')", extra={'guild_id': message.guild.id})
                    self.fun_cog.update_cooldown(message.channel.id)
                    if trigger_type == "summarize":
                        await self.fun_cog.execute_summarize(message.channel, message.author)
                    elif trigger_type == "fortune":
                        await self.fun_cog.execute_fortune(message.channel, message.author)
                    return True
        return False

    async def _handle_ai_interaction(self, message: discord.Message):
        """AI 상호작용 조건을 확인하고 처리합니다."""
        if not self.ai_handler or not self.ai_handler.is_ready:
            return

        is_guild_ai_enabled = await db_utils.get_guild_setting(self.bot.db, message.guild.id, 'ai_enabled', default=True)
        if not is_guild_ai_enabled:
            return

        allowed_channels = await db_utils.get_guild_setting(self.bot.db, message.guild.id, 'ai_allowed_channels')
        is_ai_allowed_channel = False
        if allowed_channels:
            is_ai_allowed_channel = message.channel.id in allowed_channels
        else:
            channel_config = config.CHANNEL_AI_CONFIG.get(message.channel.id, {})
            is_ai_allowed_channel = channel_config.get("allowed", False)

        if not is_ai_allowed_channel:
            return

        is_bot_mentioned = self.bot.user.mentioned_in(message)
        should_proactively_respond = await self.ai_handler.should_proactively_respond(message)

        if not is_bot_mentioned and not should_proactively_respond:
            return

        # The new agent orchestrator handles everything from planning to response.
        await self.ai_handler.process_agent_message(message)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or isinstance(message.channel, discord.DMChannel):
            return

        # [수정] AI가 명령어를 기억하거나 반응하지 않도록, 명령어 처리를 최우선으로 실행합니다.
        # 이렇게 하면 명령어는 AI 대화 기록에 추가되지 않으며, AI 상호작용 로직을 타지 않습니다.
        if message.content.startswith(self.bot.command_prefix):
            await self.bot.process_commands(message)
            return

        # 아래 로직은 명령어가 아닌 일반 메시지에 대해서만 실행됩니다.
        if self.activity_cog:
            await self.activity_cog.record_message(message)

        if self.ai_handler:
            await self.ai_handler.add_message_to_history(message)

        if await self._handle_keyword_triggers(message):
            return

        await self._handle_ai_interaction(message)

    @commands.Cog.listener()
    async def on_command_completion(self, ctx: commands.Context):
        """명령어 실행 완료 시 분석 및 Discord 로그를 기록합니다."""
        if not ctx.guild:
            return

        # 중복 실행 방지
        if ctx.message.id in self.processed_command_ids:
            logger.warning(f"중복 on_command_completion 이벤트 감지됨: Message ID {ctx.message.id}. 무시합니다.")
            return
        self.processed_command_ids.append(ctx.message.id)

        start_time = ctx.message.created_at.replace(tzinfo=pytz.UTC)
        latency_ms = (datetime.now(pytz.utc) - start_time).total_seconds() * 1000

        details = {
            "guild_id": ctx.guild.id,
            "user_id": ctx.author.id,
            "command": ctx.command.qualified_name,
            "channel_id": ctx.channel.id,
            "full_message": ctx.message.content,
            "success": True,
            "latency_ms": round(latency_ms)
        }
        await db_utils.log_analytics(self.bot.db, "COMMAND_USAGE", details)

        log_message = (
            f"Command `!{details['command']}` by `{ctx.author}` "
            f"in <#{ctx.channel.id}>. Latency: {details['latency_ms']}ms\n"
            f"```{details['full_message']}```"
        )
        logger.info(log_message, extra={'guild_id': ctx.guild.id})

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """명령어 실행 오류 시 분석 및 Discord 로그를 기록합니다."""
        if not ctx.guild:
            return

        ignored_errors = (commands.CommandNotFound, commands.CheckFailure, commands.MissingRequiredArgument)
        if isinstance(error, ignored_errors):
            logger.debug(f"무시된 명령어 오류: {error}")
            return

        details = {
            "guild_id": ctx.guild.id,
            "user_id": ctx.author.id,
            "command": ctx.command.qualified_name if ctx.command else "unknown",
            "channel_id": ctx.channel.id,
            "full_message": ctx.message.content,
            "success": False,
            "error": str(type(error).__name__),
            "error_message": str(error)
        }
        await db_utils.log_analytics(self.bot.db, "COMMAND_USAGE", details)

        log_message = (
            f"Command Error `!{details['command']}` by `{ctx.author}` "
            f"in <#{ctx.channel.id}>. Error: `{details['error']}`\n"
            f"```{details['error_message']}```"
        )
        logger.error(log_message, exc_info=error, extra={'guild_id': ctx.guild.id})

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if not message.guild or message.author.bot: return
        logger.warning(f"메시지 삭제됨 | 작성자: {message.author} | 내용: {message.content or '(내용 없음)'}", extra={'guild_id': message.guild.id})

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        logger.info(f'새로운 서버에 참여했습니다: "{guild.name}" (ID: {guild.id})', extra={'guild_id': guild.id})

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        logger.info(f'서버에서 추방되었습니다: "{guild.name}" (ID: {guild.id})', extra={'guild_id': guild.id})

async def setup(bot: commands.Bot):
    await bot.add_cog(EventListeners(bot))

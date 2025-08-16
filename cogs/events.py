# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
import re
import random
import time
import pytz

import config
from logger_config import logger, discord_log_handler
import utils
from .ai_handler import AIHandler
from .weather_cog import WeatherCog
from .fun_cog import FunCog
from .activity_cog import ActivityCog

class EventListeners(commands.Cog):
    """Discord 이벤트 리스너 (모든 상호작용의 시작점)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ai_handler: AIHandler | None = None
        self.weather_cog: WeatherCog | None = None
        self.fun_cog: FunCog | None = None
        self.activity_cog: ActivityCog | None = None

    @commands.Cog.listener()
    async def on_ready(self):
        discord_log_handler.set_bot(self.bot, config.DISCORD_LOG_CHANNEL_ID)
        logger.info(f'봇 준비 완료: {self.bot.user.name} (ID: {self.bot.user.id})')
        
        # Cog 종속성 주입
        self.ai_handler = self.bot.get_cog('AIHandler')
        self.weather_cog = self.bot.get_cog('WeatherCog')
        self.fun_cog = self.bot.get_cog('FunCog')
        self.activity_cog = self.bot.get_cog('ActivityCog')

        if self.ai_handler:
            if self.fun_cog: self.fun_cog.ai_handler = self.ai_handler
            if self.activity_cog: self.activity_cog.ai_handler = self.ai_handler
            if self.weather_cog: self.weather_cog.ai_handler = self.ai_handler
            logger.info("모든 Cog에 AIHandler 의존성 주입 완료.")
        else:
            logger.error("AIHandler를 찾을 수 없어 의존성 주입 실패.")
        
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
                    logger.info(f"{context_log} FunCog '{trigger_type}' 키워드 감지 ('{keyword}')")
                    self.fun_cog.update_cooldown(message.channel.id)
                    if trigger_type == "summarize":
                        await self.fun_cog.execute_summarize(message.channel, message.author)
                    elif trigger_type == "fortune":
                        await self.fun_cog.execute_fortune(message.channel, message.author)
                    return True
        return False

    async def _handle_ai_interaction(self, message: discord.Message):
        """AI 상호작용 조건을 확인하고 처리합니다."""
        context_log = f"[{message.guild.name}/{message.channel.name}]"
        logger.debug(f"{context_log} AI 상호작용 처리 시작...")

        if not self.ai_handler or not self.ai_handler.is_ready:
            logger.debug(f"{context_log} AI 핸들러가 준비되지 않아 처리 중단.")
            return

        is_guild_ai_enabled = utils.get_guild_setting(message.guild.id, 'ai_enabled', default=True)
        if not is_guild_ai_enabled:
            logger.debug(f"[{message.guild.name}] 서버의 AI 기능이 비활성화되어 처리 중단.")
            return

        allowed_channels = utils.get_guild_setting(message.guild.id, 'ai_allowed_channels')
        is_ai_allowed_channel = False
        if allowed_channels:
            is_ai_allowed_channel = message.channel.id in allowed_channels
        else:
            channel_config = config.CHANNEL_AI_CONFIG.get(message.channel.id, {})
            is_ai_allowed_channel = channel_config.get("allowed", False)

        if not is_ai_allowed_channel:
            logger.debug(f"{context_log}은 AI 응답이 허용되지 않은 채널이므로 처리 중단.")
            return

        is_bot_mentioned = self.bot.user.mentioned_in(message)
        should_proactively_respond = await self.ai_handler.should_proactively_respond(message)

        if not is_bot_mentioned and not should_proactively_respond:
            logger.debug(f"{context_log} 멘션도, 자발적 응답 조건도 아니므로 처리 중단.")
            return

        logger.debug(f"{context_log} 의도 분석 시작...")
        intent = await self.ai_handler.analyze_intent(message)
        logger.info(f"{context_log} 사용자 '{message.author}'의 메시지 의도: {intent}")

        if intent == 'Time':
            current_time_str = utils.get_current_time()
            time_info_for_ai = f"현재 한국 시간은 {current_time_str} 입니다."
            await self.ai_handler.process_ai_message(message, weather_info=time_info_for_ai, intent=intent)
        elif intent == 'Weather' and self.weather_cog:
            await self.weather_cog.prepare_weather_response_for_ai(message, 0, config.DEFAULT_LOCATION_NAME, config.DEFAULT_NX, config.DEFAULT_NY, message.content)
        else: # Chat, Mixed, Command
            await self.ai_handler.process_ai_message(message, intent=intent)

        logger.debug(f"{context_log} AI 상호작용 처리 종료.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or isinstance(message.channel, discord.DMChannel):
            return

        if self.activity_cog: self.activity_cog.record_message(message)
        if self.ai_handler: self.ai_handler.add_message_to_history(message)

        ctx = await self.bot.get_context(message)
        if ctx.command:
            await self.bot.process_commands(message)
            return

        if await self._handle_keyword_triggers(message):
            return
        await self._handle_ai_interaction(message)

    @commands.Cog.listener()
    async def on_command_completion(self, ctx: commands.Context):
        """명령어 실행 완료 시 분석 로그를 기록합니다."""
        if not ctx.guild:
            return

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
        utils.log_analytics("COMMAND_USAGE", details)

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """명령어 실행 오류 시 분석 로그를 기록합니다."""
        if not ctx.guild:
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
        utils.log_analytics("COMMAND_USAGE", details)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if not message.guild or message.author.bot: return
        context_log = f"[{message.guild.name}/{message.channel.name}]"
        content_to_log = message.content if message.content else "(내용 없음)"
        if message.attachments:
            content_to_log += f" [첨부: {', '.join([f'{att.filename}({att.size}b)' for att in message.attachments])}]"
        logger.warning(f"{context_log} 메시지 삭제됨 | 작성자: {message.author} | 내용: {content_to_log}")

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        logger.info(f'새로운 서버에 참여했습니다: "{guild.name}" (ID: {guild.id})')

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        logger.info(f'서버에서 추방되었습니다: "{guild.name}" (ID: {guild.id})')

async def setup(bot: commands.Bot):
    await bot.add_cog(EventListeners(bot))

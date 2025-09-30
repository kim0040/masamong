# -*- coding: utf-8 -*-
"""
Discord API에서 발생하는 주요 이벤트를 수신하고 처리하는 Cog입니다.

이벤트 리스너 목록:
- `on_ready`: 봇 준비 완료 시 초기화 작업을 수행합니다.
- `on_command_completion`: 명령어 성공 시 로그를 기록합니다.
- `on_command_error`: 명령어 실패 시 오류를 분석하고 기록합니다.
- `on_message_delete`: 메시지 삭제 이벤트를 로깅합니다.
- `on_guild_join`/`remove`: 봇의 서버 참여/추방 이벤트를 로깅합니다.
"""

import discord
from discord.ext import commands
from datetime import datetime
import pytz
from collections import deque

import config
from logger_config import logger
from utils import db as db_utils

# 의존하는 다른 Cog들을 타입 힌팅 목적으로 임포트
from .ai_handler import AIHandler
from .weather_cog import WeatherCog
from .fun_cog import FunCog
from .activity_cog import ActivityCog
from .proactive_assistant import ProactiveAssistant

class EventListeners(commands.Cog):
    """봇의 핵심 이벤트 리스너들을 관리하는 클래스입니다."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 다른 Cog들은 on_ready에서 지연 로딩됩니다.
        self.ai_handler: AIHandler | None = None
        self.weather_cog: WeatherCog | None = None
        self.fun_cog: FunCog | None = None
        self.activity_cog: ActivityCog | None = None
        self.proactive_assistant: ProactiveAssistant | None = None
        # 중복 이벤트 처리를 방지하기 위한 최근 메시지 ID 저장소
        self.processed_command_ids = deque(maxlen=100)
        logger.info("EventListeners Cog가 성공적으로 초기화되었습니다.")

    @commands.Cog.listener()
    async def on_ready(self):
        """
        봇이 성공적으로 Discord에 로그인하고 모든 데이터를 준비했을 때 호출됩니다.
        의존하는 다른 Cog들을 가져오고, 주기적인 백그라운드 작업을 시작합니다.
        """
        logger.info(f'봇 준비 완료: {self.bot.user.name} (ID: {self.bot.user.id})')
        
        # main.py에서 모든 Cog가 로드된 후, 의존성 인스턴스를 가져옵니다.
        self.ai_handler = self.bot.get_cog('AIHandler')
        self.weather_cog = self.bot.get_cog('WeatherCog')
        self.fun_cog = self.bot.get_cog('FunCog')
        self.activity_cog = self.bot.get_cog('ActivityCog')
        self.proactive_assistant = self.bot.get_cog('ProactiveAssistant')

        if not all([self.ai_handler, self.weather_cog, self.fun_cog, self.activity_cog, self.proactive_assistant]):
            logger.warning("일부 Cog를 찾을 수 없어 특정 기능이 제한될 수 있습니다.")
        
        # 날씨 Cog의 주기적 알림(강수, 아침/저녁 인사) 작업을 시작합니다.
        if self.weather_cog:
            self.weather_cog.setup_and_start_loops()

    async def _handle_keyword_triggers(self, message: discord.Message) -> bool:
        """
        메시지 내용에서 특정 키워드('요약', '운세' 등)를 감지하여 관련 기능을 실행합니다.
        `on_message` 핸들러에서 호출됩니다.
        
        Returns:
            bool: 키워드가 감지되어 기능이 실행되었으면 True, 아니면 False.
        """
        if not self.fun_cog or not config.FUN_KEYWORD_TRIGGERS.get("enabled"):
            return False
        # 채널별 쿨다운 확인
        if self.fun_cog.is_on_cooldown(message.channel.id):
            return False

        msg_content = message.content.lower()
        for trigger_type, keywords in config.FUN_KEYWORD_TRIGGERS.get("triggers", {}).items():
            if any(keyword in msg_content for keyword in keywords):
                logger.info(f"FunCog 키워드 '{trigger_type}'가 감지되었습니다.", extra={'guild_id': message.guild.id})
                self.fun_cog.update_cooldown(message.channel.id)
                
                if trigger_type == "summarize":
                    await self.fun_cog.execute_summarize(message.channel, message.author)
                elif trigger_type == "fortune":
                    await self.fun_cog.execute_fortune(message.channel, message.author)
                return True # 키워드가 처리되었음을 알림
        return False

    @commands.Cog.listener()
    async def on_command_completion(self, ctx: commands.Context):
        """명령어가 성공적으로 실행되었을 때 호출되어, 사용 통계를 기록합니다."""
        if not ctx.guild or ctx.message.id in self.processed_command_ids:
            return
        self.processed_command_ids.append(ctx.message.id)

        latency_ms = (datetime.now(pytz.utc) - ctx.message.created_at.replace(tzinfo=pytz.UTC)).total_seconds() * 1000

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
        logger.info(f"명령어 실행 완료: `!{details['command']}` by `{ctx.author}` ({details['latency_ms']}ms)", extra={'guild_id': ctx.guild.id})

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """명령어 실행 중 오류가 발생했을 때 호출되어, 오류 정보를 기록합니다."""
        if not ctx.guild:
            return

        # 무시할 수 있는 일반적인 오류들 (예: 존재하지 않는 명령어, 권한 부족)
        ignored_errors = (commands.CommandNotFound, commands.CheckFailure, commands.MissingRequiredArgument)
        if isinstance(error, ignored_errors):
            logger.debug(f"무시된 명령어 오류: {type(error).__name__} - {error}")
            return

        details = {
            "guild_id": ctx.guild.id,
            "user_id": ctx.author.id,
            "command": ctx.command.qualified_name if ctx.command else "unknown",
            "channel_id": ctx.channel.id,
            "full_message": ctx.message.content,
            "success": False,
            "error": type(error).__name__,
            "error_message": str(error)
        }
        await db_utils.log_analytics(self.bot.db, "COMMAND_USAGE", details)
        logger.error(f"명령어 오류 발생: `!{details['command']}` by `{ctx.author}`. Error: {details['error']}", exc_info=error, extra={'guild_id': ctx.guild.id})

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        """메시지가 삭제되었을 때 로깅합니다."""
        if not message.guild or message.author.bot: return
        logger.warning(f"메시지 삭제됨 | 작성자: {message.author} | 내용: {message.content or '(내용 없음)'}", extra={'guild_id': message.guild.id})

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        """봇이 새로운 서버에 추가되었을 때 로깅합니다."""
        logger.info(f'새로운 서버에 참여했습니다: "{guild.name}" (ID: {guild.id})', extra={'guild_id': guild.id})

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        """봇이 서버에서 추방되었을 때 로깅합니다."""
        logger.info(f'서버에서 추방되었습니다: "{guild.name}" (ID: {guild.id})', extra={'guild_id': guild.id})

async def setup(bot: commands.Bot):
    """Cog를 봇에 등록하는 함수입니다."""
    await bot.add_cog(EventListeners(bot))
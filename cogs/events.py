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

from __future__ import annotations
import difflib
from typing import TYPE_CHECKING

import discord
from discord.ext import commands
from datetime import datetime
import pytz
from collections import deque

import config
from logger_config import logger
from utils import db as db_utils

if TYPE_CHECKING:
    from .ai_handler import AIHandler
    from .weather_cog import WeatherCog
    from .fun_cog import FunCog
    from .activity_cog import ActivityCog
    from .proactive_assistant import ProactiveAssistant

class EventListeners(commands.Cog):
    """봇의 핵심 이벤트 리스너들을 관리하는 클래스입니다."""

    def __init__(self, bot: commands.Bot):
        """EventListeners Cog를 초기화하고 의존 Cog 참조를 준비합니다."""
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
            "success": True,
            "latency_ms": round(latency_ms)
        }
        await db_utils.log_analytics(self.bot.db, "COMMAND_USAGE", details)
        logger.info(f"명령어 실행 완료: `!{details['command']}` by `{ctx.author}` ({details['latency_ms']}ms)", extra={'guild_id': ctx.guild.id})

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        """명령어 실행 중 오류가 발생했을 때 호출되어, 오류 정보를 기록하고 사용자에게 안내합니다."""
        if not ctx.guild and not isinstance(ctx.channel, discord.DMChannel):
            return

        # 프리픽스를 직접 입력한 사용자를 침묵시키면 기능이 고장 난 것처럼
        # 보인다. 가까운 명령을 제안하고 통합 메뉴로 되돌아갈 길을 제공한다.
        if isinstance(error, commands.CommandNotFound):
            prefix = ctx.clean_prefix or config.COMMAND_PREFIX or "!"
            invoked = str(ctx.invoked_with or "").strip()
            command_names = sorted(
                {
                    command.name
                    for command in self.bot.commands
                    if not command.hidden
                }
            )
            suggestions = difflib.get_close_matches(
                invoked,
                command_names,
                n=3,
                cutoff=0.45,
            )
            suggestion_text = (
                "\n혹시 "
                + ", ".join(f"`{prefix}{name}`" for name in suggestions)
                + "을(를) 찾으셨나요?"
                if suggestions
                else ""
            )
            await ctx.send(
                f"`{prefix}{invoked}` 명령은 찾지 못했어요.{suggestion_text}\n"
                f"`{prefix}메뉴`에서 기능을 버튼으로 고르거나 "
                f"`{prefix}도움`에서 전체 목록을 확인해주세요.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        
        # 2. 사용자에게 안내가 필요한 오류 (잘못된 사용)
        if isinstance(error, commands.MissingRequiredArgument):
            # 필수 인자 누락
            prefix = ctx.clean_prefix or config.COMMAND_PREFIX or "!"
            cmd_name = ctx.command.qualified_name if ctx.command else "명령어"
            signature = ctx.command.signature if ctx.command else ""
            usage = f"{prefix}{cmd_name} {signature}"
            await ctx.send(
                "⚠️ 명령어를 완성하지 못했어요!\n"
                f"**올바른 사용법**: `{usage}`\n"
                f"(도움말이 필요하면 `{prefix}도움 {cmd_name}`을 입력해보세요)"
            )
            return

        if isinstance(error, commands.BadArgument):
            # 인자 변환 실패 (예: 숫자가 필요한데 문자 입력)
            await ctx.send("⚠️ 입력을 알아듣지 못했어요. 형식을 한 번만 확인해주세요.")
            return

        if isinstance(error, commands.CheckFailure):
            # DM 전용 명령어 체크 실패 시 안내
            if isinstance(error, commands.PrivateMessageOnly):
                await ctx.send("🔒 개인 정보가 오가는 기능이라 **DM에서만** 쓸 수 있어요.")
                return
            if isinstance(error, commands.NoPrivateMessage):
                await ctx.send("📢 이 명령어는 **서버 채널**에서만 사용할 수 있어요.")
                return
            
            # 기타 권한 부족 등은 로그만 남김
            logger.debug(f"CheckFailure: {error} by {ctx.author}")
            return
            
        # 3. 기타 예기치 않은 오류 로그 및 기록
        details = {
            # analytics_log.guild_id는 TiDB에서 BIGINT이므로 DM은 NULL로 표현한다.
            "guild_id": ctx.guild.id if ctx.guild else None,
            "user_id": ctx.author.id,
            "command": ctx.command.qualified_name if ctx.command else "unknown",
            "channel_id": ctx.channel.id,
            "success": False,
            "error": type(error).__name__,
        }
        if config.ANALYTICS_STORE_CONTENT:
            details["error_message"] = str(error)
        
        # DB 로깅 (에러 발생 시만)
        if self.bot.db:
             await db_utils.log_analytics(self.bot.db, "COMMAND_USAGE", details)
             
        logger.error(f"명령어 오류 발생: `!{details['command']}` by `{ctx.author}`. Error: {details['error']}", exc_info=error, extra={'guild_id': details['guild_id']})

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        """메시지가 삭제되었을 때 본문 없이 최소 메타데이터만 로깅합니다."""
        if not message.guild or message.author.bot: return
        logger.warning(
            "메시지 삭제됨 | message_id=%s author_id=%s channel_id=%s",
            message.id,
            message.author.id,
            message.channel.id,
            extra={
                'guild_id': message.guild.id,
                'user_id': message.author.id,
                'channel_id': message.channel.id,
            },
        )

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

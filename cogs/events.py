# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
import random

import config
from logger_config import logger # discord_log_handler는 이제 사용 안 함
from .ai_handler import AIHandler
from .activity_cog import ActivityCog
from .logging_cog import LoggingCog

class EventListeners(commands.Cog):
    """Discord 이벤트 리스너 (모든 상호작용의 시작점)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ai_handler: AIHandler | None = None
        self.activity_cog: ActivityCog | None = None
        self.logging_cog: LoggingCog | None = None

    @commands.Cog.listener()
    async def on_ready(self):
        """봇이 준비되면 다른 Cog들을 가져옵니다."""
        logger.info(f'봇 준비 완료: {self.bot.user.name} (ID: {self.bot.user.id})')
        
        self.ai_handler = self.bot.get_cog('AIHandler')
        self.activity_cog = self.bot.get_cog('ActivityCog')
        self.logging_cog = self.bot.get_cog('LoggingCog')
        
        if not self.ai_handler: logger.error("AIHandler를 찾을 수 없어 의존성 주입 실패.")
        if not self.activity_cog: logger.error("ActivityCog를 찾을 수 없어 의존성 주입 실패.")
        if not self.logging_cog: logger.error("LoggingCog를 찾을 수 없어 의존성 주입 실패.")

    async def _handle_ai_interaction(self, message: discord.Message):
        """AI 상호작용이 필요한지 판단하고, 필요하다면 AI 핸들러에 처리를 위임합니다."""
        if not self.ai_handler or not self.ai_handler.is_ready:
            return

        if not config.CHANNEL_AI_CONFIG.get(message.channel.id):
            return

        is_bot_mentioned = self.bot.user.mentioned_in(message)
        
        proactive_config = config.AI_PROACTIVE_RESPONSE_CONFIG
        should_consider_proactive = (
            proactive_config.get("enabled", False) and
            not is_bot_mentioned and
            any(keyword in message.content.lower() for keyword in proactive_config.get("keywords", [])) and
            random.random() < proactive_config.get("probability", 0.0)
        )

        if is_bot_mentioned or should_consider_proactive:
            # 자발적 응답의 경우, 쿨다운은 AI 핸들러 내부에서 처리될 수 있음 (필요 시)
            await self.ai_handler.process_ai_message(message)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """모든 메시지를 감지하고 적절한 처리를 시작합니다."""
        if message.author.bot or isinstance(message.channel, discord.DMChannel) or not message.guild:
            return

        # 1. 채널 로그 기록
        if self.logging_cog:
            await self.logging_cog.log_message(message)

        # 2. 활동 기록
        if self.activity_cog:
            await self.activity_cog.record_message(message)

        # 3. 대화 기록 (AI 메모리)
        if self.ai_handler:
            await self.ai_handler.add_message_to_history(message)

        # 4. 명령어 처리
        await self.bot.process_commands(message)

        # 5. AI 상호작용 처리 (명령어가 아닐 경우)
        if not message.content.startswith(self.bot.command_prefix):
            await self._handle_ai_interaction(message)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        # 메시지 삭제 로그는 기본 파일 로거로만 처리 (채널에 보내면 너무 많아짐)
        if message.author.bot or isinstance(message.channel, discord.DMChannel) or not message.guild: return
        content_to_log = message.content if message.content else "(내용 없음)"
        if message.attachments:
            content_to_log += f" [첨부: {', '.join([f'{att.filename}({att.size}b)' for att in message.attachments])}]"
        log_msg = f"메시지 삭제됨 | 채널: #{message.channel.name} | 작성자: {message.author} | 내용: {content_to_log}"
        logger.warning(log_msg)

async def setup(bot: commands.Bot):
    await bot.add_cog(EventListeners(bot))

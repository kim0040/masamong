# -*- coding: utf-8 -*-
"""
`!운세`, `!요약` 등 재미와 편의를 위한 기능을 제공하는 Cog입니다.
명령어뿐만 아니라, 특정 키워드에 반응하여 기능을 실행하기도 합니다.
"""

import discord
from discord.ext import commands
from typing import Dict
from datetime import datetime, timedelta

import config
from logger_config import logger
from .ai_handler import AIHandler

class FunCog(commands.Cog):
    """재미, 편의 목적의 명령어 및 키워드 기반 기능을 그룹화하는 클래스입니다."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ai_handler: AIHandler | None = None # main.py에서 주입됨
        # 채널별 키워드 기능 쿨다운을 관리하는 딕셔너리
        self.keyword_cooldowns: Dict[int, datetime] = {}
        logger.info("FunCog가 성공적으로 초기화되었습니다.")

    # --- 쿨다운 관리 ---

    def is_on_cooldown(self, channel_id: int) -> bool:
        """특정 채널이 키워드 기능 쿨다운 상태인지 확인합니다."""
        cooldown_seconds = config.FUN_KEYWORD_TRIGGERS.get("cooldown_seconds", 60)
        last_time = self.keyword_cooldowns.get(channel_id)
        if last_time and (datetime.now() - last_time) < timedelta(seconds=cooldown_seconds):
            return True
        return False

    def update_cooldown(self, channel_id: int):
        """특정 채널의 키워드 기능 쿨다운을 현재 시간으로 갱신합니다."""
        self.keyword_cooldowns[channel_id] = datetime.now()
        logger.debug(f"FunCog: 채널({channel_id})의 키워드 응답 쿨다운이 갱신되었습니다.")

    # --- 핵심 실행 로직 ---

    async def execute_fortune(self, channel: discord.TextChannel, author: discord.User):
        """
        AI를 호출하여 오늘의 운세를 생성하고 채널에 전송하는 핵심 로직입니다.
        `!운세` 명령어 또는 키워드 트리거에 의해 호출됩니다.
        """
        if not self.ai_handler or not self.ai_handler.is_ready:
            await channel.send("죄송합니다, AI 운세 기능이 현재 준비되지 않았습니다.")
            return

        async with channel.typing():
            try:
                response_text = await self.ai_handler.generate_creative_text(
                    channel=channel,
                    author=author,
                    prompt_key='fortune',
                    context={'user_name': author.display_name}
                )
                # AI 응답 생성 실패 시 기본 메시지 전송
                if not response_text or response_text in [config.MSG_AI_ERROR, config.MSG_CMD_ERROR]:
                    await channel.send(response_text or "운세를 보다가 깜빡 졸았네요. 다시 물어봐 주세요.")
                else:
                    await channel.send(response_text)
            except Exception as e:
                logger.error(f"운세 기능 실행 중 오류: {e}", exc_info=True, extra={'guild_id': channel.guild.id})
                await channel.send(config.MSG_CMD_ERROR)

    async def execute_summarize(self, channel: discord.TextChannel, author: discord.User):
        """
        AI를 호출하여 최근 대화를 요약하고 채널에 전송하는 핵심 로직입니다.
        `!요약` 명령어 또는 키워드 트리거에 의해 호출됩니다.
        """
        if not self.ai_handler or not self.ai_handler.is_ready or not config.AI_MEMORY_ENABLED:
            await channel.send("죄송합니다, 대화 요약 기능이 현재 준비되지 않았습니다.")
            return

        async with channel.typing():
            try:
                # AI 핸들러를 통해 DB에서 최근 대화 기록을 가져옵니다.
                history_str = await self.ai_handler.get_recent_conversation_text(channel.guild.id, channel.id, look_back=20)

                if not history_str:
                    await channel.send("요약할 만한 대화가 충분히 쌓이지 않았어요.")
                    return

                response_text = await self.ai_handler.generate_creative_text(
                    channel=channel,
                    author=author,
                    prompt_key='summarize',
                    context={'conversation': history_str}
                )
                
                # AI 응답 생성 실패 시 기본 메시지 전송
                if not response_text or response_text in [config.MSG_AI_ERROR, config.MSG_CMD_ERROR]:
                    await channel.send(response_text or "대화 내용을 요약하다가 머리에 쥐났어요. 다시 시도해주세요.")
                else:
                    await channel.send(f"**📈 최근 대화 요약 (마사몽 ver.)**\n{response_text}")
            except Exception as e:
                logger.error(f"요약 기능 실행 중 오류: {e}", exc_info=True, extra={'guild_id': channel.guild.id})
                await channel.send(config.MSG_CMD_ERROR)

    # --- 명령어 정의 ---

    @commands.command(name='운세', aliases=['fortune'])
    async def fortune(self, ctx: commands.Context):
        """'마사몽' 페르소나로 오늘의 운세를 알려줍니다."""
        await self.execute_fortune(ctx.channel, ctx.author)

    @commands.command(name='요약', aliases=['summarize', 'summary'])
    async def summarize(self, ctx: commands.Context):
        """현재 채널의 최근 대화를 요약합니다."""
        await self.execute_summarize(ctx.channel, ctx.author)

async def setup(bot: commands.Bot):
    """Cog를 봇에 등록하는 함수입니다."""
    await bot.add_cog(FunCog(bot))
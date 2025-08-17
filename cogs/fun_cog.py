# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
from typing import Dict
from datetime import datetime, timedelta

import config
from logger_config import logger
from .ai_handler import AIHandler

class FunCog(commands.Cog):
    """오늘의 운세, 대화 요약 등 재미와 편의를 위한 기능을 제공하는 Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ai_handler: AIHandler | None = None
        self.keyword_cooldowns: Dict[int, datetime] = {}

    def is_on_cooldown(self, channel_id: int) -> bool:
        cooldown_seconds = config.FUN_KEYWORD_TRIGGERS.get("cooldown_seconds", 60)
        last_time = self.keyword_cooldowns.get(channel_id)
        if last_time and (datetime.now() - last_time) > timedelta(seconds=cooldown_seconds):
            return False
        return True if last_time else False


    def update_cooldown(self, channel_id: int):
        self.keyword_cooldowns[channel_id] = datetime.now()
        logger.info(f"FunCog: 채널({channel_id}) 키워드 응답 쿨다운 시작.")

    async def execute_fortune(self, channel: discord.TextChannel, author: discord.User):
        """운세 기능의 실제 로직을 수행합니다."""
        if not self.ai_handler:
            await channel.send("죄송합니다, AI 기능이 현재 준비되지 않았습니다.")
            return

        async with channel.typing():
            try:
                # [로직 개선] 통합된 페르소나를 사용하도록 channel과 author 정보 전달
                response_text = await self.ai_handler.generate_creative_text(
                    channel=channel,
                    author=author,
                    prompt_key='fortune',
                    context={'user_name': author.display_name}
                )
                if response_text and response_text not in [config.MSG_AI_ERROR, config.MSG_CMD_ERROR]:
                    await channel.send(response_text)
                else:
                    await channel.send(response_text or "운세를 보다가 깜빡 졸았네. 다시 물어봐 줘.")
            except Exception as e:
                logger.error(f"운세 기능 실행 중 오류: {e}", exc_info=True)
                await channel.send(config.MSG_CMD_ERROR)

    async def execute_summarize(self, channel: discord.TextChannel, author: discord.User):
        """대화 요약 기능의 실제 로직을 수행합니다."""
        if not self.ai_handler or not config.AI_MEMORY_ENABLED:
            await channel.send("죄송합니다, 요약 기능이 현재 준비되지 않았습니다.")
            return

        async with channel.typing():
            try:
                # AI 핸들러를 통해 DB에서 최근 대화 기록을 가져옴
                history_str = await self.ai_handler.get_recent_conversation_text(channel.id, look_back=20)

                if not history_str:
                    await channel.send("요약할 만한 대화가 충분히 쌓이지 않았어.")
                    return

                # 요약용 프롬프트의 최대 길이를 초과하지 않도록 조절 (필요 시)
                # 현재는 AI_CREATIVE_PROMPTS에서 처리하므로 별도 로직은 생략

                # [로직 개선] 통합된 페르소나를 사용하도록 channel과 author 정보 전달
                response_text = await self.ai_handler.generate_creative_text(
                    channel=channel,
                    author=author,
                    prompt_key='summarize',
                    context={'conversation': history_str}
                )
                if response_text and response_text not in [config.MSG_AI_ERROR, config.MSG_CMD_ERROR]:
                    await channel.send(f"**📈 최근 대화 요약 (마사몽 ver.)**\n{response_text}")
                else:
                    await channel.send(response_text or "대화 내용을 요약하다가 머리에 쥐났어. 다시 시도해봐.")
            except Exception as e:
                logger.error(f"요약 기능 실행 중 오류: {e}", exc_info=True)
                await channel.send(config.MSG_CMD_ERROR)

    @commands.command(name='운세', aliases=['fortune'])
    async def fortune(self, ctx: commands.Context):
        """'마사몽' 페르소나로 오늘의 운세를 알려줍니다."""
        await self.execute_fortune(ctx.channel, ctx.author)

    @commands.command(name='요약', aliases=['summarize', 'summary'])
    async def summarize(self, ctx: commands.Context):
        """현재 채널의 최근 대화를 요약합니다."""
        await self.execute_summarize(ctx.channel, ctx.author)

async def setup(bot: commands.Bot):
    await bot.add_cog(FunCog(bot))

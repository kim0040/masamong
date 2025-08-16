# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
from typing import List

import config
from logger_config import logger
from .ai_handler import AIHandler

class FunCog(commands.Cog):
    """오늘의 운세, 대화 요약 등 재미와 편의를 위한 기능을 제공하는 Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ai_handler: AIHandler | None = None

    @commands.Cog.listener()
    async def on_ready(self):
        self.ai_handler = self.bot.get_cog('AIHandler')
        logger.info("FunCog 준비 완료.")

    async def get_conversation_for_summary(self, channel_id: int) -> str:
        """
        요약을 위해 지정된 채널의 최근 대화 기록을 문자열로 가져옵니다.
        :param channel_id: 요약할 Discord 채널의 ID.
        :return: 요약할 대화 내용 문자열 또는 오류 메시지.
        """
        if not self.ai_handler or not self.ai_handler.is_ready or not config.AI_MEMORY_ENABLED:
            return "오류: 요약 기능이 현재 준비되지 않았습니다."

        history_list = await self.ai_handler._get_history_from_db(channel_id)
        if not history_list or len(history_list) < 5:
            return "오류: 요약할 만한 대화가 충분히 쌓이지 않았습니다."

        history_str = "\n".join([item['parts'][0]['text'] for item in history_list])

        if len(history_str) > config.AI_SUMMARY_MAX_CHARS:
            truncated_len = len(history_str) - config.AI_SUMMARY_MAX_CHARS
            history_str = history_str[truncated_len:]
            logger.warning(f"요약용 대화 기록이 너무 길어 {truncated_len}자를 잘라냈습니다.")

        return history_str

    @commands.command(name='운세', aliases=['fortune'])
    async def fortune_command(self, ctx: commands.Context):
        """(레거시) '마사몽' 페르소나로 오늘의 운세를 알려줍니다."""
        if not self.ai_handler or not self.ai_handler.is_ready:
            await ctx.reply("죄송합니다, AI 기능이 현재 준비되지 않았습니다.", mention_author=False)
            return

        async with ctx.typing():
            prompt = config.AI_CREATIVE_PROMPTS.get('fortune', "").format(user_name=ctx.author.display_name)
            if not prompt:
                await ctx.reply("오류: 운세 프롬프트를 찾을 수 없습니다.", mention_author=False)
                return

            response = await self.ai_handler.process_direct_prompt_task(
                prompt=prompt,
                author=ctx.author,
                channel=ctx.channel
            )
            await ctx.reply(response, mention_author=False)


    @commands.command(name='요약', aliases=['summarize', 'summary'])
    async def summarize_command(self, ctx: commands.Context):
        """(레거시) 현재 채널의 최근 대화를 요약합니다."""
        if not self.ai_handler or not self.ai_handler.is_ready:
            await ctx.reply("죄송합니다, AI 기능이 현재 준비되지 않았습니다.", mention_author=False)
            return

        async with ctx.typing():
            conversation_text = await self.get_conversation_for_summary(ctx.channel.id)
            if "오류:" in conversation_text:
                await ctx.reply(conversation_text, mention_author=False)
                return

            prompt = config.AI_CREATIVE_PROMPTS.get('summarize', "").format(conversation=conversation_text)
            if not prompt:
                await ctx.reply("오류: 요약 프롬프트를 찾을 수 없습니다.", mention_author=False)
                return

            response = await self.ai_handler.process_direct_prompt_task(
                prompt=prompt,
                author=ctx.author,
                channel=ctx.channel
            )
            await ctx.reply(response, mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(FunCog(bot))

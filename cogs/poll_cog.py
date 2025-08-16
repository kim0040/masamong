# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
import re
from typing import List

from logger_config import logger

class PollCog(commands.Cog):
    """간단한 투표 기능을 제공하는 Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def create_poll(
        self,
        channel_id: int,
        author_name: str,
        question: str,
        choices: List[str]
    ) -> str:
        """
        주어진 채널에 투표를 생성합니다.
        :param channel_id: 투표를 생성할 Discord 채널의 ID.
        :param author_name: 투표를 시작한 사용자의 이름.
        :param question: 투표의 질문.
        :param choices: 투표의 선택지 목록. 최소 1개, 최대 10개까지 가능합니다.
        :return: 투표 생성 성공 또는 실패에 대한 결과 메시지.
        """
        channel = self.bot.get_channel(channel_id)
        if not channel:
            logger.error(f"투표 생성 불가: 채널 ID({channel_id})를 찾을 수 없음.")
            return "오류: 투표를 생성할 채널을 찾을 수 없습니다."

        if not (1 <= len(choices) <= 10):
            return "오류: 선택 항목은 1개 이상, 10개 이하로 만들어야 합니다."

        number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

        description = []
        for i, choice in enumerate(choices):
            description.append(f"{number_emojis[i]} {choice}")

        embed = discord.Embed(
            title=f"🗳️ {question}",
            description="\n".join(description),
            color=discord.Color.blue()
        )
        embed.set_footer(text=f"{author_name}님이 시작한 투표")

        try:
            poll_message = await channel.send(embed=embed)
            for i in range(len(choices)):
                await poll_message.add_reaction(number_emojis[i])
            return f"'{question}'에 대한 투표를 성공적으로 생성했습니다."
        except Exception as e:
            logger.error(f"투표 생성 중 오류: {e}", exc_info=True)
            return "투표를 만드는 데 실패했습니다. Discord 권한 등을 확인해주세요."


    @commands.command(name='투표', aliases=['poll'])
    @commands.guild_only()
    async def poll_command(self, ctx: commands.Context, *, content: str = ""):
        """
        (레거시) 간단한 투표를 생성합니다.
        사용법: !투표 "질문" "항목1" "항목2" ... (최대 10개)
        """
        if not content:
            await ctx.reply('명령어 형식이 잘못됐어. `!투표 "질문" "항목1" "항목2"` 처럼 써줘!')
            return

        options = re.findall(r'"(.*?)"', content)

        if len(options) < 2:
            await ctx.reply('투표를 만들려면 질문과 최소 하나 이상의 선택 항목이 필요해. `"질문" "항목1"` 형식으로 다시 써줘.')
            return

        question = options[0]
        choices = options[1:]

        async with ctx.typing():
            result = await self.create_poll(
                channel_id=ctx.channel.id,
                author_name=ctx.author.display_name,
                question=question,
                choices=choices
            )
            # 도구의 결과가 성공 메시지일 경우엔 별도 응답 없이 투표만 생성되도록 함
            if "오류" in result or "실패" in result:
                await ctx.reply(result, mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(PollCog(bot))

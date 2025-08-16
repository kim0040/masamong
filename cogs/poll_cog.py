# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
import re

from logger_config import logger

class PollCog(commands.Cog):
    """간단한 투표 기능을 제공하는 Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name='투표', aliases=['poll'])
    @commands.guild_only()
    async def poll(self, ctx: commands.Context, *, content: str = ""):
        """
        간단한 투표를 생성합니다.
        사용법: !투표 "질문" "항목1" "항목2" ... (최대 10개)
        """
        if not content:
            await ctx.send('명령어 형식이 잘못됐어. `!투표 "질문" "항목1" "항목2"` 처럼 써줘!')
            return

        options = re.findall(r'"(.*?)"', content)

        if len(options) < 2:
            await ctx.send('투표를 만들려면 질문과 최소 하나 이상의 선택 항목이 필요해. `"질문" "항목1"` 형식으로 다시 써줘.')
            return

        if len(options) > 11:
            await ctx.send('선택 항목은 최대 10개까지만 만들 수 있어.')
            return

        question = options[0]
        choices = options[1:]

        number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

        description = []
        for i, choice in enumerate(choices):
            description.append(f"{number_emojis[i]} {choice}")

        embed = discord.Embed(
            title=f"🗳️ {question}",
            description="\n".join(description),
            color=discord.Color.blue()
        )
        embed.set_footer(text=f"{ctx.author.display_name}님이 시작한 투표")

        try:
            poll_message = await ctx.send(embed=embed)
            for i in range(len(choices)):
                await poll_message.add_reaction(number_emojis[i])
        except Exception as e:
            logger.error(f"투표 생성 중 오류: {e}", exc_info=True)
            await ctx.send("투표를 만드는 데 실패했어. 다시 시도해줘.")

async def setup(bot: commands.Bot):
    await bot.add_cog(PollCog(bot))

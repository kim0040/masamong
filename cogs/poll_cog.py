# -*- coding: utf-8 -*-
"""
`!투표` 명령어를 통해 간단한 찬반 투표를 생성하는 기능을 담당하는 Cog입니다.
"""

import discord
from discord.ext import commands

from logger_config import logger

class PollCog(commands.Cog):
    """간단한 투표 기능을 제공하는 클래스입니다."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        logger.info("PollCog가 성공적으로 초기화되었습니다.")

    @commands.command(name='투표', aliases=['poll'])
    @commands.guild_only()
    async def poll(self, ctx: commands.Context, question: str, *choices: str):
        """
        주어진 질문과 선택지로 투표를 생성합니다.

        사용법: `!투표 "질문" "항목1" "항목2" ...`
        - 질문과 각 항목은 큰따옴표("")로 묶어야 합니다.
        - 선택 항목은 최대 10개까지 가능합니다.
        """
        if not choices:
            await ctx.send('투표를 만들려면 질문과 최소 하나 이상의 선택 항목이 필요해요. `!투표 "질문" "항목1"` 형식으로 다시 써주세요.')
            return

        if len(choices) > 10:
            await ctx.send('선택 항목은 최대 10개까지만 만들 수 있어요.')
            return

        # 숫자 이모지를 순서대로 사용하여 선택 항목을 표시합니다.
        number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

        description = []
        for i, choice in enumerate(choices):
            description.append(f"{number_emojis[i]} {choice}")

        # 투표 내용을 담을 임베드를 생성합니다.
        embed = discord.Embed(
            title=f"🗳️ {question}",
            description="\n".join(description),
            color=discord.Color.blue()
        )
        embed.set_footer(text=f"{ctx.author.display_name}님이 시작한 투표")

        try:
            # 임베드를 전송하고, 선택 항목 수만큼 반응 이모지를 추가합니다.
            poll_message = await ctx.send(embed=embed)
            for i in range(len(choices)):
                await poll_message.add_reaction(number_emojis[i])
        except Exception as e:
            logger.error(f"투표 생성 중 오류 발생: {e}", exc_info=True, extra={{'guild_id': ctx.guild.id}})
            await ctx.send("투표를 만드는 데 실패했어요. 다시 시도해주세요.")

async def setup(bot: commands.Bot):
    """Cog를 봇에 등록하는 함수입니다."""
    await bot.add_cog(PollCog(bot))
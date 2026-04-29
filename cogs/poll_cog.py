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
        """PollCog를 초기화합니다."""
        self.bot = bot
        logger.info("PollCog가 성공적으로 초기화되었습니다.")

    @commands.command(name='투표', aliases=['poll'])
    @commands.guild_only()
    async def poll(self, ctx: commands.Context, question: str = None, *choices: str):
        """
        간단한 찬반/다중 선택 투표를 만듭니다. (서버 전용)

        사용법:
        - 찬반 투표: `!투표 "점심으로 햄버거 어때?"`
        - 선택 투표: `!투표 "점심 메뉴" "햄버거" "피자" "치킨"`

        예시:
        - `!투표 "회식 장소" "삼겹살" "횟집" "곱창"`
        - `!투표 "지금 회의할까?"`

        참고:
        - 선택지는 최대 10개입니다.
        - 질문/항목에 공백이 있다면 큰따옴표로 감싸 주세요.
        """
        if not question:
            await ctx.send('🚫 투표 주제가 없어요!\n**사용법**: `!투표 "주제" "항목1" "항목2"`\n(예: `!투표 "회식 장소" "삼겹살" "횟집"`)')
            return

        # 선택지가 없으면 자동으로 찬반 투표 생성
        if not choices:
            embed = discord.Embed(
                title=f"🗳️ {question}",
                description="찬성(⭕) 혹은 반대(❌)를 눌러주세요!",
                color=discord.Color.green()
            )
            embed.set_footer(text=f"{ctx.author.display_name}님이 주최함")
            poll_msg = await ctx.send(embed=embed)
            await poll_msg.add_reaction("⭕")
            await poll_msg.add_reaction("❌")
            return

        if len(choices) > 10:
            await ctx.send('😅 선택 항목은 최대 10개까지만 만들 수 있어요.')
            return

        # 숫자 이모지를 순서대로 사용하여 선택 항목을 표시합니다.
        number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

        description = []
        for i, choice in enumerate(choices):
            description.append(f"{number_emojis[i]} {choice}")

        # 투표 내용을 담을 임베드를 생성합니다.
        embed = discord.Embed(
            title=f"🗳️ {question}",
            description="\n\n".join(description),
            color=discord.Color.blue()
        )
        embed.set_footer(text=f"{ctx.author.display_name}님이 주최함")

        try:
            # 임베드를 전송하고, 선택 항목 수만큼 반응 이모지를 추가합니다.
            poll_message = await ctx.send(embed=embed)
            for i in range(len(choices)):
                await poll_message.add_reaction(number_emojis[i])
        except Exception as e:
            logger.error(f"투표 생성 중 오류 발생: {e}", exc_info=True, extra={'guild_id': ctx.guild.id})
            await ctx.send("🚫 투표를 생성하다가 문제가 생겼어요. 다시 시도해주세요.")

async def setup(bot: commands.Bot):
    """Cog를 봇에 등록하는 함수입니다."""
    await bot.add_cog(PollCog(bot))

# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
from datetime import datetime
import pytz

import config
from logger_config import logger
from .ai_handler import AIHandler

KST = pytz.timezone('Asia/Seoul')

class ActivityCog(commands.Cog):
    """서버 멤버의 활동량을 DB에 기록하고 랭킹을 보여주는 Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ai_handler: AIHandler | None = None
        # JSON 파일 및 주기적 저장 로직 제거

    @commands.Cog.listener()
    async def on_ready(self):
        """Cog가 준비되었을 때 AI 핸들러를 가져옵니다."""
        # main.py에서 의존성 주입이 이루어지므로, 여기서는 로깅만 수행
        if self.bot.get_cog('AIHandler'):
            self.ai_handler = self.bot.get_cog('AIHandler')
            logger.info("ActivityCog: AIHandler 의존성 주입 확인.")
        else:
            logger.error("ActivityCog: AIHandler를 찾을 수 없습니다.")

    async def record_message(self, message: discord.Message):
        """
        메시지 활동을 데이터베이스에 기록합니다.
        INSERT ... ON CONFLICT 구문을 사용하여 원자적으로 업데이트합니다.
        """
        if not message.guild or not self.bot.db:
            return

        now_utc_str = datetime.utcnow().isoformat()
        guild_id = message.guild.id
        user_id = message.author.id

        sql = """
            INSERT INTO user_activity (guild_id, user_id, last_active_at, message_count)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                message_count = message_count + 1,
                last_active_at = excluded.last_active_at;
        """
        try:
            await self.bot.db.execute(sql, (guild_id, user_id, now_utc_str))
            await self.bot.db.commit()
            logger.debug(f"사용자 활동 기록: User {user_id} in Guild {guild_id}")
        except Exception as e:
            logger.error(f"사용자 활동 DB 기록 중 오류: {e}", exc_info=True)

    @commands.command(name='랭킹', aliases=['수다왕', 'ranking'])
    @commands.guild_only()
    async def ranking(self, ctx: commands.Context):
        """서버 활동 랭킹(메시지 수 기준)을 DB에서 조회하여 보여줍니다."""
        if not self.ai_handler:
            await ctx.send("죄송합니다, AI 기능이 현재 준비되지 않았습니다.")
            return
        if not self.bot.db:
            await ctx.send("죄송합니다, 데이터베이스가 현재 준비되지 않았습니다.")
            return

        guild_id = ctx.guild.id
        sql = """
            SELECT user_id, message_count FROM user_activity
            WHERE guild_id = ?
            ORDER BY message_count DESC
            LIMIT 5;
        """

        async with ctx.typing():
            try:
                async with self.bot.db.execute(sql, (guild_id,)) as cursor:
                    sorted_users = await cursor.fetchall()
            except Exception as e:
                logger.error(f"랭킹 조회 DB 쿼리 중 오류: {e}", exc_info=True)
                await ctx.send(config.MSG_CMD_ERROR)
                return

            if not sorted_users:
                await ctx.send("아직 서버 활동 데이터가 충분하지 않아. 다들 분발하라구!")
                return

            ranking_list = []
            for i, (user_id, count) in enumerate(sorted_users):
                try:
                    user = await self.bot.fetch_user(user_id)
                    user_name = user.display_name
                except discord.NotFound:
                    user_name = f"알수없는유저({str(user_id)[-4:]})"

                ranking_list.append(f"{i+1}위: {user_name} ({count}회)")

            ranking_str = "\n".join(ranking_list)
            response_text = await self.ai_handler.generate_creative_text(
                channel=ctx.channel,
                author=ctx.author,
                prompt_key='ranking',
                context={'ranking_list': ranking_str}
            )

            final_response = response_text if response_text and "오류" not in response_text else f"**🏆 이번 주 수다왕 랭킹! 🏆**\n\n{ranking_str}"
            await ctx.send(final_response)

async def setup(bot: commands.Bot):
    await bot.add_cog(ActivityCog(bot))

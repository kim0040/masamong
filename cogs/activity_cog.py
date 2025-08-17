# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
import aiosqlite
from datetime import datetime

import config
from logger_config import logger
from .ai_handler import AIHandler

class ActivityCog(commands.Cog):
    """서버 멤버의 활동량을 DB에 기록하고 랭킹을 보여주는 Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ai_handler: AIHandler | None = None
        logger.info("ActivityCog 초기화 완료.")

    async def record_message(self, message: discord.Message):
        """메시지 활동을 DB에 기록합니다."""
        if not message.guild or message.author.bot:
            return

        log_extra = {'guild_id': message.guild.id}
        try:
            guild_id = message.guild.id
            user_id = message.author.id
            now_utc_str = datetime.utcnow().isoformat()

            await self.bot.db.execute("""
                INSERT INTO user_activity (user_id, guild_id, message_count, last_active_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(user_id, guild_id) DO UPDATE SET
                    message_count = message_count + 1,
                    last_active_at = excluded.last_active_at;
            """, (user_id, guild_id, now_utc_str))
            await self.bot.db.commit()

        except aiosqlite.OperationalError as e:
            logger.error(f"DB 파일을 찾을 수 없거나 쓰기 권한이 없습니다. '{config.DATABASE_FILE}' 경로를 확인하세요. 오류: {e}", extra=log_extra)
        except aiosqlite.Error as e:
            logger.error(f"활동 기록 중 DB 오류 발생: {e}", exc_info=True, extra=log_extra)

    @commands.command(name='랭킹', aliases=['수다왕', 'ranking'])
    @commands.guild_only()
    async def ranking(self, ctx: commands.Context):
        """서버 활동 랭킹(메시지 수 기준)을 DB에서 조회하여 보여줍니다."""
        if not self.ai_handler:
            await ctx.send("죄송합니다, AI 기능이 현재 준비되지 않았습니다.")
            return

        log_extra = {'guild_id': ctx.guild.id}
        try:
            async with self.bot.db.execute("""
                SELECT user_id, message_count FROM user_activity
                WHERE guild_id = ?
                ORDER BY message_count DESC
                LIMIT 5;
            """, (ctx.guild.id,)) as cursor:
                top_users = await cursor.fetchall()

        except aiosqlite.OperationalError as e:
            logger.error(f"랭킹 조회 중 DB 파일을 찾을 수 없습니다. '{config.DATABASE_FILE}' 경로를 확인하세요. 오류: {e}", extra=log_extra)
            await ctx.send(config.MSG_CMD_ERROR)
            return
        except aiosqlite.Error as e:
            logger.error(f"랭킹 조회 중 DB 오류 발생: {e}", exc_info=True, extra=log_extra)
            await ctx.send(config.MSG_CMD_ERROR)
            return

        if not top_users:
            await ctx.send("아직 서버 활동 데이터가 충분하지 않아. 다들 분발하라구!")
            return

        async with ctx.typing():
            ranking_list = []
            for i, (user_id, count) in enumerate(top_users):
                try:
                    user = self.bot.get_user(int(user_id)) or await self.bot.fetch_user(int(user_id))
                    user_name = user.display_name
                except discord.NotFound:
                    user_name = f"알수없는유저({str(user_id)[-4:]})"
                except (ValueError, TypeError):
                    user_name = f"잘못된ID({user_id})"

                ranking_list.append(f"{i+1}위: {user_name} ({count}회)")

            ranking_str = "\n".join(ranking_list)

            if not self.ai_handler or not self.ai_handler.is_ready:
                 await ctx.send(f"**🏆 이번 주 수다왕 랭킹! 🏆**\n\n{ranking_str}")
                 return

            response_text = await self.ai_handler.generate_creative_text(
                channel=ctx.channel,
                author=ctx.author,
                prompt_key='ranking',
                context={'ranking_list': ranking_str}
            )

            final_response = response_text
            if not response_text or response_text in [config.MSG_AI_ERROR, config.MSG_CMD_ERROR]:
                final_response = f"**🏆 이번 주 수다왕 랭킹! 🏆**\n\n{ranking_str}"

            await ctx.send(final_response)

async def setup(bot: commands.Bot):
    await bot.add_cog(ActivityCog(bot))

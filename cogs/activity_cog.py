# -*- coding: utf-8 -*-
"""
사용자 활동을 기록하고, 서버 내 활동 순위를 보여주는 기능을 담당하는 Cog입니다.
"""

import discord
from discord.ext import commands
import aiosqlite
from datetime import datetime, timezone

import config
from logger_config import logger
from .ai_handler import AIHandler

class ActivityCog(commands.Cog):
    """서버 멤버의 메시지 활동량을 데이터베이스에 기록하고, `!랭킹` 명령어를 처리합니다."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ai_handler: AIHandler | None = None # main.py에서 주입됨
        logger.info("ActivityCog가 성공적으로 초기화되었습니다.")

    async def record_message(self, message: discord.Message):
        """
        사용자가 보낸 메시지를 데이터베이스에 기록합니다.
        메시지가 발생할 때마다 `user_activity` 테이블의 `message_count`를 1 증가시킵니다.
        """
        # 봇 메시지거나 DM 채널인 경우 무시
        if not message.guild or message.author.bot:
            return

        log_extra = {'guild_id': message.guild.id, 'author_id': message.author.id}
        try:
            guild_id = message.guild.id
            user_id = message.author.id
            now_utc_str = datetime.now(timezone.utc).isoformat()

            # ON CONFLICT를 사용하여 INSERT 또는 UPDATE를 한 번의 쿼리로 처리 (UPSERT)
            await self.bot.db.execute("""
                INSERT INTO user_activity (user_id, guild_id, message_count, last_active_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(user_id, guild_id) DO UPDATE SET
                    message_count = message_count + 1,
                    last_active_at = excluded.last_active_at;
            """, (user_id, guild_id, now_utc_str))
            await self.bot.db.commit()

        except aiosqlite.Error as e:
            logger.error(f"활동 기록 중 데이터베이스 오류 발생: {e}", exc_info=True, extra=log_extra)

    @commands.command(name='랭킹', aliases=['수다왕', 'ranking'])
    @commands.guild_only()
    async def ranking(self, ctx: commands.Context):
        """
        서버 내 메시지 작성 수 기준 TOP5 랭킹을 발표합니다. (서버 전용)

        사용법:
        - `!랭킹`

        예시:
        - `!랭킹`
        """
        if not self.ai_handler:
            await ctx.send("랭킹을 발표할 AI가 아직 준비되지 않았어요. 잠시 후 다시 시도해주세요.")
            return

        log_extra = {'guild_id': ctx.guild.id, 'author_id': ctx.author.id}
        try:
            # 데이터베이스에서 상위 5명 조회
            async with self.bot.db.execute("""
                SELECT user_id, message_count FROM user_activity
                WHERE guild_id = ?
                ORDER BY message_count DESC
                LIMIT 5;
            """, (ctx.guild.id,)) as cursor:
                top_users = await cursor.fetchall()

        except aiosqlite.Error as e:
            logger.error(f"랭킹 조회 중 데이터베이스 오류 발생: {e}", exc_info=True, extra=log_extra)
            await ctx.send(config.MSG_CMD_ERROR)
            return

        if not top_users:
            await ctx.send("아직 서버 활동 데이터가 충분하지 않아요. 다들 분발해주세요!")
            return

        async with ctx.typing():
            # 랭킹 목록 문자열 생성
            ranking_list = []
            for i, (user_id, count) in enumerate(top_users):
                try:
                    # user_id로 Discord 사용자 객체 조회
                    user = self.bot.get_user(int(user_id)) or await self.bot.fetch_user(int(user_id))
                    user_name = user.display_name
                except discord.NotFound:
                    user_name = f"알수없는유저({str(user_id)[-4:]})"
                except (ValueError, TypeError):
                    user_name = f"잘못된ID({user_id})"
                ranking_list.append(f"{i+1}위: {user_name} ({count}회)")

            ranking_str = "\n".join(ranking_list)

            # AI 핸들러를 사용하여 창의적인 랭킹 발표 멘트 생성
            if not self.ai_handler.is_ready:
                 await ctx.send(f"**🏆 이번 주 수다왕 랭킹! 🏆**\n\n{ranking_str}")
                 return

            response_text = await self.ai_handler.generate_creative_text(
                channel=ctx.channel,
                author=ctx.author,
                prompt_key='ranking',
                context={'ranking_list': ranking_str}
            )

            # AI 응답 생성에 실패하면 기본 텍스트 사용
            if not response_text or response_text in [config.MSG_AI_ERROR, config.MSG_CMD_ERROR]:
                final_response = f"**🏆 이번 주 수다왕 랭킹! 🏆**\n\n{ranking_str}"
            else:
                final_response = response_text

            await ctx.send(final_response)

async def setup(bot: commands.Bot):
    """Cog를 봇에 등록하는 함수"""
    await bot.add_cog(ActivityCog(bot))

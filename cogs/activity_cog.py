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
        """서버 내 활동 순위와 상세 통계를 보여줍니다."""
        if not self.ai_handler:
            await ctx.send("랭킹을 발표할 AI가 아직 준비되지 않았어요. 잠시 후 다시 시도해주세요.")
            return

        log_extra = {'guild_id': ctx.guild.id, 'author_id': ctx.author.id}
        try:
            # 1. 상위 10명 조회 (기존 5명에서 확대) 및 마지막 활동 시간 포함
            async with self.bot.db.execute("""
                SELECT user_id, message_count, last_active_at FROM user_activity
                WHERE guild_id = ?
                ORDER BY message_count DESC
                LIMIT 10;
            """, (ctx.guild.id,)) as cursor:
                top_users = await cursor.fetchall()

            # 2. 서버 전체 통계 조회
            async with self.bot.db.execute("""
                SELECT SUM(message_count), COUNT(user_id) FROM user_activity
                WHERE guild_id = ?
            """, (ctx.guild.id,)) as cursor:
                server_total = await cursor.fetchone()
                total_msgs, total_users = server_total if server_total else (0, 0)

        except aiosqlite.Error as e:
            logger.error(f"랭킹 조회 중 데이터베이스 오류 발생: {e}", exc_info=True, extra=log_extra)
            await ctx.send(config.MSG_CMD_ERROR)
            return

        if not top_users:
            await ctx.send("아직 서버 활동 데이터가 충분하지 않아요. 다들 분발해주세요!")
            return

        async with ctx.typing():
            # 랭킹 목록 및 상세 데이터 구성
            ranking_lines = []
            
            def get_grade(count):
                if count >= 1000: return "🏆 전설의 수다왕"
                if count >= 500: return "👑 열혈 수다쟁이"
                if count >= 100: return "✨ 활발한 멤버"
                if count >= 10: return "🌱 새내기 수다쟁이"
                return "🥚 부화 중"

            for i, (user_id, count, last_active) in enumerate(top_users):
                try:
                    user = self.bot.get_user(int(user_id)) or await self.bot.fetch_user(int(user_id))
                    user_name = user.display_name
                except:
                    user_name = f"탈퇴한유저({str(user_id)[-4:]})"
                
                grade = get_grade(count)
                # 마지막 활동 시간 포맷팅 (ISO -> HH:MM)
                last_time_str = "방금 전"
                if last_active:
                    try:
                        lt = datetime.fromisoformat(last_active)
                        last_time_str = lt.strftime("%y-%m-%d %H:%M")
                    except: pass
                
                ranking_lines.append(f"{i+1}위: {user_name} | {count}회 | {grade} (최근: {last_time_str})")

            ranking_data_str = "\n".join(ranking_lines)
            server_stat_str = f"서버 총 메시지: {total_msgs or 0}개 | 참여 인원: {total_users or 0}명"

            # AI에게 전달할 맥락 보강
            full_context = {
                'ranking_list': ranking_data_str,
                'server_stats': server_stat_str,
                'top_one_name': (self.bot.get_user(int(top_users[0][0])) or await self.bot.fetch_user(int(top_users[0][0]))).display_name if top_users else "없음"
            }

            response_text = await self.ai_handler.generate_creative_text(
                channel=ctx.channel,
                author=ctx.author,
                prompt_key='ranking',
                context=full_context
            )

            if not response_text or response_text in [config.MSG_AI_ERROR, config.MSG_CMD_ERROR]:
                final_response = f"**🏆 서버 수다왕 랭킹 리포트 🏆**\n\n{ranking_data_str}\n\n📊 {server_stat_str}"
            else:
                final_response = response_text

            await ctx.send(final_response)

async def setup(bot: commands.Bot):
    """Cog를 봇에 등록하는 함수"""
    await bot.add_cog(ActivityCog(bot))

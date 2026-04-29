# -*- coding: utf-8 -*-
"""
사용자 활동을 기록하고, 서버 내 활동 순위를 보여주는 기능을 담당하는 Cog입니다.
"""

import discord
from discord.ext import commands
import aiosqlite
import io
from datetime import datetime, timedelta, timezone

import config
from logger_config import logger
from .ai_handler import AIHandler
from utils.ranking_chart import build_activity_ranking_chart_bytes

KST = timezone(timedelta(hours=9))


class ActivityCog(commands.Cog):
    """서버 멤버의 메시지 활동량을 데이터베이스에 기록하고, `!랭킹` 명령어를 처리합니다."""

    def __init__(self, bot: commands.Bot):
        """ActivityCog를 초기화합니다."""
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
            if config.DB_BACKEND == "tidb":
                query = """
                    INSERT INTO user_activity (user_id, guild_id, message_count, last_active_at)
                    VALUES (?, ?, 1, ?)
                    ON DUPLICATE KEY UPDATE
                        message_count = message_count + 1,
                        last_active_at = VALUES(last_active_at);
                """
                log_query = """
                    INSERT IGNORE INTO user_activity_log (message_id, guild_id, channel_id, user_id, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """
            else:
                query = """
                    INSERT INTO user_activity (user_id, guild_id, message_count, last_active_at)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(user_id, guild_id) DO UPDATE SET
                        message_count = message_count + 1,
                        last_active_at = excluded.last_active_at;
                """
                log_query = """
                    INSERT OR IGNORE INTO user_activity_log (message_id, guild_id, channel_id, user_id, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """
            await self.bot.db.execute(query, (user_id, guild_id, now_utc_str))
            await self.bot.db.execute(
                log_query,
                (int(message.id), guild_id, int(message.channel.id), user_id, now_utc_str),
            )
            await self.bot.db.commit()

        except aiosqlite.Error as e:
            logger.error(f"활동 기록 중 데이터베이스 오류 발생: {e}", exc_info=True, extra=log_extra)

    @staticmethod
    def _parse_period_key(raw: str) -> str:
        token = (raw or "").strip().lower()
        if not token:
            return "all"
        if token in {"오늘", "금일", "today", "day"} or "오늘" in token:
            return "today"
        if token in {"이번주", "주간", "week", "thisweek"} or "이번주" in token or "주간" in token:
            return "week"
        if token in {"이번달", "이달", "month", "thismonth"} or "이번달" in token or "이달" in token:
            return "month"
        if token in {"전체", "누적", "all", "total"} or "전체" in token or "누적" in token:
            return "all"
        return "all"

    @staticmethod
    def _period_bounds(period_key: str) -> tuple[str | None, str | None, str]:
        now_kst = datetime.now(KST)
        if period_key == "today":
            start_kst = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
            label = f"오늘 ({start_kst.strftime('%Y-%m-%d')} KST)"
        elif period_key == "week":
            start_kst = (now_kst - timedelta(days=now_kst.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
            label = f"이번주 ({start_kst.strftime('%m/%d')}~{now_kst.strftime('%m/%d')} KST)"
        elif period_key == "month":
            start_kst = now_kst.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            label = f"이번달 ({start_kst.strftime('%Y-%m')} KST)"
        else:
            return None, None, "전체 (누적)"

        start_utc = start_kst.astimezone(timezone.utc).isoformat()
        end_utc = now_kst.astimezone(timezone.utc).isoformat()
        return start_utc, end_utc, label

    @staticmethod
    def _format_kst_time(iso_ts: str | None) -> str:
        if not iso_ts:
            return "정보 없음"
        try:
            normalized = str(iso_ts).replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(KST).strftime("%m/%d %H:%M")
        except Exception:
            return "정보 없음"

    @staticmethod
    def _grade_for_channel(count: int, total_msgs: int, total_users: int) -> tuple[str, float]:
        share = (count / total_msgs * 100.0) if total_msgs > 0 else 0.0
        avg = (total_msgs / max(1, total_users)) if total_msgs > 0 else 0.0
        ratio = (count / max(1.0, avg)) if avg > 0 else 0.0

        if ratio >= 3.2 or share >= 28:
            return "🔥 채널 지배자", share
        if ratio >= 2.1 or share >= 18:
            return "⚡ 폭주 기관차", share
        if ratio >= 1.3 or share >= 10:
            return "🎯 핵심 멤버", share
        if ratio >= 0.8 or share >= 5:
            return "🧃 꾸준 멤버", share
        return "🌱 워밍업 중", share

    @staticmethod
    def _sample_size_note(total_msgs: int, total_users: int) -> str:
        if total_msgs <= 30 or total_users <= 3:
            return "표본이 작아 순위 변동폭이 크게 보일 수 있어요."
        if total_users >= 50:
            return "참여 인원이 많아 상위권 경쟁 강도가 높은 구간이에요."
        return "표본이 안정적인 편이라 순위 해석 신뢰도가 무난해요."

    async def _resolve_user_name(self, guild: discord.Guild, user_id: int) -> str:
        member = guild.get_member(user_id)
        if member:
            return member.display_name
        try:
            fetched_member = await guild.fetch_member(user_id)
            if fetched_member:
                return fetched_member.display_name
        except Exception:
            pass
        user = self.bot.get_user(user_id)
        if user:
            return getattr(user, "display_name", None) or getattr(user, "name", str(user_id))
        try:
            fetched = await self.bot.fetch_user(user_id)
            return getattr(fetched, "display_name", None) or getattr(fetched, "name", str(user_id))
        except Exception:
            return f"탈퇴한유저({str(user_id)[-4:]})"

    @commands.command(name='랭킹', aliases=['수다왕', 'ranking'])
    @commands.guild_only()
    async def ranking(self, ctx: commands.Context, *, period_arg: str = ""):
        """서버 내 활동 순위와 상세 통계를 보여줍니다. (`오늘`/`이번주`/`이번달`/`전체`)"""
        if not self.ai_handler:
            await ctx.send("랭킹을 발표할 AI가 아직 준비되지 않았어요. 잠시 후 다시 시도해주세요.")
            return

        log_extra = {'guild_id': ctx.guild.id, 'author_id': ctx.author.id}
        period_key = self._parse_period_key(period_arg)
        period_start_utc, period_end_utc, period_label = self._period_bounds(period_key)
        try:
            where_clause = "WHERE guild_id = ? AND channel_id = ?"
            params: list = [ctx.guild.id, ctx.channel.id]
            if period_start_utc and period_end_utc:
                where_clause += " AND created_at >= ? AND created_at <= ?"
                params.extend([period_start_utc, period_end_utc])

            # 채널별 기간 집계 랭킹
            async with self.bot.db.execute(
                f"""
                SELECT user_id, COUNT(*) AS message_count, MAX(created_at) AS last_active_at
                FROM user_activity_log
                {where_clause}
                GROUP BY user_id
                ORDER BY message_count DESC, last_active_at DESC
                LIMIT 10;
                """,
                tuple(params),
            ) as cursor:
                top_users = await cursor.fetchall()

            async with self.bot.db.execute(
                f"""
                SELECT COUNT(*) AS total_messages, COUNT(DISTINCT user_id) AS total_users
                FROM user_activity_log
                {where_clause}
                """,
                tuple(params),
            ) as cursor:
                server_total = await cursor.fetchone()
                total_msgs, total_users = server_total if server_total else (0, 0)

        except aiosqlite.Error as e:
            logger.error(f"랭킹 조회 중 데이터베이스 오류 발생: {e}", exc_info=True, extra=log_extra)
            await ctx.send(config.MSG_CMD_ERROR)
            return

        if not top_users:
            await ctx.send(
                f"이 채널은 `{period_label}` 기준으로 집계할 활동 데이터가 아직 없어요. "
                "기간을 바꿔서 `!랭킹 전체`로도 확인해봐!"
            )
            return

        async with ctx.typing():
            ranking_lines = []
            ranking_rows: list[dict] = []

            for i, row in enumerate(top_users):
                user_id = int(row[0])
                count = int(row[1])
                user_name = await self._resolve_user_name(ctx.guild, user_id)

                grade, share = self._grade_for_channel(count, int(total_msgs or 0), int(total_users or 0))
                share_display = 0.1 if count > 0 and share < 0.1 else share
                ranking_lines.append(
                    f"{i+1}위: {user_name} | {count}회 | 점유율 {share_display:.1f}% | {grade}"
                )
                ranking_rows.append(
                    {
                        "rank": i + 1,
                        "user_name": user_name,
                        "count": count,
                        "share": share_display,
                        "grade": grade,
                    }
                )

            ranking_data_str = "\n".join(ranking_lines)
            server_stat_str = (
                f"집계 범위: #{ctx.channel.name} | 기간: {period_label} | "
                f"총 메시지: {int(total_msgs or 0)}개 | 참여 인원: {int(total_users or 0)}명"
            )
            sample_note = self._sample_size_note(int(total_msgs or 0), int(total_users or 0))
            chart_delivery_status = "랭킹 차트를 생성하지 못해 텍스트 기반 브리핑만 진행 중"
            try:
                chart_bytes = build_activity_ranking_chart_bytes(
                    channel_name=ctx.channel.name,
                    period_label=period_label,
                    ranking_rows=ranking_rows,
                    total_messages=int(total_msgs or 0),
                    total_users=int(total_users or 0),
                    generated_at_kst=datetime.now(KST),
                )
                chart_filename = f"masamong_activity_ranking_{ctx.guild.id}_{ctx.channel.id}.png"
                await ctx.send(file=discord.File(io.BytesIO(chart_bytes), filename=chart_filename))
                chart_delivery_status = (
                    f"랭킹 차트 이미지를 먼저 전송 완료 (파일명: {chart_filename})"
                )
            except Exception as chart_exc:
                logger.warning(
                    f"랭킹 차트 이미지 생성 실패: {chart_exc}",
                    exc_info=True,
                    extra=log_extra,
                )

            top_user_name = await self._resolve_user_name(ctx.guild, int(top_users[0][0])) if top_users else "없음"
            full_context = {
                'ranking_list': ranking_data_str,
                'server_stats': server_stat_str,
                'top_one_name': top_user_name,
                'chart_delivery_status': chart_delivery_status,
                'sample_size_note': sample_note,
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

# -*- coding: utf-8 -*-
"""
봇의 백그라운드 유지보수 작업을 관리하는 Cog입니다.

주요 기능:
- 주기적으로 오래된 대화 기록을 데이터베이스에서 정리(아카이빙)하여,
  데이터베이스 크기를 관리하고 RAG 검색 성능을 유지합니다.
"""

from datetime import datetime, timedelta, timezone

from discord.ext import commands, tasks

import config
from logger_config import logger
from utils import db as db_utils
from database.bm25_index import bulk_rebuild

class MaintenanceCog(commands.Cog):
    """봇의 백그라운드 유지보수 작업을 관리합니다."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last_conversation_ts: datetime | None = None
        self._last_bm25_rebuild_ts: datetime | None = None
        self._bm25_auto_enabled = (
            bool(config.BM25_AUTO_REBUILD_CONFIG.get("enabled"))
            and bool(config.BM25_DATABASE_PATH)
        )

        # 설정 파일(config.py)의 RAG_ARCHIVING_CONFIG 값에 따라 아카이빙 루프를 시작합니다.
        if config.RAG_ARCHIVING_CONFIG.get("enabled", False):
            interval_hours = config.RAG_ARCHIVING_CONFIG.get("check_interval_hours", 24)
            logger.info(f"RAG 아카이빙 백그라운드 작업이 활성화되었습니다. 실행 주기: {interval_hours}시간")
            
            # tasks.loop의 실행 주기를 config 값에 따라 동적으로 변경합니다.
            self.archive_loop.change_interval(hours=interval_hours)
            self.archive_loop.start()
        else:
            logger.info("RAG 아카이빙이 비활성화되어 백그라운드 작업을 시작하지 않습니다.")

        if self._bm25_auto_enabled:
            poll_minutes = max(1, config.BM25_AUTO_REBUILD_CONFIG.get("poll_minutes", 15))
            logger.info(
                "BM25 자동 재구축 백그라운드 작업이 활성화되었습니다. 체크 주기: %d분, 유휴 임계값: %d분",
                poll_minutes,
                config.BM25_AUTO_REBUILD_CONFIG.get("idle_minutes", 180),
            )
            self.bm25_rebuild_loop.change_interval(minutes=poll_minutes)
            self.bm25_rebuild_loop.start()
        else:
            logger.info("BM25 자동 재구축이 비활성화되어 루프를 시작하지 않습니다.")

    def cog_unload(self):
        """Cog가 언로드될 때, 실행 중인 루프를 안전하게 취소합니다."""
        if self.archive_loop.is_running():
            self.archive_loop.cancel()
        if self.bm25_rebuild_loop.is_running():
            self.bm25_rebuild_loop.cancel()

    @tasks.loop(hours=24)  # 기본 주기는 24시간이며, __init__에서 동적으로 재설정됩니다.
    async def archive_loop(self):
        """주기적으로 오래된 대화 기록을 아카이빙하는 메인 루프입니다."""
        logger.info("정기 RAG 아카이빙 작업을 시작합니다...")
        try:
            # db_utils에 정의된 아카이빙 함수를 호출합니다.
            await db_utils.archive_old_conversations(self.bot.db)
            logger.info("정기 RAG 아카이빙 작업을 성공적으로 완료했습니다.")
        except Exception as e:
            logger.error(f"정기 RAG 아카이빙 작업 중 예외가 발생했습니다: {e}", exc_info=True)

    @archive_loop.before_loop
    async def before_archive_loop(self):
        """루프가 처음 시작되기 전에, 봇이 완전히 준비될 때까지 기다립니다."""
        logger.info("아카이빙 루프가 봇 준비를 기다리고 있습니다...")
        await self.bot.wait_until_ready()
        logger.info("봇 준비 완료. 아카이빙 루프를 곧 시작합니다.")

    @tasks.loop(minutes=15)
    async def bm25_rebuild_loop(self):
        """대화가 일정 시간 이상 정지된 경우 BM25 인덱스를 자동으로 재구축합니다."""
        if not self._bm25_auto_enabled:
            return
        if not config.BM25_DATABASE_PATH:
            return

        idle_limit = max(1, config.BM25_AUTO_REBUILD_CONFIG.get("idle_minutes", 180))
        now = datetime.now(timezone.utc)

        if self._last_conversation_ts is None:
            return
        idle_elapsed = now - self._last_conversation_ts
        if idle_elapsed < timedelta(minutes=idle_limit):
            return
        if self._last_bm25_rebuild_ts and self._last_bm25_rebuild_ts >= self._last_conversation_ts:
            # 이미 최신 대화를 반영한 재구축이 완료된 상태
            return

        logger.info(
            "BM25 자동 재구축을 시작합니다. 최근 대화 이후 %.1f분 경과",
            idle_elapsed.total_seconds() / 60,
        )
        try:
            await bulk_rebuild(config.BM25_DATABASE_PATH)
            self._last_bm25_rebuild_ts = now
            logger.info("BM25 자동 재구축을 완료했습니다.")
        except Exception as exc:
            logger.error("BM25 자동 재구축 중 오류가 발생했습니다: %s", exc, exc_info=True)

    @bm25_rebuild_loop.before_loop
    async def before_bm25_rebuild_loop(self):
        if not self._bm25_auto_enabled:
            return
        logger.info("BM25 자동 재구축 루프가 봇 준비를 기다립니다...")
        await self.bot.wait_until_ready()
        logger.info("BM25 자동 재구축 루프를 시작합니다.")

    @commands.Cog.listener()
    async def on_message(self, message):
        if not self._bm25_auto_enabled:
            return
        if getattr(message.author, "bot", False):
            return
        created_at = getattr(message, "created_at", None)
        if isinstance(created_at, datetime):
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            self._last_conversation_ts = created_at
        else:
            self._last_conversation_ts = datetime.now(timezone.utc)

async def setup(bot: commands.Bot):
    """Cog를 봇에 등록하는 함수입니다."""
    await bot.add_cog(MaintenanceCog(bot))

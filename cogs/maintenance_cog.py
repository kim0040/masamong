# -*- coding: utf-8 -*-
"""
봇의 백그라운드 유지보수 작업을 관리하는 Cog입니다.

주요 기능:
- 주기적으로 오래된 대화 기록을 데이터베이스에서 정리(아카이빙)하여,
  데이터베이스 크기를 관리하고 RAG 검색 성능을 유지합니다.
"""

from discord.ext import commands, tasks

import config
from logger_config import logger
from utils import db as db_utils

class MaintenanceCog(commands.Cog):
    """봇의 백그라운드 유지보수 작업을 관리합니다."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # 설정 파일(config.py)의 RAG_ARCHIVING_CONFIG 값에 따라 아카이빙 루프를 시작합니다.
        if config.RAG_ARCHIVING_CONFIG.get("enabled", False):
            interval_hours = config.RAG_ARCHIVING_CONFIG.get("check_interval_hours", 24)
            logger.info(f"RAG 아카이빙 백그라운드 작업이 활성화되었습니다. 실행 주기: {interval_hours}시간")
            
            # tasks.loop의 실행 주기를 config 값에 따라 동적으로 변경합니다.
            self.archive_loop.change_interval(hours=interval_hours)
            self.archive_loop.start()
        else:
            logger.info("RAG 아카이빙이 비활성화되어 백그라운드 작업을 시작하지 않습니다.")

    def cog_unload(self):
        """Cog가 언로드될 때, 실행 중인 루프를 안전하게 취소합니다."""
        self.archive_loop.cancel()

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

async def setup(bot: commands.Bot):
    """Cog를 봇에 등록하는 함수입니다."""
    await bot.add_cog(MaintenanceCog(bot))
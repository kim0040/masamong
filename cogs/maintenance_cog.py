# -*- coding: utf-8 -*-
from discord.ext import commands, tasks
from logger_config import logger
import config
from utils import db as db_utils

class MaintenanceCog(commands.Cog):
    """
    봇의 백그라운드 유지보수 작업을 관리하는 Cog입니다.
    (예: 오래된 대화 기록 아카이빙)
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        if config.RAG_ARCHIVING_CONFIG.get("enabled", False):
            interval_hours = config.RAG_ARCHIVING_CONFIG.get("check_interval_hours", 24)
            logger.info(f"RAG 아카이빙 백그라운드 작업 시작. 실행 주기: {interval_hours}시간")
            # 동적으로 task loop의 주기를 설정
            self.archive_loop.change_interval(hours=interval_hours)
            self.archive_loop.start()
        else:
            logger.info("RAG 아카이빙이 비활성화되어 백그라운드 작업을 시작하지 않습니다.")

    def cog_unload(self):
        """Cog가 언로드될 때 루프를 취소합니다."""
        self.archive_loop.cancel()

    @tasks.loop(hours=24)  # 기본값으로 24시간, __init__에서 재설정됨
    async def archive_loop(self):
        """주기적으로 오래된 대화 기록을 아카이빙합니다."""
        logger.info("정기 RAG 아카이빙 작업을 시작합니다...")
        try:
            await db_utils.archive_old_conversations(self.bot.db)
            logger.info("정기 RAG 아카이빙 작업을 성공적으로 완료했습니다.")
        except Exception as e:
            logger.error(f"정기 RAG 아카이빙 작업 중 예외 발생: {e}", exc_info=True)

    @archive_loop.before_loop
    async def before_archive_loop(self):
        """루프가 시작되기 전에 봇이 준비될 때까지 기다립니다."""
        logger.info("아카이빙 루프 시작 대기: 봇 준비 중...")
        await self.bot.wait_until_ready()
        logger.info("봇 준비 완료. 아카이빙 루프를 시작합니다.")

async def setup(bot: commands.Bot):
    """Cog를 봇에 추가합니다."""
    await bot.add_cog(MaintenanceCog(bot))

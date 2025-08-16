#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
import os
import asyncio
import sys

# --- 데이터베이스 및 설정 임포트 ---
import aiosqlite
import sqlite_vss
import config
from logger_config import logger

# --- 초기 설정 확인 ---
if not config.TOKEN:
    logger.critical("DISCORD_BOT_TOKEN 환경 변수가 설정되지 않았습니다. 봇을 실행할 수 없습니다.")
    sys.exit(1)

# AI 또는 날씨 기능 활성화 시 API 키 확인
if not config.GEMINI_API_KEY:
    logger.warning("GEMINI_API_KEY 환경 변수가 없습니다. AI 기능이 작동하지 않습니다.")
if not config.KMA_API_KEY or config.KMA_API_KEY == 'YOUR_KMA_API_KEY':
    logger.warning("KMA_API_KEY가 설정되지 않았습니다. 날씨 기능이 작동하지 않을 수 있습니다.")


# --- 커스텀 봇 클래스 정의 ---
class ReMasamongBot(commands.Bot):
    """
    데이터베이스 연결을 관리하는 커스텀 Bot 클래스.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.db = None # 데이터베이스 연결 객체를 저장할 변수
        self.db_path = os.path.join('database', 'remasamong.db')

    async def setup_hook(self):
        """
        봇이 디스코드에 로그인하기 전에 실행되는 비동기 설정 훅입니다.
        이곳에서 데이터베이스 연결을 생성하고 Cog를 로드합니다.
        """
        db_dir = os.path.dirname(self.db_path)
        if not os.path.exists(db_dir):
            os.makedirs(db_dir)
            logger.info(f"'{db_dir}' 디렉토리를 생성했습니다.")

        try:
            self.db = await aiosqlite.connect(self.db_path)
            await self.db.enable_load_extension(True)
            # vss0은 vector0에 의존하므로, vector0를 먼저 로드해야 합니다.
            sqlite_vss.load(self.db)
            logger.info(f"데이터베이스 연결 및 VSS 확장 로드 성공.")
        except Exception as e:
            logger.critical(f"데이터베이스 연결 또는 VSS 확장 로드 실패: {e}", exc_info=True)
            await self.close()
            return

        # cogs 폴더 내의 모든 .py 파일을 동적으로 로드
        cog_list = ['events', 'commands', 'ai_handler', 'weather_cog', 'fun_cog', 'activity_cog', 'poll_cog', 'settings_cog', 'logging_cog']
        for cog_name in cog_list:
            try:
                await self.load_extension(f'cogs.{cog_name}')
                logger.info(f"Cog 로드 성공: {cog_name}")
            except Exception as e:
                logger.error(f"Cog 로드 중 오류 발생 ({cog_name}): {e}", exc_info=True)

    async def close(self):
        """
        봇 종료 시 호출되는 메서드. 데이터베이스 연결을 안전하게 닫습니다.
        """
        if self.db:
            await self.db.close()
            logger.info("데이터베이스 연결을 안전하게 닫았습니다.")
        await super().close()

# --- 메인 실행 로직 ---
async def main():
    bot = ReMasamongBot(command_prefix='!', intents=config.intents)
    async with bot:
        logger.info("봇 실행 시작...")
        try:
            await bot.start(config.TOKEN)
        except discord.errors.LoginFailure:
            logger.critical("봇 토큰이 유효하지 않습니다. DISCORD_BOT_TOKEN 환경 변수를 확인해주세요.")
        except discord.errors.PrivilegedIntentsRequired:
            logger.critical("봇 인텐트(Intents)가 활성화되지 않았습니다. Discord 개발자 포털에서 Message Content Intent 등을 활성화해주세요.")
        except Exception as e:
            logger.critical(f"봇 실행 중 치명적인 오류 발생: {e}", exc_info=True)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Ctrl+C 감지. 봇 종료 중...")

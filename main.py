#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
마사몽 Discord 봇의 메인 실행 파일 (Entrypoint) 입니다.

이 파일은 다음의 주요 작업을 수행합니다:
1. 설정 및 로거를 초기화합니다.
2. 필요한 API 키가 설정되었는지 확인합니다.
3. 데이터베이스 연결을 관리하는 커스텀 Bot 클래스(`ReMasamongBot`)를 정의합니다.
4. 봇의 핵심 로직이 담긴 Cog(모듈)들을 로드합니다.
5. 봇을 Discord에 연결하고 실행합니다.
"""

import discord
from discord.ext import commands
import os
import asyncio
import sys
import aiosqlite

import config
from logger_config import logger, register_discord_logging
from utils import initial_data

# --- 1. 초기 설정 및 API 키 유효성 검사 ---
# 봇 실행에 필수적인 토큰이 없으면 즉시 종료합니다.
if not config.TOKEN:
    logger.critical("DISCORD_BOT_TOKEN이 설정되지 않았습니다. 프로그램을 종료합니다.")
    sys.exit(1)

# AI 기능이 활성화되었지만 Gemini 키가 없는 경우 경고합니다.
is_any_ai_channel_enabled = any(settings.get("allowed", False) for settings in config.CHANNEL_AI_CONFIG.values())
if is_any_ai_channel_enabled and not config.GEMINI_API_KEY:
    logger.warning("AI 채널이 활성화되었지만 GEMINI_API_KEY가 없습니다. AI 기능이 작동하지 않을 수 있습니다.")

# 날씨 기능에 필요한 기상청 키가 없는 경우 경고합니다.
if not config.KMA_API_KEY or config.KMA_API_KEY == 'YOUR_KMA_API_KEY':
    logger.warning("KMA_API_KEY가 설정되지 않았습니다. 날씨 기능이 정상적으로 작동하지 않을 수 있습니다.")


# --- 2. 커스텀 봇 클래스 정의 ---
class ReMasamongBot(commands.Bot):
    """
    aiosqlite 데이터베이스 연결을 비동기적으로 관리하는 커스텀 Bot 클래스입니다.
    봇 인스턴스에 `db` 속성을 추가하여 모든 Cog에서 데이터베이스 연결을 공유할 수 있도록 합니다.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.db: aiosqlite.Connection = None
        self.db_path = config.DATABASE_FILE

    async def _migrate_db(self):
        """데이터베이스 스키마를 확인하고, 필요한 경우 초기 데이터를 시딩(seeding)합니다."""
        try:
            # locations 테이블이 비어있을 경우, 초기 좌표 데이터를 삽입합니다.
            async with self.db.execute("SELECT COUNT(*) FROM locations") as cursor:
                if (await cursor.fetchone())[0] == 0:
                    logger.info("'locations' 테이블이 비어있어 초기 데이터를 시딩합니다.")
                    locations_to_seed = initial_data.LOCATION_DATA
                    if locations_to_seed:
                        await self.db.executemany(
                            "INSERT OR IGNORE INTO locations (name, nx, ny) VALUES (?, ?, ?)",
                            [(loc['name'], loc['nx'], loc['ny']) for loc in locations_to_seed]
                        )
                        await self.db.commit()
                        logger.info(f"{len(locations_to_seed)}개의 위치 정보 시딩 완료.")

        except aiosqlite.OperationalError as e:
            # 테이블이 아직 존재하지 않는 경우 등
            logger.warning(f"데이터베이스 마이그레이션 중 오류 발생 (무시 가능): {e}")
        except Exception as e:
            logger.error(f"데이터베이스 마이그레이션 중 심각한 오류 발생: {e}", exc_info=True)

    async def setup_hook(self):
        """
        봇이 Discord에 로그인하기 전에 실행되는 비동기 설정 훅입니다.
        데이터베이스 연결, Cog 로드, 의존성 주입 등 중요한 초기화 작업을 수행합니다.
        """
        # 데이터베이스 디렉토리 생성 확인
        db_dir = os.path.dirname(self.db_path)
        if not os.path.exists(db_dir):
            os.makedirs(db_dir)
            logger.info(f"데이터베이스 디렉토리 '{db_dir}'을(를) 생성했습니다.")

        # 데이터베이스 연결
        try:
            self.db = await aiosqlite.connect(self.db_path)
            self.db.row_factory = aiosqlite.Row # 결과를 딕셔너리처럼 접근 가능하게 설정
            logger.info(f"데이터베이스에 성공적으로 연결되었습니다: {self.db_path}")
        except Exception as e:
            logger.critical(f"데이터베이스 연결 실패. 봇을 종료합니다: {e}", exc_info=True)
            await self.close()
            return

        # 데이터베이스 초기 데이터 확인 및 마이그레이션
        await self._migrate_db()

        # Cog(기능 모듈) 로드
        # 의존성 순서를 고려하여 리스트 순서 결정 (예: tools_cog -> 다른 cogs)
        cog_list = [
            'weather_cog', 'tools_cog', 'events', 'commands', 'ai_handler',
            'fun_cog', 'activity_cog', 'poll_cog', 'settings_cog',
            'maintenance_cog', 'proactive_assistant'
        ]

        for cog_name in cog_list:
            try:
                await self.load_extension(f'cogs.{cog_name}')
                logger.info(f"Cog 로드 성공: {cog_name}")
            except commands.ExtensionNotFound:
                logger.warning(f"Cog 파일을 찾을 수 없습니다: '{cog_name}.py'. 건너뜁니다.")
            except Exception as e:
                logger.error(f"Cog '{cog_name}' 로드 중 오류 발생: {e}", exc_info=True)

        # Cog 간 의존성 주입
        # 일부 Cog는 다른 Cog의 기능을 직접 호출해야 할 수 있습니다.
        ai_handler_cog = self.get_cog('AIHandler')
        if ai_handler_cog:
            # ActivityCog와 FunCog에 AIHandler 인스턴스를 주입합니다.
            for cog_name in ['ActivityCog', 'FunCog']:
                cog_instance = self.get_cog(cog_name)
                if cog_instance:
                    cog_instance.ai_handler = ai_handler_cog
                    logger.info(f"AIHandler를 {cog_name}에 성공적으로 주입했습니다.")
        else:
            logger.warning("AIHandler Cog를 찾을 수 없어 의존성 주입을 건너뜁니다.")

    async def on_message(self, message: discord.Message):
        """
        모든 메시지 이벤트를 처리하는 중앙 핸들러입니다.
        명령어 처리, 활동 기록, AI 응답 등 모든 메시지 기반 상호작용이 여기서 시작됩니다.
        """
        # 봇 자신의 메시지, DM, 다른 봇의 메시지는 무시합니다.
        if message.author.bot or not message.guild:
            return

        # 1. 모든 메시지에 대해 공통적으로 수행할 작업
        activity_cog = self.get_cog('ActivityCog')
        if activity_cog:
            await activity_cog.record_message(message) # 사용자 활동 기록
        
        ai_handler = self.get_cog('AIHandler')
        if ai_handler:
            await ai_handler.add_message_to_history(message) # 대화 기록에 추가

        # 2. 메시지가 명령인지 확인하고 처리합니다.
        # process_commands는 내부적으로 명령어를 찾아 실행합니다.
        await self.process_commands(message)

    async def close(self):
        """
        봇 종료 시 호출되어 데이터베이스 연결을 안전하게 닫습니다.
        """
        if self.db:
            await self.db.close()
            logger.info("데이터베이스 연결을 안전하게 닫았습니다.")
        await super().close()

# --- 3. 메인 실행 함수 ---
async def main():
    """봇을 초기화하고 실행하는 메인 비동기 함수입니다."""
    # 커스텀 봇 클래스 인스턴스 생성
    bot = ReMasamongBot(command_prefix=config.COMMAND_PREFIX, intents=config.intents)

    # Discord 로깅 핸들러 등록 및 백그라운드 태스크 시작
    register_discord_logging(bot)

    async with bot:
        logger.info("마사몽 봇을 시작합니다...")
        try:
            await bot.start(config.TOKEN)
        except discord.errors.LoginFailure:
            logger.critical("봇 토큰이 유효하지 않습니다. 설정을 확인해주세요.")
        except discord.errors.PrivilegedIntentsRequired:
            logger.critical("Privileged Intents가 활성화되지 않았습니다. Discord 개발자 포털에서 설정을 확인해주세요.")
        except Exception as e:
            logger.critical(f"봇 실행 중 치명적인 오류 발생: {e}", exc_info=True)

# --- 4. 프로그램 진입점 ---
if __name__ == "__main__":
    try:
        # asyncio 이벤트 루프를 시작하여 main 함수를 실행합니다.
        asyncio.run(main())
    except KeyboardInterrupt:
        # Ctrl+C 입력 시 정상 종료 메시지 출력
        logger.info("Ctrl+C가 감지되었습니다. 봇을 종료합니다.")
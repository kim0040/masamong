#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
마사몽 Discord 봇의 메인 실행 파일 (Entrypoint) 입니다.

이 파일은 다음의 주요 작업을 수행합니다:
1. 설정 및 로거를 초기화합니다.
2. Discord 봇 인스턴스를 생성하고 Cog를 로드합니다.
3. 데이터베이스 마이그레이션 및 초기 데이터를 세팅합니다.
4. 봇을 실행하여 Discord와 연결합니다.
"""
import asyncio
import os
import sys
from pathlib import Path

import discord
from discord.ext import commands
import aiosqlite

import config
from logger_config import logger, register_discord_logging
from utils import initial_data

# 봇 버전 정보
__version__ = "2.0.0"
__author__ = "kim0040"

# --- 1. 시작 로그 및 환경 확인 ---
logger.info("=" * 70)
logger.info(f"🤖 마사몽 Discord 봇 v{__version__} 시작 중...")
logger.info(f"Python 버전: {sys.version.split()[0]}")
logger.info(f"Discord.py 버전: {discord.__version__}")
logger.info(f"작업 디렉터리: {os.getcwd()}")
logger.info("=" * 70)

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
        """데이터베이스 스키마를 확인하고 좌표 데이터를 보강합니다.

        이 메서드는 `locations` 테이블이 존재하는지 확인하고, 부족하거나 없는 경우
        `utils.initial_data` 모듈의 CSV/상수 데이터를 활용해 기본 좌표를 시딩합니다.
        네트워크나 파일 접근 오류가 발생해도 봇이 기동될 수 있도록 예외를 자체 처리합니다.
        """
        try:
            # 스키마 파일 실행 (전체 테이블 생성)
            schema_path = Path("database/schema.sql")
            if schema_path.exists():
                logger.info(f"스키마 파일 로드 중: {schema_path}")
                with open(schema_path, "r", encoding="utf-8") as f:
                    schema_script = f.read()
                await self.db.executescript(schema_script)
                await self.db.commit()
            else:
                logger.error("database/schema.sql 파일을 찾을 수 없습니다.")
            # locations 테이블이 비어있을 경우, 초기 좌표 데이터를 삽입합니다.
            async with self.db.execute("SELECT COUNT(*) FROM locations") as cursor:
                existing_count = (await cursor.fetchone())[0]
                if existing_count < 100:
                    if existing_count:
                        logger.info("'locations' 테이블에 기존 데이터(%d개)가 부족하여 재시딩합니다.", existing_count)
                        await self.db.execute("DELETE FROM locations")
                        await self.db.commit()
                    else:
                        logger.info("'locations' 테이블이 비어있어 초기 데이터를 시딩합니다.")
                    locations_to_seed = initial_data.load_locations_from_csv()
                    if not locations_to_seed:
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
        """Discord 로그인 직전에 실행되어 필수 리소스를 초기화합니다.

        여기서는 데이터베이스 파일과 디렉터리를 준비하고, Cog 확장을 순차적으로 로드하며,
        Cog 간에 필요한 의존성을 주입합니다. 이 단계가 성공적으로 끝나야 봇이 정상 작동합니다.
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
        """모든 메시지 이벤트를 받아 명령/AI 파이프라인으로 라우팅합니다.

        Args:
            message (discord.Message): Discord로부터 전달된 원본 메시지 객체.

        Notes:
            - 명령 프리픽스가 감지되면 `process_commands`로 위임합니다.
            - 활동 기록과 AI 핸들러는 예외 발생 시에도 독립적으로 로깅하여 서로 영향을 주지 않습니다.
        """
        # 봇 자신의 메시지, DM, 다른 봇의 메시지는 무시합니다.
        if message.author.bot or not message.guild:
            return

        logger.info(f"[DEBUG] Message received from {message.author} ({message.author.id}): {message.content}")

        activity_cog = self.get_cog('ActivityCog')
        if activity_cog:
            try:
                await activity_cog.record_message(message)  # 사용자 활동 기록
            except Exception as exc:  # pragma: no cover - 방어적 로깅
                logger.error(
                    "활동 기록 처리 중 오류: %s",
                    exc,
                    exc_info=True,
                    extra={'guild_id': message.guild.id, 'channel_id': message.channel.id}
                )

        message_content = message.content or ""
        prefixes_raw = await self.get_prefix(message)
        if isinstance(prefixes_raw, str):
            prefixes = [prefixes_raw]
        else:
            prefixes = list(prefixes_raw)
        is_command = any(message_content.startswith(prefix) for prefix in prefixes if prefix)

        if is_command:
            await self.process_commands(message)
            return

        ai_handler = self.get_cog('AIHandler')
        if ai_handler:
            try:
                await ai_handler.add_message_to_history(message)  # 대화 기록에 추가
            except Exception as exc:  # pragma: no cover - 방어적 로깅
                logger.error(
                    "대화 기록 저장 중 오류: %s",
                    exc,
                    exc_info=True,
                    extra={'guild_id': message.guild.id, 'channel_id': message.channel.id}
                )

        events_cog = self.get_cog('EventListeners')
        if events_cog:
            try:
                if await events_cog._handle_keyword_triggers(message):
                    return
            except Exception as exc:  # pragma: no cover - 방어적 로깅
                logger.error(
                    "키워드 트리거 처리 중 오류: %s",
                    exc,
                    exc_info=True,
                    extra={'guild_id': message.guild.id, 'channel_id': message.channel.id}
                )

        ai_ready = ai_handler and ai_handler.is_ready
        if not ai_ready:
            return

        channel_conf = config.CHANNEL_AI_CONFIG.get(message.channel.id, {})
        ai_enabled_channel = channel_conf.get('allowed', False)
        if not ai_enabled_channel:
            return

        if not ai_handler._message_has_valid_mention(message):
            logger.info(f"[DEBUG] Message ignored (No valid mention): {message.content}")
            return

        try:
            await ai_handler.process_agent_message(message)
        except Exception as exc:  # pragma: no cover - 방어적 로깅
            logger.error(
                "AI 메시지 처리 중 오류: %s",
                exc,
                exc_info=True,
                extra={'guild_id': message.guild.id, 'channel_id': message.channel.id}
            )

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
    """봇 인스턴스를 구성하고 Discord 이벤트 루프를 시작합니다.

    이 함수는 `asyncio.run` 진입점에서 호출되며, 봇 토큰 검증과 Discord 세션 수명 관리를 담당합니다.
    """
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

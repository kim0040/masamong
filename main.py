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
import json
import os
import sys
from pathlib import Path

import discord
from discord.ext import commands
import aiosqlite
import logging

import config
from database.compat_db import TiDBSettings, connect_main_db, get_table_columns
from logger_config import logger, register_discord_logging
from utils import initial_data

# --- [Fixed] 터미널 경고 메시지(Noise) 억제 ---
import warnings
# urllib3의 LibreSSL 관련 경고 무시 (macOS 환경용)
warnings.filterwarnings("ignore", message=".*urllib3.*NotOpenSSLWarning.*")
# Google API의 Python 3.9 EOL 및 Deprecation 경고 무시 (안정적 구동을 위해)
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)
# Google GenAI SDK의 키 중복 경고 무시 (CometAPI 사용 시 고정적으로 발생)
warnings.filterwarnings("ignore", message=".*Both GOOGLE_API_KEY and GEMINI_API_KEY are set.*")
# SDK 및 관련 패키지 내부 INFO 로그 억제 (AFC 안내, HTTP 요청 상세 등 노이즈 제거)
logging.getLogger('google_genai').setLevel(logging.WARNING)
logging.getLogger('httpx').setLevel(logging.WARNING)
# ---------------------------------------------

# 봇 버전 정보
__version__ = "2.0.0"
__author__ = "kim0040"


def _format_storage_target() -> str:
    """현재 설정된 DB 백엔드 정보를 사람이 읽을 수 있는 문자열로 포맷합니다."""
    if config.DB_BACKEND == "tidb":
        return f"TiDB {config.TIDB_NAME}@{config.TIDB_HOST}:{config.TIDB_PORT}"
    return f"SQLite {config.DATABASE_FILE}"


# 학교 공지 기능을 켠 인스턴스에서만 필요한 테이블.
# SCHOOL_NOTICE_ENABLED=false인 인스턴스(masamo 포함)는 이 테이블 없이 기동한다.
SCHOOL_NOTICE_TABLES = (
    "school_notice_profiles",
    "school_notice_feedback",
    "school_notice_deliveries",
    "school_notice_batch_runs",
)


def _missing_startup_cogs(
    loaded_cogs: set[str],
    attempted_cogs: set[str],
) -> list[str]:
    """운영 프로필에서 부분 기능 상태로 기동하지 않도록 누락 Cog를 계산합니다."""
    required = set(config.REQUIRED_COGS)
    if config.REQUIRE_EXPLICIT_PROFILE:
        required.update(attempted_cogs)
    return sorted(required - loaded_cogs)


# --- 1. 시작 로그 및 환경 확인 ---
logger.info("=" * 70)
logger.info(f"🤖 마사몽 Discord 봇 v{__version__} 시작 중...")
logger.info(f"Python 버전: {sys.version.split()[0]}")
logger.info(f"Discord.py 버전: {discord.__version__}")
logger.info(f"작업 디렉터리: {os.getcwd()}")
logger.info(f"실행 프로필: {config.PROFILE} (instance={config.INSTANCE_NAME})")
logger.info(f"메인 DB 백엔드: {config.DB_BACKEND} ({_format_storage_target()})")
logger.info(f"원격 DB 강제 모드: {'enabled' if config.REMOTE_DB_STRICT_MODE else 'disabled'}")
logger.info(f"Discord 메모리 저장소: {config.DISCORD_EMBEDDING_BACKEND}")
logger.info(f"Kakao 저장소: {config.KAKAO_STORE_BACKEND}")
logger.info(f"활성 메모리 소스: {', '.join(sorted(config.MEMORY_SOURCES)) or 'none'}")
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
        # 대화형 커맨드(예: !운세 등록) 진행 중인 사용자를 추적하여 AI 자동응답을 방지합니다.
        self.locked_users = set()
        self._guild_settings_cache: dict[int, dict[str, object]] = {}

    async def _load_guild_settings_cache(self) -> None:
        """서버별 AI 정책을 한 번 읽어 메시지마다 원격 DB를 조회하지 않게 합니다."""
        if config.GUILD_SETTINGS_MODE != "database":
            self._guild_settings_cache = {}
            logger.info(
                "DB guild 설정 적용을 건너뜁니다: mode=%s",
                config.GUILD_SETTINGS_MODE,
            )
            return
        cache: dict[int, dict[str, object]] = {}
        async with self.db.execute(
            """
            SELECT guild_id, ai_enabled, ai_allowed_channels, persona_text
            FROM guild_settings
            """
        ) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            try:
                guild_id = int(row[0])
            except (TypeError, ValueError):
                continue
            raw_channels = row[2]
            allowed_channels: set[int] | None = None
            if raw_channels is not None:
                try:
                    parsed = json.loads(raw_channels) if isinstance(raw_channels, str) else raw_channels
                    if isinstance(parsed, list):
                        allowed_channels = {
                            int(item)
                            for item in parsed
                            if str(item).strip().isdigit()
                        }
                    else:
                        logger.warning(
                            "guild_settings.ai_allowed_channels가 배열이 아닙니다: guild=%s",
                            guild_id,
                        )
                except (TypeError, ValueError, json.JSONDecodeError):
                    logger.warning(
                        "guild_settings.ai_allowed_channels 파싱 실패: guild=%s",
                        guild_id,
                    )
            cache[guild_id] = {
                "ai_enabled": bool(row[1]),
                "ai_allowed_channels": allowed_channels,
                "persona_text": str(row[3]).strip() if row[3] else None,
            }
        self._guild_settings_cache = cache
        logger.info("서버별 AI 설정 캐시 로드 완료: %d개 길드", len(cache))

    def update_guild_setting_cache(self, guild_id: int, setting_name: str, value) -> None:
        """관리 명령이 DB를 갱신한 직후 런타임 캐시에도 동일 값을 반영합니다."""
        if config.GUILD_SETTINGS_MODE != "database":
            return
        guild_key = int(guild_id)
        entry = self._guild_settings_cache.setdefault(guild_key, {})
        if setting_name == "ai_allowed_channels":
            parsed = json.loads(value) if isinstance(value, str) else value
            entry[setting_name] = {
                int(item)
                for item in (parsed or [])
                if str(item).strip().isdigit()
            }
        elif setting_name == "ai_enabled":
            entry[setting_name] = bool(value)
        elif setting_name == "persona_text":
            entry[setting_name] = str(value).strip() if value else None

    def is_ai_channel_allowed(self, guild_id: int, channel_id: int) -> bool:
        """DB 정책이 있으면 우선 적용하고, 없을 때만 정적 프롬프트 설정을 사용합니다."""
        entry = self._guild_settings_cache.get(int(guild_id), {})
        if entry.get("ai_enabled") is False:
            return False
        allowed_channels = entry.get("ai_allowed_channels")
        if isinstance(allowed_channels, set):
            return int(channel_id) in allowed_channels
        channel_conf = config.CHANNEL_AI_CONFIG.get(int(channel_id), {})
        return bool(channel_conf.get("allowed", False))

    def get_guild_persona(self, guild_id: int | None) -> str | None:
        """길드 관리자가 지정한 런타임 페르소나를 반환합니다."""
        if guild_id is None:
            return None
        entry = self._guild_settings_cache.get(int(guild_id), {})
        persona = entry.get("persona_text")
        return str(persona) if persona else None

    async def _verify_runtime_schema(self) -> None:
        """DDL 없이 런타임 필수 테이블/컬럼이 이미 존재하는지 검증합니다."""
        required_tables = {
            "conversation_history",
            "guild_settings",
            "locations",
            "user_profiles",
            "user_activity_log",
            "linkup_usage_log",
        }
        if config.REQUIRE_EXPLICIT_PROFILE:
            # 명시적 운영 프로필은 startup 이후 지연 경로에서 테이블 누락을
            # 발견하지 않도록 중앙 schema의 핵심 저장소 전체를 먼저 확인한다.
            required_tables.update(
                {
                    "user_activity",
                    "conversation_windows",
                    "system_counters",
                    "api_call_log",
                    "analytics_log",
                    "conversation_history_archive",
                    "user_preferences",
                    "dm_usage_logs",
                }
            )
        if config.DB_BACKEND == "tidb":
            required_tables.update(
                {
                    "discord_chat_embeddings",
                    "discord_memory_entries",
                }
            )
            # Kakao 기억을 쓰지 않는 프로필(general)에는 이 저장소가 존재할
            # 이유가 없다. 요구하지 않아야 "General에는 Kakao가 없다"는
            # 경계가 스키마 수준에서도 성립한다.
            if config.KAKAO_MEMORY_ENABLED:
                required_tables.add("kakao_chunks")
        # 학교 공지는 기능을 켠 인스턴스에서만 요구한다. 무조건 요구하면
        # MASAMONG_AUTO_MIGRATE=false인 masamo가 기동하지 못한다.
        if config.SCHOOL_NOTICE_ENABLED:
            required_tables.update(SCHOOL_NOTICE_TABLES)
        existing_tables = await self._existing_tables(required_tables)
        missing_tables = sorted(required_tables - existing_tables)
        if missing_tables:
            raise RuntimeError(
                "런타임 필수 DB 테이블이 없습니다: " + ", ".join(missing_tables)
            )

        guild_columns = set(await get_table_columns(self.db, "guild_settings"))
        required_guild_columns = {
            "guild_id",
            "ai_enabled",
            "ai_allowed_channels",
            "persona_text",
            "language",
        }
        missing_columns = sorted(required_guild_columns - guild_columns)
        if missing_columns:
            raise RuntimeError(
                "guild_settings 필수 컬럼이 없습니다: " + ", ".join(missing_columns)
            )

        if config.REQUIRE_EXPLICIT_PROFILE:
            user_profile_columns = set(
                await get_table_columns(self.db, "user_profiles")
            )
            required_user_profile_columns = {
                "user_id",
                "birth_date",
                "birth_time",
                "gender",
                "birth_place",
                "subscription_active",
                "subscription_time",
                "pending_payload",
                "last_fortune_sent",
                "last_fortune_content",
                "created_at",
            }
            missing_user_profile_columns = sorted(
                required_user_profile_columns - user_profile_columns
            )
            if missing_user_profile_columns:
                raise RuntimeError(
                    "user_profiles 필수 컬럼이 없습니다: "
                    + ", ".join(missing_user_profile_columns)
                )

        if config.REQUIRE_EXPLICIT_PROFILE and config.DB_BACKEND == "tidb":
            required_embedding_columns = {
                "discord_chat_embeddings": {
                    "id",
                    "message_id",
                    "server_id",
                    "channel_id",
                    "user_id",
                    "user_name",
                    "message",
                    "timestamp",
                    "embedding",
                },
                "discord_memory_entries": {
                    "id",
                    "memory_id",
                    "anchor_message_id",
                    "server_id",
                    "channel_id",
                    "owner_user_id",
                    "owner_user_name",
                    "memory_scope",
                    "memory_type",
                    "summary_text",
                    "memory_text",
                    "raw_context",
                    "source_message_ids",
                    "speaker_names",
                    "keyword_json",
                    "timestamp",
                    "embedding",
                },
            }
            # Kakao 저장소는 그 기억 소스를 쓰는 프로필에서만 검증한다.
            if config.KAKAO_MEMORY_ENABLED:
                required_embedding_columns["kakao_chunks"] = {
                    "id",
                    "room_key",
                    "source_room_label",
                    "chunk_id",
                    "session_id",
                    "start_date",
                    "message_count",
                    "summary",
                    "text_long",
                    "embedding",
                }
            for table_name, expected_columns in required_embedding_columns.items():
                actual_columns = set(
                    await get_table_columns(self.db, table_name)
                )
                missing_embedding_columns = sorted(
                    expected_columns - actual_columns
                )
                if missing_embedding_columns:
                    raise RuntimeError(
                        f"{table_name} 필수 컬럼이 없습니다: "
                        + ", ".join(missing_embedding_columns)
                    )

    async def _migrate_db(self):
        """데이터베이스 스키마를 확인하고 좌표 데이터를 보강합니다.

        이 메서드는 `locations` 테이블이 존재하는지 확인하고, 부족하거나 없는 경우
        `utils.initial_data` 모듈의 CSV/상수 데이터를 활용해 기본 좌표를 시딩합니다.
        네트워크나 파일 접근 오류가 발생해도 봇이 기동될 수 있도록 예외를 자체 처리합니다.
        """
        try:
            # 스키마 파일 실행 (전체 테이블 생성)
            schema_filename = "database/schema_tidb.sql" if config.DB_BACKEND == "tidb" else "database/schema.sql"
            schema_path = Path(config.PROJECT_ROOT) / schema_filename
            if schema_path.exists():
                if config.DB_BACKEND == "tidb":
                    core_tables = (
                        "conversation_history",
                        "guild_settings",
                        "locations",
                        "user_profiles",
                        "user_activity",
                        "user_activity_log",
                        "linkup_usage_log",
                        "conversation_windows",
                        "system_counters",
                        "api_call_log",
                        "analytics_log",
                        "conversation_history_archive",
                        "user_preferences",
                        "dm_usage_logs",
                        "discord_chat_embeddings",
                        "discord_memory_entries",
                    ) + (
                        ("kakao_chunks",)
                        if config.KAKAO_MEMORY_ENABLED
                        else ()
                    ) + (
                        SCHOOL_NOTICE_TABLES
                        if config.SCHOOL_NOTICE_ENABLED
                        else ()
                    )
                else:
                    core_tables = (
                        "conversation_history",
                        "guild_settings",
                        "locations",
                        "user_profiles",
                        "user_activity",
                        "user_activity_log",
                        "linkup_usage_log",
                        "conversation_windows",
                        "system_counters",
                        "api_call_log",
                        "analytics_log",
                        "conversation_history_archive",
                        "user_preferences",
                        "dm_usage_logs",
                    ) + (
                        SCHOOL_NOTICE_TABLES
                        if config.SCHOOL_NOTICE_ENABLED
                        else ()
                    )
                existing_tables = await self._existing_tables(core_tables)
                missing_tables = [
                    name for name in core_tables
                    if name not in existing_tables
                ]
                if missing_tables:
                    logger.info("스키마 적용 시작: %s (누락 테이블: %s)", schema_path, ", ".join(missing_tables))
                    with open(schema_path, "r", encoding="utf-8") as f:
                        schema_script = f.read()
                    await self.db.executescript(schema_script)
                    await self.db.commit()
                    logger.info("스키마 적용 완료: %s", schema_path)
                else:
                    logger.info("핵심 테이블이 이미 존재하여 스키마 재적용을 건너뜁니다: %s", schema_path)
            else:
                logger.error("스키마 파일을 찾을 수 없습니다: %s", schema_path)

            # 과거 conversation_history를 user_activity_log로 1회 백필하여
            # !랭킹(채널/기간 집계)에서 이전 누적 데이터가 사라져 보이지 않게 보정합니다.
            backfill_key = "user_activity_log_backfill_v1"
            backfill_done = False
            async with self.db.execute(
                "SELECT counter_value FROM system_counters WHERE counter_name = ?",
                (backfill_key,),
            ) as cursor:
                row = await cursor.fetchone()
                backfill_done = bool(row)

            if not backfill_done:
                async with self.db.execute("SELECT COUNT(*) FROM user_activity_log") as cursor:
                    before_count = int((await cursor.fetchone())[0] or 0)

                await self.db.execute(
                    """
                    INSERT OR IGNORE INTO user_activity_log (message_id, guild_id, channel_id, user_id, created_at)
                    SELECT message_id, guild_id, channel_id, user_id, created_at
                    FROM conversation_history
                    WHERE is_bot = 0
                    """
                )
                if await self._table_exists("conversation_history_archive"):
                    await self.db.execute(
                        """
                        INSERT OR IGNORE INTO user_activity_log (message_id, guild_id, channel_id, user_id, created_at)
                        SELECT message_id, guild_id, channel_id, user_id, created_at
                        FROM conversation_history_archive
                        WHERE is_bot = 0
                        """
                    )

                async with self.db.execute("SELECT COUNT(*) FROM user_activity_log") as cursor:
                    after_count = int((await cursor.fetchone())[0] or 0)

                now_utc = discord.utils.utcnow().isoformat()
                await self.db.execute(
                    """
                    INSERT OR REPLACE INTO system_counters (counter_name, counter_value, last_reset_at)
                    VALUES (?, ?, ?)
                    """,
                    (backfill_key, after_count, now_utc),
                )
                await self.db.commit()
                logger.info(
                    "user_activity_log 백필 완료: 이전=%d, 이후=%d, 추가=%d",
                    before_count,
                    after_count,
                    max(0, after_count - before_count),
                )

            # guild_settings 테이블에 language 컬럼 추가 (기존 DB 호환)
            if config.DB_BACKEND != "tidb":
                try:
                    await self.db.execute(
                        "ALTER TABLE guild_settings ADD COLUMN language TEXT DEFAULT 'ko'"
                    )
                    await self.db.commit()
                    logger.info("guild_settings 테이블에 language 컬럼을 추가했습니다.")
                except Exception:
                    pass  # 이미 존재하면 무시
            else:
                try:
                    await self.db.execute(
                        "ALTER TABLE guild_settings ADD COLUMN language VARCHAR(10) DEFAULT 'ko'"
                    )
                    await self.db.commit()
                    logger.info("guild_settings 테이블에 language 컬럼을 추가했습니다.")
                except Exception:
                    pass  # 이미 존재하면 무시

            # locations 테이블이 비어있거나 구형 데이터(예: 2만개 미만 또는 주요 별칭 누락)일 경우 재시딩합니다.
            async with self.db.execute("SELECT COUNT(*) FROM locations") as cursor:
                existing_count = (await cursor.fetchone())[0]
            
            # [NEW] 특정 별칭(예: '청주')이 있는지 확인하여 구형 데이터인지 판별합니다.
            async with self.db.execute("SELECT 1 FROM locations WHERE name = '청주' LIMIT 1") as cursor:
                has_short_alias = await cursor.fetchone()

            if existing_count < 100 or not has_short_alias:
                # 새 시드가 준비됐는지 먼저 확인한다. 기존 행을 삭제한 뒤 로딩에 실패하면
                # 운영 위치 데이터가 비는 순서를 피한다.
                locations_to_seed = initial_data.load_locations_from_csv()
                if not locations_to_seed:
                    locations_to_seed = initial_data.LOCATION_DATA
                if not locations_to_seed:
                    raise RuntimeError(
                        "위치 데이터 재시딩이 필요하지만 사용할 시드 데이터가 없습니다."
                    )

                if existing_count:
                    logger.info("'locations' 테이블의 데이터가 구형이거나 부족하여 재시딩합니다. (현재: %d개, 별칭누락: %s)", 
                                existing_count, not has_short_alias)
                    await self.db.execute("DELETE FROM locations")
                else:
                    logger.info("'locations' 테이블이 비어있어 초기 데이터를 시딩합니다.")

                await self.db.executemany(
                    "INSERT OR IGNORE INTO locations (name, nx, ny) VALUES (?, ?, ?)",
                    [(loc['name'], loc['nx'], loc['ny']) for loc in locations_to_seed]
                )
                await self.db.commit()
                logger.info(f"{len(locations_to_seed)}개의 위치 정보 시딩 완료 (별칭 포함).")

        except aiosqlite.OperationalError as e:
            # 테이블이 아직 존재하지 않는 경우 등
            logger.warning(f"데이터베이스 마이그레이션 중 오류 발생 (무시 가능): {e}")
            if config.REMOTE_DB_STRICT_MODE:
                try:
                    await self.db.rollback()
                except Exception:
                    logger.error("마이그레이션 실패 후 rollback에도 실패했습니다.", exc_info=True)
                raise
        except Exception as e:
            logger.error(f"데이터베이스 마이그레이션 중 심각한 오류 발생: {e}", exc_info=True)
            try:
                await self.db.rollback()
            except Exception:
                logger.error("마이그레이션 실패 후 rollback에도 실패했습니다.", exc_info=True)
            if config.REMOTE_DB_STRICT_MODE:
                raise

    async def _existing_tables(self, table_names) -> set[str]:
        """필수 테이블 존재 여부를 catalog 단일 쿼리로 확인합니다."""
        names = tuple(sorted({str(name) for name in table_names if name}))
        if not names:
            return set()

        backend = getattr(self.db, "backend", config.DB_BACKEND)
        placeholders = ", ".join("?" for _ in names)
        if backend == "tidb":
            query = f"""
                SELECT TABLE_NAME
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME IN ({placeholders})
            """
        else:
            query = (
                "SELECT name FROM sqlite_master "
                f"WHERE type = 'table' AND name IN ({placeholders})"
            )

        async with self.db.execute(query, names) as cursor:
            rows = await cursor.fetchall()
        return {str(row[0]) for row in rows}

    async def _table_exists(self, table_name: str) -> bool:
        return table_name in await self._existing_tables((table_name,))

    async def setup_hook(self):
        """Discord 로그인 직전에 실행되어 필수 리소스를 초기화합니다.

        여기서는 데이터베이스 파일과 디렉터리를 준비하고, Cog 확장을 순차적으로 로드하며,
        Cog 간에 필요한 의존성을 주입합니다. 이 단계가 성공적으로 끝나야 봇이 정상 작동합니다.
        """
        expected_bot_user_id = int(
            getattr(config, "EXPECTED_DISCORD_BOT_USER_ID", 0) or 0
        )
        actual_bot_user_id = getattr(self.user, "id", None)
        if expected_bot_user_id and actual_bot_user_id != expected_bot_user_id:
            raise RuntimeError(
                "Discord bot identity가 선택한 프로필과 다릅니다: "
                f"actual={actual_bot_user_id!r}, expected={expected_bot_user_id!r}"
            )

        # SQLite 프로필에서만 로컬 데이터 디렉터리를 준비한다.
        if config.DB_BACKEND == "sqlite":
            db_dir = os.path.dirname(self.db_path)
            if db_dir and not os.path.exists(db_dir):
                os.makedirs(db_dir)
                logger.info(f"데이터베이스 디렉토리 '{db_dir}'을(를) 생성했습니다.")

        # 데이터베이스 연결
        try:
            tidb_settings = None
            if config.DB_BACKEND == "tidb":
                tidb_settings = TiDBSettings(
                    host=config.TIDB_HOST or "",
                    port=config.TIDB_PORT,
                    user=config.TIDB_USER or "",
                    password=config.TIDB_PASSWORD or "",
                    database=config.TIDB_NAME,
                    ssl_ca=config.TIDB_SSL_CA,
                    ssl_verify_identity=config.TIDB_SSL_VERIFY_IDENTITY,
                    require_tls=config.REQUIRE_DB_TLS,
                    connect_timeout=config.TIDB_CONNECT_TIMEOUT,
                    read_timeout=config.TIDB_READ_TIMEOUT,
                    write_timeout=config.TIDB_WRITE_TIMEOUT,
                    conn_max_lifetime_seconds=config.TIDB_CONN_MAX_LIFETIME_SECONDS,
                )
            self.db = await connect_main_db(config.DB_BACKEND, sqlite_path=self.db_path, tidb_settings=tidb_settings)
            self.db.row_factory = aiosqlite.Row # 결과를 딕셔너리처럼 접근 가능하게 설정
            logger.info("데이터베이스 연결 완료: backend=%s target=%s", config.DB_BACKEND, _format_storage_target())
        except Exception as e:
            logger.critical(f"데이터베이스 연결 실패. 봇을 종료합니다: {e}", exc_info=True)
            raise RuntimeError("필수 데이터베이스 연결에 실패했습니다.") from e

        # 운영 masamo 첫 프로필 전환에서는 자동 DDL/백필/재시딩을 끌 수 있다.
        if config.AUTO_MIGRATE:
            await self._migrate_db()
        else:
            logger.warning(
                "자동 DB migration이 비활성화되어 읽기 전용 schema 검증만 수행합니다."
            )
        await self._verify_runtime_schema()
        from utils.locale import load_guild_languages_from_db
        await load_guild_languages_from_db(self.db)
        await self._load_guild_settings_cache()

        # Cog(기능 모듈) 로드
        # 의존성 순서를 고려하여 리스트 순서 결정 (예: tools_cog -> 다른 cogs)
        cog_list = [
            'weather_cog', 'tools_cog', 'events', 'commands', 'ai_handler',
            'fun_cog', 'activity_cog', 'poll_cog', 'settings_cog',
            'maintenance_cog', 'proactive_assistant', 'fortune_cog', 'help_cog'
        ]
        # 학교 공지 Cog는 기능을 켠 인스턴스에서만 올린다. 끈 인스턴스에는
        # 해당 테이블이 없으므로 Cog를 올려두면 명령이 DB 오류를 낸다.
        if config.SCHOOL_NOTICE_ENABLED:
            cog_list.append('school_notice_cog')
        cog_list = [name for name in cog_list if name not in config.DISABLED_COGS]
        if config.DISABLED_COGS:
            logger.info(
                "프로필에서 비활성화된 Cog: %s",
                ", ".join(sorted(config.DISABLED_COGS)),
            )

        loaded_cog_modules: set[str] = set()
        for cog_name in cog_list:
            try:
                await self.load_extension(f'cogs.{cog_name}')
                loaded_cog_modules.add(cog_name)
                logger.info(f"Cog 로드 성공: {cog_name}")
            except commands.ExtensionNotFound:
                logger.warning(f"Cog 파일을 찾을 수 없습니다: '{cog_name}.py'. 건너뜁니다.")
            except Exception as e:
                logger.error(f"Cog '{cog_name}' 로드 중 오류 발생: {e}", exc_info=True)

        missing_required_cogs = _missing_startup_cogs(
            loaded_cog_modules,
            set(cog_list),
        )
        if missing_required_cogs:
            raise RuntimeError(
                "필수 Cog 로드에 실패했습니다: " + ", ".join(missing_required_cogs)
            )

        # Cog 간 의존성 주입
        # 일부 Cog는 다른 Cog의 기능을 직접 호출해야 할 수 있습니다.
        ai_handler_cog = self.get_cog('AIHandler')
        if ai_handler_cog:
            # LLMClient에 DB 연결 주입 (AIHandler.__init__ 시점에는 db=None)
            ai_handler_cog.llm_client.db = self.db
            ai_handler_cog.intent_analyzer.db = self.db
            ai_handler_cog.rag_manager.db = self.db
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
        # 봇 자신의 메시지는 무시합니다. (DM 허용을 위해 message.guild 체크 제거)
        if message.author.bot:
            return

        # 기본 로깅 컨텍스트 (DM일 경우, 길드/채널 ID 등은 'DM' 등으로 처리)
        guild_id = message.guild.id if message.guild else "DM"
        channel_id = message.channel.id
        
        # 프라이버시/성능: 메시지 원문은 INFO로 남기지 않는다(파일 핸들러가 동기 write라
        # 매 메시지마다 이벤트 루프를 블로킹하고, 원문 전체가 로그에 남는다). DEBUG로 격하.
        logger.debug(f"Message received from {message.author} ({message.author.id}) in {guild_id}/{channel_id}: {message.content}")

        activity_cog = self.get_cog('ActivityCog')
        if activity_cog:
            try:
                await activity_cog.record_message(message)  # 사용자 활동 기록 (ActivityCog 내부에서 DM 무시 처리함)
            except Exception as exc:  # pragma: no cover
                logger.error(
                    "활동 기록 처리 중 오류: %s",
                    exc,
                    exc_info=True,
                    extra={'guild_id': guild_id, 'channel_id': channel_id}
                )

        message_content = message.content or ""
        # 이 봇은 config의 고정 문자열 prefix로 생성된다. 모든 일반 메시지마다
        # get_prefix coroutine과 임시 list를 만들지 않는다.
        is_command = bool(
            config.COMMAND_PREFIX
            and message_content.startswith(config.COMMAND_PREFIX)
        )

        if is_command:
            await self.process_commands(message)
            return

        ai_handler = self.get_cog('AIHandler')
        if ai_handler:
            # 운세 등록처럼 개인정보를 묻는 대화형 명령 흐름은 AI 대화/RAG 저장 전에
            # 차단한다. 기존 순서는 저장 후 차단하여 생년월일 등의 답변이 중복 보관됐다.
            if message.author.id in self.locked_users:
                logger.debug(
                    "User %s is locked (interactive command flow); AI history and response skipped.",
                    message.author.id,
                )
                return
            try:
                # DM은 대화 기록에 저장하지 않거나 별도 처리 (현재 AIHandler는 DM일 경우 0으로 처리하는 로직 등이 있는지 확인 필요하지만, 
                # 여기서 에러만 안나면 됨. 보통 add_message_to_history 내부에서 guild.id 접근시 에러날 수 있음.)
                # 일단 add_message_to_history는 guild가 있어야 동작하는 것이 일반적이므로 DM이면 스킵
                # DM도 대화 기록에 저장 (AIHandler 내부에서 guild_id=0 등으로 처리)
                await ai_handler.add_message_to_history(message)
            except Exception as exc:  # pragma: no cover
                logger.error(
                    "대화 기록 저장 중 오류: %s",
                    exc,
                    exc_info=True,
                    extra={'guild_id': guild_id, 'channel_id': channel_id}
                )


        ai_ready = ai_handler and ai_handler.is_ready
        if not ai_ready:
            return

        # 채널 화이트리스트 체크 (DM은 무조건 통과, 채널은 화이트리스트)
        if message.guild:
            if not self.is_ai_channel_allowed(message.guild.id, message.channel.id):
                return
        else:
            # DM인 경우: 화이트리스트 체크 스킵 (DM은 기본 허용, Rate Limit 등은 AIHandler에서 처리)
            pass

        if not ai_handler._message_has_valid_mention(message):
            # DM에서는 멘션 없어도 대화 가능하게 할지? -> 보통 DM은 1:1이므로 멘션 없이도 대화함.
            if message.guild:
                logger.debug(f"Message ignored (No valid mention): {message.content}")
                return
            # DM은 멘션 체크 패스

        try:
            # [저사양 보호] 전역 세마포어로 동시 AI 처리 수를 제한한다.
            async with ai_handler.ai_processing_semaphore:
                await ai_handler.process_agent_message(message)
        except Exception as exc:  # pragma: no cover
            logger.error(
                "AI 메시지 처리 중 오류: %s",
                exc,
                exc_info=True,
                extra={'guild_id': guild_id, 'channel_id': channel_id}
            )

    async def close(self):
        """
        봇 종료 시 호출되어 데이터베이스 연결을 안전하게 닫습니다.
        """
        ai_handler = self.get_cog("AIHandler")
        rag_manager = getattr(ai_handler, "rag_manager", None) if ai_handler else None
        if rag_manager is not None:
            await rag_manager.close()
        discord_log_task = getattr(self, "_discord_log_task", None)
        if (
            discord_log_task is not None
            and discord_log_task is not asyncio.current_task()
            and not discord_log_task.done()
        ):
            discord_log_task.cancel()
            await asyncio.gather(discord_log_task, return_exceptions=True)
        try:
            # Cog의 scheduler/cleanup을 먼저 중단해 닫힌 DB를 뒤늦게 사용하는
            # shutdown race를 피한다.
            await super().close()
        finally:
            if self.db:
                await self.db.close()
                self.db = None
                logger.info("데이터베이스 연결을 안전하게 닫았습니다.")

# --- 3. 메인 실행 함수 ---
async def main():
    """봇 인스턴스를 구성하고 Discord 이벤트 루프를 시작합니다.

    이 함수는 `asyncio.run` 진입점에서 호출되며, 봇 토큰 검증과 Discord 세션 수명 관리를 담당합니다.
    """
    # 커스텀 봇 클래스 인스턴스 생성
    bot = ReMasamongBot(
        command_prefix=config.COMMAND_PREFIX,
        intents=config.intents,
        member_cache_flags=(
            discord.MemberCacheFlags.all()
            if config.MEMBER_CACHE_ENABLED
            else discord.MemberCacheFlags.none()
        ),
        chunk_guilds_at_startup=config.MEMBER_CACHE_ENABLED,
    )

    # Discord 로깅 핸들러 등록 및 백그라운드 태스크 시작
    register_discord_logging(bot)

    async with bot:
        logger.info("마사몽 봇을 시작합니다...")
        try:
            await bot.start(config.TOKEN)
        except discord.errors.LoginFailure:
            logger.critical("봇 토큰이 유효하지 않습니다. 설정을 확인해주세요.")
            raise
        except discord.errors.PrivilegedIntentsRequired:
            logger.critical("Privileged Intents가 활성화되지 않았습니다. Discord 개발자 포털에서 설정을 확인해주세요.")
            raise
        except Exception as e:
            logger.critical(f"봇 실행 중 치명적인 오류 발생: {e}", exc_info=True)
            raise

# --- 4. 프로그램 진입점 ---
if __name__ == "__main__":
    try:
        # asyncio 이벤트 루프를 시작하여 main 함수를 실행합니다.
        asyncio.run(main())
    except KeyboardInterrupt:
        # Ctrl+C 입력 시 정상 종료 메시지 출력
        logger.info("Ctrl+C가 감지되었습니다. 봇을 종료합니다.")

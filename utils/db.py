# -*- coding: utf-8 -*-
"""
데이터베이스 관련 작업을 처리하는 유틸리티 함수들의 모음입니다.

주요 기능:
- 분석 데이터 로깅
- 서버별 설정 조회 및 저장
- API 호출 제한(Rate Limiting) 관리
- 오래된 대화 기록 아카이빙
"""

import asyncio
from datetime import datetime, timedelta, timezone
import pytz
import json
import aiosqlite

import config
from logger_config import logger
from utils.constants import DM_LIMIT_WINDOW_HOURS, DM_LIMIT_COUNT, DM_GLOBAL_LIMIT, FORTUNE_DAILY_LIMIT
from typing import Any

KST = pytz.timezone('Asia/Seoul')

# api_call_log 정리 주기 제한 (1시간마다)
_last_api_log_cleanup: datetime | None = None
_API_LOG_CLEANUP_INTERVAL = timedelta(hours=1)
_ALLOWED_GUILD_SETTING_COLUMNS = frozenset(
    {"ai_enabled", "ai_allowed_channels", "persona_text", "language"}
)
_ANALYTICS_METADATA_FIELDS = frozenset(
    {
        "guild_id",
        "user_id",
        "channel_id",
        "command",
        "success",
        "latency_ms",
        "error",
        "trace_id",
        "user_query_chars",
        "final_response_chars",
        "tools",
    }
)
_LINKUP_SCHEMA_READY_ATTR = "_masamong_linkup_usage_table_ready"
_LINKUP_SCHEMA_LOCK_ATTR = "_masamong_linkup_usage_table_lock"


def _analytics_details_for_storage(details: dict[str, Any]) -> dict[str, Any]:
    """본문 저장이 꺼진 경우 명시적으로 허용한 운영 메타데이터만 남깁니다."""
    if config.ANALYTICS_STORE_CONTENT:
        return dict(details)
    return {
        key: value
        for key, value in details.items()
        if key in _ANALYTICS_METADATA_FIELDS
    }

def get_current_time() -> str:
    """현재 시간을 KST(UTC+9) 기준의 문자열로 반환합니다."""
    return datetime.now(KST).strftime("%Y년 %m월 %d일 %H시 %M분 %S초")

async def log_analytics(db: aiosqlite.Connection, event_type: str, details: dict):
    """봇의 주요 활동(명령어, AI 상호작용 등)을 `analytics_log` 테이블에 기록합니다."""
    try:
        stored_details = _analytics_details_for_storage(details)
        details_json = json.dumps(stored_details, ensure_ascii=False)
        await db.execute(
            """
            INSERT INTO analytics_log (
                log_timestamp, event_type, guild_id, user_id, details
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                event_type,
                stored_details.get("guild_id"),
                stored_details.get("user_id"),
                details_json,
            )
        )
        await db.commit()
    except Exception as e:
        logger.error(f"분석 로그({event_type}) 기록 중 오류: {e}", exc_info=True)

async def get_guild_setting(db: aiosqlite.Connection, guild_id: int, setting_name: str, default: Any = None) -> Any:
    """데이터베이스에서 특정 서버(guild)의 설정 값을 조회합니다."""
    if setting_name not in _ALLOWED_GUILD_SETTING_COLUMNS:
        raise ValueError(f"허용되지 않은 서버 설정 이름입니다: {setting_name}")
    try:
        async with db.execute(f"SELECT {setting_name} FROM guild_settings WHERE guild_id = ?", (guild_id,)) as cursor:
            result = await cursor.fetchone()
        return result[0] if result and result[0] is not None else default

    except Exception as e:
        logger.error(f"서버 설정({setting_name}) 조회 중 DB 오류: {e}", exc_info=True, extra={'guild_id': guild_id})
        return default

async def set_guild_setting(db: aiosqlite.Connection, guild_id: int, setting_name: str, value: Any):
    """데이터베이스에 특정 서버(guild)의 설정 값을 저장(UPSERT)합니다."""
    if setting_name not in _ALLOWED_GUILD_SETTING_COLUMNS:
        raise ValueError(f"허용되지 않은 서버 설정 이름입니다: {setting_name}")
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        # UPSERT (INSERT OR REPLACE) 쿼리 실행
        if config.DB_BACKEND == "tidb":
            query = (
                f"INSERT INTO guild_settings "
                f"(guild_id, {setting_name}, created_at, updated_at) "
                f"VALUES (?, ?, ?, ?) "
                f"ON DUPLICATE KEY UPDATE "
                f"{setting_name} = VALUES({setting_name}), "
                f"updated_at = VALUES(updated_at)"
            )
        else:
            query = (
                f"INSERT INTO guild_settings "
                f"(guild_id, {setting_name}, created_at, updated_at) "
                f"VALUES (?, ?, ?, ?) "
                f"ON CONFLICT(guild_id) DO UPDATE SET "
                f"{setting_name} = excluded.{setting_name}, "
                f"updated_at = excluded.updated_at"
            )
        await db.execute(query, (guild_id, value, now_iso, now_iso))
        await db.commit()
    except Exception as e:
        logger.error(f"서버 설정({setting_name}) 저장 중 DB 오류: {e}", exc_info=True, extra={'guild_id': guild_id})
        raise

async def check_api_rate_limit(db: aiosqlite.Connection, api_type: str, rpm_limit: int, rpd_limit: int) -> bool:
    """
    API 호출에 대한 RPM(분당) 및 RPD(일일) 제한을 확인합니다.
    제한에 도달하면 True를, 그렇지 않으면 호출을 기록하고 False를 반환합니다.
    """
    global _last_api_log_cleanup
    try:
        now_utc = datetime.now(timezone.utc)
        one_minute_ago = (now_utc - timedelta(minutes=1)).isoformat()
        one_day_ago = (now_utc - timedelta(days=1)).isoformat()

        # 1. 오래된 로그 정리 (1시간마다만 실행 - 성능 최적화)
        if _last_api_log_cleanup is None or (now_utc - _last_api_log_cleanup) >= _API_LOG_CLEANUP_INTERVAL:
            await db.execute("DELETE FROM api_call_log WHERE called_at < ?", (one_day_ago,))
            await db.commit()
            _last_api_log_cleanup = now_utc

        # 2. 일/분 사용량을 한 쿼리로 계산해 원격 DB 왕복을 줄인다.
        async with db.execute(
            """
            SELECT
                COUNT(*) AS daily_count,
                COALESCE(
                    SUM(CASE WHEN called_at >= ? THEN 1 ELSE 0 END),
                    0
                ) AS minute_count
            FROM api_call_log
            WHERE api_type = ? AND called_at >= ?
            """,
            (one_minute_ago, api_type, one_day_ago),
        ) as cursor:
            row = await cursor.fetchone()
        daily_count = int(row[0] or 0) if row else 0
        minute_count = int(row[1] or 0) if row else 0

        if daily_count >= rpd_limit:
            logger.warning(f"API 일일 호출 한도 도달: {api_type}")
            return True
        if minute_count >= rpm_limit:
            logger.warning(f"API 분당 호출 한도 도달: {api_type}")
            return True

        return False # 제한에 도달하지 않음

    except Exception as e:
        logger.error(f"API Rate Limit 확인 중 DB 오류 ({api_type}): {e}", exc_info=True)
        return True # DB 오류 시 안전하게 요청 차단

async def log_api_call(db: aiosqlite.Connection, api_type: str):
    """API 호출을 `api_call_log` 테이블에 기록합니다."""
    try:
        await db.execute("INSERT INTO api_call_log (api_type, called_at) VALUES (?, ?)", (api_type, datetime.now(timezone.utc).isoformat()))
        await db.commit()
    except Exception as e:
        logger.error(f"API 호출 기록 중 DB 오류 ({api_type}): {e}", exc_info=True)


async def _ensure_linkup_usage_table(db: aiosqlite.Connection):
    """구버전 DB 호환을 위해 Linkup 사용량 테이블 존재를 보장합니다."""
    if not bool(getattr(config, "AUTO_MIGRATE", True)):
        # 명시적 no-migrate 전환에서는 runtime helper도 DDL을 실행하지 않는다.
        # 필수 테이블 존재 여부는 startup schema 검증이 담당한다.
        return
    if bool(getattr(db, _LINKUP_SCHEMA_READY_ATTR, False)):
        return

    # 월 예산 확인과 사용량 기록 때마다 원격 TiDB에 CREATE TABLE/COMMIT 왕복을
    # 반복하지 않도록 연결 단위로 한 번만 점검한다. 동시 첫 요청도 하나만 DDL을
    # 실행하게 해 저사양 인스턴스와 TiDB RU 사용량을 함께 줄인다.
    schema_lock = getattr(db, _LINKUP_SCHEMA_LOCK_ATTR, None)
    if schema_lock is None:
        schema_lock = asyncio.Lock()
        setattr(db, _LINKUP_SCHEMA_LOCK_ATTR, schema_lock)

    async with schema_lock:
        if bool(getattr(db, _LINKUP_SCHEMA_READY_ATTR, False)):
            return
        try:
            backend = str(getattr(db, "backend", "") or "").strip().lower() or "sqlite"
            if backend == "tidb":
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS linkup_usage_log (
                        id BIGINT PRIMARY KEY AUTO_RANDOM,
                        used_at VARCHAR(64) NOT NULL,
                        endpoint VARCHAR(32) NOT NULL,
                        depth VARCHAR(32),
                        render_js BOOLEAN,
                        cost_eur DOUBLE NOT NULL,
                        KEY idx_linkup_usage_time (used_at)
                    )
                    """
                )
            else:
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS linkup_usage_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        used_at TEXT NOT NULL,
                        endpoint TEXT NOT NULL,
                        depth TEXT,
                        render_js BOOLEAN,
                        cost_eur REAL NOT NULL
                    )
                    """
                )
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_linkup_usage_time ON linkup_usage_log (used_at DESC)"
                )
            await db.commit()
            setattr(db, _LINKUP_SCHEMA_READY_ATTR, True)
        except Exception as e:
            logger.error(f"Linkup 사용량 테이블 보장 중 오류: {e}", exc_info=True)
            raise


def _get_month_window_utc(now_utc: datetime | None = None) -> tuple[str, str]:
    now = now_utc or datetime.now(timezone.utc)
    now_kst = now.astimezone(KST)
    month_start_kst = now_kst.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_start_utc = month_start_kst.astimezone(timezone.utc).isoformat()
    now_iso = now.isoformat()
    return month_start_utc, now_iso


async def get_linkup_monthly_spend_eur(db: aiosqlite.Connection, now_utc: datetime | None = None) -> float:
    """이번 달(KST 기준) Linkup 사용 금액(유로)을 반환합니다."""
    try:
        await _ensure_linkup_usage_table(db)
        start_utc, end_utc = _get_month_window_utc(now_utc)
        async with db.execute(
            "SELECT COALESCE(SUM(cost_eur), 0) FROM linkup_usage_log WHERE used_at >= ? AND used_at <= ?",
            (start_utc, end_utc),
        ) as cursor:
            row = await cursor.fetchone()
            return float(row[0] or 0.0) if row else 0.0
    except Exception as e:
        logger.error(f"Linkup 월 사용액 조회 중 오류: {e}", exc_info=True)
        return float("inf")


async def can_spend_linkup_budget(
    db: aiosqlite.Connection,
    estimated_cost_eur: float,
    *,
    budget_limit_eur: float | None = None,
    now_utc: datetime | None = None,
) -> tuple[bool, float, float]:
    """Linkup 월 예산 사용 가능 여부를 확인합니다."""
    limit = float(config.LINKUP_MONTHLY_BUDGET_EUR if budget_limit_eur is None else budget_limit_eur)
    if limit <= 0:
        return False, 0.0, limit
    used = await get_linkup_monthly_spend_eur(db, now_utc=now_utc)
    if used == float("inf"):
        return False, used, limit
    return (used + max(0.0, float(estimated_cost_eur))) <= limit, used, limit


async def log_linkup_usage(
    db: aiosqlite.Connection,
    *,
    endpoint: str,
    cost_eur: float,
    depth: str | None = None,
    render_js: bool | None = None,
    used_at_iso: str | None = None,
):
    """Linkup 과금 이벤트를 기록합니다."""
    try:
        await _ensure_linkup_usage_table(db)
        used_at = used_at_iso or datetime.now(timezone.utc).isoformat()
        await db.execute(
            """
            INSERT INTO linkup_usage_log (used_at, endpoint, depth, render_js, cost_eur)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                used_at,
                str(endpoint or "").strip().lower(),
                (str(depth or "").strip().lower() or None),
                None if render_js is None else (1 if bool(render_js) else 0),
                float(cost_eur),
            ),
        )
        await db.commit()
    except Exception as e:
        logger.error(f"Linkup 사용량 기록 중 오류: {e}", exc_info=True)


async def get_daily_api_count(db: aiosqlite.Connection, api_type: str) -> int:
    """오늘 특정 API의 호출 횟수를 반환합니다.
    
    Args:
        db: 데이터베이스 연결
        api_type: API 종류 (예: 'google_custom_search')
        
    Returns:
        오늘의 API 호출 횟수
    """
    try:
        now_utc = datetime.now(timezone.utc)
        today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        
        async with db.execute(
            "SELECT COUNT(*) FROM api_call_log WHERE api_type = ? AND called_at >= ?",
            (api_type, today_start)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0
    except Exception as e:
        logger.error(f"일일 API 호출 횟수 조회 중 오류 ({api_type}): {e}", exc_info=True)
        return 0

async def archive_old_conversations(db: aiosqlite.Connection):
    """
    `conversation_history` 테이블의 레코드 수가 한도를 초과하면,
    가장 오래된 레코드를 `conversation_history_archive` 테이블로 옮기고 삭제합니다.
    """
    conf = config.RAG_ARCHIVING_CONFIG
    if not conf.get("enabled"):
        return

    try:
        async with db.execute("SELECT COUNT(*) FROM conversation_history") as cursor:
            current_records = (await cursor.fetchone())[0]

        if current_records <= conf.get("history_limit"):
            return

        # 아카이빙할 레코드 수 계산
        records_to_archive = min(current_records - conf.get("history_limit"), conf.get("batch_size"))
        logger.info(f"대화 기록 아카이빙 시작: {records_to_archive}개 레코드.")

        async with db.execute("SELECT message_id FROM conversation_history ORDER BY created_at ASC LIMIT ?", (records_to_archive,)) as cursor:
            ids_to_archive = [row[0] for row in await cursor.fetchall()]

        if not ids_to_archive: return

        placeholders = ",".join("?" * len(ids_to_archive))
        await db.execute(f"INSERT INTO conversation_history_archive SELECT * FROM conversation_history WHERE message_id IN ({placeholders})", ids_to_archive)
        await db.execute(f"DELETE FROM conversation_history WHERE message_id IN ({placeholders})", ids_to_archive)
        await db.commit()
        logger.info(f"대화 기록 아카이빙 완료: {len(ids_to_archive)}개 레코드.")

    except Exception as e:
        logger.error(f"RAG 아카이빙 작업 중 DB 오류: {e}", exc_info=True)


async def prune_user_activity_log(db: aiosqlite.Connection, retention_days: int):
    """`user_activity_log`에서 보존 기간을 넘긴 오래된 행을 삭제합니다.

    retention_days가 0 이하이면 비활성(무한 보존)입니다. created_at은 UTC ISO8601로
    저장되므로 문자열 비교로 안전하게 절단할 수 있습니다.
    """
    if not retention_days or retention_days <= 0:
        return
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(retention_days))).isoformat()
        await db.execute("DELETE FROM user_activity_log WHERE created_at < ?", (cutoff,))
        await db.commit()
        logger.info("user_activity_log 보존정책 적용 완료: %d일 이전 삭제 (cutoff=%s)", retention_days, cutoff)
    except Exception as e:
        logger.error(f"user_activity_log 정리 중 DB 오류: {e}", exc_info=True)


# ========== 이미지 생성 Rate Limiting ==========

async def check_image_user_limit(db: aiosqlite.Connection, user_id: int) -> tuple[bool, int]:
    """유저의 이미지 생성 제한을 확인합니다.
    
    Args:
        db: 데이터베이스 연결
        user_id: 유저 ID
        
    Returns:
        (제한 도달 여부, 남은 이미지 수)
    """
    try:
        reset_hours = getattr(config, 'IMAGE_USER_RESET_HOURS', 6)
        user_limit = getattr(config, 'IMAGE_USER_LIMIT', 7)
        
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=reset_hours)).isoformat()
        api_type = f"image_gen_user_{user_id}"
        
        async with db.execute(
            "SELECT COUNT(*) FROM api_call_log WHERE api_type = ? AND called_at >= ?",
            (api_type, cutoff)
        ) as cursor:
            row = await cursor.fetchone()
            count = row[0] if row else 0
        
        remaining = max(0, user_limit - count)
        is_limited = count >= user_limit
        
        if is_limited:
            logger.warning(f"유저 {user_id} 이미지 생성 제한 도달 ({count}/{user_limit})")
        
        return is_limited, remaining
        
    except Exception as e:
        logger.error(f"이미지 유저 제한 확인 중 오류 (user_id={user_id}): {e}", exc_info=True)
        return True, 0  # 오류 시 안전하게 제한


async def check_image_global_limit(db: aiosqlite.Connection) -> tuple[bool, int]:
    """전역 일일 이미지 생성 제한을 확인합니다.
    
    Returns:
        (제한 도달 여부, 남은 이미지 수)
    """
    try:
        global_limit = getattr(config, 'IMAGE_GLOBAL_DAILY_LIMIT', 50)
        
        now_utc = datetime.now(timezone.utc)
        today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        
        async with db.execute(
            "SELECT COUNT(*) FROM api_call_log WHERE api_type = ? AND called_at >= ?",
            ("image_gen_global", today_start)
        ) as cursor:
            row = await cursor.fetchone()
            count = row[0] if row else 0
        
        remaining = max(0, global_limit - count)
        is_limited = count >= global_limit
        
        if is_limited:
            logger.warning(f"이미지 생성 전역 일일 제한 도달 ({count}/{global_limit})")
        
        return is_limited, remaining
        
    except Exception as e:
        logger.error(f"이미지 전역 제한 확인 중 오류: {e}", exc_info=True)
        return True, 0  # 오류 시 안전하게 제한


async def log_image_generation(db: aiosqlite.Connection, user_id: int):
    """이미지 생성을 기록합니다 (유저별 + 전역).
    
    Args:
        db: 데이터베이스 연결
        user_id: 이미지를 생성한 유저 ID
    """
    try:
        now = datetime.now(timezone.utc).isoformat()
        
        # 유저별 기록
        await db.execute(
            "INSERT INTO api_call_log (api_type, called_at) VALUES (?, ?)",
            (f"image_gen_user_{user_id}", now)
        )
        
        # 전역 기록
        await db.execute(
            "INSERT INTO api_call_log (api_type, called_at) VALUES (?, ?)",
            ("image_gen_global", now)
        )
        
        await db.commit()
        logger.info(f"이미지 생성 기록 완료 (user_id={user_id})")
        
    except Exception as e:
        logger.error(f"이미지 생성 기록 중 오류 (user_id={user_id}): {e}", exc_info=True)


# ========== DM Rate Limiting (New) ==========

async def check_dm_message_limit(db: aiosqlite.Connection, user_id: int) -> tuple[bool, str]:
    """DM 1:1 대화 제한을 확인합니다. (3시간당 5회)
    
    Returns:
        (허용 여부, 안내 메시지용 리셋 시간 문자열 or None)
    """
    try:
        now = datetime.now(timezone.utc)
        now_str = now.isoformat()
        
        async with db.execute("SELECT usage_count, window_start_at, reset_at FROM dm_usage_logs WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            
        if not row:
            # 기록 없음 -> 초기화
            reset_at = (now + timedelta(hours=DM_LIMIT_WINDOW_HOURS)).isoformat()
            await db.execute(
                "INSERT INTO dm_usage_logs (user_id, usage_count, window_start_at, reset_at) VALUES (?, 1, ?, ?)",
                (user_id, now_str, reset_at)
            )
            await db.commit()
            return True, None
            
        usage_count, window_start_at, reset_at_str = row
        reset_at_dt = datetime.fromisoformat(reset_at_str)
        
        if now > reset_at_dt:
            # 윈도우 지남 -> 초기화
            reset_at = (now + timedelta(hours=DM_LIMIT_WINDOW_HOURS)).isoformat()
            await db.execute(
                "UPDATE dm_usage_logs SET usage_count = 1, window_start_at = ?, reset_at = ? WHERE user_id = ?",
                (now_str, reset_at, user_id)
            )
            await db.commit()
            return True, None
        
        # 윈도우 내 사용
        if usage_count < DM_LIMIT_COUNT:
            # 카운트 증가
            await db.execute(
                "UPDATE dm_usage_logs SET usage_count = usage_count + 1 WHERE user_id = ?",
                (user_id,)
            )
            await db.commit()
            return True, None
        
        # 제한 도달
        reset_kst = reset_at_dt.astimezone(KST).strftime('%H:%M')
        return False, reset_kst
            
    except Exception as e:
        logger.error(f"DM 제한 확인 중 오류 (user_id={user_id}): {e}", exc_info=True)
        # 오류 시 통과 (서비스 가용성 우선)
        return True, None

async def check_global_dm_limit(db: aiosqlite.Connection) -> bool:
    """
    전체 유저의 하루 DM 사용량을 제한합니다. (API 비용 방어용)
    하루 100회 초과 시 False 반환.
    `system_counters` 테이블 사용.
    """
    try:
        # 오늘 날짜 키 생성 (KST 기준)
        today_key = f"dm_daily_global_{datetime.now(KST).strftime('%Y-%m-%d')}"
        now_str = datetime.now(timezone.utc).isoformat()
        
        # 현재 값 조회
        async with db.execute("SELECT counter_value FROM system_counters WHERE counter_name = ?", (today_key,)) as cursor:
            row = await cursor.fetchone()
            
        current_count = row[0] if row else 0
        
        if current_count >= DM_GLOBAL_LIMIT:
            logger.warning(f"🚨 전역 DM 일일 한도 초과! ({current_count}/{DM_GLOBAL_LIMIT})")
            return False
            
        # 카운트 증가 (UPSERT)
        if config.DB_BACKEND == "tidb":
            query = """
                INSERT INTO system_counters (counter_name, counter_value, last_reset_at)
                VALUES (?, 1, ?)
                ON DUPLICATE KEY UPDATE
                    counter_value = counter_value + 1,
                    last_reset_at = VALUES(last_reset_at)
            """
        else:
            query = """
                INSERT INTO system_counters (counter_name, counter_value, last_reset_at)
                VALUES (?, 1, ?)
                ON CONFLICT(counter_name) DO UPDATE SET
                    counter_value = counter_value + 1,
                    last_reset_at = excluded.last_reset_at
            """
        await db.execute(query, (today_key, now_str))
        await db.commit()
        
        return True
        
    except Exception as e:
        logger.error(f"전역 DM 제한 확인 중 오류: {e}", exc_info=True)
        return True # 오류 시 차단보다는 허용 (가용성)

async def check_fortune_daily_limit(db: aiosqlite.Connection, user_id: int) -> tuple[bool, int]:
    """
    유저의 일일 운세 상세/월년 운세 조회 횟수를 제한합니다. (하루 3회)
    Returns: (제한 도달 여부, 남은 횟수)
    """
    try:
        now_kst = datetime.now(KST)
        today_key = f"fortune_limit_{user_id}_{now_kst.strftime('%Y-%m-%d')}"
        
        # 24시간 후 만료되는 키로 관리하거나, 간단히 로그 테이블 사용
        # 여기서는 api_call_log 재활용 (type prefixed)
        api_type = f"fortune_detail_{user_id}"
        today_start_utc = now_kst.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
        
        async with db.execute(
            "SELECT COUNT(*) FROM api_call_log WHERE api_type = ? AND called_at >= ?",
            (api_type, today_start_utc)
        ) as cursor:
            row = await cursor.fetchone()
            count = row[0] if row else 0
            
        remaining = max(0, FORTUNE_DAILY_LIMIT - count)
        return (count >= FORTUNE_DAILY_LIMIT), remaining

    except Exception as e:
        logger.error(f"운세 제한 확인 중 오류: {e}", exc_info=True)
        return False, 3

async def log_fortune_usage(db: aiosqlite.Connection, user_id: int):
    """운세 상세 조회 사용을 기록합니다."""
    try:
        api_type = f"fortune_detail_{user_id}"
        await log_api_call(db, api_type)
    except Exception as e:
        logger.error(f"운세 사용 기록 실패: {e}")

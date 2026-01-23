# -*- coding: utf-8 -*-
"""
데이터베이스 관련 작업을 처리하는 유틸리티 함수들의 모음입니다.

주요 기능:
- 분석 데이터 로깅
- 서버별 설정 조회 및 저장
- API 호출 제한(Rate Limiting) 관리
- 오래된 대화 기록 아카이빙
"""

from datetime import datetime, timedelta, timezone
import pytz
import json
import aiosqlite
from typing import Any

import config
from logger_config import logger

KST = pytz.timezone('Asia/Seoul')

def get_current_time() -> str:
    """현재 시간을 KST(UTC+9) 기준의 문자열로 반환합니다."""
    return datetime.now(KST).strftime("%Y년 %m월 %d일 %H시 %M분 %S초")

async def log_analytics(db: aiosqlite.Connection, event_type: str, details: dict):
    """봇의 주요 활동(명령어, AI 상호작용 등)을 `analytics_log` 테이블에 기록합니다."""
    try:
        details_json = json.dumps(details, ensure_ascii=False)
        await db.execute(
            "INSERT INTO analytics_log (event_type, guild_id, user_id, details) VALUES (?, ?, ?, ?)",
            (event_type, details.get('guild_id'), details.get('user_id'), details_json)
        )
        await db.commit()
    except Exception as e:
        logger.error(f"분석 로그({event_type}) 기록 중 오류: {e}", exc_info=True)

async def get_guild_setting(db: aiosqlite.Connection, guild_id: int, setting_name: str, default: Any = None) -> Any:
    """데이터베이스에서 특정 서버(guild)의 설정 값을 조회합니다."""
    try:
        # 허용된 설정 이름인지 확인하여 SQL Injection 방지
        allowed_columns = ["ai_enabled", "ai_allowed_channels", "persona_text"]
        if setting_name not in allowed_columns:
            logger.error(f"허용되지 않은 설정({setting_name})에 대한 접근 시도.", extra={'guild_id': guild_id})
            return default

        async with db.execute(f"SELECT {setting_name} FROM guild_settings WHERE guild_id = ?", (guild_id,)) as cursor:
            result = await cursor.fetchone()
        return result[0] if result and result[0] is not None else default

    except Exception as e:
        logger.error(f"서버 설정({setting_name}) 조회 중 DB 오류: {e}", exc_info=True, extra={'guild_id': guild_id})
        return default

async def set_guild_setting(db: aiosqlite.Connection, guild_id: int, setting_name: str, value: Any):
    """데이터베이스에 특정 서버(guild)의 설정 값을 저장(UPSERT)합니다."""
    try:
        allowed_columns = ["ai_enabled", "ai_allowed_channels", "persona_text"]
        if setting_name not in allowed_columns:
            logger.error(f"허용되지 않은 설정({setting_name})에 대한 저장 시도.", extra={'guild_id': guild_id})
            return

        # UPSERT (INSERT OR REPLACE) 쿼리 실행
        await db.execute(
            f"INSERT INTO guild_settings (guild_id, {setting_name}) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET {setting_name} = excluded.{setting_name}",
            (guild_id, value)
        )
        await db.commit()
    except Exception as e:
        logger.error(f"서버 설정({setting_name}) 저장 중 DB 오류: {e}", exc_info=True, extra={'guild_id': guild_id})

async def check_api_rate_limit(db: aiosqlite.Connection, api_type: str, rpm_limit: int, rpd_limit: int) -> bool:
    """
    API 호출에 대한 RPM(분당) 및 RPD(일일) 제한을 확인합니다.
    제한에 도달하면 True를, 그렇지 않으면 호출을 기록하고 False를 반환합니다.
    """
    try:
        now_utc = datetime.now(timezone.utc)
        one_minute_ago = (now_utc - timedelta(minutes=1)).isoformat()
        one_day_ago = (now_utc - timedelta(days=1)).isoformat()

        # 1. 오래된 로그 정리 (성능 유지)
        await db.execute("DELETE FROM api_call_log WHERE called_at < ?", (one_day_ago,))

        # 2. RPD (일일 제한) 확인
        async with db.execute("SELECT COUNT(*) FROM api_call_log WHERE api_type = ? AND called_at >= ?", (api_type, one_day_ago)) as cursor:
            if (await cursor.fetchone())[0] >= rpd_limit:
                logger.warning(f"API 일일 호출 한도 도달: {api_type}")
                return True

        # 3. RPM (분당 제한) 확인
        async with db.execute("SELECT COUNT(*) FROM api_call_log WHERE api_type = ? AND called_at >= ?", (api_type, one_minute_ago)) as cursor:
            if (await cursor.fetchone())[0] >= rpm_limit:
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
        LIMIT_WINDOW_HOURS = 3
        LIMIT_COUNT = 5
        
        now = datetime.now(timezone.utc)
        now_str = now.isoformat()
        
        async with db.execute("SELECT usage_count, window_start_at, reset_at FROM dm_usage_logs WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            
        if not row:
            # 기록 없음 -> 초기화
            reset_at = (now + timedelta(hours=LIMIT_WINDOW_HOURS)).isoformat()
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
            reset_at = (now + timedelta(hours=LIMIT_WINDOW_HOURS)).isoformat()
            await db.execute(
                "UPDATE dm_usage_logs SET usage_count = 1, window_start_at = ?, reset_at = ? WHERE user_id = ?",
                (now_str, reset_at, user_id)
            )
            await db.commit()
            return True, None
        
        # 윈도우 내 사용
        if usage_count < LIMIT_COUNT:
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

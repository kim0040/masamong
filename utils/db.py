# -*- coding: utf-8 -*-
import os
from datetime import datetime, timedelta
import pytz
import json
import asyncio
import aiosqlite
import discord
from logger_config import logger
import config
from typing import Any

KST = pytz.timezone('Asia/Seoul')

def get_current_time() -> str:
    """
    현재 시간을 '년-월-일 시:분:초' 형식의 문자열로 반환합니다.
    항상 대한민국 표준시(KST, UTC+9)를 기준으로 시간을 반환합니다.
    """
    now_kst = datetime.now(KST)
    return now_kst.strftime("%Y년 %m월 %d일 %H시 %M분 %S초")

async def log_analytics(db: aiosqlite.Connection, event_type: str, details: dict):
    """분석 이벤트를 DB에 기록합니다."""
    try:
        details_json = json.dumps(details, ensure_ascii=False)
        guild_id = details.get('guild_id')
        user_id = details.get('user_id')

        await db.execute("""
            INSERT INTO analytics_log (event_type, guild_id, user_id, details)
            VALUES (?, ?, ?, ?)
        """, (event_type, guild_id, user_id, details_json))
        await db.commit()

    except aiosqlite.Error as e:
        logger.error(f"분석 로그 기록 중 DB 오류 (이벤트: {event_type}): {e}", exc_info=True, extra={'guild_id': details.get('guild_id')})
    except Exception as e:
        logger.error(f"분석 로그 기록 중 일반 오류 (이벤트: {event_type}): {e}", exc_info=True, extra={'guild_id': details.get('guild_id')})

async def get_guild_setting(db: aiosqlite.Connection, guild_id: int, setting_name: str, default: Any = None) -> Any:
    """DB에서 특정 서버(guild)의 설정 값을 가져옵니다."""
    try:
        allowed_columns = ["ai_enabled", "ai_allowed_channels", "proactive_response_probability", "proactive_response_cooldown", "persona_text"]
        if setting_name not in allowed_columns:
            logger.error(f"허용되지 않은 설정 이름에 대한 접근 시도: {setting_name}", extra={'guild_id': guild_id})
            return default

        async with db.execute(f"SELECT {setting_name} FROM guild_settings WHERE guild_id = ?", (guild_id,)) as cursor:
            result = await cursor.fetchone()

        if result:
            if setting_name == 'ai_allowed_channels' and result[0]:
                try:
                    return json.loads(result[0])
                except (json.JSONDecodeError, TypeError):
                    logger.error(f"Guild({guild_id})의 ai_allowed_channels JSON 파싱 오류.", extra={'guild_id': guild_id})
                    return default
            return result[0]
        else:
            return default

    except aiosqlite.Error as e:
        logger.error(f"Guild 설정({setting_name}) 조회 중 DB 오류: {e}", exc_info=True, extra={'guild_id': guild_id})
        return default

async def is_api_limit_reached(db: aiosqlite.Connection, counter_name: str, limit: int) -> bool:
    """DB의 API 카운터가 한도에 도달했는지 확인하고, 필요시 리셋합니다."""
    try:
        today_kst_str = datetime.now(KST).strftime('%Y-%m-%d')

        async with db.execute("SELECT counter_value, last_reset_at FROM system_counters WHERE counter_name = ?", (counter_name,)) as cursor:
            result = await cursor.fetchone()

        if result is None:
            logger.error(f"DB에 '{counter_name}' 카운터가 없습니다. init_db.py를 실행하세요.")
            return True

        count, last_reset_at_iso = result
        last_reset_date_kst_str = datetime.fromisoformat(last_reset_at_iso).astimezone(KST).strftime('%Y-%m-%d')

        if last_reset_date_kst_str != today_kst_str:
            logger.info(f"KST 날짜 변경. '{counter_name}' API 카운터를 0으로 리셋합니다.")
            await db.execute("UPDATE system_counters SET counter_value = 0, last_reset_at = ? WHERE counter_name = ?", (datetime.utcnow().isoformat(), counter_name))
            await db.commit()
            return False

        if count >= limit:
            logger.warning(f"'{counter_name}' API 일일 호출 한도 도달 ({count}/{limit}). API 요청 거부.")
            return True

        return False

    except aiosqlite.Error as e:
        logger.error(f"API 한도 확인 중 DB 오류: {e}", exc_info=True)
        return True

async def increment_api_counter(db: aiosqlite.Connection, counter_name: str):
    """DB의 API 카운터를 1 증가시킵니다."""
    try:
        await db.execute("UPDATE system_counters SET counter_value = counter_value + 1 WHERE counter_name = ?", (counter_name,))
        await db.commit()
    except aiosqlite.Error as e:
        logger.error(f"API 카운터 증가 중 DB 오류: {e}", exc_info=True)

async def archive_old_conversations(db: aiosqlite.Connection):
    """
    오래된 대화 기록을 `conversation_history`에서 `conversation_history_archive`로 옮깁니다.
    config.py의 RAG_ARCHIVING_CONFIG 설정에 따라 작동합니다.
    """
    conf = config.RAG_ARCHIVING_CONFIG
    if not conf.get("enabled"):
        logger.debug("RAG 아카이빙이 비활성화되어 있어 작업을 건너뜁니다.")
        return

    try:
        async with db.execute("SELECT COUNT(*) FROM conversation_history") as cursor:
            current_records = (await cursor.fetchone())[0]
        logger.info(f"RAG 아카이빙 확인: 현재 대화 기록 {current_records}개. (한도: {conf.get('history_limit')})")

        if current_records <= conf.get("history_limit"):
            logger.info("대화 기록이 한도 내에 있어 아카이빙을 건너뜁니다.")
            return

        records_to_archive_total = current_records - conf.get("history_limit")
        records_to_archive_batch = min(records_to_archive_total, conf.get("batch_size"))
        logger.info(f"아카이빙 목표: {records_to_archive_batch}개 레코드.")

        async with db.execute("SELECT message_id FROM conversation_history ORDER BY created_at ASC LIMIT ?", (records_to_archive_batch,)) as cursor:
            message_ids_to_archive = [row[0] for row in await cursor.fetchall()]

        if not message_ids_to_archive:
            logger.warning("아카이빙할 레코드가 없습니다. 작업을 중단합니다.")
            return

        placeholders = ",".join("?" for _ in message_ids_to_archive)

        await db.execute(f"INSERT INTO conversation_history_archive SELECT * FROM conversation_history WHERE message_id IN ({placeholders})", message_ids_to_archive)
        await db.execute(f"DELETE FROM conversation_history WHERE message_id IN ({placeholders})", message_ids_to_archive)

        await db.commit()
        logger.info(f"총 {len(message_ids_to_archive)}개의 대화 기록 아카이빙 완료.")

    except aiosqlite.Error as e:
        logger.error(f"RAG 아카이빙 작업 중 DB 오류 발생: {e}", exc_info=True)

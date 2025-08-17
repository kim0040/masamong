# -*- coding: utf-8 -*-
import os
from datetime import datetime, timedelta
import pytz
import requests
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


# --- KMA API v3 (단기예보) ---
KMA_API_BASE_URL = "https://apihub.kma.go.kr/api/typ02/openApi/VilageFcstInfoService_2.0"

def get_kma_api_key():
    """config.py에서 기상청 API 키를 가져옵니다."""
    api_key = config.KMA_API_KEY
    if not api_key or api_key == 'YOUR_KMA_API_KEY':
        logger.warning("기상청 API 키(KMA_API_KEY)가 config.py에 설정되지 않았거나 기본값입니다.")
        return None
    return api_key

async def _fetch_kma_api(db: aiosqlite.Connection, endpoint: str, params: dict) -> dict | None:
    """새로운 기상청 API를 호출하고 응답을 파싱하는 통합 함수."""
    api_key = get_kma_api_key()
    if not api_key:
        return {"error": "api_key_missing", "message": config.MSG_WEATHER_API_KEY_MISSING}

    if await is_api_limit_reached(db, 'kma_daily_calls', config.KMA_API_DAILY_CALL_LIMIT):
        return {"error": "limit_reached", "message": config.MSG_KMA_API_DAILY_LIMIT_REACHED}

    full_url = f"{KMA_API_BASE_URL}/{endpoint}"
    
    base_params = {
        "authKey": api_key,
        "pageNo": "1",
        "numOfRows": "1000",
        "dataType": "JSON"
    }
    base_params.update(params)

    try:
        response = await asyncio.to_thread(requests.get, full_url, params=base_params, timeout=15)
        response.raise_for_status()
        data = response.json()

        header = data.get('response', {}).get('header', {})
        if header.get('resultCode') != '00':
            error_msg = header.get('resultMsg', 'Unknown API Error')
            logger.error(f"기상청 API가 오류를 반환했습니다: {error_msg}")
            return {"error": "api_error", "message": f"기상청 API 오류: {error_msg}"}
        
        await increment_api_counter(db, 'kma_daily_calls')
        return data

    except requests.exceptions.Timeout:
        logger.error("기상청 API 요청 시간 초과.")
        return {"error": "timeout", "message": config.MSG_WEATHER_FETCH_ERROR}
    except requests.exceptions.HTTPError as e:
        logger.error(f"기상청 API HTTP 오류: {e.response.status_code} for url: {e.response.url}")
        return {"error": "http_error", "message": config.MSG_WEATHER_FETCH_ERROR}
    except json.JSONDecodeError:
        logger.error(f"기상청 API 응답 JSON 파싱 실패. 응답 내용: {response.text}")
        return {"error": "json_error", "message": config.MSG_WEATHER_FETCH_ERROR}
    except Exception as e:
        logger.error(f"기상청 API 처리 중 예기치 않은 오류: {e}", exc_info=True)
        return {"error": "unknown_error", "message": config.MSG_WEATHER_FETCH_ERROR}

async def get_current_weather_from_kma(db: aiosqlite.Connection, nx: str, ny: str) -> dict | None:
    """초단기실황 정보를 새로운 기상청 API로부터 가져옵니다."""
    now = datetime.now(KST)
    base_dt = now
    if now.minute < 45:
        base_dt = now - timedelta(hours=1)
    base_date = base_dt.strftime("%Y%m%d")
    base_time = base_dt.strftime("%H00")

    params = {
        "base_date": base_date,
        "base_time": base_time,
        "nx": nx,
        "ny": ny
    }
    return await _fetch_kma_api(db, "getUltraSrtNcst", params)

async def get_short_term_forecast_from_kma(db: aiosqlite.Connection, nx: str, ny: str) -> dict | None:
    """단기예보 정보를 새로운 기상청 API로부터 가져옵니다."""
    now = datetime.now(KST)
    available_times = [2, 5, 8, 11, 14, 17, 20, 23]
    current_marker = now.hour * 100 + now.minute
    
    valid_times = [t for t in available_times if (t * 100 + 10) <= current_marker]

    if not valid_times:
        base_dt = now - timedelta(days=1)
        base_time_hour = 23
    else:
        base_dt = now
        base_time_hour = max(valid_times)

    base_date = base_dt.strftime("%Y%m%d")
    base_time = f"{base_time_hour:02d}00"

    params = {
        "base_date": base_date,
        "base_time": base_time,
        "nx": nx,
        "ny": ny
    }
    return await _fetch_kma_api(db, "getVilageFcst", params)


def format_current_weather(weather_data: dict | None) -> str:
    """JSON으로 파싱된 초단기실황 데이터를 사람이 읽기 좋은 문자열로 포맷팅합니다."""
    if not weather_data or weather_data.get("error"):
        return weather_data.get("message", config.MSG_WEATHER_FETCH_ERROR)
    try:
        items = weather_data['response']['body']['items']['item']
        weather_values = {item['category']: item['obsrValue'] for item in items}

        temp = weather_values.get('T1H', 'N/A') + "°C"
        reh = weather_values.get('REH', 'N/A') + "%"
        rn1 = weather_values.get('RN1', '0')

        pty_code = weather_values.get('PTY', '0')
        pty_map = {"0": "없음", "1": "비", "2": "비/눈", "3": "눈", "5": "빗방울", "6": "빗방울/눈날림", "7": "눈날림"}
        pty = pty_map.get(pty_code, "정보 없음")
        
        rain_info = ""
        if float(rn1) > 0:
            rain_info = f" (시간당 {rn1}mm)"

        return f"🌡️기온: {temp}, 💧습도: {reh}, ☔강수: {pty}{rain_info}"
    except (KeyError, TypeError, IndexError):
        logger.error(f"초단기실황 포맷팅 중 오류: {weather_data}", exc_info=True)
        return config.MSG_WEATHER_NO_DATA


def format_short_term_forecast(forecast_data: dict | None, day_name: str, target_day_offset: int = 0) -> str:
    """JSON으로 파싱된 단기예보 데이터를 사람이 읽기 좋은 문자열로 포맷팅합니다."""
    if not forecast_data or forecast_data.get("error"):
        return f"{day_name} 날씨: {forecast_data.get('message', config.MSG_WEATHER_FETCH_ERROR)}"

    try:
        all_items = forecast_data.get('response', {}).get('body', {}).get('items', {}).get('item', [])
        if not all_items:
            return config.MSG_WEATHER_NO_DATA

        target_date = datetime.now(KST).date() + timedelta(days=target_day_offset)
        target_date_str = target_date.strftime("%Y%m%d")

        day_items = [item for item in all_items if item.get('fcstDate') == target_date_str]
        if not day_items:
            return f"{day_name} 날씨: 해당 날짜의 예보 데이터가 없습니다."

        min_temps = [float(item['fcstValue']) for item in day_items if item['category'] == 'TMN']
        max_temps = [float(item['fcstValue']) for item in day_items if item['category'] == 'TMX']
        min_temp = min(min_temps) if min_temps else None
        max_temp = max(max_temps) if max_temps else None

        sky_map = {"1": "맑음☀️", "3": "구름많음☁️", "4": "흐림🌥️"}
        noon_sky_item = next((item for item in day_items if item['category'] == 'SKY' and item['fcstTime'] == '1200'), None)
        noon_sky = sky_map.get(noon_sky_item['fcstValue'], "정보없음") if noon_sky_item else "정보없음"

        pops = [int(item['fcstValue']) for item in day_items if item['category'] == 'POP']
        max_pop = max(pops) if pops else 0

        temp_range_str = ""
        if min_temp is not None and max_temp is not None:
            temp_range_str = f"(최저 {min_temp:.1f}°C / 최고 {max_temp:.1f}°C)"
        elif max_temp is not None:
            temp_range_str = f"(최고 {max_temp:.1f}°C)"
        
        weather_desc = f"하늘: 대체로 {noon_sky}, 최고 강수확률: {max_pop}%"

        return f"{day_name} 날씨 {temp_range_str}:\n{weather_desc}".strip()
    except (KeyError, TypeError, IndexError, StopIteration, ValueError) as e:
        logger.error(f"단기예보 포맷팅 중 오류: {e}", exc_info=True)
        return config.MSG_WEATHER_NO_DATA

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

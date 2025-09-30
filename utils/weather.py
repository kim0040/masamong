# -*- coding: utf-8 -*-
"""
기상청 API와 상호작용하여 날씨 데이터를 가져오고,
사용하기 쉬운 형태로 가공하는 유틸리티 함수들을 제공합니다.
"""

import asyncio
import json
from datetime import datetime, timedelta
import pytz
import requests
import aiosqlite

import config
from logger_config import logger
from . import db as db_utils
from . import http

KST = pytz.timezone('Asia/Seoul')

def get_kma_api_key() -> str | None:
    """설정에서 기상청 API 키를 안전하게 가져옵니다."""
    api_key = config.KMA_API_KEY
    if api_key and api_key != 'YOUR_KMA_API_KEY':
        return api_key

    fallback_key = getattr(config, 'GO_DATA_API_KEY_KR', None)
    if fallback_key and fallback_key not in ('', 'YOUR_GO_DATA_API_KEY_KR'):
        logger.info("기상청 API 키가 없어 공공데이터포털 인증키를 대신 사용합니다.")
        return fallback_key

    logger.warning("기상청 API 키(KMA_API_KEY)가 설정되지 않았습니다.")
    return None

async def _fetch_kma_api(db: aiosqlite.Connection, endpoint: str, params: dict) -> dict | None:
    """
    기상청 API 엔드포인트를 호출하는 중앙 래퍼 함수입니다.
    API 키, 호출 제한 확인, 비동기 요청, 오류 처리를 담당합니다.
    """
    api_key = get_kma_api_key()
    if not api_key: return {"error": True, "message": config.MSG_WEATHER_API_KEY_MISSING}

    if await db_utils.check_api_rate_limit(db, 'kma_daily', config.KMA_API_DAILY_CALL_LIMIT, 99999):
        return {"error": True, "message": config.MSG_KMA_API_DAILY_LIMIT_REACHED}

    base_params = {"pageNo": "1", "numOfRows": "1000", "dataType": "JSON"}
    base_params.update(params)
    
    # 서비스 키가 URL 인코딩되는 것을 방지하기 위해 URL에 직접 추가합니다.
    full_url = f"{config.KMA_BASE_URL}/{endpoint}?serviceKey={api_key}"

    session = http.get_tlsv12_session()
    max_retries = max(1, getattr(config, 'KMA_API_MAX_RETRIES', 3))
    retry_delay = max(0, getattr(config, 'KMA_API_RETRY_DELAY_SECONDS', 2))

    try:
        for attempt in range(1, max_retries + 1):
            try:
                # 이제 params에는 serviceKey가 없습니다.
                response = await asyncio.to_thread(session.get, full_url, params=base_params, timeout=15)
                response.raise_for_status()
                try:
                    data = response.json()
                except ValueError as exc:
                    logger.error(f"기상청 API가 JSON을 반환하지 않았습니다: {exc} | 응답: {response.text}")
                    return {"error": True, "message": config.MSG_WEATHER_FETCH_ERROR}

                if data.get('response', {}).get('header', {}).get('resultCode') != '00':
                    error_msg = data.get('response', {}).get('header', {}).get('resultMsg', 'Unknown API Error')
                    logger.error(f"기상청 API 오류: {error_msg}")
                    return {"error": True, "message": error_msg}

                await db_utils.log_api_call(db, 'kma_daily')
                return data.get('response', {}).get('body', {}).get('items')

            except requests.exceptions.Timeout:
                if attempt >= max_retries:
                    logger.error("기상청 API 요청이 재시도 후에도 시간 초과되었습니다.", exc_info=True)
                    return {"error": True, "message": config.MSG_WEATHER_TIMEOUT}

                logger.warning(f"기상청 API 요청이 시간 초과되었습니다. 재시도합니다... (시도 {attempt}/{max_retries})")
                if retry_delay:
                    await asyncio.sleep(retry_delay * attempt)
                continue
            except requests.exceptions.RequestException as e:
                logger.error(f"기상청 API 요청 오류: {e}", exc_info=True)
                return {"error": True, "message": config.MSG_WEATHER_FETCH_ERROR}

    except Exception as e:
        logger.error(f"기상청 API 처리 중 예기치 않은 오류: {e}", exc_info=True)
        return {"error": True, "message": config.MSG_WEATHER_FETCH_ERROR}
    finally:
        session.close()

async def get_current_weather_from_kma(db: aiosqlite.Connection, nx: str, ny: str) -> dict | None:
    """초단기실황(현재 날씨) 정보를 기상청 API로부터 가져옵니다."""
    now = datetime.now(KST)
    base_dt = now if now.minute >= 45 else now - timedelta(hours=1)
    params = {"base_date": base_dt.strftime("%Y%m%d"), "base_time": base_dt.strftime("%H00"), "nx": nx, "ny": ny}
    return await _fetch_kma_api(db, "getUltraSrtNcst", params)

async def get_short_term_forecast_from_kma(db: aiosqlite.Connection, nx: str, ny: str) -> dict | None:
    """
    단기예보(3일치 예보) 정보를 기상청 API로부터 가져옵니다.
    API 데이터는 정해진 시간에 생성되므로, 현재 시간에 맞춰 가장 최신의 데이터를 요청하도록 base_time을 계산합니다.
    """
    now = datetime.now(KST)
    available_hours = [2, 5, 8, 11, 14, 17, 20, 23]
    request_time = now - timedelta(minutes=30) # 30분 전을 기준으로 확실히 생성된 데이터를 요청

    base_date_str = request_time.strftime("%Y%m%d")
    found_hour = next((hour for hour in reversed(available_hours) if request_time.hour >= hour), -1)

    if found_hour == -1: # 오늘자 데이터가 아직 없을 경우 (새벽)
        base_date_str = (request_time - timedelta(days=1)).strftime("%Y%m%d")
        base_time_str = "2300"
    else:
        base_time_str = f"{found_hour:02d}00"

    params = {"base_date": base_date_str, "base_time": base_time_str, "nx": nx, "ny": ny}
    return await _fetch_kma_api(db, "getVilageFcst", params)

def format_current_weather(items: dict | None) -> str:
    """초단기실황 원본 데이터를 사람이 읽기 좋은 문자열로 변환합니다."""
    if not items or not items.get('item'): return config.MSG_WEATHER_FETCH_ERROR
    try:
        values = {item['category']: item['obsrValue'] for item in items['item']}
        temp, reh = values.get('T1H'), values.get('REH')
        pty_code, rn1 = values.get('PTY', '0'), values.get('RN1', '0')
        
        pty_map = {"0": "없음", "1": "비", "2": "비/눈", "3": "눈", "5": "빗방울"}
        pty = pty_map.get(pty_code, "정보 없음")
        rain_info = f" (시간당 {rn1}mm)" if float(rn1) > 0 else ""
        return f"🌡️기온: {temp}°C, 💧습도: {reh}%, ☔강수: {pty}{rain_info}"
    except Exception: return config.MSG_WEATHER_NO_DATA

def format_short_term_forecast(items: dict | None, day_name: str, target_day_offset: int) -> str:
    """단기예보 원본 데이터를 특정 날짜에 대한 요약 문자열로 변환합니다."""
    if not items or not items.get('item'): return f"{day_name} 날씨: {config.MSG_WEATHER_FETCH_ERROR}"
    try:
        target_date = (datetime.now(KST) + timedelta(days=target_day_offset)).strftime("%Y%m%d")
        day_items = [item for item in items['item'] if item.get('fcstDate') == target_date]
        if not day_items: return f"{day_name} 날씨: 예보 데이터 없음"

        min_temp = next((float(i['fcstValue']) for i in day_items if i['category'] == 'TMN'), None)
        max_temp = next((float(i['fcstValue']) for i in day_items if i['category'] == 'TMX'), None)
        noon_sky_item = next((i for i in day_items if i['category'] == 'SKY' and i['fcstTime'] == '1200'), None)
        sky_map = {"1": "맑음☀️", "3": "구름많음☁️", "4": "흐림🌥️"}
        sky = sky_map.get(noon_sky_item['fcstValue']) if noon_sky_item else "정보없음"
        max_pop = max(int(i['fcstValue']) for i in day_items if i['category'] == 'POP')

        temp_range = f"🌡️기온: {min_temp:.1f}°C ~ {max_temp:.1f}°C" if min_temp and max_temp else "기온 정보 없음"
        return f"{day_name} 날씨: {temp_range}, 하늘: {sky}, 강수확률: ~{max_pop}%"
    except Exception: return config.MSG_WEATHER_NO_DATA

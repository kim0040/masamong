# -*- coding: utf-8 -*-
import asyncio
import json
from datetime import datetime, timedelta
import pytz
import requests
import aiosqlite

import config
from logger_config import logger
from . import db as db_utils

KST = pytz.timezone('Asia/Seoul')
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

    if await db_utils.is_api_limit_reached(db, 'kma_daily_calls', config.KMA_API_DAILY_CALL_LIMIT):
        return {"error": "limit_reached", "message": config.MSG_KMA_API_DAILY_LIMIT_REACHED}

    full_url = f"{KMA_API_BASE_URL}/{endpoint}"

    base_params = {
        "authKey": api_key,
        "pageNo": "1",
        "numOfRows": "1000",
        "dataType": "JSON"
    }
    base_params.update(params)

    data = await http_utils.make_async_request(full_url, params=base_params)

    if not data or isinstance(data, dict) and data.get("error"):
        logger.error(f"기상청 API({endpoint}) 호출 실패: {data}")
        return data if data else {"error": "unknown_error", "message": config.MSG_WEATHER_FETCH_ERROR}

    header = data.get('response', {}).get('header', {})
    if header.get('resultCode') != '00':
        error_msg = header.get('resultMsg', 'Unknown API Error')
        logger.error(f"기상청 API가 오류를 반환했습니다: {error_msg} (Code: {header.get('resultCode')})")
        return {"error": "api_error", "message": f"기상청 API 오류: {error_msg}"}

    await db_utils.increment_api_counter(db, 'kma_daily_calls')
    return data

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

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

    # 보안을 위해 API 키는 로그에서 제외
    log_params = base_params.copy()
    log_params["authKey"] = "[REDACTED]"
    logger.info(f"기상청 API 요청: URL='{full_url}', Params='{log_params}'")

    try:
        response = await asyncio.to_thread(requests.get, full_url, params=base_params, timeout=15)
        response.raise_for_status()
        data = response.json()
        logger.debug(f"기상청 API 응답 수신 ({endpoint}): {data}")

        header = data.get('response', {}).get('header', {})
        if header.get('resultCode') != '00':
            error_msg = header.get('resultMsg', 'Unknown API Error')
            logger.error(f"기상청 API가 오류를 반환했습니다: {error_msg}")
            return {"error": "api_error", "message": f"기상청 API 오류: {error_msg}"}

        await db_utils.increment_api_counter(db, 'kma_daily_calls')
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
    """
    단기예보 정보를 새로운 기상청 API로부터 가져옵니다.
    - KMA API는 특정 시간에 데이터를 생성하므로, 요청 시점에 따라 올바른 base_date와 base_time을 계산해야 합니다.
    - 데이터 생성 시간에 약간의 딜레이가 있을 수 있으므로, 30분의 버퍼를 두고 가장 최신이지만 확실히 생성된 데이터를 요청합니다.
    """
    now = datetime.now(KST)
    # API 데이터 생성 기준 시각 (HH00)
    available_hours = [2, 5, 8, 11, 14, 17, 20, 23]

    # 데이터가 확실히 생성되었을 시간을 계산 (현재 시간 - 30분)
    request_time = now - timedelta(minutes=30)

    # 요청 시간에 가장 가까운 과거의 API 데이터 생성 시각을 찾습니다.
    base_date = request_time.strftime("%Y%m%d")

    found_hour = -1
    for hour in reversed(available_hours):
        if request_time.hour >= hour:
            found_hour = hour
            break

    # 만약 오늘자 생성 시각을 찾지 못했다면 (예: 새벽 1시), 어제 마지막 시간을 사용합니다.
    if found_hour == -1:
        yesterday = request_time - timedelta(days=1)
        base_date = yesterday.strftime("%Y%m%d")
        base_time = "2300"
    else:
        base_time = f"{found_hour:02d}00"

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

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
from . import http

KST = pytz.timezone('Asia/Seoul')

def get_kma_api_key():
    """config.py에서 기상청 API 키를 가져옵니다."""
    api_key = config.KMA_API_KEY
    if not api_key or api_key == 'YOUR_KMA_API_KEY':
        logger.warning("기상청 API 키(KMA_API_KEY)가 config.py에 설정되지 않았거나 기본값입니다.")
        return None
    return api_key

async def _fetch_kma_api(db: aiosqlite.Connection, endpoint: str, params: dict) -> dict | None:
    """
    새로운 기상청 API를 호출하고 응답을 파싱하는 통합 함수.
    [수정] 오류 발생 시 None을 반환하여 안정성을 높입니다.
    """
    api_key = get_kma_api_key()
    if not api_key:
        logger.error("기상청 API 키가 없어 날씨를 조회할 수 없습니다.")
        return None

    if await db_utils.is_api_limit_reached(db, 'kma_daily_calls', config.KMA_API_DAILY_CALL_LIMIT):
        logger.warning("기상청 API 일일 호출 한도를 초과했습니다.")
        return None

    full_url = f"{config.KMA_BASE_URL}/{endpoint}"
    base_params = {
        "authKey": api_key,
        "pageNo": "1",
        "numOfRows": "1000",
        "dataType": "JSON"
    }
    base_params.update(params)

    log_params = base_params.copy()
    log_params["authKey"] = "[REDACTED]"
    logger.info(f"기상청 API 요청: URL='{full_url}', Params='{log_params}'")

    try:
        session = http.get_modern_tls_session()
        response = await asyncio.to_thread(session.get, full_url, params=base_params, timeout=15, verify=False)
        response.raise_for_status()
        data = response.json()
        logger.debug(f"기상청 API 응답 수신 ({endpoint}): {data}")

        header = data.get('response', {}).get('header', {})
        if header.get('resultCode') != '00':
            error_msg = header.get('resultMsg', 'Unknown API Error')
            logger.error(f"기상청 API 오류: {error_msg} (Code: {header.get('resultCode')})")
            return None

        await db_utils.increment_api_counter(db, 'kma_daily_calls')
        return data.get('response', {}).get('body', {}).get('items')

    except requests.exceptions.RequestException as e:
        logger.error(f"기상청 API 요청 중 오류: {e}", exc_info=True)
        return None
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        response_text = response.text if 'response' in locals() else "N/A"
        logger.error(f"기상청 API 응답 파싱 중 오류: {e}. 응답: {response_text}", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"기상청 API 처리 중 예기치 않은 오류: {e}", exc_info=True)
        return None

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


def _get_wind_direction_str(vec_value: float) -> str:
    """풍향 각도를 16방위 문자열로 변환합니다."""
    angles = ["북", "북북동", "북동", "동북동", "동", "동남동", "남동", "남남동", "남", "남남서", "남서", "서남서", "서", "서북서", "북서", "북북서"]
    index = round(vec_value / 22.5) % 16
    return angles[index]

def format_current_weather(items: dict | None) -> str:
    """
    JSON으로 파싱된 초단기실황 데이터를 사람이 읽기 좋은 문자열로 포맷팅합니다.
    [수정] None 입력 처리 및 데이터 구조 변경에 따른 로직 수정.
    [Phase 3] 풍속, 풍향 정보 추가.
    """
    if not items:
        return config.MSG_WEATHER_FETCH_ERROR
    try:
        weather_values = {item['category']: item['obsrValue'] for item in items.get('item', [])}

        temp = weather_values.get('T1H', 'N/A')
        reh = weather_values.get('REH', 'N/A')
        wsd = weather_values.get('WSD', 'N/A')
        vec = weather_values.get('VEC', 'N/A')
        pty_code = weather_values.get('PTY', '0')
        rn1 = weather_values.get('RN1', '0')

        if 'N/A' in [temp, reh, wsd, vec]:
            logger.warning(f"초단기실황 데이터 일부 누락: {weather_values}")
            return config.MSG_WEATHER_NO_DATA

        pty_map = {"0": "없음", "1": "비", "2": "비/눈", "3": "눈", "5": "빗방울", "6": "빗방울/눈날림", "7": "눈날림"}
        pty = pty_map.get(pty_code, "정보 없음")
        rain_info = f" (시간당 {rn1}mm)" if float(rn1) > 0 else ""

        wind_dir_str = _get_wind_direction_str(float(vec))
        wind_info = f", 💨바람: {wind_dir_str} {wsd}m/s"

        return f"🌡️기온: {temp}°C, 💧습도: {reh}%, ☔강수: {pty}{rain_info}{wind_info}"
    except (KeyError, TypeError, IndexError, ValueError) as e:
        logger.error(f"초단기실황 포맷팅 중 오류: {items}", exc_info=True)
        return config.MSG_WEATHER_NO_DATA


def format_short_term_forecast(items: dict | None, day_name: str, target_day_offset: int = 0) -> str:
    """
    JSON으로 파싱된 단기예보 데이터를 사람이 읽기 좋은 문자열로 포맷팅합니다.
    [수정] None 입력 처리 및 데이터 구조 변경에 따른 로직 수정.
    """
    if not items:
        return f"{day_name} 날씨: {config.MSG_WEATHER_FETCH_ERROR}"

    try:
        all_items = items.get('item', [])
        if not all_items:
            return config.MSG_WEATHER_NO_DATA

        target_date = (datetime.now(KST) + timedelta(days=target_day_offset)).strftime("%Y%m%d")
        day_items = [item for item in all_items if item.get('fcstDate') == target_date]
        if not day_items:
            return f"{day_name} 날씨: 해당 날짜의 예보 데이터가 없습니다."

        min_temps = [float(item['fcstValue']) for item in day_items if item['category'] == 'TMN']
        max_temps = [float(item['fcstValue']) for item in day_items if item['category'] == 'TMX']
        min_temp = min(min_temps) if min_temps else None
        max_temp = max(max_temps) if max_temps else None

        sky_map = {"1": "맑음☀️", "3": "구름많음☁️", "4": "흐림🌥️"}
        # 정오(1200) 하늘 상태를 우선적으로 찾음
        noon_sky_item = next((item for item in day_items if item['category'] == 'SKY' and item['fcstTime'] == '1200'), None)
        if noon_sky_item:
            noon_sky = sky_map.get(noon_sky_item['fcstValue'], "정보없음")
        else: # 정오 정보가 없으면 가장 이른 시간의 하늘 상태를 사용
            first_sky_item = next((item for item in day_items if item['category'] == 'SKY'), None)
            noon_sky = sky_map.get(first_sky_item['fcstValue'], "정보없음") if first_sky_item else "정보없음"

        pops = [int(item['fcstValue']) for item in day_items if item['category'] == 'POP']
        max_pop = max(pops) if pops else 0

        temp_range_str = f"🌡️기온: {min_temp:.1f}°C ~ {max_temp:.1f}°C" if min_temp is not None and max_temp is not None else "기온 정보 없음"
        weather_desc = f" 하늘: {noon_sky}, 강수확률: ~{max_pop}%"

        return f"{day_name} 날씨: {temp_range_str},{weather_desc}"
    except (KeyError, TypeError, IndexError, StopIteration, ValueError) as e:
        logger.error(f"단기예보 포맷팅 중 오류: {e}", exc_info=True)
        return config.MSG_WEATHER_NO_DATA

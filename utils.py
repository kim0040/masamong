# -*- coding: utf-8 -*-
import os
from datetime import datetime, timedelta
import pytz
import requests
import json
import asyncio
from logger_config import logger
import config

KST = pytz.timezone('Asia/Seoul')

# --- KMA API v2 (단기예보) ---
# 기상청 공공데이터포털의 '단기예보 조회' 서비스 URL 및 정보
KMA_API_BASE_URL = "http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0"
KMA_API_ENDPOINTS = {
    "ultrasrt_ncst": "/getUltraSrtNcst",  # 초단기실황
    "ultrasrt_fcst": "/getUltraSrtFcst",  # 초단기예보
    "vilage_fcst": "/getVilageFcst",      # 단기예보
}

kma_api_call_count = 0
kma_api_last_reset_date_kst = datetime.now(KST).date()
kma_api_call_lock = asyncio.Lock()

def get_kma_api_key():
    api_key = config.KMA_API_KEY
    if not api_key or api_key == 'YOUR_KMA_API_KEY':
        logger.warning("기상청 API 키(KMA_API_KEY)가 config.py에 설정되지 않았거나 기본값입니다.")
        return None
    return api_key

async def _fetch_kma_api(endpoint_key: str, params: dict) -> dict | None:
    """기상청 API를 호출하고 응답을 파싱하는 통합 함수."""
    global kma_api_call_count, kma_api_last_reset_date_kst
    
    api_key = get_kma_api_key()
    if not api_key: return None

    async with kma_api_call_lock:
        now_kst_date = datetime.now(KST).date()
        if now_kst_date > kma_api_last_reset_date_kst:
            logger.info(f"KST 날짜 변경. 기상청 API 일일 호출 횟수 초기화 (이전: {kma_api_call_count}회).")
            kma_api_call_count = 0
            kma_api_last_reset_date_kst = now_kst_date

        if kma_api_call_count >= config.KMA_API_DAILY_CALL_LIMIT:
            logger.warning(f"기상청 API 일일 호출 한도 도달 ({kma_api_call_count}/{config.KMA_API_DAILY_CALL_LIMIT}). API 요청 거부.")
            return {"error": "limit_reached", "message": config.MSG_KMA_API_DAILY_LIMIT_REACHED}

    full_url = KMA_API_BASE_URL + KMA_API_ENDPOINTS[endpoint_key]
    
    # 기본 파라미터 설정
    base_params = {
        "serviceKey": api_key,
        "pageNo": "1",
        "numOfRows": "1000", # 충분한 양을 요청하여 페이징 회피
        "dataType": "JSON"
    }
    base_params.update(params)

    try:
        response = await asyncio.to_thread(requests.get, full_url, params=base_params, timeout=15)
        logger.debug(f"기상청 API 요청: {response.url}")
        logger.debug(f"기상청 API 응답 상태 코드: {response.status_code}")
        
        response.raise_for_status()
        data = response.json()
        logger.debug(f"기상청 API 원본 응답: {str(data)[:500]}")

        # API 자체 에러 확인 (response.body.header.resultCode != "00")
        if data.get('response', {}).get('header', {}).get('resultCode') != '00':
            error_msg = data.get('response', {}).get('header', {}).get('resultMsg', 'Unknown API Error')
            logger.error(f"기상청 API가 오류를 반환했습니다: {error_msg}")
            return None
        
        async with kma_api_call_lock:
            kma_api_call_count += 1
        
        return data

    except requests.exceptions.Timeout:
        logger.error("기상청 API 요청 시간 초과.")
        return None
    except requests.exceptions.HTTPError as e:
        logger.error(f"기상청 API HTTP 오류: {e.response.status_code} for url: {e.response.url}")
        return None
    except json.JSONDecodeError:
        logger.error(f"기상청 API 응답 JSON 파싱 실패. 응답 내용: {response.text}")
        return None
    except Exception as e:
        logger.error(f"기상청 API 처리 중 예기치 않은 오류: {e}", exc_info=True)
        return None

async def get_current_weather_from_kma(nx: str, ny: str) -> dict | None:
    """초단기실황 정보를 기상청 API로부터 가져옵니다. (getUltraSrtNcst)"""
    now = datetime.now(KST)
    base_date = now.strftime("%Y%m%d")

    # 초단기실황 API는 매시 40분에 업데이트되므로, 안정적으로 45분 이전에는 이전 시간을 사용
    base_time_moment = now
    if now.minute < 45:
        base_time_moment = now - timedelta(hours=1)
    base_time = base_time_moment.strftime("%H00")

    params = {"base_date": base_date, "base_time": base_time, "nx": nx, "ny": ny}
    return await _fetch_kma_api("ultrasrt_ncst", params)

async def get_short_term_forecast_from_kma(nx: str, ny: str) -> dict | None:
    """
    가장 최신의 단기예보(VilageFcst) 전문을 통째로 기상청 API로부터 가져옵니다.
    이 함수는 `target_day_offset`을 받지 않습니다. 항상 가장 최신 회차의 예보를 가져옵니다.
    """
    now = datetime.now(KST)
    
    # 단기예보는 하루 8번 (02:10, 05:10, 08:10, 11:10, 14:10, 17:10, 20:10, 23:10) 발표
    available_times = [2, 5, 8, 11, 14, 17, 20, 23]

    # 현재 시간과 비교하여 가장 최근의 발표 시간을 찾음 (API 제공시간은 +10분)
    current_hour_minute = now.hour * 100 + now.minute
    valid_times = [t for t in available_times if t * 100 + 10 <= current_hour_minute]

    base_date = now.date()
    if not valid_times:
        # 오늘자 발표가 아직 없다면 (예: 02:10 이전), 어제 23시 발표자료를 사용
        base_time_hour = 23
        base_date -= timedelta(days=1)
    else:
        base_time_hour = max(valid_times)

    base_date_str = base_date.strftime("%Y%m%d")
    base_time_str = f"{base_time_hour:02d}00"

    params = {"base_date": base_date_str, "base_time": base_time_str, "nx": nx, "ny": ny}
    return await _fetch_kma_api("vilage_fcst", params)


def format_current_weather(weather_data: dict | None) -> str:
    """JSON으로 파싱된 초단기실황 데이터를 사람이 읽기 좋은 문자열로 포맷팅합니다."""
    if not weather_data or weather_data.get("error"):
        return weather_data.get("message", config.MSG_WEATHER_FETCH_ERROR)
    try:
        items = weather_data['response']['body']['items']['item']
        weather_values = {item['category']: item['obsrValue'] for item in items}

        temp = weather_values.get('T1H', 'N/A')
        reh = weather_values.get('REH', 'N/A')
        rn1 = weather_values.get('RN1', '0')
        pty_code = weather_values.get('PTY', '0')

        pty_map = {"0": "강수 없음", "1": "비", "2": "비/눈", "3": "눈", "5": "빗방울", "6": "빗방울/눈날림", "7": "눈날림"}
        pty = pty_map.get(pty_code, "정보 없음")
        
        rain_info = ""
        if pty_code != '0' and rn1 != "강수없음":
             rain_info = f" (시간당 {rn1}mm)"

        return f"🌡️기온: {temp}°C, 💧습도: {reh}%, ☔강수: {pty}{rain_info}"
    except (KeyError, TypeError, IndexError) as e:
        logger.error(f"초단기실황 데이터 포맷팅 실패: {e}\n데이터: {str(weather_data)[:500]}", exc_info=True)
        return config.MSG_WEATHER_NO_DATA


def format_short_term_forecast(forecast_data: dict | None, target_date: datetime.date) -> str:
    """
    JSON으로 파싱된 단기예보 데이터에서 특정 날짜(target_date)의 정보를 추출하여
    사람이 읽기 좋은 문자열로 포맷팅합니다.
    """
    day_name_map = {0: "오늘", 1: "내일", 2: "모레"}
    day_offset = (target_date - datetime.now(KST).date()).days
    day_name = day_name_map.get(day_offset, f"{day_offset}일 후")

    if not forecast_data or forecast_data.get("error"):
        return f"{day_name} 날씨: {forecast_data.get('message', config.MSG_WEATHER_FETCH_ERROR)}"

    try:
        items = forecast_data['response']['body']['items']['item']
        target_date_str = target_date.strftime("%Y%m%d")

        # 해당 날짜의 데이터만 필터링
        date_specific_items = [item for item in items if item.get('fcstDate') == target_date_str]

        if not date_specific_items:
            logger.warning(f"{target_date_str}에 해당하는 예보 데이터가 없습니다. 원본 데이터: {str(forecast_data)[:500]}")
            return f"{day_name}({target_date_str})의 예보 정보가 없습니다."

        # 최저/최고 기온 찾기 (TMN, TMX)
        min_temp = next((item['fcstValue'] for item in date_specific_items if item['category'] == 'TMN'), None)
        max_temp = next((item['fcstValue'] for item in date_specific_items if item['category'] == 'TMX'), None)

        # 오전/오후 하늘 상태 및 강수확률
        sky_map = {"1": "맑음☀️", "3": "구름많음☁️", "4": "흐림🌥️"}
        am_sky_val, pm_sky_val = "1", "1"
        am_pop, pm_pop = 0, 0
        
        hourly_pops = {item['fcstTime']: int(item['fcstValue']) for item in date_specific_items if item['category'] == 'POP'}
        hourly_skies = {item['fcstTime']: item['fcstValue'] for item in date_specific_items if item['category'] == 'SKY'}

        # 오전(06-12시), 오후(13-18시)의 대표 날씨
        am_pops = [v for k, v in hourly_pops.items() if "0600" <= k <= "1200"]
        pm_pops = [v for k, v in hourly_pops.items() if "1300" <= k <= "1800"]
        am_pop = max(am_pops) if am_pops else 0
        pm_pop = max(pm_pops) if pm_pops else 0

        # 대표 하늘상태는 가장 빈번하게 나타난 것으로 결정
        am_skies = [v for k, v in hourly_skies.items() if "0600" <= k <= "1200"]
        pm_skies = [v for k, v in hourly_skies.items() if "1300" <= k <= "1800"]
        if am_skies: am_sky_val = max(set(am_skies), key=am_skies.count)
        if pm_skies: pm_sky_val = max(set(pm_skies), key=pm_skies.count)

        am_sky = sky_map.get(am_sky_val, "정보없음")
        pm_sky = sky_map.get(pm_sky_val, "정보없음")

        # 하루 중 최고 강수확률
        max_pop = max(hourly_pops.values()) if hourly_pops else 0

        # 최종 문자열 조합
        temp_range_str = ""
        if min_temp and max_temp:
            temp_range_str = f" (최저 {min_temp}°C / 최고 {max_temp}°C)"
        elif max_temp:
            temp_range_str = f" (최고 {max_temp}°C)"
        
        weather_desc = f"오전: {am_sky} (강수 {am_pop}%), 오후: {pm_sky} (강수 {pm_pop}%)"

        return f"**{day_name}** 날씨{temp_range_str}\n> {weather_desc}"

    except (KeyError, TypeError, IndexError, StopIteration) as e:
        logger.error(f"단기예보 데이터 포맷팅 실패: {e}\n데이터: {str(forecast_data)[:500]}", exc_info=True)
        return config.MSG_WEATHER_NO_DATA

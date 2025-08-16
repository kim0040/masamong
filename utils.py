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
    """초단기실황 정보를 기상청 API로부터 가져옵니다."""
    now = datetime.now(KST)
    base_date = now.strftime("%Y%m%d")
    # API는 매시 30분에 생성되어 40분부터 제공되므로, 안전하게 이전 시간 것을 조회
    if now.minute < 45:
        now -= timedelta(hours=1)
    base_time = now.strftime("%H00")

    params = {
        "base_date": base_date,
        "base_time": base_time,
        "nx": nx,
        "ny": ny
    }
    return await _fetch_kma_api("ultrasrt_ncst", params)

async def get_short_term_forecast_from_kma(nx: str, ny: str, target_day_offset: int = 0) -> dict | None:
    """단기예보 정보를 기상청 API로부터 가져옵니다."""
    now = datetime.now(KST)
    target_date = now.date() + timedelta(days=target_day_offset)
    
    # 단기예보는 하루 8번 (02, 05, 08, 11, 14, 17, 20, 23시) 발표
    # 현재 시간 기준으로 가장 가까운 과거 발표 시간을 찾아야 함
    if target_day_offset == 0: # 오늘 예보
        available_times = [2, 5, 8, 11, 14, 17, 20, 23]
        base_time_hour = 23 # 기본값
        # 현재 시간보다 작은 발표 시간 중 가장 큰 값
        valid_times = [t for t in available_times if t * 100 + 10 <= now.hour * 100 + now.minute]
        if valid_times:
            base_time_hour = max(valid_times)
        
        base_date = now.date()
        # 만약 새벽 2시 10분 이전이면, 전날 23시 발표자료를 봐야 함
        if not valid_times:
            base_date -= timedelta(days=1)
        
        base_date_str = base_date.strftime("%Y%m%d")
        base_time_str = f"{base_time_hour:02d}00"
    else: # 내일, 모레 예보는 보통 가장 최신 자료를 보면 됨 (05시 발표 자료 추천)
        base_date_str = (now - timedelta(days=1)).strftime("%Y%m%d") if now.hour < 5 else now.strftime("%Y%m%d")
        base_time_str = "0500"

    params = {
        "base_date": base_date_str,
        "base_time": base_time_str,
        "nx": nx,
        "ny": ny
    }
    return await _fetch_kma_api("vilage_fcst", params)


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
        return config.MSG_WEATHER_NO_DATA


def format_short_term_forecast(forecast_data: dict | None, day_name: str) -> str:
    """JSON으로 파싱된 단기예보 데이터를 사람이 읽기 좋은 문자열로 포맷팅합니다."""
    if not forecast_data or forecast_data.get("error"):
        return f"{day_name} 날씨: {forecast_data.get('message', config.MSG_WEATHER_FETCH_ERROR)}"
    try:
        items = forecast_data['response']['body']['items']['item']
        
        # 최저/최고 기온 찾기
        min_temp = next((item['fcstValue'] for item in items if item['category'] == 'TMN'), None)
        max_temp = next((item['fcstValue'] for item in items if item['category'] == 'TMX'), None)

        # 특정 시간대(예: 정오)의 하늘 상태와 강수확률 찾기
        sky_map = {"1": "맑음☀️", "3": "구름많음☁️", "4": "흐림🌥️"}
        
        noon_sky_val = next((item['fcstValue'] for item in items if item['category'] == 'SKY' and item['fcstTime'] == '1200'), "1")
        noon_sky = sky_map.get(noon_sky_val, "정보없음")

        # 하루 중 최대 강수확률
        pops = [int(item['fcstValue']) for item in items if item['category'] == 'POP']
        max_pop = max(pops) if pops else 0

        temp_range_str = ""
        if min_temp and max_temp:
            temp_range_str = f"(최저 {min_temp}°C / 최고 {max_temp}°C)"
        
        weather_desc = f"하늘: 대체로 {noon_sky}, 최고 강수확률: {max_pop}%"
            
        return f"{day_name} 날씨 {temp_range_str}:\n{weather_desc}".strip()
    except (KeyError, TypeError, IndexError, StopIteration):
        return config.MSG_WEATHER_NO_DATA

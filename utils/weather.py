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

async def _fetch_kma_api(db: aiosqlite.Connection, endpoint: str, params: dict, api_type: str = 'forecast') -> dict | str | None:
    """
    기상청 API 엔드포인트를 호출하는 중앙 래퍼 함수입니다.
    api_type에 따라 다른 API 엔드포인트를 사용합니다.
    - 'forecast': 동네예보 (JSON 응답)
    - 'alert': 기상특보 (텍스트 응답)
    """
    api_key = get_kma_api_key()
    if not api_key: return {"error": True, "message": config.MSG_WEATHER_API_KEY_MISSING}

    if await db_utils.check_api_rate_limit(db, 'kma_daily', config.KMA_API_DAILY_CALL_LIMIT, 99999):
        return {"error": True, "message": config.MSG_KMA_API_DAILY_LIMIT_REACHED}

    base_params = {}
    base_url = ""

    if api_type == 'forecast':
        base_url = "https://apihub.kma.go.kr/api/typ02/openApi/VilageFcstInfoService_2.0"
        base_params.update({"pageNo": "1", "numOfRows": "1000", "dataType": "JSON"})
    elif api_type == 'alert':
        base_url = "https://apihub.kma.go.kr/api/typ01/url"
        base_params.update({"disp": "1"})
    else:
        raise ValueError(f"Invalid api_type: {api_type}")

    base_params['authKey'] = api_key
    base_params.update(params)
    full_url = f"{base_url}/{endpoint}"

    session = http.get_insecure_session()
    max_retries = max(1, getattr(config, 'KMA_API_MAX_RETRIES', 3))
    retry_delay = max(0, getattr(config, 'KMA_API_RETRY_DELAY_SECONDS', 2))

    try:
        for attempt in range(1, max_retries + 1):
            try:
                response = await asyncio.to_thread(session.get, full_url, params=base_params, timeout=15, verify=False)
                response.raise_for_status()
                await db_utils.log_api_call(db, 'kma_daily')

                if api_type == 'alert':
                    return response.text

                # forecast (JSON) 처리
                try:
                    data = response.json()
                except ValueError as exc:
                    logger.error(f"기상청 API가 JSON을 반환하지 않았습니다: {exc} | 응답: {response.text}")
                    return {"error": True, "message": config.MSG_WEATHER_FETCH_ERROR}

                if data.get('response', {}).get('header', {}).get('resultCode') != '00':
                    error_msg = data.get('response', {}).get('header', {}).get('resultMsg', 'Unknown API Error')
                    logger.error(f"기상청 API 오류: {error_msg}")
                    return {"error": True, "message": error_msg}

                return data.get('response', {}).get('body', {}).get('items')

            except requests.exceptions.Timeout:
                if attempt >= max_retries:
                    logger.error("기상청 API 요청이 재시도 후에도 시간 초과되었습니다.", exc_info=True)
                    return {"error": True, "message": config.MSG_WEATHER_TIMEOUT}
                logger.warning(f"기상청 API 요청이 시간 초과되었습니다. 재시도합니다... (시도 {attempt}/{max_retries})")
                if retry_delay: await asyncio.sleep(retry_delay * attempt)
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
    return await _fetch_kma_api(db, "getUltraSrtNcst", params, api_type='forecast')

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
    return await _fetch_kma_api(db, "getVilageFcst", params, api_type='forecast')

async def get_weather_alerts_from_kma(db: aiosqlite.Connection) -> str | dict | None:
    """기상특보 정보를 기상청 API허브로부터 가져옵니다."""
    now = datetime.now(KST)
    params = {
        "tmfc1": (now - timedelta(days=1)).strftime("%Y%m%d%H%M"),
        "tmfc2": now.strftime("%Y%m%d%H%M"),
    }
    return await _fetch_kma_api(db, "wrn_met_data.php", params, api_type='alert')

def format_weather_alerts(raw_data: str) -> str | None:
    """기상특보 원본 텍스트 데이터를 사람이 읽기 좋은 문자열로 변환합니다."""
    if not raw_data or raw_data.startswith("#"): # 데이터 없음 또는 헤더만 있음
        return None

    lines = raw_data.strip().split('\r\n')
    alerts = []
    
    # 첫 줄은 헤더이므로 건너뜁니다.
    header_line = lines[0]
    if not header_line.startswith("REG_ID"):
        logger.error(f"Unexpected alert data format: {raw_data}")
        return "기상특보 데이터를 해석하는 데 실패했습니다."

    # 실제 데이터 라인들을 처리합니다.
    for line in lines[1:]:
        if not line.strip() or line.startswith("#"):
            continue
        
        parts = line.split(',')
        if len(parts) < 9:
            continue

        try:
            # content는 disp=1일 때 마지막에 추가되는 것으로 보임
            reg_name, tm_fc_str, wrn, lvl, cmd, content = parts[1], parts[2], parts[5], parts[6], parts[7], parts[8]
            
            tm_fc = datetime.strptime(tm_fc_str, '%Y%m%d%H%M').strftime('%m/%d %H:%M')
            
            alert_map = {'W': '강풍', 'R': '호우', 'C': '한파', 'D': '건조', 'O': '해일', 'V': '풍랑', 'T': '태풍', 'S': '대설', 'Y': '황사', 'H': '폭염', 'F': '안개'}
            level_map = {'1': '주의보', '2': '경보'}
            cmd_map = {'1': '발표', '2': '대치', '3': '해제'}

            alert_type = alert_map.get(wrn, '알 수 없는 특보')
            alert_level = level_map.get(lvl, '')
            command = cmd_map.get(cmd, '')

            alerts.append(f"""📢 **[{reg_name}] {alert_type} {alert_level} {command}** ({tm_fc} 발표)
> {content}""")

        except (ValueError, IndexError) as e:
            logger.error(f"기상특보 파싱 오류: {e} | 라인: {line}")
            continue
            
    return "\n\n".join(alerts) if alerts else None

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

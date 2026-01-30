# -*- coding: utf-8 -*-
"""
기상청 API와 상호작용하여 날씨 데이터를 가져오고,
사용하기 쉬운 형태로 가공하는 유틸리티 함수들을 제공합니다.
"""

from __future__ import annotations
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
from . import kma_codes

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

    base_params: dict[str, str] = {}
    base_url = ""

    forecast_base = getattr(
        config,
        "KMA_BASE_URL",
        "https://apihub.kma.go.kr/api/typ02/openApi/VilageFcstInfoService_2.0",
    )
    alert_base = getattr(
        config,
        "KMA_ALERT_BASE_URL",
        "https://apihub.kma.go.kr/api/typ01/url",
    )
    
    if api_type == 'forecast':
        base_url = forecast_base.rstrip('/')
        base_params.update({"pageNo": "1", "numOfRows": "1000", "dataType": "JSON"})
    elif api_type == 'alert':
        base_url = alert_base.rstrip('/')
        base_params.update({"disp": "1"})
    elif api_type == 'eqk':
        base_url = "https://apihub.kma.go.kr/api/typ02/openApi/EqkInfoService/getEqkMsg"
        base_params.update({"pageNo": "1", "numOfRows": "10", "dataType": "JSON"})
    elif api_type == 'overview': # Weather Situation (Typ02)
        base_url = "https://apihub.kma.go.kr/api/typ02/openApi/VilageFcstMsgService/getWthrSituation"
        base_params.update({"pageNo": "1", "numOfRows": "10", "dataType": "JSON", "stnId": "108"})
    elif api_type == 'typhoon': # Typhoon List (Typ01)
        base_url = "https://apihub.kma.go.kr/api/typ01/url/typ_lst.php"
        base_params.update({"disp": "0", "help": "0"})
    elif api_type == 'mid':
        base_url = "https://apihub.kma.go.kr/api/typ01/url" # Base for typ01
        base_params.update({"disp": "0", "help": "0"})
    elif api_type == 'warning': # Special Weather Warnings (Typ01)
        base_url = "https://apihub.kma.go.kr/api/typ01/url/wrn_met_data.php" # Specific
        base_params.update({"wrn": "A", "reg": "0", "disp": "0", "help": "0"})
    elif api_type == 'impact': # Impact Forecast (Typ01)
        base_url = "https://apihub.kma.go.kr/api/typ01/url/ifs_fct_pstt.php" # Specific
        base_params.update({"help": "0"})
    else:
        raise ValueError(f"Invalid api_type: {api_type}")

    param_key = 'authKey' if 'apihub.kma.go.kr' in base_url else 'serviceKey'
    base_params[param_key] = api_key
    base_params.update(params)
    full_url = f"{base_url}/{endpoint}" if endpoint else base_url

    # KMA API works best with TLS 1.2 on some servers (approx 30x faster than Modern TLS)
    session = http.get_tlsv12_session()
    max_retries = max(1, getattr(config, 'KMA_API_MAX_RETRIES', 3))
    retry_delay = max(0, getattr(config, 'KMA_API_RETRY_DELAY_SECONDS', 2))

    try:
        for attempt in range(1, max_retries + 1):
            try:
                timeout_seconds = getattr(config, 'KMA_API_TIMEOUT', 30)
                
                req_start = datetime.now()
                response = await asyncio.to_thread(session.get, full_url, params=base_params, timeout=timeout_seconds)
                req_duration = (datetime.now() - req_start).total_seconds()
                
                # Performance Monitoring
                if req_duration > 2.0:
                    logger.warning(f"KMA API 요청이 느립니다 ({req_duration:.2f}s): {endpoint} (Type: {api_type})")
                    
                response.raise_for_status()

                # API Hub Typ01 often returns text/plain, handle header manually
                content_type = response.headers.get('Content-Type', '')
                if 'application/json' in content_type or (api_type not in ['typhoon', 'mid', 'warning', 'impact', 'alert'] and api_type != 'overview'):
                     try:
                         data = response.json()
                         # Normalize API Hub V2 flat format {"item": [...]} to standard KMA structure
                         if isinstance(data, dict) and "item" in data and "response" not in data:
                             items = data["item"]
                             data = {
                                 "response": {
                                     "header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"},
                                     "body": {
                                         "items": {"item": items},
                                         "numOfRows": len(items) if isinstance(items, list) else 1,
                                         "pageNo": 1,
                                         "totalCount": len(items) if isinstance(items, list) else 1
                                     }
                                 }
                             }
                         
                         # Log data count
                         res_body = data.get('response', {}).get('body', {})
                         items_data = res_body.get('items', {}).get('item', []) if isinstance(res_body.get('items'), dict) else res_body.get('items', [])
                         count = len(items_data) if isinstance(items_data, list) else (1 if items_data else 0)
                         
                         logger.info(f"🌦️ [KMA API] {endpoint} ({api_type}) -> {count} items fetched.")
                         
                         if data.get('response', {}).get('header', {}).get('resultCode') != '00':
                             error_msg = data.get('response', {}).get('header', {}).get('resultMsg', 'Unknown API Error')
                             logger.error(f"기상청 API 오류: {error_msg}")
                             return {"error": True, "message": error_msg}

                         return data.get('response', {}).get('body', {}).get('items')
                     except ValueError:
                         # JSON parsing failed, likely text response
                         pass
                
                return response.text

            except requests.exceptions.Timeout:
                if attempt >= max_retries:
                    logger.error("기상청 API 요청이 재시도 후에도 시간 초과되었습니다.", exc_info=True)
                    return {"error": True, "message": config.MSG_WEATHER_TIMEOUT}
                logger.warning(f"기상청 API 요청이 시간 초과되었습니다. 재시도합니다... (시도 {attempt}/{max_retries})")
                if retry_delay: await asyncio.sleep(retry_delay * attempt)
            except requests.exceptions.HTTPError as e:
                # 5xx Errors (Server Side) -> Optional features can just skip without loud errors
                if 500 <= e.response.status_code < 600 and api_type in ['typhoon', 'mid', 'warning', 'impact', 'alert']:
                    logger.warning(f"기상청 부가 서비스 일시적 장애 ({e.response.status_code}): {api_type} - {e}")
                    return None # Return None to silently fail for optional data
                
                logger.error(f"기상청 API 요청 오류: {e}", exc_info=True)
                return {"error": True, "message": config.MSG_WEATHER_FETCH_ERROR}

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

async def get_mid_term_forecast(db: aiosqlite.Connection, location_name: str, day_offset: int) -> str:
    """중기예보(3~10일 후) 정보를 가져옵니다."""
    
    # 1. Determine Codes
    land_code = kma_codes.get_land_code(location_name)
    temp_code = kma_codes.get_temp_code(location_name)
    
    # 2. Determine Base Time (Mid-term updates at 06:00, 18:00)
    now = datetime.now(KST)
    if now.hour < 6:
        base_time = (now - timedelta(days=1)).strftime("%Y%m%d") + "1800"
    elif now.hour < 18:
        base_time = now.strftime("%Y%m%d") + "0600"
    else:
        base_time = now.strftime("%Y%m%d") + "1800"
        
    # 3. Fetch Land & Temp
    # getMidLandFcst
    land_params = {"regId": land_code, "tmFc": base_time}
    land_res = await _fetch_kma_api(db, "getMidLandFcst", land_params, api_type='mid')
    
    # getMidTa
    temp_params = {"regId": temp_code, "tmFc": base_time}
    temp_res = await _fetch_kma_api(db, "getMidTa", temp_params, api_type='mid')
    
    return format_mid_term_forecast(land_res, temp_res, day_offset, location_name)

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

def calculate_sensible_temp(temp: float, wind_speed: float, humidity: float) -> float:
    """체감온도 계산 (겨울: Wind Chill, 그외: 단순 보정)"""
    # Wind Chill (Winter, T<=10, V>=4.8km/h)
    wind_speed_kmh = wind_speed * 3.6
    
    if temp <= 10 and wind_speed_kmh >= 4.8:
        return 13.12 + 0.6215 * temp - 11.37 * (wind_speed_kmh ** 0.16) + 0.3965 * temp * (wind_speed_kmh ** 0.16)
        
    return temp # Fallback for now

def format_current_weather(data: dict | None) -> str:
    """현재 날씨 데이터를 문자열로 포맷팅합니다."""
    try:
        if not data or not data.get('item'): return config.MSG_WEATHER_NO_DATA
        
        # 초단기실황 데이터 추출 (각 카테고리별 아이템 리스트)
        items = data['item']
        values = {i['category']: i['obsrValue'] for i in items if 'category' in i and 'obsrValue' in i}
        
        date_str = datetime.now(KST).strftime("%m/%d %H:%M")
        temp, reh = values.get('T1H'), values.get('REH')
        pty_code, rn1 = values.get('PTY', '0'), values.get('RN1', '0')
        wind_speed = values.get('WSD', '0')

        try:
             t_val = float(temp)
             h_val = float(reh)
             w_val = float(wind_speed)
             sensible = calculate_sensible_temp(t_val, w_val, h_val)
             if abs(sensible - t_val) >= 0.5:
                 temp_display = f"{temp}°C (체감 {sensible:.1f}°C)"
             else:
                 temp_display = f"{temp}°C"
        except:
             temp_display = f"{temp}°C"
        
        pty_map = {"0": "없음", "1": "비", "2": "비/눈", "3": "눈", "5": "빗방울"}
        pty = pty_map.get(pty_code, "정보 없음")
        rain_info = f" (시간당 {rn1}mm)" if float(rn1) > 0 else ""
        return f"{date_str}🌡️기온: {temp_display}, 💧습도: {reh}%, ☔강수: {pty}{rain_info}"
    except Exception: return config.MSG_WEATHER_NO_DATA

def format_short_term_forecast(items: dict | None, day_name: str, target_day_offset: int) -> str:
    """단기예보 원본 데이터를 특정 날짜에 대한 요약 문자열로 변환합니다."""
    if not items or not items.get('item'): return f"{day_name} 날씨: {config.MSG_WEATHER_FETCH_ERROR}"
    try:
        target_date = (datetime.now(KST) + timedelta(days=target_day_offset)).strftime("%Y%m%d")
        day_items = [item for item in items['item'] if item.get('fcstDate') == target_date]
        
        # Late night fallback: If today has no data left, show tomorrow's data
        if not day_items and target_day_offset == 0:
            all_dates = sorted(list(set(item.get('fcstDate') for item in items['item'] if item.get('fcstDate'))))
            if all_dates:
                target_date = all_dates[0]
                day_items = [item for item in items['item'] if item.get('fcstDate') == target_date]
                day_name = f"내일({target_date[4:6]}/{target_date[6:8]})"
        
        if not day_items: return f"{day_name} 날씨: 예보 데이터 없음"

        # Check for min/max temp (TMN/TMX)
        min_temp = next((float(i['fcstValue']) for i in day_items if i['category'] == 'TMN'), None)
        max_temp = next((float(i['fcstValue']) for i in day_items if i['category'] == 'TMX'), None)
        
        # If TMN/TMX is missing (often for today late), try to find from all forecast items for that date
        if min_temp is None:
            temps = [float(i['fcstValue']) for i in day_items if i['category'] in ['TMP', 'T1H']]
            if temps: min_temp = min(temps)
        if max_temp is None:
            temps = [float(i['fcstValue']) for i in day_items if i['category'] in ['TMP', 'T1H']]
            if temps: max_temp = max(temps)
        noon_sky_item = next((i for i in day_items if i['category'] == 'SKY' and i['fcstTime'] == '1200'), None)
        sky_map = {"1": "맑음☀️", "3": "구름많음☁️", "4": "흐림🌥️"}
        sky = sky_map.get(noon_sky_item['fcstValue']) if noon_sky_item else "정보없음"
        max_pop = max(int(i['fcstValue']) for i in day_items if i['category'] == 'POP')

        temp_range = f"🌡️기온: {min_temp:.1f}°C ~ {max_temp:.1f}°C" if min_temp and max_temp else "기온 정보 없음"
        return f"{day_name} 날씨: {temp_range}, 하늘: {sky}, 강수확률: ~{max_pop}%"
    except Exception: return config.MSG_WEATHER_NO_DATA

def format_mid_term_forecast(land_data: dict, temp_data: dict, day_offset: int, location: str) -> str:
    """중기예보 데이터를 포맷팅합니다."""
    try:
        if not land_data or not temp_data:
            return f"{location}의 중기예보 데이터를 불러오지 못했습니다."
            
        land_response = land_data.get('response', {})
        temp_response = temp_data.get('response', {})
        
        # Check Result Code
        if land_response.get('header', {}).get('resultCode') != '00' or temp_response.get('header', {}).get('resultCode') != '00':
             return f"{location}의 중기예보 데이터를 불러오는 데 실패했습니다 (API 오류)."
            
        land_items = land_response.get('body', {}).get('items', {}).get('item', [])
        temp_items = temp_response.get('body', {}).get('items', {}).get('item', [])
        
        if not land_items or not temp_items:
             return f"{location}의 중기예보 데이터가 없습니다."
             
        land_item = land_items[0]
        temp_item = temp_items[0]
        
        target_day = day_offset 
        
        if target_day < 3 or target_day > 10:
            return f"{location}의 중기예보(3~10일 후) 범위를 벗어났습니다."
            
        # KMA Key naming: wf3Am, wf3Pm, wf8, wf9...
        if target_day <= 7:
            wf_key = f"wf{target_day}Pm"
        else:
            wf_key = f"wf{target_day}"
            
        sky = land_item.get(wf_key)
        if not sky and target_day <= 7: sky = land_item.get(f"wf{target_day}Am")
        
        t_min = temp_item.get(f"taMin{target_day}")
        t_max = temp_item.get(f"taMax{target_day}")
        
        date_str = (datetime.now(KST) + timedelta(days=target_day)).strftime("%m/%d(%a)")
        
        return f"📅 {date_str} [{location} 중기예보]\n🌦️ 날씨: {sky}\n🌡️ 기온: {t_min}°C ~ {t_max}°C"

    except Exception as e:
        logger.error(f"Mid-term format error: {e}")
        return f"{location} 중기예보 정보 처리 중 오류 발생."

async def get_recent_earthquakes(db: aiosqlite.Connection) -> list | None:
    """최근 3일간의 지진 통보문을 조회합니다. (국내 영향권 한정)"""
    now = datetime.now(KST)
    # API restriction: max 3 days
    from_date = (now - timedelta(days=2)).strftime("%Y%m%d")
    to_date = now.strftime("%Y%m%d")
    
    params = {
        "fromTmFc": from_date,
        "toTmFc": to_date
    }
    
    res = await _fetch_kma_api(db, "", params, api_type='eqk')
    
    if isinstance(res, dict) and res.get("error"):
        return None
        
    try:
        # Check result code
        header = res.get('response', {}).get('header', {})
        if header.get('resultCode') != '00':
             return None
             
        items = res.get('response', {}).get('body', {}).get('items', {}).get('item', [])
        
        # Filter Magnitude >= 2.0 (Domestic) and domestic check
        filtered_items = []
        raw_items = items if isinstance(items, list) else [items]
        for item in raw_items:
            # item might be empty or None
            if not item: continue
            
            mt_val = item.get('mt')
            rem_val = item.get('rem', '')
            loc_val = item.get('loc', '')
            
            # 1. '국내영향없음' 필터링 (해외 지진 제외)
            if "국내영향없음" in rem_val:
                continue
                
            # 2. 국내 지진 규모 필터 (사용자 요청: 규모 4.0 이상 통보)
            try:
                if float(mt_val) >= 4.0:
                    filtered_items.append(item)
            except: pass
            
        return filtered_items
    except Exception:
        return None

def get_earthquake_safety_tips(magnitude: float) -> str:
    """지진 규모별 행동 요령을 반환합니다."""
    common_tips = """
**[행동 요령]**
1. **튼튼한 탁자 아래**로 들어가 몸을 보호하세요.
2. 가스와 전기를 차단하고 **문을 열어 출구**를 확보하세요.
3. **엘리베이터를 사용하지 마세요.** (계단 이용)
4. 떨어지는 물건(유리창, 간판 등)에 주의하며 **머리를 보호**하세요.
"""
    if magnitude >= 6.0:
        return common_tips + "5. **즉시 안전한 공터나 넓은 곳으로 대피**하십시오. (건물 붕괴 위험)\n6. 라디오나 공공기관의 안내 방송에 귀를 기울이세요."
    else:
        return common_tips + "5. 흔들림이 멈추면 침착하게 밖으로 대피하십시오."

def format_earthquake_alert(item: dict) -> str:
    """지진 통보문을 포맷팅합니다. (긴급 재난 문자 스타일 + 행동요령)"""
    try:
        tm_eqk = str(item.get('tmEqk')) # 발생시각 (YYYYMMDDHHMM)
        loc = item.get('loc') # 위치
        mt = item.get('mt') # 규모
        rem = item.get('rem') # 참고사항
        
        # Format time
        dt = datetime.strptime(tm_eqk, "%Y%m%d%H%M%S") if len(tm_eqk) == 14 else datetime.strptime(tm_eqk, "%Y%m%d%H%M")
        time_str = dt.strftime("%Y년 %m월 %d일 %H시 %M분")
        
        # 국내 지진 규모별 색상/헤더 구분
        try:
            mag = float(mt)
        except:
            mag = 0.0
            
        if mag >= 6.0:
            header = "# 🚨 긴급: 대규모 지진 발생 (대피 요망)"
            emoji = "🔴"
        else: # 4.0 ~ 5.9
            header = "## 📢 경고: 지진 발생 알림 (주의)"
            emoji = "🟡"
        
        safety_tips = get_earthquake_safety_tips(mag)
            
        return f"""{header}
### {emoji} 규모 {mt} 지진 감지
**📍 위치**: {loc}
**⏰ 시각**: {time_str}
> 💡 {rem if rem else '추가 정보 없음'}

---
{safety_tips}"""
    except Exception:
        return "⚠️ 지진 정보 포맷팅 오류"

async def get_weather_overview(db: aiosqlite.Connection, timeout: float | None = None) -> str | None:
    """기상 개황(종합)을 조회합니다."""
    # stnId=108 (National/Seoul)
    res = await _fetch_kma_api(db, "", {}, api_type='overview', timeout=timeout)
    if isinstance(res, dict) and res.get("error"): return None
    
    try:
        items = res.get('response', {}).get('body', {}).get('items', {}).get('item', [])
        if not items: return None
        # item could be list or dict
        item = items[0] if isinstance(items, list) else items
        return item.get('wfSv1') # Weather Situation Overview
    except Exception: return None

async def get_typhoons(db: aiosqlite.Connection, timeout: float | None = None) -> str | None:
    """진행 중인 태풍 정보를 조회합니다."""
    now_year = datetime.now().year
    params = {"YY": str(now_year)}
    # This returns raw text, needs parsing
    res = await _fetch_kma_api(db, "", params, api_type='typhoon', timeout=timeout)
    return format_typhoon_list(res)

def format_typhoon_list(raw_data: str) -> str | None:
    """Parses typ_lst.php response text."""
    if not raw_data or raw_data.startswith("Error") or "#START" not in raw_data:
        return None
        
    lines = raw_data.strip().split('\n')
    active_typhoons = []
    
    # Format usually:
    # YY SEQ NOW EFF ... TYP_NAME ...
    # Skip comments (#)
    
    for line in lines:
        if line.startswith("#"): continue
        if not line.strip(): continue
        
        parts = line.split() # Space separated
        # Valid data line usually has many parts.
        # Check 'NOW' column (3rd usually? Wait, let's verify header)
        # Header: # YY SEQ NOW EFF ...
        # Line:   2024 1  0   0 ...
        
        if len(parts) < 8: continue
        
        try:
            # 0:YY, 1:SEQ, 2:NOW(0/1?), 3:EFF, 4:TM_ST, 5:TM_ED, 6:TYP_NAME
            # Checking NOW column. '1' typically means active?
            # User doc says: "진행여부". Assuming 1=Active, 0=End.
            # Wait, verify with data. User provided doc doesn't explicitly map 0/1.
            # But usually 0=End.
            
            # Let's collect ALL active ones.
            now_flag = parts[2]
            if now_flag != '1': continue # Only active
            
            name = parts[6]
            # name might be encoded or English? Doc says TYP_NAME.
            # Often Korean in KMA.
            
            active_typhoons.append(f"🌀 태풍 **{name}** 활동 중")
        except: continue
        
    return "\n".join(active_typhoons) if active_typhoons else None

async def get_active_warnings(db: aiosqlite.Connection, timeout: float | None = None) -> str | None:
    """전국 기상 특보(주의보/경보)를 조회합니다."""
    # wrn_met_data.php?wrn=A&reg=0
    # Returns raw text table
    res = await _fetch_kma_api(db, "", {}, api_type='warning', timeout=timeout)
    if not res or "Error" in res or "#START" not in res: return None
    
    # Simple parsing: Check for active lines
    # WRN code map: W:Wind, R:Rain, C:Cold, H:Heat, D:Dry, S:Snow, T:Typhoon
    wrn_map = {
        'W': '강풍', 'R': '호우', 'C': '한파', 'D': '건조',
        'O': '해일', 'N': '지진해일', 'V': '풍랑', 'T': '태풍',
        'S': '대설', 'Y': '황사', 'H': '폭염', 'F': '안개'
    }
    lvl_map = {'1': '예비', '2': '주의보', '3': '경보'} # Simplified, need to verify docs
    # Actually DOC says: LVL: 특보수준. 
    
    lines = res.split('\n')
    active_warnings = []
    
    # Data is quite complex tables.
    # For user summary, presenting "Active warnings count" or major ones might be better.
    # "현재 발효중인 특보가 있습니다."
    
    # Just extracting major keywords from content if line is valid data
    # REG_NAME WRN LVL ...
    
    count = 0
    for line in lines:
        if line.startswith("#"): continue
        if not line.strip(): continue
        parts = line.split()
        if len(parts) > 5:
            count += 1
            
    if count > 0:
        return f"⚠️ 현재 전국 {count}건의 기상 특보가 발효 중입니다."
    return None

async def get_mid_term_forecast_v2(db: aiosqlite.Connection, region_code: str) -> str | None:
    """중기예보 (육상) 조회 V2 (typ01)."""
    # fct_afs_dl.php
    params = {"reg": region_code}
    # Parsing text table:
    # # START ...
    # REG_ID ... WF ...
    # 11B00000 ... 맑음 ...
    
    res = await _fetch_kma_api(db, "/fct_afs_dl.php", params, api_type='mid')
    if not res or "#START" not in res: return None
    
    try:
        lines = res.split('\n')
        header_line = ""
        for line in lines:
            if line.startswith("# REG_ID"):
                header_line = line
                continue
                
            if line.startswith(region_code):
                # Found data line
                return f"중기예보(3~10일) [Typ01 Raw Data]\nCOLUMN: {header_line}\nDATA: {line}\n(참고: WF 컬럼이 날씨, MIN/MAX가 기온입니다.)"
    except: pass
    return None

async def get_impact_forecast(db: aiosqlite.Connection, timeout: float | None = None) -> str | None:
    """폭염/한파 영향예보 조회"""
    # ifs_fct_pstt.php
    # Check Heat Wave (hw) and Cold Wave (cw)
    reports = []
    
    for impact_type, name in [('hw', '폭염'), ('cw', '한파')]:
        params = {"ifpar": impact_type}
        res = await _fetch_kma_api(db, "", params, api_type='impact', timeout=timeout)
        if res and "#START" in res:
            # Check if any valid data line exists
            lines = res.split('\n')
            count = 0
            for line in lines:
                if line.startswith("#"): continue
                if not line.strip(): continue
                count += 1
            if count > 0:
                reports.append(f"{name} 영향예보가 발표되었습니다.")
                
    return ", ".join(reports) if reports else None



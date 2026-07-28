# -*- coding: utf-8 -*-
"""
기상청 API와 상호작용하여 날씨 데이터를 가져오고,
사용하기 쉬운 형태로 가공하는 유틸리티 함수들을 제공합니다.
"""

from __future__ import annotations
import asyncio
import csv
import re
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import pytz
import requests
import aiosqlite

import config
from logger_config import logger
from . import db as db_utils
from . import http
from . import kma_codes

KST = pytz.timezone('Asia/Seoul')
_SENSITIVE_QUERY_KEYS = frozenset({
    "authkey",
    "servicekey",
    "apikey",
    "api_key",
    "key",
    "token",
})
_KMA_HTTP_LOCAL = threading.local()


def _get_kma_http_session() -> requests.Session:
    """실행 스레드별 KMA 세션을 재사용해 TLS 연결 비용을 줄입니다.

    requests.Session은 스레드 안전성을 보장하지 않으므로 전역 단일 객체 대신
    executor 스레드마다 하나씩 둡니다. 연결 풀은 같은 스레드의 다음 호출에서
    재사용되고 프로세스 종료 시 운영체제가 정리합니다.
    """
    session = getattr(_KMA_HTTP_LOCAL, "session", None)
    if session is None:
        session = http.get_tlsv12_session()
        _KMA_HTTP_LOCAL.session = session
    return session


def _mask_sensitive_url(url: str | None) -> str:
    """URL 쿼리 문자열의 민감 키를 마스킹합니다."""
    raw = str(url or "").strip()
    if not raw:
        return ""

    try:
        parsed = urlsplit(raw)
        if not parsed.query:
            return raw

        redacted: list[tuple[str, str]] = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if str(key).lower() in _SENSITIVE_QUERY_KEYS:
                redacted.append((key, "REDACTED"))
            else:
                redacted.append((key, value))

        return urlunsplit((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(redacted, doseq=True),
            parsed.fragment,
        ))
    except Exception:
        return raw


def _masked_request_url(full_url: str, error: Exception) -> str:
    """예외 객체로부터 실제 요청 URL을 추출한 뒤 민감 정보를 마스킹하여 반환한다."""
    request = getattr(error, "request", None)
    request_url = getattr(request, "url", None) or full_url
    return _mask_sensitive_url(request_url)


def _http_status(error: Exception) -> int | None:
    """예외 객체의 HTTP 응답에서 상태 코드를 추출하여 반환한다."""
    response = getattr(error, "response", None)
    return getattr(response, "status_code", None)


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

async def _fetch_kma_api(
    db: aiosqlite.Connection,
    endpoint: str,
    params: dict,
    api_type: str = 'forecast',
    timeout: float | None = None,
) -> dict | str | None:
    """
    기상청 API 엔드포인트를 호출하는 중앙 래퍼 함수입니다.
    api_type에 따라 다른 API 엔드포인트를 사용합니다.
    - 'forecast': 동네예보 (JSON 응답)
    - 'alert': 기상특보 (텍스트 응답)
    """
    api_key = get_kma_api_key()
    if not api_key: return {"error": True, "message": config.MSG_WEATHER_API_KEY_MISSING}

    if await db_utils.check_api_rate_limit(db, 'kma_daily', 99999, config.KMA_API_DAILY_CALL_LIMIT):
        return {"error": True, "message": config.MSG_KMA_API_DAILY_LIMIT_REACHED}
    await db_utils.log_api_call(db, 'kma_daily')

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
        base_url = "https://apihub.kma.go.kr/api/typ02/openApi/MidFcstInfoService"
        base_params.update({"pageNo": "1", "numOfRows": "1000", "dataType": "JSON"})
    elif api_type == 'mid_v2':
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

    # 지진은 다음 30초 tick이 곧 재시도이므로 한 번의 느린 요청 안에서 재시도를
    # 겹치지 않는다. 일반 조회만 짧고 유한한 transient retry를 사용한다.
    max_retries = (
        max(1, int(getattr(config, "KMA_URGENT_API_MAX_RETRIES", 1)))
        if api_type == "eqk"
        else max(1, getattr(config, 'KMA_API_MAX_RETRIES', 3))
    )
    retry_delay = max(0, getattr(config, 'KMA_API_RETRY_DELAY_SECONDS', 2))

    try:
        for attempt in range(1, max_retries + 1):
            try:
                timeout_seconds = float(timeout) if timeout is not None else float(getattr(config, 'KMA_API_TIMEOUT', 30))
                
                req_start = datetime.now()
                response = await asyncio.to_thread(
                    _get_kma_http_session().get,
                    full_url,
                    params=base_params,
                    timeout=timeout_seconds,
                )
                req_duration = (datetime.now() - req_start).total_seconds()
                
                # Performance Monitoring
                if req_duration > 2.0:
                    logger.warning(f"KMA API 요청이 느립니다 ({req_duration:.2f}s): {endpoint} (Type: {api_type})")
                    
                response.raise_for_status()

                # API Hub Typ01 often returns text/plain, handle header manually
                content_type = response.headers.get('Content-Type', '')
                if 'application/json' in content_type or (api_type not in ['typhoon', 'mid', 'mid_v2', 'warning', 'impact', 'alert'] and api_type != 'overview'):
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
                             if error_msg == "NO_DATA":
                                 logger.info(f"기상청 API: {endpoint} ({api_type}) 데이터가 현재 없습니다 (NO_DATA).")
                             else:
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
                status_code = _http_status(e)
                safe_url = _masked_request_url(full_url, e)
                if (
                    status_code in {429, 500, 502, 503, 504}
                    and attempt < max_retries
                ):
                    logger.warning(
                        "기상청 API 일시 오류 재시도: status=%s type=%s "
                        "attempt=%d/%d",
                        status_code,
                        api_type,
                        attempt,
                        max_retries,
                    )
                    if retry_delay:
                        await asyncio.sleep(retry_delay * attempt)
                    continue
                # 5xx Errors (Server Side) -> Optional features can just skip without loud errors
                if (
                    status_code is not None
                    and 500 <= status_code < 600
                    and api_type in ['typhoon', 'mid', 'warning', 'impact', 'alert']
                ):
                    logger.warning(
                        "기상청 부가 서비스 일시적 장애 (status=%s, type=%s): %s",
                        status_code,
                        api_type,
                        safe_url,
                    )
                    return None # Return None to silently fail for optional data
                
                logger.error(
                    "기상청 API 요청 오류(status=%s, type=%s): %s",
                    status_code if status_code is not None else "unknown",
                    api_type,
                    safe_url,
                )
                return {"error": True, "message": config.MSG_WEATHER_FETCH_ERROR}

            except requests.exceptions.RequestException as e:
                if attempt < max_retries:
                    logger.warning(
                        "기상청 API 연결 오류 재시도: type=%s exc=%s "
                        "attempt=%d/%d",
                        api_type,
                        e.__class__.__name__,
                        attempt,
                        max_retries,
                    )
                    if retry_delay:
                        await asyncio.sleep(retry_delay * attempt)
                    continue
                logger.error(
                    "기상청 API 요청 오류(type=%s, exc=%s): %s",
                    api_type,
                    e.__class__.__name__,
                    _masked_request_url(full_url, e),
                )
                return {"error": True, "message": config.MSG_WEATHER_FETCH_ERROR}

    except Exception as e:
        logger.error(f"기상청 API 처리 중 예기치 않은 오류: {e}", exc_info=True)
        return {"error": True, "message": config.MSG_WEATHER_FETCH_ERROR}


# 지진처럼 새 발표를 즉시 봐야 하는 polling 경로를 제외한 날씨 자료 캐시.
# 같은 base time/격자의 동시 요청은 하나의 물리 호출로 합쳐 API와 TLS 비용을 줄인다.
_KMA_RESPONSE_CACHE: dict[str, tuple[float, object]] = {}
_KMA_RESPONSE_CACHE_MAX = 384
_KMA_INFLIGHT: dict[str, asyncio.Task] = {}
_CACHE_MISS = object()


def _response_cache_get(key: str, ttl_seconds: float) -> object:
    """TTL 이내 캐시값을 반환하고 miss는 전용 sentinel로 구분합니다."""
    entry = _KMA_RESPONSE_CACHE.get(key)
    if not entry:
        return _CACHE_MISS
    ts, val = entry
    if (time.monotonic() - ts) > ttl_seconds:
        _KMA_RESPONSE_CACHE.pop(key, None)
        return _CACHE_MISS
    return val


def _response_cache_put(key: str, val: object) -> None:
    """정상 응답만 캐시하고 오류/빈 응답은 다음 요청에서 재시도합니다."""
    if val is None or (isinstance(val, dict) and val.get("error")):
        return
    if isinstance(val, str) and not val.strip():
        return
    if len(_KMA_RESPONSE_CACHE) >= _KMA_RESPONSE_CACHE_MAX:
        oldest = min(
            _KMA_RESPONSE_CACHE,
            key=lambda cache_key: _KMA_RESPONSE_CACHE[cache_key][0],
        )
        _KMA_RESPONSE_CACHE.pop(oldest, None)
    _KMA_RESPONSE_CACHE[key] = (time.monotonic(), val)


async def _fetch_kma_cached(
    db: aiosqlite.Connection,
    endpoint: str,
    params: dict,
    *,
    api_type: str,
    cache_key: str,
    ttl_seconds: float,
    timeout: float | None = None,
) -> dict | str | None:
    """TTL 캐시와 singleflight를 적용한 KMA 조회입니다."""
    cached = _response_cache_get(cache_key, ttl_seconds)
    if cached is not _CACHE_MISS:
        return cached

    current_loop = asyncio.get_running_loop()
    task = _KMA_INFLIGHT.get(cache_key)
    if task is None or task.done() or task.get_loop() is not current_loop:
        task = current_loop.create_task(
            _fetch_kma_api(
                db,
                endpoint,
                params,
                api_type=api_type,
                timeout=timeout,
            )
        )
        _KMA_INFLIGHT[cache_key] = task
    try:
        result = await asyncio.shield(task)
        _response_cache_put(cache_key, result)
        return result
    finally:
        if _KMA_INFLIGHT.get(cache_key) is task and task.done():
            _KMA_INFLIGHT.pop(cache_key, None)


async def get_current_weather_from_kma(db: aiosqlite.Connection, nx: str, ny: str) -> dict | None:
    """초단기실황(현재 날씨) 정보를 기상청 API로부터 가져옵니다."""
    now = datetime.now(KST)
    base_dt = now if now.minute >= 45 else now - timedelta(hours=1)
    params = {
        "base_date": base_dt.strftime("%Y%m%d"),
        "base_time": base_dt.strftime("%H00"),
        "nx": nx,
        "ny": ny,
        "numOfRows": "20",
    }
    cache_key = f"getUltraSrtNcst:{params['base_date']}:{params['base_time']}:{nx}:{ny}"
    return await _fetch_kma_cached(
        db,
        "getUltraSrtNcst",
        params,
        api_type="forecast",
        cache_key=cache_key,
        ttl_seconds=600,
    )


async def get_ultra_short_forecast_from_kma(
    db: aiosqlite.Connection,
    nx: str,
    ny: str,
) -> dict | None:
    """앞으로 약 6시간의 초단기예보를 조회합니다.

    typ02 endpoint는 30분 발표 계약을 사용하므로 45분의 공개 여유를 둡니다.
    강수형태·강수량·확률·낙뢰·바람·습도를 한 응답에서 함께 받습니다.
    """
    now = datetime.now(KST)
    base_dt = now.replace(
        minute=30 if now.minute >= 45 else 0,
        second=0,
        microsecond=0,
    )
    if now.minute < 15:
        base_dt -= timedelta(hours=1)
        base_dt = base_dt.replace(minute=30)
    params = {
        "base_date": base_dt.strftime("%Y%m%d"),
        "base_time": base_dt.strftime("%H%M"),
        "nx": nx,
        "ny": ny,
        "numOfRows": "100",
    }
    cache_key = (
        f"getUltraSrtFcst:{params['base_date']}:{params['base_time']}:{nx}:{ny}"
    )
    return await _fetch_kma_cached(
        db,
        "getUltraSrtFcst",
        params,
        api_type="forecast",
        cache_key=cache_key,
        ttl_seconds=300,
    )

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

    params = {
        "base_date": base_date_str,
        "base_time": base_time_str,
        "nx": nx,
        "ny": ny,
        "numOfRows": "1000",
    }
    cache_key = f"getVilageFcst:{base_date_str}:{base_time_str}:{nx}:{ny}"
    return await _fetch_kma_cached(
        db,
        "getVilageFcst",
        params,
        api_type="forecast",
        cache_key=cache_key,
        ttl_seconds=1800,
    )

async def get_weather_alerts_from_kma(db: aiosqlite.Connection) -> str | dict | None:
    """과거 발표 목록이 아닌 현재 발효 중인 기상특보를 조회합니다."""
    bucket = datetime.now(KST).strftime("%Y%m%d%H%M")[:-1]
    return await _fetch_kma_cached(
        db,
        "wrn_now_data.php",
        {"fe": "e", "tm": "", "disp": "1", "help": "0"},
        api_type="alert",
        cache_key=f"active-warnings:{bucket}",
        ttl_seconds=120,
        timeout=8,
    )

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
        
    # 3. Fetch Land & Temp. 두 응답은 서로 독립적이라 함께 요청한다.
    land_params = {"regId": land_code, "tmFc": base_time}
    temp_params = {"regId": temp_code, "tmFc": base_time}
    land_res, temp_res = await asyncio.gather(
        _fetch_kma_cached(
            db,
            "getMidLandFcst",
            land_params,
            api_type="mid",
            cache_key=f"mid-land:{land_code}:{base_time}",
            ttl_seconds=21600,
        ),
        _fetch_kma_cached(
            db,
            "getMidTa",
            temp_params,
            api_type="mid",
            cache_key=f"mid-temp:{temp_code}:{base_time}",
            ttl_seconds=21600,
        ),
    )
    
    return format_mid_term_forecast(land_res, temp_res, day_offset, location_name)


async def get_mid_term_weekly_forecast(
    db: aiosqlite.Connection,
    location_name: str,
) -> str:
    """중기예보 3~10일을 같은 발표본 캐시에서 한 번에 구성합니다."""
    results = await asyncio.gather(
        *(
            get_mid_term_forecast(db, location_name, day_offset)
            for day_offset in range(3, 11)
        )
    )
    return "\n\n".join(results)

def _parse_active_warning_rows(raw_data: str) -> list[dict[str, str]]:
    """`wrn_now_data.php?disp=1`의 현재 특보 행을 파싱합니다."""
    if not raw_data or "Error" in raw_data:
        return []
    header: list[str] | None = None
    parsed: list[dict[str, str]] = []
    expected = [
        "REG_UP",
        "REG_UP_KO",
        "REG_ID",
        "REG_KO",
        "TM_FC",
        "TM_EF",
        "WRN",
        "LVL",
        "CMD",
    ]
    for raw_line in raw_data.splitlines():
        line = raw_line.strip()
        if not line or line in {"#START7777", "#7777END"}:
            continue
        candidate = line.lstrip("#").strip()
        cells = next(csv.reader([candidate])) if "," in candidate else candidate.split()
        cells = [cell.strip() for cell in cells]
        upper = [
            re.sub(r"-+$", "", cell.upper()).strip()
            for cell in cells
        ]
        if "REG_UP" in upper and "WRN" in upper:
            header = upper
            continue
        if line.startswith("#"):
            continue
        keys = header or expected
        if len(cells) < min(7, len(keys)):
            continue
        row = {
            key: cells[index]
            for index, key in enumerate(keys)
            if index < len(cells)
        }
        if row.get("WRN"):
            parsed.append(row)
    return parsed


def _location_warning_matches(location_name: str, region_name: str) -> bool:
    """사용자 지역명과 특보 구역명을 보수적으로 대조합니다."""
    location = re.sub(r"\s+", "", str(location_name or ""))
    region = re.sub(r"\s+", "", str(region_name or ""))
    if not location or not region:
        return False
    candidates = {location}
    stripped = re.sub(r"(특별자치도|특별자치시|광역시|특별시|도|시|군|구)$", "", location)
    if len(stripped) >= 2:
        candidates.add(stripped)
    return any(len(token) >= 2 and token in region for token in candidates)


def format_weather_alerts(
    raw_data: str,
    location_name: str | None = None,
) -> str | None:
    """현재 발효 특보를 지역 우선의 짧은 Discord 문장으로 변환합니다."""
    rows = _parse_active_warning_rows(raw_data)
    if not rows:
        return None

    alert_map = {
        "W": "강풍",
        "R": "호우",
        "C": "한파",
        "D": "건조",
        "O": "폭풍해일",
        "N": "지진해일",
        "V": "풍랑",
        "T": "태풍",
        "S": "대설",
        "Y": "황사",
        "H": "폭염",
        "F": "안개",
        "K": "열대야",
    }
    level_map = {"1": "예비", "2": "주의보", "3": "경보"}
    local_rows = [
        row
        for row in rows
        if _location_warning_matches(
            location_name or "",
            row.get("REG_KO") or row.get("REG_UP_KO") or "",
        )
    ]
    if not local_rows:
        phenomena = sorted(
            {
                alert_map.get(row.get("WRN", ""), row.get("WRN", "특보"))
                for row in rows
            }
        )
        return (
            f"⚠️ **전국 발효 특보:** {len(rows)}개 구역"
            + (f" ({', '.join(phenomena[:5])})" if phenomena else "")
            + "\n기상청 지역별 최신 특보를 확인하세요."
        )

    alerts: list[str] = []
    for row in local_rows[:6]:
        region = row.get("REG_KO") or row.get("REG_UP_KO") or "해당 지역"
        raw_warning = row.get("WRN", "")
        raw_level = row.get("LVL", "")
        warning = alert_map.get(raw_warning, raw_warning or "기상특보")
        level = level_map.get(raw_level, raw_level)
        effective = row.get("TM_EF", "")
        if len(effective) >= 12 and effective[:12].isdigit():
            effective = datetime.strptime(
                effective[:12],
                "%Y%m%d%H%M",
            ).strftime("%m/%d %H:%M 발효")
        alerts.append(
            f"• **{region}: {warning} {level}**"
            + (f" · {effective}" if effective else "")
        )
    if len(local_rows) > len(alerts):
        alerts.append(f"• 그 외 {len(local_rows) - len(alerts)}개 구역")
    return "🚨 **현재 지역 기상특보**\n" + "\n".join(alerts)

def calculate_sensible_temp(temp: float, wind_speed: float, humidity: float) -> float:
    """체감온도 계산 (겨울: Wind Chill, 그외: 단순 보정)"""
    # Wind Chill (Winter, T<=10, V>=4.8km/h)
    wind_speed_kmh = wind_speed * 3.6
    
    if temp <= 10 and wind_speed_kmh >= 4.8:
        return 13.12 + 0.6215 * temp - 11.37 * (wind_speed_kmh ** 0.16) + 0.3965 * temp * (wind_speed_kmh ** 0.16)
        
    return temp # Fallback for now


def _safe_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _wind_direction_name(value) -> str | None:
    degrees = _safe_float(value)
    if degrees is None:
        return None
    names = (
        "북",
        "북북동",
        "북동",
        "동북동",
        "동",
        "동남동",
        "남동",
        "남남동",
        "남",
        "남남서",
        "남서",
        "서남서",
        "서",
        "서북서",
        "북서",
        "북북서",
    )
    return names[int((degrees % 360) / 22.5 + 0.5) % 16]


def _precipitation_name(code) -> str:
    return {
        "0": "없음",
        "1": "비",
        "2": "비/눈",
        "3": "눈",
        "5": "빗방울",
        "6": "빗방울/눈날림",
        "7": "눈날림",
    }.get(str(code), "정보 없음")


def _amount_upper_bound(value) -> float | None:
    """`1.0mm 미만`, `30.0~50.0mm` 등 KMA 양적 표현의 상한을 구합니다."""
    numbers = re.findall(r"\d+(?:\.\d+)?", str(value or ""))
    return max((float(number) for number in numbers), default=None)

def format_current_weather(data: dict | None) -> str:
    """현재 날씨 데이터를 문자열로 포맷팅합니다."""
    try:
        if not data or not data.get('item'): return config.MSG_WEATHER_NO_DATA
        
        # 초단기실황 데이터 추출 (각 카테고리별 아이템 리스트)
        items = data['item']
        values = {i['category']: i['obsrValue'] for i in items if 'category' in i and 'obsrValue' in i}
        
        first_item = items[0] if items else {}
        base_date = str(first_item.get("baseDate", ""))
        base_time = str(first_item.get("baseTime", ""))
        observed = datetime.now(KST).strftime("%m/%d %H:%M")
        if len(base_date) == 8 and len(base_time) >= 4:
            try:
                observed = datetime.strptime(
                    base_date + base_time[:4],
                    "%Y%m%d%H%M",
                ).strftime("%m/%d %H:%M")
            except ValueError:
                pass
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
        except (TypeError, ValueError):
             temp_display = f"{temp}°C"
        
        pty = _precipitation_name(pty_code)
        rain_amount = _amount_upper_bound(rn1)
        rain_info = (
            f" · 1시간 {rn1}{'' if 'mm' in str(rn1).lower() else ' mm'}"
            if rain_amount and rain_amount > 0
            else ""
        )
        wind_value = _safe_float(wind_speed)
        direction = _wind_direction_name(values.get("VEC"))
        wind_info = (
            f"{direction + '풍 ' if direction else ''}{wind_value:.1f} m/s"
            if wind_value is not None
            else "정보 없음"
        )
        return (
            f"**현재 실황** · {observed} 관측\n"
            f"• **기온:** {temp_display} · **습도:** {reh}%\n"
            f"• **강수:** {pty}{rain_info}\n"
            f"• **바람:** {wind_info}"
        )
    except Exception: return config.MSG_WEATHER_NO_DATA


def format_ultra_short_forecast(data: dict | None) -> str | None:
    """초단기예보에서 향후 6시간의 위험·변화 신호를 요약합니다."""
    if not data or not data.get("item"):
        return None
    hourly: dict[tuple[str, str], dict[str, str]] = {}
    for item in data["item"]:
        key = (str(item.get("fcstDate", "")), str(item.get("fcstTime", "")))
        category = str(item.get("category", ""))
        if not all(key) or not category:
            continue
        hourly.setdefault(key, {})[category] = str(item.get("fcstValue", ""))
    if not hourly:
        return None

    ordered = sorted(hourly.items())[:6]
    wet_hours: list[str] = []
    max_pop = 0
    max_wind = 0.0
    wind_direction = None
    lightning = False
    humidities: list[float] = []
    for (date_value, time_value), values in ordered:
        pop = int(_safe_float(values.get("POP")) or 0)
        max_pop = max(max_pop, pop)
        rain = _amount_upper_bound(values.get("RN1")) or 0.0
        pty = values.get("PTY", "0")
        if pty != "0" or rain > 0 or pop >= 40:
            label = f"{time_value[:2]}시 {_precipitation_name(pty)}"
            if pop:
                label += f" {pop}%"
            if rain:
                amount_text = str(values.get("RN1", "")).strip()
                label += (
                    f" · {amount_text}"
                    f"{'' if 'mm' in amount_text.lower() else ' mm'}"
                )
            wet_hours.append(label)
        wind = _safe_float(values.get("WSD")) or 0.0
        if wind > max_wind:
            max_wind = wind
            wind_direction = _wind_direction_name(values.get("VEC"))
        lightning = lightning or (_safe_float(values.get("LGT")) or 0) > 0
        humidity = _safe_float(values.get("REH"))
        if humidity is not None:
            humidities.append(humidity)

    start = ordered[0][0][1][:2]
    end = ordered[-1][0][1][:2]
    lines = [f"**앞으로 6시간** · {start}시~{end}시"]
    if wet_hours:
        preview = ", ".join(wet_hours[:4])
        if len(wet_hours) > 4:
            preview += f" 외 {len(wet_hours) - 4}개 시간대"
        lines.append(f"• **강수 변화:** {preview}")
    else:
        lines.append(f"• **강수:** 뚜렷한 신호 없음 (최대 확률 {max_pop}%)")
    if max_wind:
        lines.append(
            f"• **최대 풍속:** "
            f"{wind_direction + '풍 ' if wind_direction else ''}{max_wind:.1f} m/s"
        )
    if humidities:
        lines.append(
            f"• **습도:** {min(humidities):.0f}~{max(humidities):.0f}%"
        )
    if lightning:
        lines.append("• **낙뢰 신호:** 있음 · 야외 활동 시 즉시 안전한 실내로 이동")
    return "\n".join(lines)

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
        sky_map = {"1": "맑음☀️", "3": "구름많음☁️", "4": "흐림🌥️"}

        def _nearest_item(category: str, hour: int):
            candidates = [
                item
                for item in day_items
                if item.get("category") == category
                and str(item.get("fcstTime", ""))[:2].isdigit()
            ]
            return min(
                candidates,
                key=lambda item: abs(int(str(item["fcstTime"])[:2]) - hour),
                default=None,
            )

        morning_sky_item = _nearest_item("SKY", 9)
        afternoon_sky_item = _nearest_item("SKY", 15)
        morning_sky = (
            sky_map.get(str(morning_sky_item.get("fcstValue")), "정보없음")
            if morning_sky_item
            else "정보없음"
        )
        afternoon_sky = (
            sky_map.get(str(afternoon_sky_item.get("fcstValue")), "정보없음")
            if afternoon_sky_item
            else "정보없음"
        )
        pop_values = [
            int(i["fcstValue"])
            for i in day_items
            if i.get("category") == "POP" and i.get("fcstValue") is not None
        ]
        max_pop = max(pop_values) if pop_values else 0
        precip_types = sorted(
            {
                _precipitation_name(item.get("fcstValue"))
                for item in day_items
                if item.get("category") == "PTY"
                and str(item.get("fcstValue")) != "0"
            }
        )
        rain_items = [
            str(item.get("fcstValue", ""))
            for item in day_items
            if item.get("category") == "PCP"
        ]
        snow_items = [
            str(item.get("fcstValue", ""))
            for item in day_items
            if item.get("category") == "SNO"
        ]
        rain_max = max(
            (_amount_upper_bound(value) or 0.0 for value in rain_items),
            default=0.0,
        )
        snow_max = max(
            (_amount_upper_bound(value) or 0.0 for value in snow_items),
            default=0.0,
        )
        humidity_values = [
            float(item["fcstValue"])
            for item in day_items
            if item.get("category") == "REH"
            and _safe_float(item.get("fcstValue")) is not None
        ]
        wind_items = [
            item
            for item in day_items
            if item.get("category") == "WSD"
            and _safe_float(item.get("fcstValue")) is not None
        ]
        max_wind_item = max(
            wind_items,
            key=lambda item: float(item["fcstValue"]),
            default=None,
        )
        max_wind = (
            float(max_wind_item["fcstValue"]) if max_wind_item else None
        )
        wind_time = (
            str(max_wind_item.get("fcstTime", ""))[:2]
            if max_wind_item
            else ""
        )
        wave_values = [
            float(item["fcstValue"])
            for item in day_items
            if item.get("category") == "WAV"
            and (_safe_float(item.get("fcstValue")) or 0) > 0
        ]

        temp_range = (
            f"{min_temp:.1f}°C ~ {max_temp:.1f}°C"
            if min_temp is not None and max_temp is not None
            else "기온 정보 없음"
        )
        precipitation = (
            ", ".join(precip_types) if precip_types else "없음"
        )
        amount_parts: list[str] = []
        if rain_max:
            amount_parts.append(f"1시간 강수 최대 약 {rain_max:g} mm")
        if snow_max:
            amount_parts.append(f"1시간 신적설 최대 약 {snow_max:g} cm")
        lines = [
            f"**{day_name} 예보**",
            f"• **기온:** {temp_range}",
            f"• **하늘:** 오전 {morning_sky} · 오후 {afternoon_sky}",
            f"• **강수:** {precipitation} · 강수확률: ~{max_pop}%",
        ]
        if amount_parts:
            lines.append("• **예상량:** " + " · ".join(amount_parts))
        if humidity_values:
            lines.append(
                f"• **습도:** {min(humidity_values):.0f}~"
                f"{max(humidity_values):.0f}%"
            )
        if max_wind is not None:
            lines.append(
                f"• **최대 풍속:** {max_wind:.1f} m/s"
                + (f" ({wind_time}시)" if wind_time else "")
            )
        if wave_values:
            lines.append(f"• **파고:** 최대 {max(wave_values):.1f} m")
        return "\n".join(lines)
    except Exception: return config.MSG_WEATHER_NO_DATA

def format_mid_term_forecast(land_data: dict, temp_data: dict, day_offset: int, location: str) -> str:
    """중기예보 데이터를 포맷팅합니다."""
    try:
        if not land_data or not temp_data:
            return f"{location}의 중기예보 데이터를 불러오지 못했습니다."

        if isinstance(land_data, dict) and land_data.get("error"):
            return f"{location}의 중기예보 데이터를 불러오는 데 실패했습니다 ({land_data.get('message', 'API 오류')})."
        if isinstance(temp_data, dict) and temp_data.get("error"):
            return f"{location}의 중기예보 데이터를 불러오는 데 실패했습니다 ({temp_data.get('message', 'API 오류')})."

        if not isinstance(land_data, dict) or not isinstance(temp_data, dict):
            return f"{location}의 중기예보 데이터를 불러오지 못했습니다."

        # _fetch_kma_api returns body.items directly for JSON endpoints.
        if "response" not in land_data and "item" in land_data:
            land_items = land_data.get("item", [])
            temp_items = temp_data.get("item", [])
        else:
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
        def _get_sky(day: int) -> tuple[str | None, str | None]:
            if day <= 7:
                return (
                    land_item.get(f"wf{day}Am"),
                    land_item.get(f"wf{day}Pm"),
                )
            value = land_item.get(f"wf{day}")
            return value, value

        def _get_temp(day: int):
            return temp_item.get(f"taMin{day}"), temp_item.get(f"taMax{day}")

        sky_am, sky_pm = _get_sky(target_day)
        t_min, t_max = _get_temp(target_day)

        # If target day fields are missing, fallback to the nearest available day.
        if (sky_am is None and sky_pm is None) or t_min is None or t_max is None:
            import re

            land_days = {int(m.group(1)) for k in land_item.keys() for m in [re.match(r"wf(\d+)", k)] if m}
            temp_days = {int(m.group(1)) for k in temp_item.keys() for m in [re.match(r"taMin(\d+)", k)] if m}
            available_days = sorted(land_days & temp_days) or sorted(land_days | temp_days)

            if available_days:
                fallback_day = next((d for d in available_days if d >= target_day), available_days[-1])
                sky_am, sky_pm = _get_sky(fallback_day)
                t_min, t_max = _get_temp(fallback_day)
                target_day = fallback_day

        date_str = (datetime.now(KST) + timedelta(days=target_day)).strftime("%m/%d(%a)")
        rain_am = land_item.get(
            f"rnSt{target_day}Am",
            land_item.get(f"rnSt{target_day}"),
        )
        rain_pm = land_item.get(
            f"rnSt{target_day}Pm",
            land_item.get(f"rnSt{target_day}"),
        )
        min_low = temp_item.get(f"taMin{target_day}Low")
        min_high = temp_item.get(f"taMin{target_day}High")
        max_low = temp_item.get(f"taMax{target_day}Low")
        max_high = temp_item.get(f"taMax{target_day}High")
        uncertainty = ""
        if all(
            value is not None
            for value in (min_low, min_high, max_low, max_high)
        ):
            uncertainty = (
                f"\n• **기온 범위:** 최저 {min_low}~{min_high}°C · "
                f"최고 {max_low}~{max_high}°C"
            )

        return (
            f"**📅 {date_str} {location}**\n"
            f"• **날씨:** 오전 {sky_am or '정보 없음'} · "
            f"오후 {sky_pm or '정보 없음'}\n"
            f"• **기온:** {t_min}°C ~ {t_max}°C\n"
            f"• **강수확률:** 오전 {rain_am if rain_am is not None else '-'}% · "
            f"오후 {rain_pm if rain_pm is not None else '-'}%"
            f"{uncertainty}"
        )

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
        if res is None:
            return []

        items: list[dict] = []
        if isinstance(res, list):
            items = [item for item in res if isinstance(item, dict)]
        elif isinstance(res, dict):
            # _fetch_kma_api(JSON)는 일반적으로 body.items를 반환한다.
            if "item" in res:
                item_value = res.get("item", [])
                if isinstance(item_value, list):
                    items = [item for item in item_value if isinstance(item, dict)]
                elif isinstance(item_value, dict):
                    items = [item_value]
            # 혹시 모를 원본 response 구조 fallback
            elif "response" in res:
                item_value = res.get('response', {}).get('body', {}).get('items', {}).get('item', [])
                if isinstance(item_value, list):
                    items = [item for item in item_value if isinstance(item, dict)]
                elif isinstance(item_value, dict):
                    items = [item_value]
        
        # Filter Magnitude >= 2.0 (Domestic) and domestic check
        filtered_items = []
        for item in items:
            if not item: continue
            
            mt_val = item.get('mt')
            rem_val = item.get('rem', '')
            
            # 1. '국내영향없음' 필터링 (해외 지진 제외)
            if "국내영향없음" in rem_val:
                continue
                
            # 2. 국내 지진 규모 필터 (사용자 요청: 규모 4.0 이상 통보)
            try:
                if float(mt_val) >= 4.0:
                    filtered_items.append(item)
            except (TypeError, ValueError):
                logger.debug(f"지진 규모 파싱 실패: mt={mt_val}")
            
        return filtered_items
    except Exception:
        return None

def get_earthquake_safety_tips(magnitude: float) -> str:
    """공식 국민행동요령을 즉시 읽을 수 있는 길이로 반환합니다."""
    del magnitude  # 행동은 추정 규모와 무관하게 보수적으로 안내한다.
    return """**지금 해야 할 일**
1. 흔들리는 동안에는 튼튼한 탁자 아래에서 몸과 머리를 보호하세요.
2. 흔들림이 멈춘 뒤 전기·가스를 차단하고 문을 열어 출구를 확보하세요.
3. 이동할 때는 엘리베이터를 사용하지 말고 계단을 이용하세요.
4. 밖에서는 머리를 보호하고 건물·유리창에서 떨어져 넓은 곳으로 이동하세요.
5. 여진에 주의하고 기상청·재난문자 등 공공기관의 후속 안내를 따르세요."""


def _clean_earthquake_field(value, *, fallback: str = "", limit: int = 240) -> str:
    """외부 통보문 필드를 단일 행으로 제한해 경보 레이아웃을 보호합니다."""
    normalized = " ".join(str(value or "").split()).strip()
    if not normalized:
        return fallback
    return normalized[:limit]

def format_earthquake_alert(item: dict) -> str:
    """LLM이나 서버 말투를 거치지 않는 고정 지진 알림을 생성합니다."""
    try:
        tm_eqk = _clean_earthquake_field(item.get("tmEqk"))
        dt = (
            datetime.strptime(tm_eqk, "%Y%m%d%H%M%S")
            if len(tm_eqk) == 14
            else datetime.strptime(tm_eqk, "%Y%m%d%H%M")
        )
        time_str = dt.strftime("%Y년 %m월 %d일 %H시 %M분 %S초")
        tm_fc = _clean_earthquake_field(item.get("tmFc"), limit=16)
        published_time = ""
        if len(tm_fc) in {12, 14} and tm_fc.isdigit():
            published_dt = datetime.strptime(
                tm_fc,
                "%Y%m%d%H%M%S" if len(tm_fc) == 14 else "%Y%m%d%H%M",
            )
            published_time = published_dt.strftime(
                "%Y년 %m월 %d일 %H시 %M분"
                + (" %S초" if len(tm_fc) == 14 else "")
            )

        mt = _clean_earthquake_field(item.get("mt"), fallback="확인 중", limit=16)
        try:
            mag = float(mt)
        except (TypeError, ValueError):
            mag = 0.0

        loc = _clean_earthquake_field(
            item.get("loc"),
            fallback="기상청 확인 중",
        )
        depth = _clean_earthquake_field(item.get("dep"), limit=32)
        intensity = _clean_earthquake_field(
            item.get("inT") or item.get("int"),
            limit=80,
        )
        correction = _clean_earthquake_field(item.get("cor"), limit=80)
        remark = _clean_earthquake_field(
            item.get("rem"),
            fallback="추가 발표를 확인하세요.",
        )

        detail_lines = [
            f"• **발생 시각:** {time_str} (한국시간)",
            f"• **발생 위치:** {loc}",
            f"• **규모:** {mt}",
        ]
        if published_time:
            detail_lines.insert(
                1,
                f"• **기상청 발표:** {published_time} (한국시간)",
            )
        if depth:
            detail_lines.append(
                f"• **깊이:** {depth}{'' if 'km' in depth.lower() else ' km'}"
            )
        if intensity:
            detail_lines.append(f"• **계기진도:** {intensity}")
        if correction and correction not in {"-", "없음"}:
            detail_lines.append(f"• **수정사항:** {correction}")
        detail_lines.append(f"• **기상청 참고:** {remark}")

        safety_tips = get_earthquake_safety_tips(mag)
        return (
            "**🚨 지진 발생 알림**\n"
            "**기상청 발표 자료를 자동 감지해 즉시 전송했습니다.**\n\n"
            + "\n".join(detail_lines)
            + "\n\n"
            + safety_tips
            + "\n\n"
            "※ 이후 정정 통보가 있으면 최신 기상청·재난문자 안내가 "
            "우선합니다.\n"
            "출처: https://www.weather.go.kr/w/eqk-vol/recent-eqk.do"
        )
    except (TypeError, ValueError):
        logger.error("지진 통보문 필수 필드 파싱 실패", exc_info=True)
        return (
            "**🚨 지진 발생 알림**\n"
            "기상청 지진 통보를 감지했으나 일부 항목을 해석하지 못했습니다. "
            "기상청 최신 발표와 재난문자를 즉시 확인하세요.\n"
            "https://www.weather.go.kr/w/eqk-vol/recent-eqk.do"
        )

async def get_weather_overview(db: aiosqlite.Connection, timeout: float | None = None) -> str | None:
    """기상 개황(종합)을 조회합니다."""
    # stnId=108 (National/Seoul)
    bucket = datetime.now(KST).strftime("%Y%m%d%H")
    res = await _fetch_kma_cached(
        db,
        "",
        {},
        api_type="overview",
        cache_key=f"weather-overview:108:{bucket}",
        ttl_seconds=900,
        timeout=timeout,
    )
    if isinstance(res, dict) and res.get("error"): return None
    
    try:
        # _fetch_kma_api의 정상 JSON 계약은 body.items이며 보통
        # {"item": [...]} 형태다. 원본 response 구조도 하위 호환으로 수용한다.
        if isinstance(res, list):
            items = res
        elif isinstance(res, dict) and "item" in res:
            items = res.get("item") or []
        elif isinstance(res, dict):
            item_container = (
                res.get("response", {})
                .get("body", {})
                .get("items", {})
            )
            items = (
                item_container.get("item", [])
                if isinstance(item_container, dict)
                else item_container
            )
        else:
            return None

        item = items[0] if isinstance(items, list) and items else items
        if not isinstance(item, dict):
            return None
        parts = []
        if item.get("wfSv1"):
            parts.append(str(item["wfSv1"]).strip())
        if item.get("wn"):
            parts.append(f"특보사항: {str(item['wn']).strip()}")
        if item.get("wr"):
            parts.append(f"예비특보: {str(item['wr']).strip()}")
        return "\n".join(part for part in parts if part) or None
    except (AttributeError, IndexError, TypeError):
        return None

async def get_typhoons(db: aiosqlite.Connection, timeout: float | None = None) -> str | None:
    """진행 중인 태풍 정보를 조회합니다."""
    now_year = datetime.now().year
    params = {"YY": str(now_year)}
    # This returns raw text, needs parsing
    res = await _fetch_kma_cached(
        db,
        "",
        params,
        api_type="typhoon",
        cache_key=f"active-typhoons:{now_year}",
        ttl_seconds=1800,
        timeout=timeout,
    )
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
        except (IndexError, KeyError) as e:
            logger.debug(f"태풍 데이터 파싱 실패: {e}")
            continue
        
    return "\n".join(active_typhoons) if active_typhoons else None

async def get_active_warnings(db: aiosqlite.Connection, timeout: float | None = None) -> str | None:
    """전국 기상 특보(주의보/경보)를 조회합니다."""
    del timeout
    res = await get_weather_alerts_from_kma(db)
    if not isinstance(res, str):
        return None
    rows = _parse_active_warning_rows(res)
    return (
        f"⚠️ 현재 전국 {len(rows)}개 구역에 기상특보가 발효 중입니다."
        if rows
        else None
    )

async def get_mid_term_forecast_v2(db: aiosqlite.Connection, region_code: str) -> str | None:
    """중기예보 (육상) 조회 V2 (typ01)."""
    # fct_afs_dl.php
    params = {"reg": region_code}
    # Parsing text table:
    # # START ...
    # REG_ID ... WF ...
    # 11B00000 ... 맑음 ...
    
    bucket = datetime.now(KST).strftime("%Y%m%d%H")
    res = await _fetch_kma_cached(
        db,
        "fct_afs_dl.php",
        params,
        api_type="mid_v2",
        cache_key=f"mid-v2:{region_code}:{bucket}",
        ttl_seconds=21600,
    )
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
    except Exception as e:
        logger.warning(f"중기예보 V2 파싱 실패: {e}")
        pass
    return None

async def get_impact_forecast(db: aiosqlite.Connection, timeout: float | None = None) -> str | None:
    """폭염/한파 영향예보 조회"""
    # ifs_fct_pstt.php
    # Check Heat Wave (hw) and Cold Wave (cw)
    async def _fetch_impact(impact_type: str, name: str) -> str | None:
        params = {"ifpar": impact_type}
        bucket = datetime.now(KST).strftime("%Y%m%d%H")
        res = await _fetch_kma_cached(
            db,
            "",
            params,
            api_type="impact",
            cache_key=f"impact:{impact_type}:{bucket}",
            ttl_seconds=600,
            timeout=timeout,
        )
        if res and "#START" in res:
            # Check if any valid data line exists
            lines = res.split('\n')
            count = 0
            for line in lines:
                if line.startswith("#"): continue
                if not line.strip(): continue
                count += 1
            if count > 0:
                return f"{name} 영향예보가 발표되었습니다."
        return None

    results = await asyncio.gather(
        _fetch_impact("hw", "폭염"),
        _fetch_impact("cw", "한파"),
    )
    reports = [result for result in results if result]
    return ", ".join(reports) if reports else None

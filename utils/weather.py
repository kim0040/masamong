# -*- coding: utf-8 -*-
"""
기상청 API와 상호작용하여 날씨 데이터를 가져오고,
사용하기 쉬운 형태로 가공하는 유틸리티 함수들을 제공합니다.
"""

from __future__ import annotations
import asyncio
import csv
import difflib
import math
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
_KMA_SUCCESS_LOG_STATE: dict[str, tuple[float, int]] = {}
_KMA_URGENT_SUCCESS_LOG_INTERVAL_SECONDS = 60 * 60


def _should_log_kma_success(
    api_type: str,
    item_count: int,
    *,
    now_monotonic: float | None = None,
) -> bool:
    """1분 긴급 폴링 성공은 변화·첫 실행·시간별 heartbeat만 INFO로 남깁니다."""
    normalized_type = str(api_type or "").strip().lower()
    if normalized_type != "eqk":
        return True
    now_value = (
        time.monotonic()
        if now_monotonic is None
        else float(now_monotonic)
    )
    previous = _KMA_SUCCESS_LOG_STATE.get(normalized_type)
    should_log = (
        previous is None
        or int(previous[1]) != int(item_count)
        or now_value - float(previous[0])
        >= _KMA_URGENT_SUCCESS_LOG_INTERVAL_SECONDS
    )
    if should_log:
        _KMA_SUCCESS_LOG_STATE[normalized_type] = (
            now_value,
            int(item_count),
        )
    return should_log


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
        # 같은 지진군의 기준 사건이 후속 통보 10건에 밀려 사라지면 Discord
        # 원본 메시지 key가 바뀔 수 있으므로 공식 3일 조회 범위를 넉넉히 받는다.
        base_params.update({"pageNo": "1", "numOfRows": "100", "dataType": "JSON"})
    elif api_type == 'overview': # Weather Situation (Typ02)
        base_url = "https://apihub.kma.go.kr/api/typ02/openApi/VilageFcstMsgService/getWthrSituation"
        base_params.update({"pageNo": "1", "numOfRows": "10", "dataType": "JSON", "stnId": "108"})
    elif api_type == 'typhoon': # Typhoon List (Typ01)
        base_url = "https://apihub.kma.go.kr/api/typ01/url/typ_lst.php"
        base_params.update({"disp": "0", "help": "0"})
    elif api_type == 'typhoon_detail': # Current Typhoon Analysis/Forecast (Typ01)
        base_url = "https://apihub.kma.go.kr/api/typ01/url/typ_now.php"
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
                if 'application/json' in content_type or (api_type not in ['typhoon', 'typhoon_detail', 'mid', 'mid_v2', 'warning', 'impact', 'alert'] and api_type != 'overview'):
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
                         
                         success_extra = {
                             "event": "kma_call_completed",
                             "outcome": "succeeded",
                             "tool_name": f"kma_{api_type}",
                             "source_count": count,
                         }
                         if _should_log_kma_success(api_type, count):
                             logger.info(
                                 "기상청 API 조회 완료: type=%s item_count=%d",
                                 api_type,
                                 count,
                                 extra=success_extra,
                             )
                         else:
                             logger.debug(
                                 "기상청 API 조회 완료: type=%s item_count=%d",
                                 api_type,
                                 count,
                                 extra=success_extra,
                             )
                         
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
                    and api_type in ['typhoon', 'typhoon_detail', 'mid', 'warning', 'impact', 'alert']
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


def earthquake_event_datetime(item: dict) -> datetime | None:
    """기상청 지진 발생시각을 KST aware datetime으로 변환합니다."""
    raw = _clean_earthquake_field(item.get("tmEqk"), limit=14)
    if len(raw) not in {12, 14} or not raw.isdigit():
        return None
    try:
        parsed = datetime.strptime(
            raw,
            "%Y%m%d%H%M%S" if len(raw) == 14 else "%Y%m%d%H%M",
        )
    except ValueError:
        return None
    return KST.localize(parsed)


def _earthquake_coordinate(item: dict, key: str) -> float | None:
    value = _safe_float(item.get(key))
    if value is None:
        return None
    if key == "lat" and not -90 <= value <= 90:
        return None
    if key == "lon" and not -180 <= value <= 180:
        return None
    return value


def _earthquake_distance_km(left: dict, right: dict) -> float | None:
    """두 진앙의 위·경도로 haversine 거리를 계산합니다."""
    left_lat = _earthquake_coordinate(left, "lat")
    left_lon = _earthquake_coordinate(left, "lon")
    right_lat = _earthquake_coordinate(right, "lat")
    right_lon = _earthquake_coordinate(right, "lon")
    if None in {left_lat, left_lon, right_lat, right_lon}:
        return None

    lat1, lon1, lat2, lon2 = map(
        math.radians,
        (left_lat, left_lon, right_lat, right_lon),
    )
    delta_lat = lat2 - lat1
    delta_lon = lon2 - lon1
    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(haversine)))


def _earthquake_location_similarity(left: dict, right: dict) -> float:
    """좌표가 빠진 통보문을 위한 보수적 위치명 유사도를 계산합니다."""

    def normalize(value) -> str:
        text = str(value or "").casefold()
        text = re.sub(r"\d+(?:\.\d+)?\s*km.*$", "", text)
        text = re.sub(
            r"(북북동|북동|동북동|동남동|남동|남남동|남남서|남서|서남서|"
            r"서북서|북서|북북서|북쪽|남쪽|동쪽|서쪽|지역|해역)",
            "",
            text,
        )
        return re.sub(r"[^0-9a-z가-힣]", "", text)

    left_name = normalize(left.get("loc"))
    right_name = normalize(right.get("loc"))
    if len(left_name) < 4 or len(right_name) < 4:
        return 0.0
    return difflib.SequenceMatcher(None, left_name, right_name).ratio()


def _earthquake_magnitude(item: dict) -> float:
    return _safe_float(item.get("mt")) or 0.0


def cluster_earthquake_events(
    items: list[dict],
    *,
    sequence_window_hours: float = 72,
    sequence_radius_km: float = 150,
) -> list[list[dict]]:
    """발생시각·진앙 거리로 연속 지진군을 보수적으로 묶습니다.

    이것은 기상청의 공식 여진 판정이 아니라 Discord 표시를 정리하기 위한
    자동 분류다. 좌표가 있으면 거리 기준을 사용하고, 좌표가 빠진 경우에만
    위치명 유사도를 fallback으로 사용한다. 같은 발생시각의 정정 통보는 가장
    최근 발표본 하나로 합친다.
    """
    bounded_hours = max(1.0, min(168.0, float(sequence_window_hours)))
    bounded_radius = max(10.0, min(500.0, float(sequence_radius_km)))

    # 같은 사건의 정정 통보가 여러 건이면 tmFc/tmSeq가 최신인 항목을 남긴다.
    by_occurrence: dict[str, dict] = {}
    for raw_item in items or []:
        if not isinstance(raw_item, dict):
            continue
        occurred_at = earthquake_event_datetime(raw_item)
        if occurred_at is None:
            continue
        occurrence_key = occurred_at.strftime("%Y%m%d%H%M%S")
        previous = by_occurrence.get(occurrence_key)
        candidate_order = (
            str(raw_item.get("tmFc") or ""),
            str(raw_item.get("tmSeq") or raw_item.get("cnt") or ""),
        )
        previous_order = (
            str(previous.get("tmFc") or "") if previous else "",
            str(previous.get("tmSeq") or previous.get("cnt") or "")
            if previous
            else "",
        )
        if previous is None or candidate_order >= previous_order:
            by_occurrence[occurrence_key] = dict(raw_item)

    # by_occurrence에는 위에서 발생시각 파싱에 성공한 항목만 들어온다. 원문
    # 숫자 문자열은 YYYYMMDDHHMMSS로 정규화되어 있어 문자열 정렬도 시간순이다.
    ordered = sorted(
        by_occurrence.values(),
        key=lambda item: str(item.get("tmEqk") or ""),
    )
    clusters: list[list[dict]] = []
    for item in ordered:
        occurred_at = earthquake_event_datetime(item)
        if occurred_at is None:
            continue
        matched_cluster: list[dict] | None = None
        for cluster in reversed(clusters):
            first_at = earthquake_event_datetime(cluster[0])
            if first_at is None:
                continue
            elapsed_hours = (occurred_at - first_at).total_seconds() / 3600
            if elapsed_hours < 0 or elapsed_hours > bounded_hours:
                continue
            reference = max(cluster, key=_earthquake_magnitude)
            distance = _earthquake_distance_km(item, reference)
            same_area = (
                distance <= bounded_radius
                if distance is not None
                else _earthquake_location_similarity(item, reference) >= 0.58
            )
            if same_area:
                matched_cluster = cluster
                break
        if matched_cluster is None:
            clusters.append([item])
        else:
            matched_cluster.append(item)
    return clusters


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


def format_earthquake_incident_alert(
    events: list[dict],
    *,
    max_followups: int = 6,
) -> str:
    """한 지진군을 단일 Discord 메시지의 수정 가능한 현황으로 렌더링합니다."""
    valid_events = [
        dict(item)
        for item in events
        if isinstance(item, dict) and earthquake_event_datetime(item) is not None
    ]
    valid_events.sort(key=lambda item: earthquake_event_datetime(item))
    if not valid_events:
        return (
            "**🚨 지진 발생 알림**\n"
            "기상청 지진 통보를 감지했으나 유효한 발생시각을 확인하지 못했습니다. "
            "기상청 최신 발표와 재난문자를 즉시 확인하세요.\n"
            "https://www.weather.go.kr/w/eqk-vol/recent-eqk.do"
        )
    if len(valid_events) == 1:
        return (
            format_earthquake_alert(valid_events[0])
            + "\n\n후속 지진이 감지되면 새 메시지를 반복 전송하지 않고 "
            "**이 메시지를 수정해 현황을 갱신합니다.**"
        )

    main_event = max(valid_events, key=_earthquake_magnitude)
    main_at = earthquake_event_datetime(main_event)
    latest_at = earthquake_event_datetime(valid_events[-1])
    main_loc = _clean_earthquake_field(
        main_event.get("loc"),
        fallback="기상청 확인 중",
        limit=120,
    )
    main_mag = _clean_earthquake_field(
        main_event.get("mt"),
        fallback="확인 중",
        limit=12,
    )
    main_depth = _clean_earthquake_field(main_event.get("dep"), limit=20)
    main_intensity = _clean_earthquake_field(
        main_event.get("inT") or main_event.get("int"),
        limit=80,
    )
    max_display = max(1, min(10, int(max_followups)))
    followups = [
        item
        for item in reversed(valid_events)
        if item is not main_event
    ][:max_display]

    def followup_line(item: dict) -> str:
        occurred_at = earthquake_event_datetime(item)
        occurred = (
            occurred_at.strftime("%m/%d %H:%M:%S")
            if occurred_at is not None
            else "시각 확인 중"
        )
        magnitude = _clean_earthquake_field(
            item.get("mt"),
            fallback="확인 중",
            limit=12,
        )
        location = _clean_earthquake_field(
            item.get("loc"),
            fallback="위치 확인 중",
            limit=100,
        )
        return f"• `{occurred}` · 규모 **{magnitude}** · {location}"

    def render(displayed: list[dict]) -> str:
        omitted = max(0, len(valid_events) - 1 - len(displayed))
        main_details = [
            f"• **발생 시각:** {main_at.strftime('%Y년 %m월 %d일 %H시 %M분 %S초')} (한국시간)",
            f"• **발생 위치:** {main_loc}",
            f"• **최대 규모:** {main_mag}",
        ]
        if main_depth:
            main_details.append(
                f"• **깊이:** {main_depth}"
                f"{'' if 'km' in main_depth.lower() else ' km'}"
            )
        if main_intensity:
            main_details.append(f"• **계기진도:** {main_intensity}")
        followup_lines = [followup_line(item) for item in displayed]
        if omitted:
            followup_lines.append(f"• 앞선 후속 지진 {omitted}건은 길이 제한으로 생략")
        return (
            "**🚨 지진 연속 발생 현황**\n"
            "**기상청 통보를 자동 감지해 같은 지진군의 단일 메시지를 갱신했습니다.**\n\n"
            f"**기준 지진(현재 지진군 내 최대 규모)**\n"
            + "\n".join(main_details)
            + "\n\n"
            f"**후속 현황** · 총 {len(valid_events)}건 · "
            f"최근 {latest_at.strftime('%m/%d %H:%M:%S')}\n"
            + "\n".join(followup_lines)
            + "\n\n"
            + get_earthquake_safety_tips(_earthquake_magnitude(main_event))
            + "\n\n"
            "※ `후속 지진(여진 가능)` 묶음은 발생시각과 진앙 거리로 자동 분류한 "
            "표시이며 공식 여진 판정이 아닙니다. 기상청 정정 통보와 재난문자가 "
            "우선합니다.\n"
            "출처: https://www.weather.go.kr/w/eqk-vol/recent-eqk.do"
        )

    rendered = render(followups)
    while len(rendered) > 1950 and followups:
        followups.pop()
        rendered = render(followups)
    return rendered[:1950]

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
    """진행 중인 태풍의 공식 목록·분석·예측 정보를 조회합니다.

    목록 조회로 활동 여부를 먼저 확인하고, 활동 태풍이 있을 때만 상세 API를
    한 번 더 호출합니다. 두 응답은 각각 캐시되므로 같은 질문이 반복돼도
    기상청과 TiDB에 불필요한 호출을 만들지 않습니다.
    """
    now_year = datetime.now(KST).year
    params = {"YY": str(now_year)}
    list_result = await _fetch_kma_cached(
        db,
        "",
        params,
        api_type="typhoon",
        cache_key=f"active-typhoons:{now_year}",
        ttl_seconds=1800,
        timeout=timeout,
    )
    active_records = parse_active_typhoon_records(list_result)
    if active_records is None:
        return None
    if not active_records:
        return "현재 기상청 목록에 활동 중인 태풍이 없습니다."

    # typ_now.php는 현재 UTC 시각 이하의 가장 최근 분석을 mode=2로 조회하면
    # 해당 분석과 공식 예측을 함께 반환한다. 15분 버킷으로 같은 요청을 합친다.
    now_utc = datetime.now(pytz.UTC)
    bucket_minute = (now_utc.minute // 15) * 15
    query_time = now_utc.replace(
        minute=bucket_minute,
        second=0,
        microsecond=0,
    ).strftime("%Y%m%d%H%M")
    detail_result = await _fetch_kma_cached(
        db,
        "",
        {"tm": query_time, "mode": "2"},
        api_type="typhoon_detail",
        cache_key=f"active-typhoon-detail:{query_time}",
        ttl_seconds=900,
        timeout=timeout,
    )
    return format_typhoon_list(list_result, detail_result)


def parse_active_typhoon_records(
    raw_data: object,
) -> list[dict[str, str]] | None:
    """typ_lst.php에서 활동 중인 태풍만 구조화합니다.

    ``None``은 API/형식 오류, 빈 목록은 정상 응답이지만 활동 태풍이 없음을
    뜻합니다. 이 둘을 구분해야 조회 실패를 "태풍 없음"으로 오인하지 않습니다.
    """
    if (
        not isinstance(raw_data, str)
        or raw_data.startswith("Error")
        or "#START" not in raw_data
    ):
        return None

    records: list[dict[str, str]] = []
    for line in raw_data.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        # REM에는 공백이 포함되므로 고정된 앞 8개 열까지만 분리한다.
        parts = line.split(maxsplit=8)
        if len(parts) < 8 or parts[2] != "1":
            continue
        if not (
            parts[0].isdigit()
            and parts[1].isdigit()
            and parts[4].isdigit()
        ):
            continue
        records.append(
            {
                "year": parts[0],
                "seq": parts[1],
                "impact": parts[3],
                "started_at": parts[4],
                "name": parts[6],
                "english_name": parts[7],
            }
        )
    return records


def parse_typhoon_detail_rows(raw_data: object) -> list[dict[str, object]]:
    """typ_now.php의 현재 분석/예측 행을 필요한 필드만 구조화합니다."""
    if (
        not isinstance(raw_data, str)
        or raw_data.startswith("Error")
        or "#START" not in raw_data
    ):
        return []

    rows: list[dict[str, object]] = []
    for line in raw_data.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        # 18개 수치/코드 열 뒤 LOC에는 공백이 들어간다.
        parts = line.split(maxsplit=18)
        if len(parts) < 19:
            continue
        try:
            location = re.sub(
                r"\s+(?:[A-Z-]+),-?\d+,\s*$",
                "",
                parts[18],
            ).strip()
            rows.append(
                {
                    "forecast": parts[0] == "1",
                    "year": int(parts[1]),
                    "typhoon": int(parts[2]),
                    "sequence": int(parts[3]),
                    "hours_ahead": int(parts[4]),
                    "analysis_time": parts[5],
                    "forecast_time": parts[6],
                    "latitude": float(parts[7]),
                    "longitude": float(parts[8]),
                    "direction": parts[9],
                    "speed_kmh": int(parts[10]),
                    "pressure_hpa": int(parts[11]),
                    "wind_ms": int(parts[12]),
                    "radius_15ms_km": int(parts[13]),
                    "location": location,
                }
            )
        except (TypeError, ValueError):
            logger.debug("태풍 상세 데이터 행 파싱 실패", exc_info=True)
    return rows


_TYPHOON_IMPACT_LABELS = {
    "1": "한반도 상륙",
    "2": "한반도 직접 영향",
    "3": "한반도 간접 영향",
    "4": "현재 영향 없음",
}
_TYPHOON_DIRECTION_LABELS = {
    "N": "북",
    "NNE": "북북동",
    "NE": "북동",
    "ENE": "동북동",
    "E": "동",
    "ESE": "동남동",
    "SE": "남동",
    "SSE": "남남동",
    "S": "남",
    "SSW": "남남서",
    "SW": "남서",
    "WSW": "서남서",
    "W": "서",
    "WNW": "서북서",
    "NW": "북서",
    "NNW": "북북서",
}


def _format_typhoon_time(value: object) -> str:
    raw = str(value or "")
    try:
        utc_dt = pytz.UTC.localize(datetime.strptime(raw, "%Y%m%d%H%M"))
        return utc_dt.astimezone(KST).strftime("%m/%d %H:%M")
    except ValueError:
        return raw


def format_typhoon_list(
    raw_data: object,
    detail_data: object | None = None,
) -> str | None:
    """기상청 태풍 목록과 최신 분석/예측을 Discord용으로 요약합니다."""
    active_records = parse_active_typhoon_records(raw_data)
    if active_records is None:
        return None
    if not active_records:
        return "현재 기상청 목록에 활동 중인 태풍이 없습니다."

    detail_rows = parse_typhoon_detail_rows(detail_data)
    rendered: list[str] = []
    for record in active_records[:3]:
        typhoon_number = int(record["seq"])
        title = (
            f"제{typhoon_number}호 태풍 "
            f"{record['name']}({record['english_name']})"
        )
        rendered.append(f"**{title}** · 활동 중")

        matching = [
            row
            for row in detail_rows
            if row["year"] == int(record["year"])
            and row["typhoon"] == typhoon_number
        ]
        analyses = [row for row in matching if not row["forecast"]]
        analysis = max(
            analyses,
            key=lambda row: (
                str(row["analysis_time"]),
                int(row["sequence"]),
            ),
            default=None,
        )
        if analysis:
            direction = _TYPHOON_DIRECTION_LABELS.get(
                str(analysis["direction"]),
                str(analysis["direction"]),
            )
            rendered.append(
                f"• **기준:** {_format_typhoon_time(analysis['analysis_time'])} KST"
                f" · {analysis['location']}"
            )
            rendered.append(
                f"• **세력:** 중심기압 {analysis['pressure_hpa']} hPa"
                f" · 최대풍속 {analysis['wind_ms']} m/s"
                f" · {direction}쪽 {analysis['speed_kmh']} km/h"
            )

        impact = _TYPHOON_IMPACT_LABELS.get(
            record["impact"],
            "기상청 영향 분류 확인 필요",
        )
        rendered.append(f"• **한반도 영향:** {impact}")

        forecasts = sorted(
            (
                row
                for row in matching
                if row["forecast"]
                and (
                    not analysis
                    or row["analysis_time"] == analysis["analysis_time"]
                )
            ),
            key=lambda row: int(row["hours_ahead"]),
        )
        forecast = next(
            (
                row
                for row in forecasts
                if int(row["hours_ahead"]) >= 24
            ),
            forecasts[0] if forecasts else None,
        )
        if forecast:
            rendered.append(
                f"• **{forecast['hours_ahead']}시간 전망:** "
                f"{_format_typhoon_time(forecast['forecast_time'])} KST"
                f" · {forecast['location']}"
                f" · 최대풍속 {forecast['wind_ms']} m/s"
            )
        rendered.append("")

    return "\n".join(rendered).strip()

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

# -*- coding: utf-8 -*-
"""ExchangeRate-API(v6) 환율 클라이언트.

무료 플랜 실측(2026-09):
- Standard ``GET /v6/latest/{base}`` 와 Pair 변환이 열려 있음
- 인증은 URL 키 또는 ``Authorization: Bearer``
- 응답 필드는 ``conversion_rates`` (open access의 ``rates``와 다름)
- ``plan_quota`` 1500 / 월, 갱신일은 가입일 기준
- 데이터는 일 1회 갱신. Historical/Enriched는 유료
- ``/quota`` 조회도 사용량에 잡히므로 런타임에서는 호출하지 않음

한도를 지키기 위해 base별 응답을 ``time_next_update``까지 캐시하고,
페어마다 Pair 엔드포인트를 치지 않습니다.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

import config
from logger_config import logger
from utils import http

_KEYED_LATEST_URL = "https://v6.exchangerate-api.com/v6/latest/{base}"
_OPEN_LATEST_URL = "https://open.er-api.com/v6/latest/{base}"
_SOURCE_URL = "https://www.exchangerate-api.com"
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_PLACEHOLDER_KEYS = {
    "",
    "YOUR_EXCHANGE_RATE_API_KEY",
    "your_exchange_rate_api_key_here",
    "replace-with-current-masamo-key",
}


class ExchangeRateApiError(RuntimeError):
    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type


def _kst_now() -> str:
    return datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds")


def _api_key() -> str:
    return str(getattr(config, "EXCHANGE_RATE_API_KEY", "") or "").strip()


def extract_rate_map(payload: dict[str, Any]) -> dict[str, float]:
    """Standard/open 응답에서 통화코드 → 환율 맵만 꺼냅니다."""
    raw = payload.get("conversion_rates")
    if not isinstance(raw, dict):
        raw = payload.get("rates")
    if not isinstance(raw, dict):
        return {}
    rates: dict[str, float] = {}
    for code, value in raw.items():
        if isinstance(value, (int, float)) and value > 0:
            rates[str(code).upper()] = float(value)
    return rates


def _cache_ttl_seconds(payload: dict[str, Any]) -> float:
    next_unix = payload.get("time_next_update_unix")
    now = time.time()
    if isinstance(next_unix, (int, float)) and next_unix > now:
        return min(float(next_unix - now), 24 * 60 * 60)
    return 60 * 60


def _load_rates(base: str) -> dict[str, Any]:
    code = base.upper()
    cached = _CACHE.get(code)
    if cached and cached[0] > time.monotonic():
        return cached[1]

    key = _api_key()
    headers = {"User-Agent": "masamong-fx"}
    if key and key not in _PLACEHOLDER_KEYS:
        url = _KEYED_LATEST_URL.format(base=code)
        headers["Authorization"] = f"Bearer {key}"
    else:
        url = _OPEN_LATEST_URL.format(base=code)

    with http.get_modern_tls_session() as session:
        response = session.get(url, headers=headers, timeout=10)

    try:
        payload = response.json()
    except ValueError as exc:
        raise ExchangeRateApiError("malformed-request", str(exc)) from exc

    if not isinstance(payload, dict):
        raise ExchangeRateApiError("malformed-request", "fx payload was not an object")

    if payload.get("result") != "success" or response.status_code != 200:
        error_type = str(payload.get("error-type") or "provider-error")
        raise ExchangeRateApiError(error_type, error_type)

    rates = extract_rate_map(payload)
    if not rates:
        raise ExchangeRateApiError("malformed-request", "fx rate map was empty")

    normalized = {
        "result": "success",
        "base_code": payload.get("base_code") or code,
        "rates": rates,
        "time_next_update_unix": payload.get("time_next_update_unix"),
    }
    _CACHE[code] = (time.monotonic() + _cache_ttl_seconds(payload), normalized)
    logger.info(
        "FX latest 캐시 저장. base=%s rates=%d keyed=%s",
        code,
        len(rates),
        bool(key and key not in _PLACEHOLDER_KEYS),
    )
    return normalized


async def get_fx_quote(base: str, quote: str) -> dict[str, Any]:
    """1 base = rate quote 형태의 검증 가능한 환율 결과를 반환합니다."""
    base_code = str(base or "").strip().upper()
    quote_code = str(quote or "").strip().upper()
    if len(base_code) != 3 or len(quote_code) != 3:
        return {
            "status": "error",
            "error": "환율 통화 코드를 확인하지 못했어요.",
            "failure_kind": "invalid_symbol",
            "provider_failure": False,
        }
    if base_code == quote_code:
        return {
            "status": "success",
            "symbol": f"{base_code}{quote_code}=X",
            "name": f"{base_code}/{quote_code}",
            "price": 1.0,
            "currency": quote_code,
            "change_percent": None,
            "provider": "exchangerate-api",
            "checked_at_kst": _kst_now(),
            "source_url": _SOURCE_URL,
            "source_urls": [_SOURCE_URL],
            "pair": {"base": base_code, "quote": quote_code, "rate": 1.0},
        }

    try:
        payload = await asyncio.to_thread(_load_rates, base_code)
        rate = (payload.get("rates") or {}).get(quote_code)
        if not isinstance(rate, (int, float)) or rate <= 0:
            return {
                "status": "error",
                "error": f"{base_code}/{quote_code} 환율을 찾지 못했어요.",
                "failure_kind": "invalid_symbol",
                "provider_failure": False,
            }
        inverse = 1.0 / float(rate)
        logger.info("FX 조회 성공: %s/%s", base_code, quote_code)
        return {
            "status": "success",
            "symbol": f"{base_code}{quote_code}=X",
            "name": f"{base_code}/{quote_code}",
            "price": float(rate),
            "currency": quote_code,
            "change_percent": None,
            "provider": "exchangerate-api",
            "checked_at_kst": _kst_now(),
            "source_url": _SOURCE_URL,
            "source_urls": [_SOURCE_URL],
            "pair": {
                "base": base_code,
                "quote": quote_code,
                "rate": float(rate),
                "inverse": inverse,
            },
            "summary": (
                f"1 {base_code} = {float(rate):,.4f} {quote_code}, "
                f"1 {quote_code} = {inverse:,.6f} {base_code}"
            ),
        }
    except ExchangeRateApiError as exc:
        if exc.error_type == "quota-reached":
            logger.warning("ExchangeRate-API 월 한도에 도달했습니다.")
            return {
                "status": "error",
                "error": "이번 달 환율 조회 한도에 도달했어요. 잠시 뒤에 다시 시도해 주세요.",
                "failure_kind": "provider_error",
                "provider_failure": True,
            }
        if exc.error_type in {"unsupported-code", "malformed-request"}:
            return {
                "status": "error",
                "error": f"{base_code}/{quote_code} 환율을 찾지 못했어요.",
                "failure_kind": "invalid_symbol",
                "provider_failure": False,
            }
        logger.warning("FX API 오류: %s", exc.error_type)
        return {
            "status": "error",
            "error": "환율 서버가 요청을 거절했어요.",
            "failure_kind": "provider_error",
            "provider_failure": True,
        }
    except requests.HTTPError as exc:
        logger.warning("FX HTTP 오류: %s", exc)
        return {
            "status": "error",
            "error": "환율 서버가 요청을 거절했어요.",
            "failure_kind": "provider_error",
            "provider_failure": True,
        }
    except Exception:
        logger.error("FX 조회 실패", exc_info=True)
        return {
            "status": "error",
            "error": "환율을 가져오는 쪽에서 문제가 생겼어요.",
            "failure_kind": "provider_error",
            "provider_failure": True,
        }

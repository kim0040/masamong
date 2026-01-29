# -*- coding: utf-8 -*-
"""한국수출입은행 환율(Open API)을 통해 환율 정보를 조회하는 헬퍼."""


from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable

import aiohttp

import config
from logger_config import logger
from utils.data_formatters import FinancialDataFormatter

_API_DATA_CODE = "AP01"
_REQ_TIMEOUT = 10


def _candidate_dates(days: int = 5) -> Iterable[str]:
    """최근 며칠간의 날짜(YYYYMMDD)를 생성합니다."""
    today = datetime.now()
    for offset in range(days):
        yield (today - timedelta(days=offset)).strftime("%Y%m%d")


async def _fetch_exchange_rates_for_date(session: aiohttp.ClientSession, date_str: str) -> list[Dict[str, Any]] | None:
    """지정한 날짜의 환율 데이터를 호출합니다."""
    base_url = getattr(config, "EXIM_BASE_URL", None) or "https://www.koreaexim.go.kr/site/program/financial/exchangeJSON"
    params = {
        "authkey": getattr(config, "EXIM_API_KEY_KR", None),
        "data": _API_DATA_CODE,
        "searchdate": date_str,
    }

    try:
        async with session.get(base_url, params=params, timeout=_REQ_TIMEOUT) as resp:
            if resp.status != 200:
                logger.warning("환율 API 호출 실패(HTTP %s): %s", resp.status, await resp.text())
                return None

            try:
                payload = await resp.json(content_type=None)
            except Exception as exc:  # pragma: no cover - JSON 파싱 오류 대비
                logger.error("환율 응답 JSON 파싱 실패: %s", exc, exc_info=True)
                return None

            if isinstance(payload, dict) and payload.get("result") != 1:
                logger.warning("환율 API가 오류를 반환했습니다: %s", payload)
                return None

            if isinstance(payload, list):
                return payload

            logger.warning("환율 API 응답 형식을 인식할 수 없습니다: %s", payload)
            return None
    except asyncio.TimeoutError:
        logger.warning("환율 API 호출이 시간 초과되었습니다 (%s).", date_str)
    except aiohttp.ClientError as exc:
        logger.error("환율 API 호출 중 네트워크 오류: %s", exc, exc_info=True)
    return None


async def _fetch_latest_exchange_rates() -> list[Dict[str, Any]] | None:
    """최근 날짜부터 순차적으로 환율 데이터를 조회합니다."""
    api_key = getattr(config, "EXIM_API_KEY_KR", None)
    if not api_key or api_key in {"", "YOUR_EXIM_API_KEY_KR"}:
        logger.error("EXIM_API_KEY_KR가 설정되지 않아 환율 데이터를 조회할 수 없습니다.")
        return None

    async with aiohttp.ClientSession() as session:
        for date_str in _candidate_dates():
            records = await _fetch_exchange_rates_for_date(session, date_str)
            if records:
                logger.info("환율 데이터 조회 성공 (%s)", date_str)
                return records
    return None


async def get_krw_exchange_rate(currency_code: str = "USD") -> str:
    """요청한 통화의 원화 환율 정보를 포맷팅하여 반환합니다."""
    currency_code = currency_code.upper()
    records = await _fetch_latest_exchange_rates()
    if not records:
        return "환율 정보를 가져오지 못했습니다. EXIM API 키와 네트워크 상태를 확인해주세요."

    match = next((item for item in records if item.get("cur_unit") == currency_code), None)
    if not match:
        return f"'{currency_code}' 통화에 대한 환율 정보를 찾을 수 없습니다."

    return FinancialDataFormatter.format_exchange_rate(match)


async def get_raw_exchange_rate(currency_code: str = "USD") -> float | None:
    """매매기준율을 float 값으로 반환합니다."""
    records = await _fetch_latest_exchange_rates()
    if not records:
        return None

    target = next((item for item in records if item.get("cur_unit") == currency_code.upper()), None)
    if not target:
        return None

    try:
        return float(str(target.get("deal_bas_r", "0")).replace(",", ""))
    except (TypeError, ValueError):  # pragma: no cover - 값 파싱 실패 대비
        logger.error("환율 값 파싱 실패: %s", target)
        return None

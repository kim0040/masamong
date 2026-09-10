# -*- coding: utf-8 -*-
"""미국·글로벌 지수 스냅샷 전용 Yahoo 클라이언트.

종목 시세는 Finnhub(`finnhub.py`)가 기본이고, 이 모듈은 지수 배치 조회만
담당합니다. Finnhub 무료 플랜은 주요 지수를 막으므로 Yahoo를 유지합니다.
국장(^KS11/^KQ11)은 조회하지 않습니다.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import yfinance as yf

from logger_config import logger
from utils.finance_query import kr_market_unsupported_result

_MARKET_FETCH_TIMEOUT_SEC = 20
_US_INDEXES = (
    ("^DJI", "다우존스"),
    ("^GSPC", "S&P 500"),
    ("^IXIC", "나스닥 종합"),
)


async def get_market_snapshot(region: str = "global") -> dict[str, Any]:
    """주요 시장 지수의 최신 가용 일봉을 한 번의 배치 요청으로 조회합니다."""
    normalized_region = str(region or "global").strip().lower()
    if normalized_region == "kr":
        return kr_market_unsupported_result()
    if normalized_region not in {"us", "global"}:
        normalized_region = "global"

    selected = _US_INDEXES
    tickers = [symbol for symbol, _name in selected]

    def _fetch() -> dict[str, Any]:
        frame = yf.download(
            tickers,
            period="5d",
            interval="1d",
            auto_adjust=False,
            progress=False,
            threads=False,
            timeout=10,
        )
        if frame is None or frame.empty:
            return {"error": "주요 시장 지수 데이터가 비어 있습니다."}

        close_data = frame.get("Close")
        if close_data is None:
            return {"error": "주요 시장 종가 열을 확인하지 못했습니다."}

        indices: list[dict[str, Any]] = []
        for symbol, display_name in selected:
            try:
                series = close_data[symbol].dropna()
            except (KeyError, TypeError, AttributeError):
                continue
            if series.empty:
                continue

            latest_value = float(series.iloc[-1])
            previous_value = (
                float(series.iloc[-2])
                if len(series.index) >= 2
                else None
            )
            change = (
                latest_value - previous_value
                if previous_value not in (None, 0)
                else None
            )
            change_percent = (
                (change / previous_value) * 100
                if change is not None and previous_value
                else None
            )
            latest_index = series.index[-1]
            try:
                market_date = latest_index.strftime("%Y-%m-%d")
            except (AttributeError, ValueError):
                market_date = str(latest_index)[:10]

            indices.append(
                {
                    "symbol": symbol,
                    "name": display_name,
                    "market_date": market_date,
                    "value": round(latest_value, 4),
                    "change": round(change, 4) if change is not None else None,
                    "change_percent": (
                        round(change_percent, 4)
                        if change_percent is not None
                        else None
                    ),
                    "source_url": (
                        "https://finance.yahoo.com/quote/"
                        f"{quote(symbol, safe='')}/"
                    ),
                }
            )

        if not indices:
            return {"error": "선택한 시장의 최신 지수 데이터를 찾지 못했습니다."}

        now_kst = datetime.now(timezone(timedelta(hours=9)))
        return {
            "status": "success",
            "region": normalized_region,
            "checked_at_kst": now_kst.isoformat(timespec="seconds"),
            "indices": indices,
            "provider": "yfinance",
            "source_urls": [
                item["source_url"]
                for item in indices
                if item.get("source_url")
            ],
            "freshness_note": (
                "각 지수의 market_date가 최신 가용 거래일입니다. "
                "장중이면 당일 값은 변동될 수 있습니다."
            ),
        }

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_fetch),
            timeout=_MARKET_FETCH_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "yfinance 시장 지수 조회 타임아웃(%ss): region=%s",
            _MARKET_FETCH_TIMEOUT_SEC,
            normalized_region,
        )
        return {"error": "시장 지수 조회가 지연되어 취소되었습니다."}
    except Exception as exc:
        logger.error(
            "yfinance 시장 지수 조회 실패(region=%s): %s",
            normalized_region,
            exc,
            exc_info=True,
        )
        return {"error": "시장 지수 정보를 가져오는 중 오류가 발생했습니다."}

    if result.get("error"):
        logger.warning(
            "yfinance 시장 지수 결과 없음(region=%s): %s",
            normalized_region,
            result["error"],
        )
    else:
        logger.info(
            "yfinance 시장 지수 조회 성공(region=%s, indices=%d)",
            normalized_region,
            len(result.get("indices") or []),
        )
    return result

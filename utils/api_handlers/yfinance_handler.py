
# -*- coding: utf-8 -*-
"""
yfinance 주식 데이터 클라이언트.

yfinance 라이브러리를 통해 미국/한국 주식의 시세, 기업 정보,
재무제표 등을 비동기적으로 조회합니다.
"""

import yfinance as yf
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
from urllib.parse import quote
from logger_config import logger

# yfinance 내부 requests 호출에는 타임아웃이 없어, 야후 엔드포인트가 멈추면
# to_thread 워커가 무한 점유되어 공용 스레드풀이 고갈될 수 있다. 조회 전체에
# 상한을 둬 최소한 호출측은 해제되도록 한다.
_STOCK_FETCH_TIMEOUT_SEC = 15
_MARKET_FETCH_TIMEOUT_SEC = 20
_MARKET_INDEXES = {
    "kr": (
        ("^KS11", "코스피"),
        ("^KQ11", "코스닥"),
    ),
    "us": (
        ("^DJI", "다우존스"),
        ("^GSPC", "S&P 500"),
        ("^IXIC", "나스닥 종합"),
    ),
}

async def get_stock_info(ticker: str) -> Dict[str, Any]:
    """
    yfinance를 사용하여 주식/암호화폐 정보를 조회합니다.
    """
    try:
        # 동기 yfinance 호출을 스레드에서 실행
        def _fetch():
            """yfinance Ticker에서 시세/정보를 동기적으로 조회합니다."""
            stock = yf.Ticker(ticker)
            # 기업 상세 조회는 가격 한 건에 비해 비싸지만, 현재 출력 계약에서
            # 회사명·통화·산업·설명을 사용하므로 한 번만 가져온다.
            info = {}
            try:
                info = stock.info
            except Exception:
                pass
                
            price = None
            currency = info.get('currency', 'USD')
            
            # Fetch Price
            try:
                price = stock.fast_info.last_price
            except Exception:
                # Fallback to history
                hist = stock.history(
                    period="5d",
                    timeout=10,
                    raise_errors=True,
                )
                if not hist.empty:
                    price = hist['Close'].iloc[-1]
            
            # Calculate Change (approximate if fast_info)
            change_p = None
            try:
                prev_close = stock.fast_info.previous_close
                if price and prev_close:
                    change_p = ((price - prev_close) / prev_close) * 100
            except Exception:
                pass

            return {
                "symbol": ticker,
                "name": info.get('shortName') or info.get('longName') or ticker,
                "price": price,
                "currency": currency,
                "change_percent": change_p,
                "market_cap": info.get('marketCap'),
                "industry": info.get('industry'),
                "summary": info.get('longBusinessSummary') or info.get('description'),
                "website": info.get('website')
            }

        data = await asyncio.wait_for(asyncio.to_thread(_fetch), timeout=_STOCK_FETCH_TIMEOUT_SEC)

        if data['price'] is None:
            logger.warning(f"yfinance 조회 실패 (Price None): {ticker}")
            return {"error": f"'{ticker}'에 대한 시세 정보를 가져올 수 없습니다."}
            
        logger.info(f"yfinance 조회 성공: {ticker} -> {data.get('price')}")
        return data

    except asyncio.TimeoutError:
        logger.warning(f"yfinance 조회 타임아웃({_STOCK_FETCH_TIMEOUT_SEC}s): {ticker}")
        return {"error": f"'{ticker}' 시세 조회가 지연되어 취소되었습니다."}
    except Exception as e:
        logger.error(f"yfinance 조회 실패 ({ticker}): {e}")
        return {"error": "주식 정보를 가져오는 중 오류가 발생했습니다."}


async def get_market_snapshot(region: str = "global") -> Dict[str, Any]:
    """주요 시장 지수의 최신 가용 일봉을 한 번의 배치 요청으로 조회합니다."""
    normalized_region = str(region or "global").strip().lower()
    if normalized_region not in {"kr", "us", "global"}:
        normalized_region = "global"

    selected = (
        _MARKET_INDEXES["kr"] + _MARKET_INDEXES["us"]
        if normalized_region == "global"
        else _MARKET_INDEXES[normalized_region]
    )
    tickers = [symbol for symbol, _name in selected]

    def _fetch() -> Dict[str, Any]:
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

# -*- coding: utf-8 -*-
"""
Finnhub 금융 데이터 API 클라이언트.

미국 주식 시세 조회, 기업 뉴스 검색, 회사 프로필, 애널리스트 추천 등의
엔드포인트를 비동기로 호출하고 LLM 친화적 형식으로 포맷팅합니다.
"""

from __future__ import annotations
import asyncio
import re
import time
import requests
from datetime import datetime, timedelta
import config
from logger_config import logger

from .. import http
from utils.finance_query import (
    is_kr_listing,
    kr_market_unsupported_result,
    looks_like_ticker,
)

BASE_URL = config.FINNHUB_BASE_URL
_SEARCH_CACHE: dict[str, tuple[float, tuple[int, dict | list | None]]] = {}
_SEARCH_CACHE_TTL_SEC = 10 * 60

def _get_client():
    """API 키 존재 여부를 확인하고, 요청에 필요한 딕셔너리를 반환합니다."""
    api_key = config.FINNHUB_API_KEY
    if not api_key or api_key == 'YOUR_FINNHUB_API_KEY':
        logger.error("Finnhub API 키(FINNHUB_API_KEY)가 설정되지 않았습니다.")
        return None
    return {"token": api_key}

def _format_finnhub_quote_data(symbol: str, quote_data: dict) -> str:
    """Finnhub 시세 데이터를 LLM 친화적인 문자열로 포맷팅합니다."""
    price = quote_data.get('current_price', 0)
    change = quote_data.get('change', 0)

    if change > 0:
        change_str = f"+{change:.2f}"
    elif change < 0:
        change_str = f"{change:.2f}"
    else:
        change_str = "0.00"

    return f"{symbol}: {price:.2f} USD ({change_str})"

def _format_finnhub_news_data(symbol: str, news_items: list) -> str:
    """Finnhub 뉴스 데이터를 LLM 친화적인 문자열로 포맷팅합니다."""
    if not news_items:
        return f"'{symbol}'에 대한 최신 뉴스를 찾을 수 없습니다."

    headlines = [f"- {item['headline']} ({item['url']})" for item in news_items]
    return f"'{symbol}' 관련 최신 뉴스:\n" + "\n".join(headlines)

async def get_raw_stock_quote(symbol: str) -> dict | None:
    """시세 dict. 심볼 확정은 lookup_quote와 같은 검색 경로를 씁니다."""
    result = await lookup_quote(symbol)
    if result.get("status") != "success":
        return None
    return {
        "symbol": result.get("symbol"),
        "price": result.get("price"),
        "change": result.get("change"),
    }


async def get_stock_quote(symbol: str) -> str:
    """
    Finnhub API로 해외 주식 시세를 조회하고, LLM 친화적인 문자열로 반환합니다.
    """
    raw_data = await get_raw_stock_quote(symbol)
    if not raw_data:
        return f"'{symbol}'에 대한 주식 정보를 찾을 수 없습니다. 티커나 회사 이름이 정확한지 확인해주세요."
    
    # 포맷팅에 필요한 데이터만 전달
    formatted_data = {
        "current_price": raw_data.get('price'),
        "change": raw_data.get('change'),
    }
    return _format_finnhub_quote_data(raw_data.get('symbol'), formatted_data)

async def get_company_news(symbol: str, count: int = 3) -> str:
    """
    Finnhub API로 최신 뉴스를 조회하고, LLM 친화적인 문자열로 반환합니다.
    """
    params = _get_client()
    if not params:
        return f"'{symbol}' 관련 뉴스를 조회할 수 없습니다 (API 키 미설정)."
    
    normalized_symbol = await _search_listed_symbol(symbol)
    if not normalized_symbol:
        return f"'{symbol}' 관련 뉴스를 조회할 수 없습니다."
    logger.info("Finnhub News: resolved symbol=%s", normalized_symbol)
    params['symbol'] = normalized_symbol

    today = datetime.now()
    one_week_ago = today - timedelta(days=7)
    params['from'] = one_week_ago.strftime('%Y-%m-%d')
    params['to'] = today.strftime('%Y-%m-%d')

    try:
        with http.get_modern_tls_session() as session:
            response = await asyncio.to_thread(session.get, f"{BASE_URL}/company-news", params=params, timeout=15)
        response.raise_for_status()
        news_items = response.json()

        if not isinstance(news_items, list):
            logger.warning(
                "Finnhub 뉴스 API('%s')에서 예상치 못한 형식의 응답을 받았습니다. response_type=%s",
                normalized_symbol,
                type(news_items).__name__,
            )
            return f"'{normalized_symbol}' 관련 뉴스를 가져왔지만, 형식이 올바르지 않습니다."

        formatted_news = [
            {"headline": item.get('headline'), "summary": item.get('summary'), "url": item.get('url')}
            for item in news_items[:count]
        ]
        return _format_finnhub_news_data(normalized_symbol, formatted_news)

    except requests.exceptions.RequestException as e:
        logger.error(f"Finnhub 뉴스 API('{normalized_symbol}') 요청 중 오류: {e}", exc_info=True)
        return "뉴스 조회 중 네트워크 오류가 발생했습니다."
    except (ValueError, KeyError) as e:
        response_text = "N/A"
        logger.error(f"Finnhub 뉴스 API('{normalized_symbol}') 응답 파싱 중 오류: {e}. 응답: {response_text}", exc_info=True)
        return "뉴스 조회 중 데이터 처리 오류가 발생했습니다."
    except Exception as e:
        logger.error(f"Finnhub 뉴스 API('{normalized_symbol}') 처리 중 예기치 않은 오류: {e}", exc_info=True)
        return "뉴스 조회 중 알 수 없는 오류가 발생했습니다."

async def get_company_profile(symbol: str) -> dict | None:
    """
    Finnhub API로 기업 프로필(업종, 시총, 웹사이트 등)을 조회합니다.
    URL: /stock/profile2
    """
    params = _get_client()
    if not params:
        return None
    
    normalized_symbol = await _search_listed_symbol(symbol)
    if not normalized_symbol:
        return None
    params['symbol'] = normalized_symbol

    try:
        with http.get_modern_tls_session() as session:
            response = await asyncio.to_thread(session.get, f"{BASE_URL}/stock/profile2", params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        if not data:
            return None
            
        return {
            "name": data.get("name"),
            "industry": data.get("finnhubIndustry"),
            "market_cap": data.get("marketCapitalization"), # Million USD
            "website": data.get("weburl"),
            "logo": data.get("logo")
        }
    except Exception as e:
        logger.error(f"Finnhub Profile API('{normalized_symbol}') 오류: {e}")
        return None

async def get_recommendation_trends(symbol: str) -> str:
    """
    Finnhub API로 애널리스트 추천 트렌드(Buy/Sell/Hold)를 조회합니다.
    URL: /stock/recommendation
    """
    params = _get_client()
    if not params:
        return ""
    
    normalized_symbol = await _search_listed_symbol(symbol)
    if not normalized_symbol:
        return ""
    params['symbol'] = normalized_symbol

    try:
        with http.get_modern_tls_session() as session:
            response = await asyncio.to_thread(session.get, f"{BASE_URL}/stock/recommendation", params=params, timeout=10)
        response.raise_for_status()
        data = response.json() # List of dicts
        
        if not data or not isinstance(data, list):
            return "추천 트렌드 데이터가 없습니다."
            
        # 최신 데이터 (보통 첫번째가 최신이지만 날짜 확인 필요)
        latest = data[0] # period 기준 정렬되어 있다고 가정
        period = latest.get("period", "N/A")
        strong_buy = latest.get("strongBuy", 0)
        buy = latest.get("buy", 0)
        hold = latest.get("hold", 0)
        sell = latest.get("sell", 0)
        strong_sell = latest.get("strongSell", 0)
        
        return (f"[{period} 기준] 강력매수:{strong_buy}, 매수:{buy}, "
                f"중립:{hold}, 매도:{sell}, 강력매도:{strong_sell}")

    except Exception as e:
        logger.error(f"Finnhub Recommendation API('{normalized_symbol}') 오류: {e}")
        return "추천 트렌드 조회 실패"


def _is_hangul(text: str) -> bool:
    return bool(re.search(r"[가-힣]", text or ""))


def _pick_search_hit(results: list[dict], *, query: str, search_term: str) -> dict | None:
    if not results:
        return None
    term = str(search_term or "").strip().upper()
    query_text = str(query or "")
    scored: list[tuple[int, dict]] = []
    for item in results:
        symbol = str(item.get("symbol") or "").strip()
        if not symbol:
            continue
        description = str(item.get("description") or "")
        kind = str(item.get("type") or "")
        score = 0
        if kind == "Common Stock":
            score += 30
        if symbol.upper() == term:
            score += 80
        if term and term.casefold() in description.casefold():
            score += 40
        if is_kr_listing(symbol):
            continue
        kind_upper = kind.upper()
        is_crypto = (
            "CRYPTO" in kind_upper
            or symbol.startswith(("BINANCE:", "COINBASE:", "KRAKEN:"))
        )
        crypto_intent = any(
            marker in query_text.casefold()
            for marker in ("crypto", "coin", "bitcoin", "ethereum", "코인")
        )
        if crypto_intent and is_crypto:
            score += 40
        elif is_crypto:
            score -= 25
        elif crypto_intent and kind == "Common Stock":
            score -= 30
        if not _is_hangul(query_text) and "." not in symbol:
            score += 20
        if symbol.endswith(".SS") or symbol.endswith(".SZ"):
            score -= 10
        scored.append((score, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    if not scored or scored[0][0] <= 0:
        return None
    return scored[0][1]


def _hangul_only(text: str) -> bool:
    return _is_hangul(text) and not re.search(r"[A-Za-z]{2,}", text or "")


async def _search_listed_symbol(query: str) -> str | None:
    text = str(query or "").strip()
    if not text or is_kr_listing(text):
        return None
    if looks_like_ticker(text.upper()) and " " not in text:
        return text.upper()
    if _hangul_only(text):
        return None
    status, payload = await _request_json("/search", {"q": text})
    if status != 200 or not isinstance(payload, dict):
        return None
    hit = _pick_search_hit(
        list(payload.get("result") or []),
        query=text,
        search_term=text,
    )
    symbol = str((hit or {}).get("symbol") or "").strip()
    if not symbol or is_kr_listing(symbol):
        return None
    return symbol


async def _request_json(path: str, extra: dict) -> tuple[int, dict | list | None]:
    params = _get_client()
    if not params:
        return 0, None
    if path == "/search":
        cache_key = str(extra.get("q") or "")
        cached = _SEARCH_CACHE.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
    params.update(extra)
    with http.get_modern_tls_session() as session:
        response = await asyncio.to_thread(
            session.get,
            f"{BASE_URL}{path}",
            params=params,
            timeout=10,
        )
    try:
        payload = response.json()
    except ValueError:
        payload = None
    result = (response.status_code, payload)
    if path == "/search" and response.status_code == 200:
        _SEARCH_CACHE[str(extra.get("q") or "")] = (
            time.monotonic() + _SEARCH_CACHE_TTL_SEC,
            result,
        )
    return result


async def lookup_quote(
    query: str,
    *,
    hint_symbol: str | None = None,
) -> dict:
    """검색+시세를 Finnhub 무료 엔드포인트로만 조회합니다.

    호출은 search 0~1회 + quote 1회로 제한해 분당 60회 한도를 넘기지 않습니다.
    """
    from datetime import datetime, timedelta, timezone

    original = str(query or "").strip()
    hint = str(hint_symbol or "").strip()
    if is_kr_listing(original) or is_kr_listing(hint):
        return kr_market_unsupported_result()
    if not _get_client():
        return {
            "status": "error",
            "error": "주식 정보를 조회할 수 없습니다 (API 키 미설정).",
            "failure_kind": "provider_error",
            "provider_failure": True,
        }

    candidates: list[str] = []
    if original and looks_like_ticker(original.upper()) and not is_kr_listing(original):
        candidates.append(original.upper())
    if hint and looks_like_ticker(hint) and not is_kr_listing(hint):
        candidates.append(hint)
    elif hint and ":" in hint:
        candidates.append(hint)

    should_search = (
        original
        and not looks_like_ticker(original.upper())
        and not _hangul_only(original)
    )
    if should_search:
        status, payload = await _request_json("/search", {"q": original})
        if status == 200 and isinstance(payload, dict):
            search_results = list(payload.get("result") or [])
            hit = _pick_search_hit(
                search_results,
                query=original,
                search_term=original,
            )
            if hit and hit.get("symbol"):
                candidates.append(str(hit["symbol"]))
            elif any(
                is_kr_listing(str(item.get("symbol") or ""))
                for item in search_results
            ):
                return kr_market_unsupported_result()
        elif status == 429:
            return {
                "status": "error",
                "error": "시세 조회가 잠시 너무 많아요. 조금 뒤에 다시 물어봐 주세요.",
                "failure_kind": "provider_error",
                "provider_failure": True,
            }

    seen: set[str] = set()
    unique_candidates: list[str] = []
    for item in candidates:
        key = item.upper()
        if key in seen or is_kr_listing(item):
            continue
        seen.add(key)
        unique_candidates.append(item)

    last_limited = None
    for symbol in unique_candidates:
        status, payload = await _request_json("/quote", {"symbol": symbol})
        if status == 403:
            last_limited = symbol
            logger.info("Finnhub 무료 플랜 범위 밖: %s", symbol)
            continue
        if status == 429:
            return {
                "status": "error",
                "error": "시세 조회가 잠시 너무 많아요. 조금 뒤에 다시 물어봐 주세요.",
                "failure_kind": "provider_error",
                "provider_failure": True,
            }
        if status != 200 or not isinstance(payload, dict):
            continue
        if payload.get("error"):
            last_limited = symbol
            continue
        price = payload.get("c")
        if not isinstance(price, (int, float)) or (
            price == 0 and payload.get("d") is None
        ):
            continue
        source_url = f"https://finnhub.io/quote/{symbol}"
        logger.info("Finnhub 조회 성공: %s -> %s", symbol, price)
        return {
            "status": "success",
            "symbol": symbol,
            "name": symbol,
            "price": float(price),
            "change": payload.get("d"),
            "currency": "USD",
            "change_percent": payload.get("dp"),
            "provider": "finnhub",
            "checked_at_kst": datetime.now(
                timezone(timedelta(hours=9))
            ).isoformat(timespec="seconds"),
            "source_url": source_url,
            "source_urls": [source_url],
        }

    if last_limited:
        return {
            "status": "error",
            "error": (
                "이 종목 시세는 Finnhub 무료 플랜 범위가 아니에요. "
                "미국 상장 종목이나 티커로 다시 물어봐 주세요."
            ),
            "failure_kind": "plan_limited",
            "provider_failure": False,
            "symbol": last_limited,
        }
    return {
        "status": "error",
        "error": (
            "정확한 종목을 파악하지 못했어요. "
            "회사명이나 티커와 거래소를 함께 알려주세요."
        ),
        "failure_kind": "invalid_symbol",
        "provider_failure": False,
    }

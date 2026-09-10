
# -*- coding: utf-8 -*-
"""
yfinance 종목 시세 클라이언트.

기본 시세 경로는 Finnhub입니다. 이 모듈은 USE_YFINANCE일 때만 종목을 조회합니다.
지수 스냅샷은 yfinance_snapshot 모듈로 분리되어 있으며, 호환을 위해 재수출합니다.
"""

import yfinance as yf
import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
from urllib.parse import quote
from logger_config import logger
from utils.finance_query import detect_fx_symbol, is_kr_listing, kr_market_unsupported_result, looks_like_ticker
from utils.api_handlers.yfinance_snapshot import get_market_snapshot

# yfinance 내부 requests 호출에는 타임아웃이 없어, 야후 엔드포인트가 멈추면
# to_thread 워커가 무한 점유되어 공용 스레드풀이 고갈될 수 있다. 조회 전체에
# 상한을 둬 최소한 호출측은 해제되도록 한다.
_STOCK_FETCH_TIMEOUT_SEC = 15
_SYMBOL_SEARCH_TIMEOUT_SEC = 10
_SEARCH_CACHE_TTL_SEC = 6 * 60 * 60
_SEARCH_EMPTY_TTL_SEC = 5 * 60
_SEARCH_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_PRIMARY_EXCHANGES = frozenset({
    "NMS", "NYQ", "NGM", "NYS", "NAS", "PCX", "ASE",
    "CCC", "CCY",
})
_OTC_EXCHANGES = frozenset({"PNK", "OTC", "YHD", "OQB", "OQX"})
_FOREIGN_SUFFIXES = (
    ".DE", ".F", ".PA", ".L", ".SW", ".MI", ".AS", ".BR",
    ".SA", ".MX", ".TO", ".V", ".NE", ".HK", ".T", ".AX",
)
_QUERY_NOISE = frozenset({
    "주가", "주식", "시세", "현재가", "종가", "시가", "가격", "얼마야", "얼마",
    "알려줘", "알려", "해줘", "해주세요", "좀", "지금", "현재", "오늘", "어제",
    "내일", "요즘", "조회", "검색", "흐름", "상황", "어때", "어떤가", "몇이야",
    "몇임", "좀요", "부탁", "한국", "미국", "국내", "해외", "증시", "시장",
    "코스피", "코스닥", "나스닥", "뉴욕", "상장", "종목",
    "환율", "환전", "환산", "원화", "엔화", "달러",
    "price", "stock", "quote", "ticker", "now", "today", "current", "please",
})
_MARKET_HINTS_KR = ("한국", "국내", "코스피", "코스닥", "유가증권")
_MARKET_HINTS_US = ("미국", "나스닥", "뉴욕", "다우", "s&p")


def _looks_like_invalid_symbol_error(exc: Exception | str) -> bool:
    """Yahoo가 존재하지 않는 티커에 사용하는 오류 문구를 식별합니다."""
    text = str(exc or "").casefold()
    return any(
        marker in text
        for marker in (
            "quote not found",
            "no price data",
            "no data found",
            "possibly delisted",
            "symbol may be delisted",
        )
    )


def _looks_like_yahoo_ticker(value: str) -> bool:
    return looks_like_ticker(value)


def _query_has_hangul(text: str) -> bool:
    return bool(re.search(r"[가-힣]", text or ""))


def _normalize_search_key(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().casefold())


def _strip_query_noise(query: str) -> str:
    text = str(query or "")
    tokens = sorted(_QUERY_NOISE, key=len, reverse=True)
    for token in tokens:
        text = re.sub(re.escape(token), " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[?!~.,]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _hangul_entities(query: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[가-힣]{2,}", query or ""):
        if token in _QUERY_NOISE or token in seen:
            continue
        seen.add(token)
        found.append(token)
    return found


def build_symbol_search_terms(
    query: str,
    *,
    hint_symbol: str | None = None,
) -> dict[str, Any]:
    """조회 문장에서 Yahoo 검색어 후보를 만듭니다."""
    original = str(query or "").strip()
    hint = str(hint_symbol or "").strip().upper()
    hangul = _hangul_entities(original)
    if len(hangul) >= 2:
        return {
            "terms": [],
            "failure_kind": "ambiguous_symbol",
            "reason": "multiple_entities",
        }

    terms: list[str] = []

    def _add(term: str | None) -> None:
        value = str(term or "").strip()
        if not value or len(value) > 80:
            return
        if value not in terms:
            terms.append(value)

    fx = detect_fx_symbol(original)
    _add(fx)
    if hint and _looks_like_yahoo_ticker(hint):
        _add(hint)
    stripped = _strip_query_noise(original)
    _add(stripped)
    if hangul:
        _add(hangul[0])
    for latin in re.findall(r"[A-Za-z][A-Za-z0-9.=^-]{0,19}", original):
        _add(latin.upper() if _looks_like_yahoo_ticker(latin.upper()) else latin)
    if original and original not in terms and len(original) <= 80:
        _add(original)
    return {"terms": terms, "failure_kind": None, "reason": None}


def _quote_names(quote: dict[str, Any]) -> str:
    return " ".join(
        str(quote.get(key) or "")
        for key in ("symbol", "shortname", "longname", "exchDisp")
    )


def score_listed_quote(
    quote: dict[str, Any],
    *,
    query: str,
    search_term: str,
) -> int:
    """상장 검색 결과 한 건의 적합도를 점수화합니다."""
    symbol = str(quote.get("symbol") or "").strip().upper()
    qtype = str(quote.get("quoteType") or "").upper()
    exchange = str(quote.get("exchange") or "").upper()
    if not symbol:
        return -1000
    if is_kr_listing(symbol) or exchange in {"KSC", "KOE"}:
        return 0
    names = _quote_names(quote).casefold()
    term = str(search_term or "").strip()
    term_cf = term.casefold()
    query_text = str(query or "")
    score = 0

    if qtype == "EQUITY":
        score += 30
    elif qtype in {"ETF", "INDEX"}:
        score += 18
    elif qtype in {"CRYPTOCURRENCY", "CURRENCY"}:
        score += 28 if detect_fx_symbol(query_text) or "=X" in symbol else 8
    elif qtype in {"FUTURE", "OPTION"}:
        score -= 40

    if exchange in _PRIMARY_EXCHANGES:
        score += 25
    if exchange in _OTC_EXCHANGES:
        score -= 45
    if any(symbol.endswith(suffix) for suffix in _FOREIGN_SUFFIXES):
        score -= 18
    if "pref" in names or str(quote.get("shortname") or "").endswith("우"):
        if "우" not in query_text and "pref" not in query_text.casefold():
            score -= 22

    if symbol == term.upper() or symbol.casefold() == term_cf:
        score += 80
    elif term_cf and term_cf in names:
        score += 40
    elif term and _query_has_hangul(term) is False and term_cf[:4] and term_cf[:4] in names:
        score += 12

    wants_us = any(hint in query_text.casefold() or hint in query_text for hint in _MARKET_HINTS_US)
    if wants_us and "." not in symbol:
        score += 16
    if "." not in symbol and qtype == "EQUITY":
        score += 10
    if symbol.endswith("=X") and detect_fx_symbol(query_text):
        score += 40
    return score


def pick_listed_quote(
    quotes: list[dict[str, Any]],
    *,
    query: str,
    search_term: str,
) -> dict[str, Any] | None:
    """검색 결과에서 유일한 1순위 상장 종목을 고르거나, 동점이면 포기합니다."""
    ranked = sorted(
        (
            (score_listed_quote(quote, query=query, search_term=search_term), quote)
            for quote in quotes
            if str(quote.get("symbol") or "").strip()
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    ranked = [item for item in ranked if item[0] > 0]
    if not ranked:
        return None
    best_score, best = ranked[0]
    if len(ranked) > 1:
        second_score, second = ranked[1]
        best_symbol = str(best.get("symbol") or "").upper()
        second_symbol = str(second.get("symbol") or "").upper()
        if (
            best_symbol != second_symbol
            and best_score - second_score < 12
            and not (
                best_symbol.split(".")[0] == second_symbol.split(".")[0]
                and best_score >= second_score
            )
        ):
            return {"ambiguous": True, "candidates": [best, second]}
    return best


def _cached_quotes(cache_key: str) -> list[dict[str, Any]] | None:
    item = _SEARCH_CACHE.get(cache_key)
    if not item:
        return None
    expires_at, quotes = item
    if expires_at < time.monotonic():
        _SEARCH_CACHE.pop(cache_key, None)
        return None
    return quotes


def _store_quotes(cache_key: str, quotes: list[dict[str, Any]]) -> None:
    ttl = _SEARCH_EMPTY_TTL_SEC if not quotes else _SEARCH_CACHE_TTL_SEC
    if len(_SEARCH_CACHE) > 256:
        _SEARCH_CACHE.clear()
    _SEARCH_CACHE[cache_key] = (time.monotonic() + ttl, quotes)


def search_yahoo_quotes(query: str) -> list[dict[str, Any]]:
    """Yahoo Finance 검색으로 상장 심볼 후보를 가져옵니다."""
    term = str(query or "").strip()
    if not term:
        return []
    cache_key = _normalize_search_key(term)
    cached = _cached_quotes(cache_key)
    if cached is not None:
        return cached

    def _fetch() -> list[dict[str, Any]]:
        search = yf.Search(
            term,
            max_results=8,
            news_count=0,
            enable_fuzzy_query=True,
            timeout=8,
        )
        quotes = getattr(search, "quotes", None) or []
        return [dict(item) for item in quotes if isinstance(item, dict)]

    try:
        quotes = _fetch()
    except TypeError:
        search = yf.Search(term, max_results=8, news_count=0)
        quotes = [
            dict(item)
            for item in (getattr(search, "quotes", None) or [])
            if isinstance(item, dict)
        ]
    _store_quotes(cache_key, quotes)
    return quotes


async def resolve_listed_symbol(
    query: str,
    *,
    hint_symbol: str | None = None,
    original_query: str | None = None,
) -> Dict[str, Any]:
    """자연어/힌트 심볼을 Yahoo에 실제로 상장된 티커로 해석합니다.

    LLM이 만든 티커를 그대로 믿지 않고, 검색 결과에 있는 종목만 채택합니다.
    """
    original = str(query or "").strip()
    scoring_query = str(original_query or original).strip() or original
    hint = str(hint_symbol or "").strip().upper() or None
    planned = build_symbol_search_terms(original, hint_symbol=hint)
    if planned.get("failure_kind"):
        return {
            "status": "error",
            "error": "조회할 종목이 여러 개로 보여요. 회사명 하나만 알려주세요.",
            "failure_kind": planned["failure_kind"],
            "provider_failure": False,
        }
    terms = list(planned.get("terms") or [])
    if not terms:
        return {
            "status": "error",
            "error": "정확한 종목을 파악하지 못했어요. 회사명이나 티커와 거래소를 함께 알려주세요.",
            "failure_kind": "ambiguous_symbol",
            "provider_failure": False,
        }

    def _resolve() -> Dict[str, Any]:
        last_ambiguous: dict[str, Any] | None = None
        for term in terms:
            try:
                quotes = search_yahoo_quotes(term)
            except Exception:
                logger.warning(
                    "yfinance 심볼 검색 실패. term_chars=%d",
                    len(term),
                    exc_info=True,
                )
                continue
            picked = pick_listed_quote(
                quotes,
                query=scoring_query or term,
                search_term=term,
            )
            if not picked:
                continue
            if picked.get("ambiguous"):
                last_ambiguous = picked
                continue
            symbol = str(picked.get("symbol") or "").strip().upper()
            if is_kr_listing(symbol):
                return kr_market_unsupported_result()
            if not _looks_like_yahoo_ticker(symbol):
                continue
            logger.info(
                "yfinance 심볼 검색 확정: %s term_chars=%d quote_type=%s",
                symbol,
                len(term),
                picked.get("quoteType"),
            )
            return {
                "status": "success",
                "symbol": symbol,
                "name": picked.get("shortname") or picked.get("longname") or symbol,
                "quote_type": picked.get("quoteType"),
                "exchange": picked.get("exchange"),
                "matched_term": term,
            }
        if last_ambiguous:
            return {
                "status": "error",
                "error": "같은 이름의 상장 종목이 여러 개라 하나를 고르지 못했어요.",
                "failure_kind": "ambiguous_symbol",
                "provider_failure": False,
            }
        return {
            "status": "error",
            "error": "정확한 종목을 파악하지 못했어요. 회사명이나 티커와 거래소를 함께 알려주세요.",
            "failure_kind": "invalid_symbol",
            "provider_failure": False,
        }

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_resolve),
            timeout=_SYMBOL_SEARCH_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "yfinance 심볼 검색 타임아웃(%ss)",
            _SYMBOL_SEARCH_TIMEOUT_SEC,
        )
        return {
            "status": "error",
            "error": "종목 검색이 지연되어 취소됐어요.",
            "failure_kind": "provider_timeout",
            "provider_failure": True,
        }
    except Exception:
        logger.error("yfinance 심볼 검색 중 오류", exc_info=True)
        return {
            "status": "error",
            "error": "종목을 찾는 쪽에서 문제가 생겼어요.",
            "failure_kind": "provider_error",
            "provider_failure": True,
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
            return {
                "status": "error",
                "error": f"'{ticker}' 종목을 Yahoo Finance에서 찾지 못했어요.",
                "failure_kind": "invalid_symbol",
                "provider_failure": False,
            }
            
        logger.info(f"yfinance 조회 성공: {ticker} -> {data.get('price')}")
        source_url = (
            "https://finance.yahoo.com/quote/"
            f"{quote(ticker, safe='')}/"
        )
        return {
            **data,
            "status": "success",
            "provider": "yfinance",
            "checked_at_kst": datetime.now(
                timezone(timedelta(hours=9))
            ).isoformat(timespec="seconds"),
            "source_url": source_url,
            "source_urls": [source_url],
        }

    except asyncio.TimeoutError:
        logger.warning(f"yfinance 조회 타임아웃({_STOCK_FETCH_TIMEOUT_SEC}s): {ticker}")
        return {
            "status": "error",
            "error": f"'{ticker}' 시세 조회가 지연되어 취소됐어요.",
            "failure_kind": "provider_timeout",
            "provider_failure": True,
        }
    except Exception as e:
        if _looks_like_invalid_symbol_error(e):
            logger.info(
                "yfinance 티커 없음: %s error_type=%s",
                ticker,
                type(e).__name__,
            )
            return {
                "status": "error",
                "error": f"'{ticker}' 종목을 Yahoo Finance에서 찾지 못했어요.",
                "failure_kind": "invalid_symbol",
                "provider_failure": False,
            }
        logger.error(
            "yfinance 조회 실패 (%s): error_type=%s",
            ticker,
            type(e).__name__,
            exc_info=True,
        )
        return {
            "status": "error",
            "error": "주식 정보를 가져오는 쪽에서 문제가 생겼어요.",
            "failure_kind": "provider_error",
            "provider_failure": True,
        }

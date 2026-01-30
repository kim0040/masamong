# -*- coding: utf-8 -*-
from __future__ import annotations
import asyncio
import requests
from datetime import datetime, timedelta
import config
from logger_config import logger

from .. import http

# Popular company names/aliases to ticker symbol mapping
# This helps the agent understand natural language queries
ALIAS_TO_TICKER = {
    # Top 40 US companies by market cap + common aliases
    "nvidia": "NVDA", "엔비디아": "NVDA",
    "microsoft": "MSFT", "마이크로소프트": "MSFT", "마소": "MSFT",
    "apple": "AAPL", "애플": "AAPL",
    "alphabet": "GOOGL", "알파벳": "GOOGL", "google": "GOOGL", "구글": "GOOGL",
    "amazon": "AMZN", "아마존": "AMZN",
    "meta platforms": "META", "meta": "META", "메타": "META", "facebook": "META", "페이스북": "META",
    "broadcom": "AVGO", "브로드컴": "AVGO",
    "tesla": "TSLA", "테슬라": "TSLA",
    "berkshire hathaway": "BRK.B", "버크셔해서웨이": "BRK.B",
    "jpmorgan chase": "JPM", "jp모건": "JPM",
    "oracle": "ORCL", "오라클": "ORCL",
    "walmart": "WMT", "월마트": "WMT",
    "eli lilly": "LLY", "일라이릴리": "LLY",
    "visa": "V", "비자": "V",
    "mastercard": "MA", "마스터카드": "MA",
    "netflix": "NFLX", "넷플릭스": "NFLX",
    "exxon mobil": "XOM", "엑슨모빌": "XOM",
    "costco": "COST", "코스트코": "COST",
    "johnson & johnson": "JNJ", "존슨앤드존슨": "JNJ",
    "home depot": "HD", "홈디포": "HD",
    "palantir": "PLTR", "팔란티어": "PLTR",
    "abbvie": "ABBV", "애브비": "ABBV",
    "bank of america": "BAC", "뱅크오브아메리카": "BAC",
    "procter & gamble": "PG", "프록터앤드갬블": "PG", "p&g": "PG",
    "chevron": "CVX", "쉐브론": "CVX",
    "unitedhealth group": "UNH", "유나이티드헬스": "UNH",
    "general electric": "GE", "제너럴일렉트릭": "GE",
    "coca-cola": "KO", "코카콜라": "KO",
    "cisco": "CSCO", "시스코": "CSCO",
    "wells fargo": "WFC", "웰스파고": "WFC",
    "philip morris": "PM", "필립모리스": "PM",
    "amd": "AMD", "advanced micro devices": "AMD",
    "morgan stanley": "MS", "모건스탠리": "MS",
    "goldman sachs": "GS", "골드만삭스": "GS",
    "ibm": "IBM", "international business machines": "IBM",
    "abbott laboratories": "ABT", "애보트": "ABT",
    "salesforce": "CRM", "세일즈포스": "CRM",
    "american express": "AXP", "아메리칸익스프레스": "AXP",
    "linde": "LIN", "린데": "LIN",
    "mcdonald's": "MCD", "맥도날드": "MCD",
}

BASE_URL = config.FINNHUB_BASE_URL

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

async def _search_symbol(query: str) -> str | None:
    """Search for a stock symbol using a query string."""
    params = _get_client()
    if not params:
        return None
    params['q'] = query

    try:
        session = http.get_modern_tls_session()
        response = await asyncio.to_thread(session.get, f"{BASE_URL}/search", params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data.get('result') and len(data['result']) > 0:
            for item in data['result']:
                if '.' not in item.get('symbol', '') and item.get('type') == 'Common Stock':
                    logger.info(f"Finnhub search: Found symbol '{item['symbol']}' for query '{query}'")
                    return item['symbol']
            first_result = data['result'][0]
            logger.info(f"Finnhub search: Falling back to first result '{first_result['symbol']}' for query '{query}'")
            return first_result['symbol']

        logger.warning(f"Finnhub search: No results found for query '{query}'")
        return None

    except requests.exceptions.RequestException as e:
        logger.error(f"Finnhub search API ('{query}') 요청 중 오류: {e}", exc_info=True)
        return None

async def get_raw_stock_quote(symbol: str) -> dict | None:
    """
    Finnhub API로 해외 주식 시세를 조회하고, 주요 정보를 dict 형태로 반환합니다.
    다른 도구에서 사용하기 위한 내부용 함수입니다.
    """
    params = _get_client()
    if not params:
        logger.error("Finnhub API 키가 설정되지 않아 get_raw_stock_quote를 실행할 수 없습니다.")
        return None

    normalized_symbol = ALIAS_TO_TICKER.get(symbol.lower(), symbol).upper()
    logger.info(f"Finnhub (raw): Original symbol '{symbol}' normalized to '{normalized_symbol}'")

    async def _get_quote_for_symbol(ticker: str) -> dict | None:
        """Internal function to fetch quote for a given ticker."""
        params['symbol'] = ticker
        session = http.get_modern_tls_session()
        response = await asyncio.to_thread(session.get, f"{BASE_URL}/quote", params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        # 0, d=None은 유효하지 않은 응답으로 간주
        if data.get('c') != 0 or data.get('d') is not None:
            return data
        return None

    try:
        quote_data = await _get_quote_for_symbol(normalized_symbol)

        # 첫 시도 실패 시, 심볼 검색 후 재시도
        if not quote_data:
            logger.info(f"Finnhub API에서 '{normalized_symbol}' 종목 정보를 찾지 못했습니다. 검색을 시도합니다.")
            searched_symbol = await _search_symbol(symbol)
            if searched_symbol and searched_symbol.lower() != normalized_symbol.lower():
                logger.info(f"Finnhub: 검색된 Ticker '{searched_symbol}'(으)로 재시도합니다.")
                quote_data = await _get_quote_for_symbol(searched_symbol)
                normalized_symbol = searched_symbol

        if not quote_data:
            logger.warning(f"Finnhub: 최종적으로 '{symbol}'에 대한 정보를 찾지 못했습니다.")
            return None

        return {
            "symbol": normalized_symbol,
            "price": quote_data.get('c'),
            "change": quote_data.get('d'),
        }
    except requests.exceptions.RequestException as e:
        logger.error(f"Finnhub API('{symbol}') 요청 중 오류: {e}", exc_info=True)
        return None
    except (ValueError, KeyError) as e:
        logger.error(f"Finnhub API('{symbol}') 응답 파싱 중 오류: {e}", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"Finnhub API('{symbol}') 처리 중 예기치 않은 오류: {e}", exc_info=True)
        return None

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
    
    normalized_symbol = ALIAS_TO_TICKER.get(symbol.lower(), symbol).upper()
    logger.info(f"Finnhub News: Original symbol '{symbol}' normalized to '{normalized_symbol}'")
    params['symbol'] = normalized_symbol

    today = datetime.now()
    one_week_ago = today - timedelta(days=7)
    params['from'] = one_week_ago.strftime('%Y-%m-%d')
    params['to'] = today.strftime('%Y-%m-%d')

    try:
        session = http.get_modern_tls_session()
        response = await asyncio.to_thread(session.get, f"{BASE_URL}/company-news", params=params, timeout=15)
        response.raise_for_status()
        news_items = response.json()

        if not isinstance(news_items, list):
            logger.warning(f"Finnhub 뉴스 API('{normalized_symbol}')에서 예상치 못한 형식의 응답을 받았습니다: {news_items}")
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
    
    normalized_symbol = ALIAS_TO_TICKER.get(symbol.lower(), symbol).upper()
    params['symbol'] = normalized_symbol

    try:
        session = http.get_modern_tls_session()
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
    
    normalized_symbol = ALIAS_TO_TICKER.get(symbol.lower(), symbol).upper()
    params['symbol'] = normalized_symbol

    try:
        session = http.get_modern_tls_session()
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
# -*- coding: utf-8 -*-
import asyncio
from datetime import datetime, timedelta
import config
from logger_config import logger
from .. import http as http_utils

BASE_URL = "https://finnhub.io/api/v1"

def _get_auth_params():
    """API 키 존재 여부를 확인하고, 요청에 필요한 딕셔너리를 반환합니다."""
    api_key = config.FINNHUB_API_KEY
    if not api_key or api_key == 'YOUR_FINNHUB_API_KEY':
        logger.error("Finnhub API 키(FINNHUB_API_KEY)가 설정되지 않았습니다.")
        return None
    return {"token": api_key}

async def get_stock_quote(symbol: str) -> dict:
    """
    Finnhub API를 사용하여 해외 주식의 현재 시세를 조회합니다.
    https://finnhub.io/docs/api/quote
    """
    params = _get_auth_params()
    if not params:
        return {"error": "API 키가 설정되지 않았습니다."}
    params['symbol'] = symbol.upper()

    data = await http_utils.make_async_request(f"{BASE_URL}/quote", params=params)

    if not data or data.get("error"):
        logger.error(f"Finnhub API에서 '{symbol}'의 시세 조회 실패: {data}")
        return data if data else {"error": "API 요청 실패"}

    if data.get('c') == 0 and data.get('d') is None:
            logger.warning(f"Finnhub API에서 '{symbol}' 종목 정보를 찾지 못했습니다.")
            return {"error": f"'{symbol}' 종목 정보를 찾을 수 없습니다."}

    return {
        "current_price": data.get('c'),
        "change": data.get('d'),
        "percent_change": data.get('dp'),
        "high_price": data.get('h'),
        "low_price": data.get('l'),
        "open_price": data.get('o'),
        "previous_close": data.get('pc')
    }

async def get_company_news(symbol: str, count: int = 3) -> dict:
    """
    Finnhub API를 사용하여 특정 종목에 대한 최신 뉴스를 조회합니다.
    https://finnhub.io/docs/api/company-news
    """
    params = _get_auth_params()
    if not params:
        return {"error": "API 키가 설정되지 않았습니다."}
    params['symbol'] = symbol.upper()

    today = datetime.now()
    one_week_ago = today - timedelta(days=7)
    params['from'] = one_week_ago.strftime('%Y-%m-%d')
    params['to'] = today.strftime('%Y-%m-%d')

    news_items = await http_utils.make_async_request(f"{BASE_URL}/company-news", params=params)

    if news_items is None or isinstance(news_items, dict) and news_items.get("error"):
        logger.error(f"Finnhub API에서 '{symbol}'의 뉴스 조회 실패: {news_items}")
        return news_items if news_items else {"error": "API 요청 실패"}

    if not news_items:
        return {"news": []}

    formatted_news = [
        {
            "headline": item.get('headline'),
            "summary": item.get('summary'),
            "url": item.get('url')
        }
        for item in news_items[:count]
    ]
    return {"news": formatted_news}

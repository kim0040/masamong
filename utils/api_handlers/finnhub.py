# -*- coding: utf-8 -*-
import asyncio
import requests
from datetime import datetime, timedelta
import config
from logger_config import logger

BASE_URL = "https://finnhub.io/api/v1"

def _get_client():
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
    params = _get_client()
    if not params:
        return {"error": "API 키가 설정되지 않았습니다."}
    params['symbol'] = symbol.upper()

    try:
        response = await asyncio.to_thread(requests.get, f"{BASE_URL}/quote", params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

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
    except requests.exceptions.Timeout:
        logger.error("Finnhub API 요청 시간 초과.")
        return {"error": "API 요청 시간 초과"}
    except requests.exceptions.HTTPError as e:
        logger.error(f"Finnhub API HTTP 오류: {e.response.status_code}")
        return {"error": f"API 서버 오류 ({e.response.status_code})"}
    except (requests.exceptions.RequestException, ValueError) as e:
        logger.error(f"Finnhub API 처리 중 오류: {e}", exc_info=True)
        return {"error": "API 요청 또는 데이터 처리 중 오류 발생"}

async def get_company_news(symbol: str, count: int = 3) -> dict:
    """
    Finnhub API를 사용하여 특정 종목에 대한 최신 뉴스를 조회합니다.
    https://finnhub.io/docs/api/company-news
    """
    params = _get_client()
    if not params:
        return {"error": "API 키가 설정되지 않았습니다."}
    params['symbol'] = symbol.upper()

    # 뉴스를 조회할 기간 설정 (최근 1주일)
    today = datetime.now()
    one_week_ago = today - timedelta(days=7)
    params['from'] = one_week_ago.strftime('%Y-%m-%d')
    params['to'] = today.strftime('%Y-%m-%d')

    try:
        response = await asyncio.to_thread(requests.get, f"{BASE_URL}/company-news", params=params, timeout=15)
        response.raise_for_status()
        news_items = response.json()

        if not news_items:
            return {"news": []} # 뉴스가 없는 것은 오류가 아님

        # agent.md 명세에 따라 필요한 정보만 추출
        formatted_news = [
            {
                "headline": item.get('headline'),
                "summary": item.get('summary'),
                "url": item.get('url')
            }
            for item in news_items[:count] # 요청된 개수만큼만 반환
        ]
        return {"news": formatted_news}

    except requests.exceptions.Timeout:
        logger.error("Finnhub 뉴스 API 요청 시간 초과.")
        return {"error": "API 요청 시간 초과"}
    except requests.exceptions.HTTPError as e:
        logger.error(f"Finnhub 뉴스 API HTTP 오류: {e.response.status_code}")
        return {"error": f"API 서버 오류 ({e.response.status_code})"}
    except (requests.exceptions.RequestException, ValueError) as e:
        logger.error(f"Finnhub 뉴스 API 처리 중 오류: {e}", exc_info=True)
        return {"error": "API 요청 또는 데이터 처리 중 오류 발생"}

# -*- coding: utf-8 -*-
import asyncio
import requests
from datetime import datetime, timedelta
import config
from logger_config import logger

from .. import http

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

    change_str = "변동 없음"
    if change > 0:
        change_str = f"{change:.2f} 상승"
    elif change < 0:
        change_str = f"{abs(change):.2f} 하락"

    return f"종목 '{symbol}'의 현재가는 {price:.2f} USD이며, 전일 대비 {change_str}했습니다."

def _format_finnhub_news_data(symbol: str, news_items: list) -> str:
    """Finnhub 뉴스 데이터를 LLM 친화적인 문자열로 포맷팅합니다."""
    if not news_items:
        return f"'{symbol}'에 대한 최신 뉴스를 찾을 수 없습니다."

    headlines = [f"- {item['headline']} ({item['url']})" for item in news_items]
    return f"'{symbol}' 관련 최신 뉴스:\n" + "\n".join(headlines)

async def get_stock_quote(symbol: str) -> str:
    """
    Finnhub API로 해외 주식 시세를 조회하고, LLM 친화적인 문자열로 반환합니다.
    [수정] 반환 형식을 dict에서 str으로 변경하여 토큰 사용량을 최적화합니다.
    """
    params = _get_client()
    if not params:
        return f"'{symbol}' 주식 정보를 조회할 수 없습니다 (API 키 미설정)."
    params['symbol'] = symbol.upper()

    try:
        session = http.get_modern_tls_session()
        response = await asyncio.to_thread(session.get, f"{BASE_URL}/quote", params=params, timeout=10, verify=False)
        response.raise_for_status()
        data = response.json()

        if data.get('c') == 0 and data.get('d') is None:
             logger.warning(f"Finnhub API에서 '{symbol}' 종목 정보를 찾지 못했습니다. (데이터 없음)")
             return f"'{symbol}' 종목 정보를 찾을 수 없습니다."

        quote_data = {
            "current_price": data.get('c'),
            "change": data.get('d'),
        }
        return _format_finnhub_quote_data(symbol, quote_data)

    except requests.exceptions.RequestException as e:
        logger.error(f"Finnhub API('{symbol}') 요청 중 오류: {e}", exc_info=True)
        return "해외 주식 조회 중 네트워크 오류가 발생했습니다."
    except (ValueError, KeyError) as e:
        response_text = response.text if 'response' in locals() else 'N/A'
        logger.error(f"Finnhub API('{symbol}') 응답 파싱 중 오류: {e}. 응답: {response_text}", exc_info=True)
        return "해외 주식 조회 중 데이터 처리 오류가 발생했습니다."
    except Exception as e:
        logger.error(f"Finnhub API('{symbol}') 처리 중 예기치 않은 오류: {e}", exc_info=True)
        return "해외 주식 조회 중 알 수 없는 오류가 발생했습니다."

async def get_company_news(symbol: str, count: int = 3) -> str:
    """
    Finnhub API로 최신 뉴스를 조회하고, LLM 친화적인 문자열로 반환합니다.
    [수정] 반환 형식을 dict에서 str으로 변경하여 토큰 사용량을 최적화합니다.
    """
    params = _get_client()
    if not params:
        return f"'{symbol}' 관련 뉴스를 조회할 수 없습니다 (API 키 미설정)."
    params['symbol'] = symbol.upper()

    today = datetime.now()
    one_week_ago = today - timedelta(days=7)
    params['from'] = one_week_ago.strftime('%Y-%m-%d')
    params['to'] = today.strftime('%Y-%m-%d')

    try:
        session = http.get_modern_tls_session()
        response = await asyncio.to_thread(session.get, f"{BASE_URL}/company-news", params=params, timeout=15, verify=False)
        response.raise_for_status()
        news_items = response.json()

        if not isinstance(news_items, list):
            logger.warning(f"Finnhub 뉴스 API('{symbol}')에서 예상치 못한 형식의 응답을 받았습니다: {news_items}")
            return f"'{symbol}' 관련 뉴스를 가져왔지만, 형식이 올바르지 않습니다."

        formatted_news = [
            {"headline": item.get('headline'), "summary": item.get('summary'), "url": item.get('url')}
            for item in news_items[:count]
        ]
        return _format_finnhub_news_data(symbol, formatted_news)

    except requests.exceptions.RequestException as e:
        logger.error(f"Finnhub 뉴스 API('{symbol}') 요청 중 오류: {e}", exc_info=True)
        return "뉴스 조회 중 네트워크 오류가 발생했습니다."
    except (ValueError, KeyError) as e:
        response_text = response.text if 'response' in locals() else 'N/A'
        logger.error(f"Finnhub 뉴스 API('{symbol}') 응답 파싱 중 오류: {e}. 응답: {response_text}", exc_info=True)
        return "뉴스 조회 중 데이터 처리 오류가 발생했습니다."
    except Exception as e:
        logger.error(f"Finnhub 뉴스 API('{symbol}') 처리 중 예기치 않은 오류: {e}", exc_info=True)
        return "뉴스 조회 중 알 수 없는 오류가 발생했습니다."

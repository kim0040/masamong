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

async def get_stock_quote(symbol: str) -> dict | None:
    """
    Finnhub API를 사용하여 해외 주식의 현재 시세를 조회합니다.
    [수정] 오류 발생 시 None을 반환하여 안정성을 높입니다.
    """
    params = _get_client()
    if not params:
        return None
    params['symbol'] = symbol.upper()

    try:
        response = await asyncio.to_thread(requests.get, f"{BASE_URL}/quote", params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        # Finnhub는 존재하지 않는 종목에 대해 200 OK와 함께 빈 데이터를 반환함
        if data.get('c') == 0 and data.get('d') is None:
             logger.warning(f"Finnhub API에서 '{symbol}' 종목 정보를 찾지 못했습니다. (데이터 없음)")
             return None

        return {
            "current_price": data.get('c'),
            "change": data.get('d'),
            "percent_change": data.get('dp'),
            "high_price": data.get('h'),
            "low_price": data.get('l'),
            "open_price": data.get('o'),
            "previous_close": data.get('pc')
        }
    except requests.exceptions.RequestException as e:
        logger.error(f"Finnhub API('{symbol}') 요청 중 오류: {e}", exc_info=True)
        return None
    except (ValueError, KeyError) as e:
        response_text = response.text if 'response' in locals() else 'N/A'
        logger.error(f"Finnhub API('{symbol}') 응답 파싱 중 오류: {e}. 응답: {response_text}", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"Finnhub API('{symbol}') 처리 중 예기치 않은 오류: {e}", exc_info=True)
        return None

async def get_company_news(symbol: str, count: int = 3) -> dict | None:
    """
    Finnhub API를 사용하여 특정 종목에 대한 최신 뉴스를 조회합니다.
    [수정] 오류 발생 시 None을 반환하고, 성공 시 뉴스 리스트를 포함한 딕셔너리를 반환합니다.
    """
    params = _get_client()
    if not params:
        return None
    params['symbol'] = symbol.upper()

    today = datetime.now()
    one_week_ago = today - timedelta(days=7)
    params['from'] = one_week_ago.strftime('%Y-%m-%d')
    params['to'] = today.strftime('%Y-%m-%d')

    try:
        response = await asyncio.to_thread(requests.get, f"{BASE_URL}/company-news", params=params, timeout=15)
        response.raise_for_status()
        news_items = response.json()

        if not isinstance(news_items, list):
            logger.warning(f"Finnhub 뉴스 API('{symbol}')에서 예상치 못한 형식의 응답을 받았습니다: {news_items}")
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

    except requests.exceptions.RequestException as e:
        logger.error(f"Finnhub 뉴스 API('{symbol}') 요청 중 오류: {e}", exc_info=True)
        return None
    except (ValueError, KeyError) as e:
        response_text = response.text if 'response' in locals() else 'N/A'
        logger.error(f"Finnhub 뉴스 API('{symbol}') 응답 파싱 중 오류: {e}. 응답: {response_text}", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"Finnhub 뉴스 API('{symbol}') 처리 중 예기치 않은 오류: {e}", exc_info=True)
        return None

# -*- coding: utf-8 -*-
import asyncio
import requests
import config
from logger_config import logger
from .. import http

def _format_krx_price_data(stock_info: dict) -> str:
    """KRX 주식 가격 데이터를 LLM 친화적인 문자열로 포맷팅합니다."""
    name = stock_info.get('name', 'N/A')
    price = stock_info.get('price', 0)
    change_value = stock_info.get('change_value', 0)

    change_str = "변동 없음"
    if change_value > 0:
        change_str = f"{change_value:,}원 상승"
    elif change_value < 0:
        change_str = f"{abs(change_value):,}원 하락"

    return f"종목 '{name}'의 현재가는 {price:,}원이며, 전일 대비 {change_str}했습니다."

async def get_stock_price(stock_name: str) -> str | None:
    """
    공공데이터포털(KRX) API로 주식 정보를 조회하고, LLM 친화적인 문자열로 반환합니다.
    [수정] 반환 형식을 dict에서 str으로 변경하여 토큰 사용량을 최적화합니다.
    """
    if not config.GO_DATA_API_KEY_KR or config.GO_DATA_API_KEY_KR == 'YOUR_GO_DATA_API_KEY_KR':
        logger.error("공공데이터포털 API 키(GO_DATA_API_KEY_KR)가 설정되지 않았습니다.")
        return f"{stock_name} 주식 정보를 조회할 수 없습니다 (API 키 미설정)."

    params = {"serviceKey": config.GO_DATA_API_KEY_KR, "itmsNm": stock_name, "resultType": "json", "numOfRows": "1"}
    log_params = params.copy()
    log_params["serviceKey"] = "[REDACTED]"
    logger.info(f"KRX API 요청: URL='{config.KRX_BASE_URL}', Params='{log_params}'")

    try:
        session = http.get_modern_tls_session()
        response = await asyncio.to_thread(session.get, config.KRX_BASE_URL, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        logger.debug(f"KRX API 응답 수신: {data}")

        items = data.get('response', {}).get('body', {}).get('items', {}).get('item', [])
        if not items:
            logger.warning(f"KRX API에서 '{stock_name}' 주식 정보를 찾지 못했습니다. 응답: {data}")
            return f"'{stock_name}'에 대한 주식 정보를 찾을 수 없습니다."

        stock_info_raw = items[0] if isinstance(items, list) else items
        stock_info = {
            "name": stock_info_raw.get('itmsNm'),
            "price": int(stock_info_raw.get('clpr', '0')),
            "change_value": int(stock_info_raw.get('vs', '0')),
        }
        return _format_krx_price_data(stock_info)

    except requests.exceptions.RequestException as e:
        logger.error(f"KRX API('{stock_name}') 요청 중 오류: {e}", exc_info=True)
        return "주식 정보 조회 중 네트워크 오류가 발생했습니다."
    except (KeyError, TypeError, ValueError) as e:
        response_text = data if 'data' in locals() else 'N/A'
        logger.error(f"KRX API('{stock_name}') 응답 파싱 중 오류: {e}. 응답: {response_text}", exc_info=True)
        return "주식 정보 조회 중 데이터 처리 오류가 발생했습니다."
    except Exception as e:
        logger.error(f"KRX API('{stock_name}') 처리 중 예기치 않은 오류: {e}", exc_info=True)
        return "주식 정보 조회 중 알 수 없는 오류가 발생했습니다."

# -*- coding: utf-8 -*-
import asyncio
import requests
import config
from logger_config import logger

async def get_stock_price(stock_name: str) -> dict | None:
    """
    공공데이터포털(KRX) API를 사용하여 국내 주식 시세 정보를 조회합니다.
    [수정] 오류 발생 시 None을 반환하여 안정성을 높입니다.
    """
    if not config.GO_DATA_API_KEY_KR or config.GO_DATA_API_KEY_KR == 'YOUR_GO_DATA_API_KEY_KR':
        logger.error("공공데이터포털 API 키(GO_DATA_API_KEY_KR)가 설정되지 않았습니다.")
        return None

    url = "https://apis.data.go.kr/1160100/service/GetStockSecuritiesInfoService/getStockPriceInfo"
    params = {
        "serviceKey": config.GO_DATA_API_KEY_KR,
        "itmsNm": stock_name,
        "resultType": "json",
        "numOfRows": "1"
    }

    log_params = params.copy()
    log_params["serviceKey"] = "[REDACTED]"
    logger.info(f"KRX API 요청: URL='{url}', Params='{log_params}'")

    try:
        response = await asyncio.to_thread(requests.get, url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        logger.debug(f"KRX API 응답 수신: {data}")

        items = data.get('response', {}).get('body', {}).get('items', {}).get('item', [])
        if not items:
            logger.warning(f"KRX API에서 '{stock_name}' 주식 정보를 찾지 못했습니다. 응답: {data}")
            return None

        stock_info = items[0] if isinstance(items, list) else items

        return {
            "name": stock_info.get('itmsNm'),
            "price": int(stock_info.get('clpr', '0')),
            "change_value": int(stock_info.get('vs', '0')),
            "change_rate": float(stock_info.get('fltRt', '0.0'))
        }
    except requests.exceptions.RequestException as e:
        logger.error(f"KRX API('{stock_name}') 요청 중 오류: {e}", exc_info=True)
        return None
    except (KeyError, TypeError, ValueError) as e:
        response_text = data if 'data' in locals() else 'N/A'
        logger.error(f"KRX API('{stock_name}') 응답 파싱 중 오류: {e}. 응답: {response_text}", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"KRX API('{stock_name}') 처리 중 예기치 않은 오류: {e}", exc_info=True)
        return None

# -*- coding: utf-8 -*-
import asyncio
import requests
import config
from logger_config import logger

async def get_stock_price(stock_name: str) -> dict:
    """
    공공데이터포털(KRX) API를 사용하여 국내 주식 시세 정보를 조회합니다.
    https://www.data.go.kr/data/15094808/openapi.do
    """
    if not config.GO_DATA_API_KEY_KR or config.GO_DATA_API_KEY_KR == 'YOUR_GO_DATA_API_KEY_KR':
        logger.error("공공데이터포털 API 키(GO_DATA_API_KEY_KR)가 설정되지 않았습니다.")
        return {"error": "API 키가 설정되지 않았습니다."}

    url = "https://apis.data.go.kr/1160100/service/GetStockSecuritiesInfoService/getStockPriceInfo"
    params = {
        "serviceKey": config.GO_DATA_API_KEY_KR,
        "itmsNm": stock_name,
        "resultType": "json",
        "numOfRows": "1" # 가장 정확한 1개만 받도록 설정
    }

    try:
        response = await asyncio.to_thread(requests.get, url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        items = data.get('response', {}).get('body', {}).get('items', {}).get('item', [])
        if not items:
            logger.warning(f"KRX API에서 '{stock_name}' 주식 정보를 찾지 못했습니다.")
            return {"error": f"'{stock_name}' 주식 정보를 찾을 수 없습니다."}

        # item이 리스트가 아닌 단일 dict로 올 경우를 대비
        if isinstance(items, dict):
            stock_info = items
        else:
            stock_info = items[0]

        return {
            "name": stock_info.get('itmsNm'),
            "price": int(stock_info.get('clpr', '0')),
            "change_value": int(stock_info.get('vs', '0')),
            "change_rate": float(stock_info.get('fltRt', '0.0'))
        }

    except requests.exceptions.Timeout:
        logger.error("KRX API 요청 시간 초과.")
        return {"error": "API 요청 시간 초과"}
    except requests.exceptions.HTTPError as e:
        logger.error(f"KRX API HTTP 오류: {e.response.status_code}")
        return {"error": f"API 서버 오류 ({e.response.status_code})"}
    except (requests.exceptions.RequestException, ValueError) as e:
        logger.error(f"KRX API 처리 중 오류: {e}", exc_info=True)
        return {"error": "API 요청 또는 데이터 처리 중 오류 발생"}

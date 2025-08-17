# -*- coding: utf-8 -*-
import asyncio
import requests
from datetime import datetime
import config
from logger_config import logger

async def get_exchange_rate(target_currency: str = "USD") -> dict:
    """
    한국수출입은행 API를 사용하여 원화(KRW) 대비 특정 통화의 환율을 조회합니다.
    https://www.koreaexim.go.kr/ir/HPHKIR019M01
    """
    if not config.EXIM_API_KEY_KR or config.EXIM_API_KEY_KR == 'YOUR_EXIM_API_KEY_KR':
        logger.error("한국수출입은행 API 키(EXIM_API_KEY_KR)가 설정되지 않았습니다.")
        return {"error": "API 키가 설정되지 않았습니다."}

    search_date = datetime.now().strftime('%Y%m%d')
    url = f"https://www.koreaexim.go.kr/site/program/financial/exchangeJSON?authkey={config.EXIM_API_KEY_KR}&searchdate={search_date}&data=AP01"

    try:
        response = await asyncio.to_thread(requests.get, url, timeout=10)
        response.raise_for_status()
        data = response.json()

        if not data:
            logger.warning(f"수출입은행 API에서 {search_date} 날짜의 환율 데이터를 받지 못했습니다.")
            return {"error": "데이터를 찾을 수 없습니다."}

        for rate_info in data:
            if rate_info.get('cur_unit') == target_currency.upper():
                return {
                    "currency_code": rate_info.get('cur_unit'),
                    "currency_name": rate_info.get('cur_nm'),
                    "rate": float(rate_info.get('deal_bas_r', '0').replace(',', ''))
                }

        logger.warning(f"수출입은행 API 응답에서 '{target_currency}' 통화를 찾지 못했습니다.")
        return {"error": f"'{target_currency}' 통화를 찾을 수 없습니다."}

    except requests.exceptions.Timeout:
        logger.error("수출입은행 API 요청 시간 초과.")
        return {"error": "API 요청 시간 초과"}
    except requests.exceptions.HTTPError as e:
        logger.error(f"수출입은행 API HTTP 오류: {e.response.status_code}")
        return {"error": f"API 서버 오류 ({e.response.status_code})"}
    except (requests.exceptions.RequestException, ValueError) as e:
        logger.error(f"수출입은행 API 처리 중 오류: {e}", exc_info=True)
        return {"error": "API 요청 또는 데이터 처리 중 오류 발생"}

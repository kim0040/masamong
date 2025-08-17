# -*- coding: utf-8 -*-
from datetime import datetime, timedelta
import config
from logger_config import logger
from .. import http as http_utils

BASE_URL = "https://www.koreaexim.go.kr/site/program/financial/exchangeJSON"

async def _fetch_exim_data(data_param: str, date_str: str) -> list | dict:
    """한국수출입은행 API에서 특정 날짜의 데이터를 가져오는 내부 헬퍼 함수."""
    if not config.EXIM_API_KEY_KR or config.EXIM_API_KEY_KR == 'YOUR_EXIM_API_KEY_KR':
        logger.error("한국수출입은행 API 키(EXIM_API_KEY_KR)가 설정되지 않았습니다.")
        return {"error": "API 키가 설정되지 않았습니다."}

    params = {
        "authkey": config.EXIM_API_KEY_KR,
        "searchdate": date_str,
        "data": data_param
    }

    # 해당 API는 레거시 SSL 핸드셰이크가 필요할 수 있음
    data = await http_utils.make_async_request(BASE_URL, params=params, use_legacy_ssl=True)

    if data is None: # API가 비어있는 응답(204 No Content 등)을 줄 경우
        logger.warning(f"수출입은행 API({data_param})에서 {date_str} 날짜의 데이터를 받지 못했습니다. (빈 응답)")
        return [] # 오류가 아닌 빈 리스트 반환

    if isinstance(data, dict) and data.get("error"):
        logger.error(f"수출입은행 API({data_param}) 처리 중 오류: {data}")
        return data

    return data

async def get_exchange_rate(target_currency: str = "USD") -> dict:
    """환율 정보를 조회합니다 (data=AP01). 주말/공휴일일 경우 이전 영업일 데이터로 재시도합니다."""
    today = datetime.now()
    # 최대 7일까지 과거 데이터를 조회 시도
    for i in range(7):
        search_date = today - timedelta(days=i)
        date_str = search_date.strftime('%Y%m%d')

        logger.info(f"수출입은행 환율 정보 조회 시도: {date_str} (대상: {target_currency})")
        data = await _fetch_exim_data("AP01", date_str)

        if isinstance(data, dict) and "error" in data:
            # 심각한 오류(예: API 키 문제)는 즉시 반환
            return data

        if not data: # 빈 리스트는 해당 날짜에 데이터가 없음을 의미
            logger.info(f"{date_str} 날짜에 환율 데이터가 없습니다. 이전 날짜로 재시도합니다.")
            continue

        for rate_info in data:
            if rate_info.get('cur_unit') == target_currency.upper():
                logger.info(f"성공적으로 {date_str} 날짜의 {target_currency} 환율 정보를 찾았습니다.")
                return {
                    "currency_code": rate_info.get('cur_unit'),
                    "currency_name": rate_info.get('cur_nm'),
                    "rate": float(rate_info.get('deal_bas_r', '0').replace(',', '')),
                    "search_date": date_str
                }

        # 데이터는 있지만 원하는 통화가 없는 경우, 계속 이전 날짜를 찾아봄
        logger.warning(f"{date_str} 데이터에서 '{target_currency}' 통화를 찾지 못했습니다.")

    logger.error(f"지난 7일간 '{target_currency}' 통화의 환율 정보를 찾지 못했습니다.")
    return {"error": f"'{target_currency}' 통화 정보를 찾을 수 없습니다."}


async def get_loan_interest_rates() -> dict:
    """대출 금리 정보를 조회합니다 (data=AP02 - 가정)."""
    date_str = datetime.now().strftime('%Y%m%d')
    data = await _fetch_exim_data("AP02", date_str)
    if not data or isinstance(data, dict) and "error" in data:
        return data if isinstance(data, dict) else {"error": "데이터를 찾을 수 없습니다."}

    formatted_rates = [
        {
            "rate_name": item.get("rate_name", "N/A"),
            "interest_rate": item.get("interest_rate", "N/A")
        }
        for item in data
    ]
    return {"loan_rates": formatted_rates}

async def get_international_interest_rates() -> dict:
    """국제 금리 정보를 조회합니다 (data=AP03 - 가정)."""
    date_str = datetime.now().strftime('%Y%m%d')
    data = await _fetch_exim_data("AP03", date_str)
    if not data or isinstance(data, dict) and "error" in data:
        return data if isinstance(data, dict) else {"error": "데이터를 찾을 수 없습니다."}

    formatted_rates = [
        {
            "country": item.get("country", "N/A"),
            "rate_type": item.get("rate_type", "N/A"),
            "interest_rate": item.get("interest_rate", "N/A")
        }
        for item in data
    ]
    return {"international_rates": formatted_rates}

# -*- coding: utf-8 -*-
import asyncio
import requests
from datetime import datetime
import config
from logger_config import logger
from .. import http

async def _fetch_exim_data(data_param: str) -> list | dict:
    """한국수출입은행 API에서 데이터를 가져오는 내부 헬퍼 함수."""
    if not config.EXIM_API_KEY_KR or config.EXIM_API_KEY_KR == 'YOUR_EXIM_API_KEY_KR':
        logger.error("한국수출입은행 API 키(EXIM_API_KEY_KR)가 설정되지 않았습니다.")
        return {"error": "API 키가 설정되지 않았습니다."}

    search_date = datetime.now().strftime('%Y%m%d')
    params = {
        "authkey": config.EXIM_API_KEY_KR,
        "searchdate": search_date,
        "data": data_param
    }

    # 보안을 위해 API 키는 로그에서 제외
    log_params = params.copy()
    log_params["authkey"] = "[REDACTED]"
    logger.info(f"수출입은행 API 요청: URL='{config.EXIM_BASE_URL}', Params='{log_params}'")

    try:
        # 한국수출입은행 API는 SSL 인증서 문제가 있을 수 있으므로 인증되지 않은 세션 사용
        session = http.get_insecure_session()
        response = await asyncio.to_thread(session.get, config.EXIM_BASE_URL, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        logger.debug(f"수출입은행 API 응답 수신 ({data_param}): {data}")

        if not data:
            logger.warning(f"수출입은행 API({data_param})에서 {search_date} 날짜의 데이터를 받지 못했습니다.")
            return {"error": "데이터를 찾을 수 없습니다."}
        return data
    except requests.exceptions.RequestException as e:
        logger.error(f"수출입은행 API({data_param}) 처리 중 오류: {e}", exc_info=True)
        return {"error": "API 요청 또는 데이터 처리 중 오류 발생"}
    except (KeyError, TypeError, ValueError) as e:
        logger.error(f"수출입은행 API 응답 파싱 중 오류: {e}. 응답 데이터: {response.text}", exc_info=True)
        return {"error": "API 응답 데이터 파싱 중 오류 발생"}


async def get_krw_exchange_rate(target_currency: str = "USD") -> str:
    """환율 정보를 조회합니다 (data=AP01)."""
    data = await _fetch_exim_data("AP01")
    if isinstance(data, dict) and "error" in data:
        return data.get("error", "환율 정보를 가져오는 중 오류가 발생했습니다.")

    for rate_info in data:
        if rate_info.get('cur_unit') == target_currency.upper():
            currency_name = rate_info.get('cur_nm')
            rate = float(rate_info.get('deal_bas_r', '0').replace(',', ''))
            return f"💰 {target_currency.upper()} → KRW: {rate:,.2f}원 ({currency_name})"

    logger.warning(f"수출입은행 환율 API 응답에서 '{target_currency}' 통화를 찾지 못했습니다.")
    return f"❌ '{target_currency}' 통화를 찾을 수 없습니다."

async def get_raw_exchange_rate(target_currency: str = "USD") -> float | None:
    """
    환율 정보를 조회하여 숫자(float) 값으로 반환합니다.
    계산기 등 다른 도구에서 사용하기 위한 내부용 함수입니다.
    """
    data = await _fetch_exim_data("AP01")
    if isinstance(data, dict) and "error" in data:
        return None

    for rate_info in data:
        if rate_info.get('cur_unit') == target_currency.upper():
            try:
                return float(rate_info.get('deal_bas_r', '0').replace(',', ''))
            except (ValueError, TypeError):
                logger.error(f"수출입은행 환율 값 파싱 실패: {rate_info.get('deal_bas_r')}")
                return None

    logger.warning(f"수출입은행 환율 API 응답에서 '{target_currency}' 통화를 찾지 못했습니다.")
    return None

async def get_loan_rates() -> str:
    """대출 금리 정보를 조회합니다 (data=AP02 - 가정)."""
    data = await _fetch_exim_data("AP02")
    if isinstance(data, dict) and "error" in data:
        return data.get("error", "대출 금리 정보를 가져오는 중 오류가 발생했습니다.")

    if not data:
        return "❌ 대출 금리 정보를 찾을 수 없습니다."
    
    rate_strings = [
        f"• {item.get('rate_name', 'N/A')}: {item.get('interest_rate', 'N/A')}%"
        for item in data
    ]
    return f"🏦 **대출 금리 정보**\n" + "\n".join(rate_strings)

async def get_international_rates() -> str:
    """국제 금리 정보를 조회합니다 (data=AP03 - 가정)."""
    data = await _fetch_exim_data("AP03")
    if isinstance(data, dict) and "error" in data:
        return data.get("error", "국제 금리 정보를 가져오는 중 오류가 발생했습니다.")

    if not data:
        return "❌ 국제 금리 정보를 찾을 수 없습니다."
    
    rate_strings = [
        f"• {item.get('country', 'N/A')} ({item.get('rate_type', 'N/A')}): {item.get('interest_rate', 'N/A')}%"
        for item in data
    ]
    return f"🌍 **국제 금리 정보**\n" + "\n".join(rate_strings)

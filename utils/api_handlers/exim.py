# -*- coding: utf-8 -*-
import asyncio
import requests
from datetime import datetime
import config
from logger_config import logger
from .. import http

BASE_URL = "https://www.koreaexim.go.kr/site/program/financial/exchangeJSON"

async def _fetch_exim_data(data_param: str) -> list:
    """
    한국수출입은행 API에서 데이터를 가져오는 내부 헬퍼 함수.
    [수정] 오류 발생 시 빈 리스트 `[]`를 반환하여 안정성을 높입니다.
    """
    if not config.EXIM_API_KEY_KR or config.EXIM_API_KEY_KR == 'YOUR_EXIM_API_KEY_KR':
        logger.error("한국수출입은행 API 키(EXIM_API_KEY_KR)가 설정되지 않았습니다.")
        return []

    search_date = datetime.now().strftime('%Y%m%d')
    params = {
        "authkey": config.EXIM_API_KEY_KR,
        "searchdate": search_date,
        "data": data_param
    }

    log_params = params.copy()
    log_params["authkey"] = "[REDACTED]"
    logger.info(f"수출입은행 API 요청: URL='{BASE_URL}', Params='{log_params}'")

    try:
        session = http.get_modern_tls_session()
        response = await asyncio.to_thread(session.get, BASE_URL, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        if not data:
            logger.warning(f"수출입은행 API({data_param})에서 {search_date} 날짜의 데이터를 받지 못했습니다.")
            return []

        logger.debug(f"수출입은행 API 응답 수신 ({data_param}): {data}")
        return data
    except requests.exceptions.RequestException as e:
        logger.error(f"수출입은행 API({data_param}) 요청 중 오류: {e}", exc_info=True)
        return []
    except (KeyError, TypeError, ValueError) as e:
        # response 변수가 예외 발생 시 정의되지 않았을 수 있으므로 확인
        response_text = response.text if 'response' in locals() else 'N/A'
        logger.error(f"수출입은행 API 응답 파싱 중 오류: {e}. 응답 데이터: {response_text}", exc_info=True)
        return []
    except Exception as e:
        logger.error(f"수출입은행 API 처리 중 예기치 않은 오류({data_param}): {e}", exc_info=True)
        return []


def _format_exchange_rate_data(rate_info: dict) -> str:
    """환율 정보를 LLM 친화적인 문자열로 포맷팅합니다."""
    name = rate_info.get('currency_name', 'N/A')
    code = rate_info.get('currency_code', 'N/A')
    tts = rate_info.get('tts', 'N/A')
    ttb = rate_info.get('ttb', 'N/A')
    deal_basis = rate_info.get('deal_bas_r', 'N/A')
    return (f"{name}({code}) 환율 정보: 매매기준율은 {deal_basis}원입니다. "
            f"실제 송금받을 때(TTB)는 {ttb}원, 보낼 때(TTS)는 {tts}원입니다.")

def _format_loan_rates_data(loan_rates: list) -> str:
    """대출 금리 정보를 LLM 친화적인 문자열로 포맷팅합니다."""
    if not loan_rates:
        return "현재 조회 가능한 대출 금리 정보가 없습니다."
    lines = [f"- {rate.get('rate_name', 'N/A')}: {rate.get('interest_rate', 'N/A')}" for rate in loan_rates]
    return "수출입은행 대출 금리 정보:\n" + "\n".join(lines)

def _format_international_rates_data(intl_rates: list) -> str:
    """국제 금리 정보를 LLM 친화적인 문자열로 포맷팅합니다."""
    if not intl_rates:
        return "현재 조회 가능한 국제 금리 정보가 없습니다."
    lines = [f"- {rate.get('country', 'N/A')} ({rate.get('rate_type', 'N/A')}): {rate.get('interest_rate', 'N/A')}" for rate in intl_rates]
    return "주요 국제 금리 정보:\n" + "\n".join(lines)

async def get_krw_exchange_rate(currency_code: str = "USD") -> str:
    """
    환율 정보를 조회하여 LLM 친화적인 문자열로 반환합니다.
    [수정] 반환 형식을 dict에서 str으로 변경하여 토큰 사용량을 최적화합니다.
    """
    data = await _fetch_exim_data("AP01")
    if not data:
        return "환율 정보를 가져오는 데 실패했습니다."

    for rate_info_raw in data:
        if rate_info_raw.get('cur_unit') == currency_code.upper():
            rate_info = {
                "currency_code": rate_info_raw.get('cur_unit'),
                "currency_name": rate_info_raw.get('cur_nm'),
                "ttb": rate_info_raw.get('ttb', '0'),
                "tts": rate_info_raw.get('tts', '0'),
                "deal_bas_r": rate_info_raw.get('deal_bas_r', '0'),
            }
            return _format_exchange_rate_data(rate_info)

    return f"'{currency_code}' 통화에 대한 환율 정보를 찾을 수 없습니다."

async def get_loan_rates() -> str:
    """
    대출 금리 정보를 조회하여 LLM 친화적인 문자열로 반환합니다.
    [수정] 반환 형식을 dict에서 str으로 변경하여 토큰 사용량을 최적화합니다.
    """
    data = await _fetch_exim_data("AP02")
    return _format_loan_rates_data(data)

async def get_international_rates() -> str:
    """
    국제 금리 정보를 조회하여 LLM 친화적인 문자열로 반환합니다.
    [수정] 반환 형식을 dict에서 str으로 변경하여 토큰 사용량을 최적화합니다.
    """
    data = await _fetch_exim_data("AP03")
    return _format_international_rates_data(data)

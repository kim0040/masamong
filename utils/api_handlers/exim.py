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


async def get_exchange_rate(target_currency: str = "USD") -> dict | None:
    """
    환율 정보를 조회합니다 (data=AP01).
    [수정] API 호출 실패 또는 통화 부재 시 None을 반환합니다.
    """
    data = await _fetch_exim_data("AP01")
    if not data:
        logger.warning("수출입은행 환율 정보 조회 실패 (API로부터 데이터 없음).")
        return None

    for rate_info in data:
        if rate_info.get('cur_unit') == target_currency.upper():
            try:
                return {
                    "currency_code": rate_info.get('cur_unit'),
                    "currency_name": rate_info.get('cur_nm'),
                    "ttb": rate_info.get('ttb', '0'), # TTB: Telegraphic Transfer Buying Rate (송금 받을때)
                    "tts": rate_info.get('tts', '0'), # TTS: Telegraphic Transfer Selling Rate (송금 보낼때)
                    "deal_bas_r": rate_info.get('deal_bas_r', '0'), # 매매 기준율
                    "tc_b": rate_info.get('tc_b', '0'), # T/C 살때
                    "fc_s": rate_info.get('fc_s', '0'), # 현찰 팔때
                }
            except (TypeError, ValueError) as e:
                logger.error(f"환율 데이터 파싱 중 오류: {e}, 데이터: {rate_info}", exc_info=True)
                return None


    logger.warning(f"수출입은행 환율 API 응답에서 '{target_currency}' 통화를 찾지 못했습니다.")
    return None

async def get_loan_interest_rates() -> dict:
    """
    대출 금리 정보를 조회합니다 (data=AP02 - 가정).
    [수정] 오류 발생 시 빈 리스트를 포함한 딕셔너리를 반환합니다.
    """
    data = await _fetch_exim_data("AP02")

    formatted_rates = [
        {
            "rate_name": item.get("rate_name", "N/A"),
            "interest_rate": item.get("interest_rate", "N/A")
        }
        for item in data
    ]
    return {"loan_rates": formatted_rates}

async def get_international_interest_rates() -> dict:
    """
    국제 금리 정보를 조회합니다 (data=AP03 - 가정).
    [수정] 오류 발생 시 빈 리스트를 포함한 딕셔너리를 반환합니다.
    """
    data = await _fetch_exim_data("AP03")

    formatted_rates = [
        {
            "country": item.get("country", "N/A"),
            "rate_type": item.get("rate_type", "N/A"),
            "interest_rate": item.get("interest_rate", "N/A")
        }
        for item in data
    ]
    return {"international_rates": formatted_rates}

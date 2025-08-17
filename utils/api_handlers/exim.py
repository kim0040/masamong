# -*- coding: utf-8 -*-
import asyncio
import requests
from datetime import datetime
import config
from logger_config import logger

BASE_URL = "https://www.koreaexim.go.kr/site/program/financial/exchangeJSON"

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

    for attempt in range(3): # 최대 3번 재시도
        try:
            response = await asyncio.to_thread(requests.get, BASE_URL, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            break # 성공 시 루프 탈출
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            logger.warning(f"수출입은행 API 연결 오류 (시도 {attempt + 1}/3): {e}")
            if attempt == 2: # 마지막 시도 실패 시 에러 반환
                return {"error": "API 서버에 연결할 수 없습니다."}
            await asyncio.sleep(1) # 1초 후 재시도
        except requests.exceptions.SSLError as e:
            logger.error(f"수출입은행 API SSL 오류: {e}", exc_info=True)
            return {"error": "API 서버와 보안 연결(SSL)에 실패했습니다."}
        except requests.exceptions.HTTPError as e:
            logger.error(f"수출입은행 API({data_param}) HTTP 오류: {e.response.status_code}")
            return {"error": f"API 서버 오류 ({e.response.status_code})"}
        except ValueError as e: # JSONDecodeError
            logger.error(f"수출입은행 API({data_param}) JSON 파싱 오류: {e}", exc_info=True)
            return {"error": "API 응답 데이터 처리 중 오류 발생"}
    else: # 루프가 break 없이 끝났을 경우 (모든 재시도 실패)
        return {"error": "API 서버에 여러 번 연결 시도했으나 실패했습니다."}

        if not data:
            logger.warning(f"수출입은행 API({data_param})에서 {search_date} 날짜의 데이터를 받지 못했습니다.")
            return {"error": "데이터를 찾을 수 없습니다."}
        return data


async def get_exchange_rate(target_currency: str = "USD") -> dict:
    """환율 정보를 조회합니다 (data=AP01)."""
    data = await _fetch_exim_data("AP01")
    if isinstance(data, dict) and "error" in data:
        return data

    for rate_info in data:
        if rate_info.get('cur_unit') == target_currency.upper():
            return {
                "currency_code": rate_info.get('cur_unit'),
                "currency_name": rate_info.get('cur_nm'),
                "rate": float(rate_info.get('deal_bas_r', '0').replace(',', ''))
            }

    logger.warning(f"수출입은행 환율 API 응답에서 '{target_currency}' 통화를 찾지 못했습니다.")
    return {"error": f"'{target_currency}' 통화를 찾을 수 없습니다."}

async def get_loan_interest_rates() -> dict:
    """대출 금리 정보를 조회합니다 (data=AP02 - 가정)."""
    data = await _fetch_exim_data("AP02")
    if isinstance(data, dict) and "error" in data:
        return data

    # API 응답 형식을 가정하여 파싱
    # 실제 필드명은 API 문서를 확인해야 함
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
    data = await _fetch_exim_data("AP03")
    if isinstance(data, dict) and "error" in data:
        return data

    # API 응답 형식을 가정하여 파싱
    # 실제 필드명은 API 문서를 확인해야 함
    formatted_rates = [
        {
            "country": item.get("country", "N/A"),
            "rate_type": item.get("rate_type", "N/A"),
            "interest_rate": item.get("interest_rate", "N/A")
        }
        for item in data
    ]
    return {"international_rates": formatted_rates}

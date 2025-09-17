# -*- coding: utf-8 -*-
import asyncio
import requests
import config
import re
from logger_config import logger
from .. import http
from datetime import datetime
from . import kakao # Import kakao handler

# KRX stock name normalization mapping (fast path for common stocks)
# Top 30 KR companies by market cap + common aliases
KR_ALIAS_TO_NAME = {
    "삼성전자": "삼성전자",
    "삼전": "삼성전자",
    "sk하이닉스": "SK하이닉스",
    "하이닉스": "SK하이닉스",
    "하닉": "SK하이닉스",
    "lg에너지솔루션": "LG에너지솔루션",
    "엔솔": "LG에너지솔루션",
    "삼성바이오로직스": "삼성바이오로직스",
    "삼바": "삼성바이오로직스",
    "한화에어로스페이스": "한화에어로스페이스",
    "삼성전자우": "삼성전자우",
    "kb금융": "KB금융",
    "hd현대중공업": "HD현대중공업",
    "현대차": "현대차",
    "현대자동차": "현대차",
    "기아": "기아",
    "두산에너빌리티": "두산에너빌리티",
    "셀트리온": "셀트리온",
    "네이버": "NAVER",
    "naver": "NAVER",
    "한화오션": "한화오션",
    "신한지주": "신한지주",
    "삼성물산": "삼성물산",
    "삼성생명": "삼성생명",
    "hd한국조선해양": "HD한국조선해양",
    "현대모비스": "현대모비스",
    "카카오": "카카오",
    "kakao": "카카오",
    "sk스퀘어": "SK스퀘어",
    "하나금융지주": "하나금융지주",
    "hmm": "HMM",
    "한국전력": "한국전력",
    "현대로템": "현대로템",
    "posco홀딩스": "POSCO홀딩스",
    "포스코홀딩스": "POSCO홀딩스",
    "메리츠금융지주": "메리츠금융지주",
    "hd현대일렉트릭": "HD현대일렉트릭",
    "삼성화재": "삼성화재",
    "고려아연": "고려아연",
}

def _format_krx_price_data(stock_info: dict) -> str:
    """KRX 주식 가격 데이터를 LLM 친화적인 문자열로 포맷팅합니다."""
    name = stock_info.get('name', 'N/A')
    price = stock_info.get('price', 0)
    change_value = stock_info.get('change_value', 0)

    if change_value > 0:
        change_str = f"+{change_value:,}"
    elif change_value < 0:
        change_str = f"{change_value:,}"
    else:
        change_str = "0"

    return f"{name}: {price:,}원 ({change_str})"

async def _search_for_full_name(alias: str) -> str | None:
    """Use Kakao web search to find the full company name for a stock alias."""
    if not alias:
        return None
    
    query = f'"{alias}" 주식 종목명'
    logger.info(f"KRX: Kakao 웹 검색으로 종목명 검색: {query}")
    search_results = await kakao.search_web(query, page_size=1)

    if search_results and search_results[0]:
        title = search_results[0].get('title', '')
        # Remove HTML tags
        title = re.sub('<[^<]+?>', '', title)
        
        # Heuristic to find a plausible name. Look for multi-character Korean words.
        # This is not perfect but can cover many cases.
        # e.g., "<b>삼성전자</b>(005930) : 네이버 증권" -> "삼성전자"
        candidates = re.findall(r'([가-힣]{2,})', title)
        if candidates:
            # Avoid common words that are not company names
            stop_words = ["종목", "증권", "뉴스", "주식", "정보"]
            for candidate in candidates:
                if candidate not in stop_words and candidate != alias:
                    logger.info(f"KRX: 검색된 종목명 후보: '{candidate}'")
                    return candidate
    
    logger.warning(f"KRX: Kakao 웹 검색으로 '{alias}'에 대한 종목명을 찾지 못했습니다.")
    return None


async def get_stock_price(stock_name: str) -> str | None:
    """
    공공데이터포털(KRX) API로 주식 정보를 조회하고, LLM 친화적인 문자열로 반환합니다.
    [수정] API 조회 실패 시 웹 검색을 통해 종목명을 찾아 재시도하는 기능 추가.
    """
    if not config.GO_DATA_API_KEY_KR or config.GO_DATA_API_KEY_KR == 'YOUR_GO_DATA_API_KEY_KR':
        logger.error("공공데이터포털 API 키(GO_DATA_API_KEY_KR)가 설정되지 않았습니다.")
        return f"주식 정보를 조회할 수 없습니다 (API 키 미설정)."

    # 1. Normalize from alias map (fast path)
    normalized_name = KR_ALIAS_TO_NAME.get(stock_name.lower().replace(" ", ""), stock_name)
    logger.info(f"KRX: Original name '{stock_name}' normalized to '{normalized_name}'")

    async def _get_price_from_krx(name_to_search: str) -> dict | None:
        """Internal function to fetch price from KRX API."""
        today_str = datetime.now().strftime('%Y%m%d')
        params = {"serviceKey": config.GO_DATA_API_KEY_KR, "itmsNm": name_to_search, "resultType": "json", "numOfRows": "1", "basDt": today_str}
        
        log_params = params.copy()
        log_params["serviceKey"] = "[REDACTED]"
        logger.info(f"KRX API 요청: URL='{config.KRX_BASE_URL}', Params='{log_params}'")

        # Use a session that forces TLSv1.2 for compatibility with data.go.kr
        session = http.get_tlsv12_session()
        response = await asyncio.to_thread(session.get, config.KRX_BASE_URL, params=params, timeout=10)
        response.raise_for_status()
        try:
            data = response.json()
        except requests.exceptions.JSONDecodeError:
            logger.error(f"KRX API가 유효한 JSON을 반환하지 않았습니다. 응답 내용: {response.text}")
            return None
        
        items = data.get('response', {}).get('body', {}).get('items', {}).get('item', [])
        if items:
            return items[0] if isinstance(items, list) else items
        return None

    try:
        # 2. First attempt with the normalized name
        stock_info_raw = await _get_price_from_krx(normalized_name)

        # 3. If first attempt fails, search via web and retry
        if not stock_info_raw:
            logger.warning(f"KRX API에서 '{normalized_name}' 정보를 찾지 못했습니다. 웹 검색을 시도합니다.")
            searched_name = await _search_for_full_name(stock_name)
            if searched_name and searched_name.lower() != normalized_name.lower():
                logger.info(f"KRX: 검색된 종목명 '{searched_name}'(으)로 재시도합니다.")
                stock_info_raw = await _get_price_from_krx(searched_name)
                normalized_name = searched_name # Update name for final output

        # 4. Process the final result
        if not stock_info_raw:
            logger.warning(f"KRX: 최종적으로 '{stock_name}'에 대한 정보를 찾지 못했습니다.")
            return f"'{stock_name}'에 대한 주식 정보를 찾을 수 없습니다. 이름이 정확한지 확인해주세요."

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
        logger.error(f"KRX API('{stock_name}') 응답 파싱 중 오류: {e}", exc_info=True)
        return "주식 정보 조회 중 데이터 처리 오류가 발생했습니다."
    except Exception as e:
        logger.error(f"KRX API('{stock_name}') 처리 중 예기치 않은 오류: {e}", exc_info=True)
        return "주식 정보 조회 중 알 수 없는 오류가 발생했습니다."
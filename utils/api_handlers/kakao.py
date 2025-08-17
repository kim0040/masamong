# -*- coding: utf-8 -*-
import asyncio
import requests
import config
from logger_config import logger

async def search_place_by_keyword(query: str) -> dict:
    """
    카카오 로컬 API를 사용하여 키워드로 장소를 검색합니다.
    https://developers.kakao.com/docs/latest/ko/local/dev-guide#search-by-keyword
    """
    if not config.KAKAO_API_KEY or config.KAKAO_API_KEY == 'YOUR_KAKAO_API_KEY':
        logger.error("카카오 API 키(KAKAO_API_KEY)가 설정되지 않았습니다.")
        return {"error": "API 키가 설정되지 않았습니다."}

    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {
        "Authorization": f"KakaoAK {config.KAKAO_API_KEY}"
    }
    params = {
        "query": query
    }

    try:
        response = await asyncio.to_thread(requests.get, url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        documents = data.get('documents', [])
        if not documents:
            logger.warning(f"카카오맵 API에서 '{query}'에 대한 검색 결과가 없습니다.")
            return {"error": f"'{query}'에 대한 장소를 찾을 수 없습니다."}

        # agent.md 명세에 따라 필요한 정보만 추출하여 반환
        # 여기서는 가장 첫 번째 결과만 사용
        place_info = documents[0]
        return {
            "place_name": place_info.get('place_name'),
            "road_address": place_info.get('road_address_name'),
            "phone": place_info.get('phone'),
            "place_url": place_info.get('place_url')
        }

    except requests.exceptions.Timeout:
        logger.error("카카오맵 API 요청 시간 초과.")
        return {"error": "API 요청 시간 초과"}
    except requests.exceptions.HTTPError as e:
        logger.error(f"카카오맵 API HTTP 오류: {e.response.status_code}")
        return {"error": f"API 서버 오류 ({e.response.status_code})"}
    except (requests.exceptions.RequestException, ValueError) as e:
        logger.error(f"카카오맵 API 처리 중 오류: {e}", exc_info=True)
        return {"error": "API 요청 또는 데이터 처리 중 오류 발생"}

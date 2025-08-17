# -*- coding: utf-8 -*-
import asyncio
import requests
import config
from logger_config import logger

async def search_place_by_keyword(query: str, page_size: int = 5) -> dict:
    """
    카카오 로컬 API를 사용하여 키워드로 장소를 검색하고, 상세 정보를 포함한 여러 결과를 반환합니다.
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
        "query": query,
        "size": page_size
    }

    try:
        response = await asyncio.to_thread(requests.get, url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        documents = data.get('documents', [])
        if not documents:
            logger.warning(f"카카오맵 API에서 '{query}'에 대한 검색 결과가 없습니다.")
            return {"error": f"'{query}'에 대한 장소를 찾을 수 없습니다.", "places": []}

        # 상세 정보를 포함하여 여러 결과 반환
        formatted_places = [
            {
                "place_name": place.get('place_name'),
                "category_name": place.get('category_name'),
                "road_address_name": place.get('road_address_name'),
                "phone": place.get('phone'),
                "place_url": place.get('place_url')
            }
            for place in documents
        ]
        return {"places": formatted_places}

    except requests.exceptions.Timeout:
        logger.error("카카오맵 API 요청 시간 초과.")
        return {"error": "API 요청 시간 초과"}
    except requests.exceptions.HTTPError as e:
        logger.error(f"카카오맵 API HTTP 오류: {e.response.status_code}")
        return {"error": f"API 서버 오류 ({e.response.status_code})"}
    except (requests.exceptions.RequestException, ValueError) as e:
        logger.error(f"카카오맵 API 처리 중 오류: {e}", exc_info=True)
        return {"error": "API 요청 또는 데이터 처리 중 오류 발생"}

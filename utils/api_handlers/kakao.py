# -*- coding: utf-8 -*-
import asyncio
import requests
import config
from logger_config import logger

async def search_place_by_keyword(query: str, page_size: int = 5) -> dict | None:
    """
    카카오 로컬 API를 사용하여 키워드로 장소를 검색합니다.
    [수정] 오류 발생 시 None을 반환하고, 결과가 없으면 빈 리스트를 포함한 딕셔너리를 반환합니다.
    """
    if not config.KAKAO_API_KEY or config.KAKAO_API_KEY == 'YOUR_KAKAO_API_KEY':
        logger.error("카카오 API 키(KAKAO_API_KEY)가 설정되지 않았습니다.")
        return None

    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {"Authorization": f"KakaoAK {config.KAKAO_API_KEY}"}
    params = {"query": query, "size": page_size}

    try:
        response = await asyncio.to_thread(requests.get, url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        documents = data.get('documents', [])
        if not documents:
            logger.warning(f"카카오맵 API에서 '{query}'에 대한 검색 결과가 없습니다.")
            return {"places": []} # 결과 없는 것은 오류가 아님

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

    except requests.exceptions.RequestException as e:
        logger.error(f"카카오맵 API('{query}') 요청 중 오류: {e}", exc_info=True)
        return None
    except (ValueError, KeyError) as e:
        response_text = response.text if 'response' in locals() else 'N/A'
        logger.error(f"카카오맵 API('{query}') 응답 파싱 중 오류: {e}. 응답: {response_text}", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"카카오맵 API('{query}') 처리 중 예기치 않은 오류: {e}", exc_info=True)
        return None

# -*- coding: utf-8 -*-
import asyncio
import requests
import config
from logger_config import logger

def _format_places_data(query: str, places: list) -> str:
    """장소 검색 결과를 LLM 친화적인 문자열로 포맷팅합니다."""
    if not places:
        return f"'{query}'에 대한 장소를 찾을 수 없습니다."

    lines = [f"- {place.get('place_name', 'N/A')} ({place.get('category_name', 'N/A')}, {place.get('road_address_name', '주소 없음')})" for place in places]
    return f"'{query}' 주변 장소 검색 결과:\n" + "\n".join(lines)

async def search_place_by_keyword(query: str, page_size: int = 5) -> str:
    """
    카카오 로컬 API로 장소를 검색하고, LLM 친화적인 문자열로 반환합니다.
    [수정] 반환 형식을 dict에서 str으로 변경하여 토큰 사용량을 최적화합니다.
    """
    if not config.KAKAO_API_KEY or config.KAKAO_API_KEY == 'YOUR_KAKAO_API_KEY':
        logger.error("카카오 API 키(KAKAO_API_KEY)가 설정되지 않았습니다.")
        return f"장소 '{query}'을(를) 검색할 수 없습니다 (API 키 미설정)."

    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {"Authorization": f"KakaoAK {config.KAKAO_API_KEY}"}
    params = {"query": query, "size": page_size}

    try:
        response = await asyncio.to_thread(requests.get, url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        documents = data.get('documents', [])
        return _format_places_data(query, documents)

    except requests.exceptions.RequestException as e:
        logger.error(f"카카오맵 API('{query}') 요청 중 오류: {e}", exc_info=True)
        return "장소 검색 중 네트워크 오류가 발생했습니다."
    except (ValueError, KeyError) as e:
        response_text = response.text if 'response' in locals() else 'N/A'
        logger.error(f"카카오맵 API('{query}') 응답 파싱 중 오류: {e}. 응답: {response_text}", exc_info=True)
        return "장소 검색 중 데이터 처리 오류가 발생했습니다."
    except Exception as e:
        logger.error(f"카카오맵 API('{query}') 처리 중 예기치 않은 오류: {e}", exc_info=True)
        return "장소 검색 중 알 수 없는 오류가 발생했습니다."

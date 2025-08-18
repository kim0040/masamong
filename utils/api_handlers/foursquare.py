# -*- coding: utf-8 -*-
import asyncio
import requests
import config
from logger_config import logger
from .. import http

async def get_places_by_coords(lat: float, lon: float, query: str = None, limit: int = 10) -> dict:
    """
    Foursquare API를 사용하여 특정 좌표 주변의 장소(POI)를 검색합니다.
    """
    if not config.FOURSQUARE_API_KEY or config.FOURSQUARE_API_KEY == 'YOUR_FOURSQUARE_API_KEY':
        logger.error("Foursquare API 키(FOURSQUARE_API_KEY)가 설정되지 않았습니다.")
        return {"error": "장소 정보를 조회할 수 없습니다 (API 키 미설정)."}

    url = f"{config.FOURSQUARE_BASE_URL}/search"
    headers = {
        "Authorization": config.FOURSQUARE_API_KEY,
        "Accept": "application/json"
    }
    params = {
        "ll": f"{lat},{lon}",
        "limit": limit,
        "sort": "RELEVANCE"
    }
    if query:
        params["query"] = query

    logger.info(f"Foursquare API 요청: URL='{url}', Params='{params}'")

    try:
        session = http.get_modern_tls_session()
        response = await asyncio.to_thread(session.get, url, headers=headers, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        logger.debug(f"Foursquare API 응답 수신: {data}")

        # 필요한 정보만 추출하여 리스트로 가공
        places = data.get('results', [])
        formatted_places = [
            {
                "name": place.get('name'),
                "address": place.get('location', {}).get('formatted_address'),
                "categories": [cat.get('name') for cat in place.get('categories', [])],
            }
            for place in places
        ]
        return {"places": formatted_places}

    except requests.exceptions.RequestException as e:
        logger.error(f"Foursquare API({lat},{lon}) 요청 중 오류: {e}", exc_info=True)
        return {"error": "주변 장소 검색 중 네트워크 오류가 발생했습니다."}
    except (ValueError, KeyError, IndexError) as e:
        response_text = response.text if 'response' in locals() else 'N/A'
        logger.error(f"Foursquare API({lat},{lon}) 응답 파싱 중 오류: {e}. 응답: {response_text}", exc_info=True)
        return {"error": "주변 장소 검색 중 데이터 처리 오류가 발생했습니다."}
    except Exception as e:
        logger.error(f"Foursquare API({lat},{lon}) 처리 중 예기치 않은 오류: {e}", exc_info=True)
        return {"error": "주변 장소 검색 중 알 수 없는 오류가 발생했습니다."}

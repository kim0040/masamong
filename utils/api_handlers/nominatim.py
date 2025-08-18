# -*- coding: utf-8 -*-
import asyncio
import requests
import config
from logger_config import logger
from .. import http

# This will be used for caching
_geocode_cache = {}

async def geocode_location(query: str) -> dict:
    """
    Nominatim API를 사용하여 위치 정보를 검색하고, 결과를 파싱하여 반환합니다.
    - 캐싱 및 Rate-limiting 정책을 준수합니다.
    - 결과에 따라 명확한 데이터, 명확화가 필요한 후보 목록, 또는 에러를 반환합니다.
    """
    # 캐시 확인
    if query in _geocode_cache:
        logger.info(f"Geocoding cache HIT for query: '{query}'")
        return _geocode_cache[query]

    logger.info(f"Geocoding cache MISS for query: '{query}'")

    url = f"{config.NOMINATIM_BASE_URL}/search"
    headers = {
        "User-Agent": "MasamongBot/1.0 (Discord Bot for location-based services; contact: a.developer@example.com)"
    }
    params = {
        "q": query,
        "format": "json",
        "addressdetails": 1,
        "limit": 5
    }

    api_response_data = None
    try:
        session = http.get_http_session()
        response = await asyncio.to_thread(session.get, url, headers=headers, params=params, timeout=15)
        await asyncio.sleep(1.1)
        response.raise_for_status()
        api_response_data = response.json()
        logger.debug(f"Nominatim API 응답 수신: {api_response_data}")

    except requests.exceptions.RequestException as e:
        logger.error(f"Nominatim API('{query}') 요청 중 오류: {e}", exc_info=True)
        result = {"error": "위치 정보 검색 중 네트워크 오류가 발생했습니다."}
        _geocode_cache[query] = result # 실패한 요청도 캐시
        return result
    except (ValueError, KeyError) as e:
        response_text = response.text if 'response' in locals() else 'N/A'
        logger.error(f"Nominatim API('{query}') 응답 파싱 중 오류: {e}. 응답: {response_text}", exc_info=True)
        return {"error": "위치 정보 검색 중 데이터 처리 오류가 발생했습니다."}

    # --- Disambiguation Logic ---
    if not api_response_data:
        result = {"error": f"'{query}'에 대한 위치를 찾을 수 없습니다."}
    elif len(api_response_data) == 1:
        loc = api_response_data[0]
        result = {
            "status": "found",
            "lat": float(loc.get('lat')),
            "lon": float(loc.get('lon')),
            "country_code": loc.get('address', {}).get('country_code'),
            "display_name": loc.get('display_name')
        }
    else:
        choices = [f"{i+1}. {item.get('display_name')}" for i, item in enumerate(api_response_data)]
        result = {
            "status": "disambiguation",
            "message": f"어떤 '{query}'를 말씀하시는 건가요? 🧐\n" + "\n".join(choices) + "\n번호로 선택해주세요!",
            "options": api_response_data # 원본 데이터를 보존하여 다음 단계에서 사용
        }

    _geocode_cache[query] = result
    return result

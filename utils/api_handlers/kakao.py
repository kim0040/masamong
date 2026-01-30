# -*- coding: utf-8 -*-
from __future__ import annotations
import asyncio
import aiohttp
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
    """
    if not config.KAKAO_API_KEY or config.KAKAO_API_KEY == 'YOUR_KAKAO_API_KEY':
        logger.error("카카오 API 키(KAKAO_API_KEY)가 설정되지 않았습니다.")
        return f"장소 '{query}'을(를) 검색할 수 없습니다 (API 키 미설정)."

    headers = {
        "Authorization": f"KakaoAK {config.KAKAO_API_KEY}",
        "User-Agent": "Masamong/2.0"
    }
    params = {"query": query, "size": page_size}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(config.KAKAO_BASE_URL, headers=headers, params=params, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    documents = data.get('documents', [])
                    return _format_places_data(query, documents)
                else:
                    error_text = await resp.text()
                    logger.error(f"카카오맵 API 오류 (상태: {resp.status}): {error_text}")
                    return "장소 검색 중 오류가 발생했습니다."

    except Exception as e:
        logger.error(f"카카오맵 API('{query}') 처리 중 예기치 않은 오류: {e}", exc_info=True)
        return "장소 검색 중 알 수 없는 오류가 발생했습니다."

async def search_web(query: str, page_size: int = 1) -> list | None:
    """
    카카오 웹 검색 API로 검색을 수행하고, 결과 문서 리스트를 반환합니다.
    """
    if not config.KAKAO_API_KEY or config.KAKAO_API_KEY == 'YOUR_KAKAO_API_KEY':
        logger.error("카카오 API 키(KAKAO_API_KEY)가 설정되지 않았습니다.")
        return None

    headers = {
        "Authorization": f"KakaoAK {config.KAKAO_API_KEY}",
        "User-Agent": "Masamong/2.0"
    }
    params = {"query": query, "size": page_size}
    url = "https://dapi.kakao.com/v2/search/web"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get('documents')
                else:
                    logger.error(f"카카오 웹 검색 API 오류 (상태: {resp.status}): {await resp.text()}")
                    return None

    except Exception as e:
        logger.error(f"카카오 웹 검색 API('{query}') 처리 중 예기치 않은 오류: {e}", exc_info=True)
        return None

async def search_image(query: str, page_size: int = 1) -> list | None:
    """
    카카오 이미지 검색 API로 검색을 수행하고, 결과 문서 리스트를 반환합니다.
    """
    if not config.KAKAO_API_KEY or config.KAKAO_API_KEY == 'YOUR_KAKAO_API_KEY':
        logger.error("카카오 API 키(KAKAO_API_KEY)가 설정되지 않았습니다.")
        return None

    headers = {
        "Authorization": f"KakaoAK {config.KAKAO_API_KEY}",
        "User-Agent": "Masamong/2.0"
    }
    params = {"query": query, "size": page_size}
    url = "https://dapi.kakao.com/v2/search/image"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get('documents')
                else:
                    logger.error(f"카카오 이미지 검색 API 오류 (상태: {resp.status}): {await resp.text()}")
                    return None

    except Exception as e:
        logger.error(f"카카오 이미지 검색 API('{query}') 처리 중 예기치 않은 오류: {e}", exc_info=True)
        return None

async def search_blog(query: str, page_size: int = 3) -> list | None:
    """
    카카오 블로그 검색 API로 블로그 글을 검색합니다. (리뷰, 후기 등)
    """
    if not config.KAKAO_API_KEY or config.KAKAO_API_KEY == 'YOUR_KAKAO_API_KEY':
        return None

    headers = {
        "Authorization": f"KakaoAK {config.KAKAO_API_KEY}",
        "User-Agent": "Masamong/2.0"
    }
    params = {"query": query, "size": page_size, "sort": "accuracy"}
    url = "https://dapi.kakao.com/v2/search/blog"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get('documents')
                else:
                    logger.error(f"카카오 블로그 검색 API 오류 (상태: {resp.status}): {await resp.text()}")
                    return None
    except Exception as e:
        logger.error(f"카카오 블로그 검색 API('{query}') 처리 중 예기치 않은 오류: {e}", exc_info=True)
        return None

async def search_vclip(query: str, page_size: int = 3) -> list | None:
    """
    카카오 동영상 검색 API로 동영상을 검색합니다.
    """
    if not config.KAKAO_API_KEY or config.KAKAO_API_KEY == 'YOUR_KAKAO_API_KEY':
        return None

    headers = {
        "Authorization": f"KakaoAK {config.KAKAO_API_KEY}",
        "User-Agent": "Masamong/2.0"
    }
    params = {"query": query, "size": page_size, "sort": "accuracy"}
    url = "https://dapi.kakao.com/v2/search/vclip"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get('documents')
                else:
                    logger.error(f"카카오 동영상 검색 API 오류 (상태: {resp.status}): {await resp.text()}")
                    return None
    except Exception as e:
        logger.error(f"카카오 동영상 검색 API('{query}') 처리 중 예기치 않은 오류: {e}", exc_info=True)
        return None
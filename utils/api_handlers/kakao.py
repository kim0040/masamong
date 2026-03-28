# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

import aiohttp

import config
from logger_config import logger

_kakao_session: aiohttp.ClientSession | None = None
_session_lock = asyncio.Lock()
_rate_lock = asyncio.Lock()
_minute_calls: deque[float] = deque()
_daily_calls: deque[float] = deque()
_concurrency_limit = max(1, int(getattr(config, "KAKAO_API_MAX_CONCURRENCY", 6)))
_request_guard = asyncio.Semaphore(_concurrency_limit)


def _format_places_data(query: str, places: list) -> str:
    """장소 검색 결과를 LLM 친화적인 문자열로 포맷팅합니다."""
    if not places:
        return f"'{query}'에 대한 장소를 찾을 수 없습니다."

    lines = [
        f"- {place.get('place_name', 'N/A')} ({place.get('category_name', 'N/A')}, {place.get('road_address_name', '주소 없음')})"
        for place in places
    ]
    return f"'{query}' 주변 장소 검색 결과:\n" + "\n".join(lines)


def _is_kakao_key_ready() -> bool:
    return bool(config.KAKAO_API_KEY and config.KAKAO_API_KEY != "YOUR_KAKAO_API_KEY")


def _kakao_headers() -> dict[str, str]:
    return {
        "Authorization": f"KakaoAK {config.KAKAO_API_KEY}",
        "User-Agent": "Masamong/2.0",
    }


async def _get_kakao_session() -> aiohttp.ClientSession:
    global _kakao_session

    if _kakao_session and not _kakao_session.closed:
        return _kakao_session

    async with _session_lock:
        if _kakao_session and not _kakao_session.closed:
            return _kakao_session

        timeout_seconds = max(1, int(getattr(config, "KAKAO_API_TIMEOUT_SECONDS", 10)))
        connector = aiohttp.TCPConnector(
            limit=_concurrency_limit * 2,
            limit_per_host=_concurrency_limit,
            ttl_dns_cache=300,
        )
        _kakao_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout_seconds),
            connector=connector,
        )
        return _kakao_session


async def close_kakao_session() -> None:
    global _kakao_session
    if _kakao_session and not _kakao_session.closed:
        await _kakao_session.close()
    _kakao_session = None


def _prune_rate_window(now: float) -> None:
    minute_cutoff = now - 60.0
    day_cutoff = now - 86400.0
    while _minute_calls and _minute_calls[0] < minute_cutoff:
        _minute_calls.popleft()
    while _daily_calls and _daily_calls[0] < day_cutoff:
        _daily_calls.popleft()


async def _acquire_rate_slot() -> bool:
    rpm_limit = max(1, int(getattr(config, "KAKAO_API_RPM_LIMIT", 60)))
    rpd_limit = max(1, int(getattr(config, "KAKAO_API_RPD_LIMIT", 95000)))

    async with _rate_lock:
        now = time.time()
        _prune_rate_window(now)

        if len(_minute_calls) >= rpm_limit:
            logger.warning("카카오 API 분당 호출 제한 도달")
            return False
        if len(_daily_calls) >= rpd_limit:
            logger.warning("카카오 API 일일 호출 제한 도달")
            return False

        _minute_calls.append(now)
        _daily_calls.append(now)
        return True


async def _request_kakao_json(url: str, params: dict[str, Any], endpoint_name: str) -> dict[str, Any] | None:
    if not _is_kakao_key_ready():
        logger.error("카카오 API 키(KAKAO_API_KEY)가 설정되지 않았습니다.")
        return None

    if not await _acquire_rate_slot():
        logger.warning(f"카카오 API 호출 제한으로 요청 건너뜀: {endpoint_name}")
        return None

    try:
        session = await _get_kakao_session()
        async with _request_guard:
            async with session.get(url, headers=_kakao_headers(), params=params) as resp:
                if resp.status == 200:
                    return await resp.json()
                error_text = await resp.text()
                logger.error(f"카카오 {endpoint_name} API 오류 (상태: {resp.status}): {error_text}")
                return None
    except asyncio.TimeoutError:
        logger.error(f"카카오 {endpoint_name} API 시간 초과")
        return None
    except Exception as e:
        logger.error(f"카카오 {endpoint_name} API 처리 중 예기치 않은 오류: {e}", exc_info=True)
        return None


async def search_place_by_keyword(query: str, page_size: int = 5) -> str:
    """
    카카오 로컬 API로 장소를 검색하고, LLM 친화적인 문자열로 반환합니다.
    """
    data = await _request_kakao_json(
        config.KAKAO_BASE_URL,
        {"query": query, "size": page_size},
        "장소 검색",
    )
    if data is None:
        return "장소 검색 중 오류가 발생했습니다."
    return _format_places_data(query, data.get("documents", []))


async def search_web(query: str, page_size: int = 1) -> list | None:
    """
    카카오 웹 검색 API로 검색을 수행하고, 결과 문서 리스트를 반환합니다.
    """
    data = await _request_kakao_json(
        "https://dapi.kakao.com/v2/search/web",
        {"query": query, "size": page_size},
        "웹 검색",
    )
    return data.get("documents") if data else None


async def search_image(query: str, page_size: int = 1) -> list | None:
    """
    카카오 이미지 검색 API로 검색을 수행하고, 결과 문서 리스트를 반환합니다.
    """
    data = await _request_kakao_json(
        "https://dapi.kakao.com/v2/search/image",
        {"query": query, "size": page_size},
        "이미지 검색",
    )
    return data.get("documents") if data else None


async def search_blog(query: str, page_size: int = 3) -> list | None:
    """
    카카오 블로그 검색 API로 블로그 글을 검색합니다. (리뷰, 후기 등)
    """
    data = await _request_kakao_json(
        "https://dapi.kakao.com/v2/search/blog",
        {"query": query, "size": page_size, "sort": "accuracy"},
        "블로그 검색",
    )
    return data.get("documents") if data else None


async def search_vclip(query: str, page_size: int = 3) -> list | None:
    """
    카카오 동영상 검색 API로 동영상을 검색합니다.
    """
    data = await _request_kakao_json(
        "https://dapi.kakao.com/v2/search/vclip",
        {"query": query, "size": page_size, "sort": "accuracy"},
        "동영상 검색",
    )
    return data.get("documents") if data else None

# -*- coding: utf-8 -*-
import asyncio
import requests
import config
from logger_config import logger
from datetime import datetime, timedelta

BASE_URL = "https://api.rawg.io/api"

async def get_games(ordering: str = '-released', dates: str = None, genres: str = None, page_size: int = 5) -> dict:
    """
    RAWG.io API를 사용하여 게임 목록을 조회합니다.
    https://api.rawg.io/docs/
    """
    if not config.RAWG_API_KEY or config.RAWG_API_KEY == 'YOUR_RAWG_API_KEY':
        logger.error("RAWG API 키(RAWG_API_KEY)가 설정되지 않았습니다.")
        return {"error": "API 키가 설정되지 않았습니다."}

    # 'dates' 파라미터가 없으면, 최근 1년으로 기본 설정
    if not dates:
        today = datetime.now()
        one_year_ago = today - timedelta(days=365)
        dates = f"{one_year_ago.strftime('%Y-%m-%d')},{today.strftime('%Y-%m-%d')}"

    params = {
        "key": config.RAWG_API_KEY,
        "ordering": ordering,
        "dates": dates,
        "page_size": page_size
    }
    if genres:
        params["genres"] = genres.lower()

    try:
        response = await asyncio.to_thread(requests.get, f"{BASE_URL}/games", params=params, timeout=15)
        response.raise_for_status()
        data = response.json()

        results = data.get('results', [])
        if not results:
            return {"games": []}

        formatted_games = [
            {
                "name": game.get('name'),
                "released": game.get('released'),
                "rating": game.get('rating'),
                "metacritic": game.get('metacritic'),
                "genres": [genre['name'] for genre in game.get('genres', [])],
                "platforms": [p['platform']['name'] for p in game.get('platforms', [])]
            }
            for game in results
        ]
        return {"games": formatted_games}

    except requests.exceptions.Timeout:
        logger.error("RAWG API 요청 시간 초과.")
        return {"error": "API 요청 시간 초과"}
    except requests.exceptions.HTTPError as e:
        logger.error(f"RAWG API HTTP 오류: {e.response.status_code}")
        return {"error": f"API 서버 오류 ({e.response.status_code})"}
    except (requests.exceptions.RequestException, ValueError) as e:
        logger.error(f"RAWG API 처리 중 오류: {e}", exc_info=True)
        return {"error": "API 요청 또는 데이터 처리 중 오류 발생"}

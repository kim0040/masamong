# -*- coding: utf-8 -*-
import asyncio
import requests
import config
from logger_config import logger
from datetime import datetime, timedelta

BASE_URL = "https://api.rawg.io/api"

async def get_games(ordering: str = '-released', dates: str = None, genres: str = None, page_size: int = 5) -> dict | None:
    """
    RAWG.io API를 사용하여 게임 목록을 조회합니다.
    [수정] 오류 발생 시 None을 반환하고, 결과가 없으면 빈 리스트를 포함한 딕셔너리를 반환합니다.
    """
    if not config.RAWG_API_KEY or config.RAWG_API_KEY == 'YOUR_RAWG_API_KEY':
        logger.error("RAWG API 키(RAWG_API_KEY)가 설정되지 않았습니다.")
        return None

    if not dates:
        today = datetime.now()
        three_months_ago = today - timedelta(days=90)
        dates = f"{three_months_ago.strftime('%Y-%m-%d')},{today.strftime('%Y-%m-%d')}"

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
                "genres": [genre['name'] for genre in game.get('genres') or []],
                "platforms": [p['platform']['name'] for p in game.get('platforms') or [] if p and p.get('platform')]
            }
            for game in results
        ]
        return {"games": formatted_games}

    except requests.exceptions.RequestException as e:
        logger.error(f"RAWG API 요청 중 오류: {e}", exc_info=True)
        return None
    except (ValueError, KeyError) as e:
        response_text = response.text if 'response' in locals() else 'N/A'
        logger.error(f"RAWG API 응답 파싱 중 오류: {e}. 응답: {response_text}", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"RAWG API 처리 중 예기치 않은 오류: {e}", exc_info=True)
        return None

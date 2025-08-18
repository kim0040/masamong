# -*- coding: utf-8 -*-
import asyncio
import requests
import config
from logger_config import logger
from datetime import datetime, timedelta

BASE_URL = "https://api.rawg.io/api"

def _format_games_data(games: list) -> str:
    """게임 목록 데이터를 LLM 친화적인 문자열로 포맷팅합니다."""
    if not games:
        return "추천할 만한 게임을 찾지 못했습니다. 다른 조건으로 시도해보세요."

    lines = []
    for game in games:
        name = game.get('name', 'N/A')
        rating = game.get('rating', 'N/A')
        metacritic = game.get('metacritic', 'N/A')
        genre_list = game.get('genres', [])
        genres = ", ".join(genre_list) if genre_list else "N/A"

        lines.append(f"- {name} (평점: {rating}, 메타스코어: {metacritic}, 장르: {genres})")

    return "추천 게임 목록:\n" + "\n".join(lines)

async def get_games(ordering: str = '-released', dates: str = None, genres: str = None, page_size: int = 5) -> str:
    """
    RAWG.io API로 게임 목록을 조회하고, LLM 친화적인 문자열로 반환합니다.
    [수정] 반환 형식을 dict에서 str으로 변경하여 토큰 사용량을 최적화합니다.
    """
    if not config.RAWG_API_KEY or config.RAWG_API_KEY == 'YOUR_RAWG_API_KEY':
        logger.error("RAWG API 키(RAWG_API_KEY)가 설정되지 않았습니다.")
        return "게임을 추천할 수 없습니다 (API 키 미설정)."

    if not dates:
        today = datetime.now()
        three_months_ago = today - timedelta(days=90)
        dates = f"{three_months_ago.strftime('%Y-%m-%d')},{today.strftime('%Y-%m-%d')}"

    params = {"key": config.RAWG_API_KEY, "ordering": ordering, "dates": dates, "page_size": page_size}
    if genres:
        params["genres"] = genres.lower()

    try:
        response = await asyncio.to_thread(requests.get, f"{BASE_URL}/games", params=params, timeout=15)
        response.raise_for_status()
        data = response.json()

        results = data.get('results', [])
        if not results:
            return "조건에 맞는 게임을 찾을 수 없습니다."

        # agent.md 명세에 따른 상세 정보 추출
        formatted_games = [
            {
                "name": game.get('name'),
                "rating": game.get('rating'),
                "metacritic": game.get('metacritic'),
                "genres": [genre['name'] for genre in game.get('genres') or []],
            }
            for game in results
        ]
        return _format_games_data(formatted_games)

    except requests.exceptions.RequestException as e:
        logger.error(f"RAWG API 요청 중 오류: {e}", exc_info=True)
        return "게임 추천 중 네트워크 오류가 발생했습니다."
    except (ValueError, KeyError) as e:
        response_text = response.text if 'response' in locals() else 'N/A'
        logger.error(f"RAWG API 응답 파싱 중 오류: {e}. 응답: {response_text}", exc_info=True)
        return "게임 추천 중 데이터 처리 오류가 발생했습니다."
    except Exception as e:
        logger.error(f"RAWG API 처리 중 예기치 않은 오류: {e}", exc_info=True)
        return "게임 추천 중 알 수 없는 오류가 발생했습니다."

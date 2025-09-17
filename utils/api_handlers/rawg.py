# -*- coding: utf-8 -*-
import asyncio
import requests
import config
from logger_config import logger
from datetime import datetime, timedelta
from .. import http

def _format_games_data(games: list) -> str:
    """
    게임 목록 데이터를 LLM 친화적인 문자열로 포맷팅합니다.
    [Phase 3] 플레이타임, 구매처 정보 추가.
    """
    if not games:
        return "추천할 만한 게임을 찾지 못했습니다. 다른 조건으로 시도해보세요."

    lines = []
    for game in games:
        name = game.get('name', 'N/A')
        metacritic = game.get('metacritic', 'N/A')
        playtime = game.get('playtime', 'N/A')
        stores = ", ".join(game.get('stores', [])) if game.get('stores') else "정보 없음"

        lines.append(f"- {name} (메타스코어: {metacritic}, 평균 플레이타임: {playtime}시간, 구매처: {stores})")

    return "추천 게임 목록:\n" + "\n".join(lines)

ALLOWED_GENRES = {
    'action', 'indie', 'adventure', 'rpg', 'strategy', 'shooter', 'casual', 
    'simulation', 'puzzle', 'arcade', 'platformer', 'racing', 'sports', 
    'massively-multiplayer', 'fighting', 'family', 'board-games', 
    'educational', 'card'
}

async def get_games(ordering: str = '-released', dates: str = None, genres: str = None, page_size: int = 5) -> str:
    """
    RAWG.io API로 게임 목록을 조회하고, LLM 친화적인 문자열로 반환합니다.
    [수정] genres 파라미터 검증 로직 추가.
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
        # 입력된 장르를 소문자, 쉼표 기준으로 분리 및 공백 제거
        input_genres = [genre.strip().lower() for genre in genres.split(',')]
        # 허용된 장르 목록에 있는 것만 필터링
        valid_genres = [genre for genre in input_genres if genre in ALLOWED_GENRES]
        
        if not valid_genres:
            return f"요청하신 장르 '{genres}'를 찾을 수 없거나 유효하지 않습니다. 일반적인 영문 장르(예: action, rpg, shooter)로 다시 시도해주세요."
            
        params["genres"] = ",".join(valid_genres)

    try:
        session = http.get_modern_tls_session()
        response = await asyncio.to_thread(session.get, f"{config.RAWG_BASE_URL}/games", params=params, timeout=15)
        response.raise_for_status()
        data = response.json()

        results = data.get('results', [])
        if not results:
            return "조건에 맞는 게임을 찾을 수 없습니다."

        # agent.md 명세에 따른 상세 정보 추출
        formatted_games = [
            {
                "name": game.get('name'),
                "metacritic": game.get('metacritic'),
                "playtime": game.get('playtime', 0),
                "stores": [store['store']['name'] for store in game.get('stores') or [] if store and store.get('store')]
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

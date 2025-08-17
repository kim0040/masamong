# -*- coding: utf-8 -*-
import asyncio
import requests
import config
from logger_config import logger

# Riot API는 지역(region)별로 엔드포인트가 다름
# 아시아(KR, JP)는 'asia', 북미는 'americas', 유럽은 'europe'
ACCOUNT_API_URL = "https://asia.api.riotgames.com/riot/account/v1/accounts/by-riot-id"
LOL_API_URL = "https://kr.api.riotgames.com/lol"

def _get_headers():
    """API 키 존재 여부를 확인하고, 요청 헤더를 반환합니다."""
    api_key = config.RIOT_API_KEY
    if not api_key or api_key == 'YOUR_RIOT_API_KEY':
        logger.error("Riot API 키(RIOT_API_KEY)가 설정되지 않았습니다.")
        return None
    return {"X-Riot-Token": api_key}

async def get_puuid_by_riot_id(game_name: str, tag_line: str) -> dict:
    """Riot ID (gameName#tagLine)를 사용하여 puuid를 조회합니다."""
    headers = _get_headers()
    if not headers:
        return {"error": "API 키가 설정되지 않았습니다."}

    url = f"{ACCOUNT_API_URL}/{game_name}/{tag_line}"

    try:
        response = await asyncio.to_thread(requests.get, url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        puuid = data.get('puuid')
        if not puuid:
            return {"error": "PUUID를 찾을 수 없습니다."}
        return {"puuid": puuid}

    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            logger.warning(f"Riot ID '{game_name}#{tag_line}'을(를) 찾을 수 없습니다.")
            return {"error": "해당 Riot ID를 찾을 수 없습니다."}
        logger.error(f"Riot Account API HTTP 오류: {e.response.status_code}")
        return {"error": f"API 서버 오류 ({e.response.status_code})"}
    except (requests.exceptions.RequestException, ValueError) as e:
        logger.error(f"Riot Account API 처리 중 오류: {e}", exc_info=True)
        return {"error": "API 요청 또는 데이터 처리 중 오류 발생"}

async def get_match_ids_by_puuid(puuid: str, count: int = 5) -> dict:
    """puuid를 사용하여 최근 LoL 매치 ID 목록을 조회합니다."""
    headers = _get_headers()
    if not headers:
        return {"error": "API 키가 설정되지 않았습니다."}

    url = f"https://asia.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids"
    params = {"count": count}

    try:
        response = await asyncio.to_thread(requests.get, url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        if not data:
            return {"error": "최근 매치 기록이 없습니다."}
        return {"match_ids": data}

    except requests.exceptions.HTTPError as e:
        logger.error(f"Riot Match-V5 (by-puuid) API HTTP 오류: {e.response.status_code}")
        return {"error": f"API 서버 오류 ({e.response.status_code})"}
    except (requests.exceptions.RequestException, ValueError) as e:
        logger.error(f"Riot Match-V5 (by-puuid) API 처리 중 오류: {e}", exc_info=True)
        return {"error": "API 요청 또는 데이터 처리 중 오류 발생"}


async def get_match_details_by_id(match_id: str, puuid_to_find: str) -> dict:
    """매치 ID를 사용하여 상세 매치 정보를 조회하고, 특정 플레이어의 정보를 추출합니다."""
    headers = _get_headers()
    if not headers:
        return {"error": "API 키가 설정되지 않았습니다."}

    url = f"https://asia.api.riotgames.com/lol/match/v5/matches/{match_id}"

    try:
        response = await asyncio.to_thread(requests.get, url, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()

        participants = data.get("info", {}).get("participants", [])
        if not participants:
            return {"error": "매치에서 참가자 정보를 찾을 수 없습니다."}

        for participant in participants:
            if participant.get("puuid") == puuid_to_find:
                # agent.md 명세에 따라 필요한 정보만 추출
                return {
                    "summoner_name": participant.get("summonerName"),
                    "champion_name": participant.get("championName"),
                    "win": participant.get("win"),
                    "kills": participant.get("kills"),
                    "deaths": participant.get("deaths"),
                    "assists": participant.get("assists"),
                    "gold_earned": participant.get("goldEarned"),
                    "vision_score": participant.get("visionScore"),
                    "items": [
                        participant.get("item0"), participant.get("item1"), participant.get("item2"),
                        participant.get("item3"), participant.get("item4"), participant.get("item5"),
                        participant.get("item6")
                    ]
                }

        return {"error": "해당 매치에서 지정된 플레이어를 찾을 수 없습니다."}

    except requests.exceptions.HTTPError as e:
        logger.error(f"Riot Match-V5 (matches) API HTTP 오류: {e.response.status_code}")
        return {"error": f"API 서버 오류 ({e.response.status_code})"}
    except (requests.exceptions.RequestException, ValueError) as e:
        logger.error(f"Riot Match-V5 (matches) API 처리 중 오류: {e}", exc_info=True)
        return {"error": "API 요청 또는 데이터 처리 중 오류 발생"}

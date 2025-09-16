# -*- coding: utf-8 -*-
import asyncio
import requests
import config
from logger_config import logger
from .. import http
from datetime import datetime, timedelta, timezone

async def get_events_by_coords(lat: float, lon: float, radius: int = 50, unit: str = 'km') -> dict:
    """
    Ticketmaster API를 사용하여 특정 좌표 주변의 이벤트를 검색합니다.
    """
    if not config.TICKETMASTER_API_KEY or config.TICKETMASTER_API_KEY == 'YOUR_TICKETMASTER_API_KEY':
        logger.error("Ticketmaster API 키(TICKETMASTER_API_KEY)가 설정되지 않았습니다.")
        return {"error": "이벤트 정보를 조회할 수 없습니다 (API 키 미설정)."}

    url = f"{config.TICKETMASTER_BASE_URL}/events.json"

    # 날짜 범위 설정 (오늘부터 30일 후까지)
    start_date_time = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    end_date_time = (datetime.now(timezone.utc) + timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%SZ')

    params = {
        "apikey": config.TICKETMASTER_API_KEY,
        "latlong": f"{lat},{lon}",
        "radius": radius,
        "unit": unit,
        "startDateTime": start_date_time,
        "endDateTime": end_date_time,
        "sort": "date,asc"
    }

    logger.info(f"Ticketmaster API 요청: URL='{url}', Params='{params}'")

    try:
        session = http.get_modern_tls_session()
        response = await asyncio.to_thread(session.get, url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        logger.debug(f"Ticketmaster API 응답 수신: {data}")

        events = data.get('_embedded', {}).get('events', [])
        formatted_events = [
            {
                "name": event.get('name'),
                "type": event.get('type'),
                "url": event.get('url'),
                "start_date": event.get('dates', {}).get('start', {}).get('localDate'),
                "genre": event.get('classifications', [{}])[0].get('genre', {}).get('name'),
                "venue": event.get('_embedded', {}).get('venues', [{}])[0].get('name')
            }
            for event in events
        ]
        return {"events": formatted_events}

    except requests.exceptions.RequestException as e:
        logger.error(f"Ticketmaster API({lat},{lon}) 요청 중 오류: {e}", exc_info=True)
        return {"error": "이벤트 정보 검색 중 네트워크 오류가 발생했습니다."}
    except (ValueError, KeyError, IndexError) as e:
        response_text = response.text if 'response' in locals() else 'N/A'
        logger.error(f"Ticketmaster API({lat},{lon}) 응답 파싱 중 오류: {e}. 응답: {response_text}", exc_info=True)
        return {"error": "이벤트 정보 검색 중 데이터 처리 오류가 발생했습니다."}
    except Exception as e:
        logger.error(f"Ticketmaster API({lat},{lon}) 처리 중 예기치 않은 오류: {e}", exc_info=True)
        return {"error": "이벤트 정보 검색 중 알 수 없는 오류가 발생했습니다."}

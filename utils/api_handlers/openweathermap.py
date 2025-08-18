# -*- coding: utf-8 -*-
import asyncio
import requests
import config
from logger_config import logger
from .. import http

async def get_weather_by_coords(lat: float, lon: float) -> dict:
    """
    OpenWeatherMap API를 사용하여 특정 좌표의 현재 날씨 정보를 조회합니다.
    """
    if not config.OPENWEATHERMAP_API_KEY or config.OPENWEATHERMAP_API_KEY == 'YOUR_OPENWEATHERMAP_API_KEY':
        logger.error("OpenWeatherMap API 키(OPENWEATHERMAP_API_KEY)가 설정되지 않았습니다.")
        return {"error": "날씨 정보를 조회할 수 없습니다 (API 키 미설정)."}

    url = f"{config.OPENWEATHERMAP_BASE_URL}/weather"
    params = {
        "lat": lat,
        "lon": lon,
        "appid": config.OPENWEATHERMAP_API_KEY,
        "units": "metric",  # 섭씨 온도로 받기
        "lang": "kr"      # 한국어 설명으로 받기
    }

    log_params = params.copy()
    log_params["appid"] = "[REDACTED]"
    logger.info(f"OpenWeatherMap API 요청: URL='{url}', Params='{log_params}'")

    try:
        session = http.get_modern_tls_session()
        response = await asyncio.to_thread(session.get, url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        logger.debug(f"OpenWeatherMap API 응답 수신: {data}")

        # 필요한 정보만 추출하여 리턴
        weather = data.get('weather', [{}])[0]
        main = data.get('main', {})
        wind = data.get('wind', {})
        sys_info = data.get('sys', {})

        return {
            "description": weather.get('description'),
            "temp": main.get('temp'),
            "feels_like": main.get('feels_like'),
            "humidity": main.get('humidity'),
            "wind_speed": wind.get('speed'),
            "sunrise": sys_info.get('sunrise'),
            "sunset": sys_info.get('sunset')
        }

    except requests.exceptions.RequestException as e:
        logger.error(f"OpenWeatherMap API({lat},{lon}) 요청 중 오류: {e}", exc_info=True)
        return {"error": "해외 날씨 조회 중 네트워크 오류가 발생했습니다."}
    except (ValueError, KeyError, IndexError) as e:
        response_text = response.text if 'response' in locals() else 'N/A'
        logger.error(f"OpenWeatherMap API({lat},{lon}) 응답 파싱 중 오류: {e}. 응답: {response_text}", exc_info=True)
        return {"error": "해외 날씨 조회 중 데이터 처리 오류가 발생했습니다."}
    except Exception as e:
        logger.error(f"OpenWeatherMap API({lat},{lon}) 처리 중 예기치 않은 오류: {e}", exc_info=True)
        return {"error": "해외 날씨 조회 중 알 수 없는 오류가 발생했습니다."}

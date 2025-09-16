# -*- coding: utf-8 -*-
import asyncio
import discord
from discord.ext import commands
import re

import config
from logger_config import logger
from utils.api_handlers import finnhub, kakao, krx, exim, rawg, nominatim, openweathermap, foursquare, ticketmaster
from utils import db as db_utils
from utils import coords as coords_utils
from .weather_cog import WeatherCog

def is_korean(text: str) -> bool:
    """텍스트에 한글이 포함되어 있는지 확인합니다."""
    if not text:
        return False
    return bool(re.search("[\uac00-\ud7a3]", text))

class ToolsCog(commands.Cog):
    """
    AI 에이전트가 사용할 수 있는 '도구'들의 모음입니다.
    각 도구는 하나 이상의 API 핸들러를 호출하여 특정 작업을 수행합니다.
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.weather_cog: WeatherCog = self.bot.get_cog('WeatherCog')
        logger.info("ToolsCog 초기화 완료.")

    async def get_travel_recommendation(self, location_name: str) -> str:
        """
        주어진 위치에 대한 날씨, 명소, 이벤트 등 여행 정보를 종합하여 LLM 친화적인 문자열로 반환합니다.
        This is a high-level meta-tool that acts as an intelligent router.
        """
        # 1. Geocode the location name
        geo_info = await self.geocode(location_name)

        if "error" in geo_info or "disambiguation" in geo_info:
            return geo_info

        if geo_info.get("status") != "found":
            return {"error": "위치를 찾는 데 실패했지만 명확한 오류가 반환되지 않았습니다."}

        lat = geo_info.get("lat")
        lon = geo_info.get("lon")
        country_code = geo_info.get("country_code")
        display_name = geo_info.get("display_name")

        logger.info(f"Travel recommendation for '{display_name}' ({lat}, {lon}), country: {country_code}")

        # 2. Route to appropriate weather tool and gather all data concurrently
        weather_task = None
        if country_code == 'kr':
            # Convert to KMA grid and call Korean weather tool
            nx, ny = coords_utils.latlon_to_kma_grid(lat, lon)
            # The existing weather tool is complex, let's call its internal logic directly for now
            # This might need refactoring later to be a proper tool call
            weather_task = self.weather_cog.get_formatted_weather_string(0, display_name, str(nx), str(ny))
        else:
            # Call foreign weather tool
            weather_task = self.get_foreign_weather(lat, lon)

        poi_task = self.find_points_of_interest(lat, lon)
        events_task = self.find_events(lat, lon)

        results = await asyncio.gather(weather_task, poi_task, events_task, return_exceptions=True)

        weather_result, poi_result, events_result = results

        # Handle potential errors from individual API calls
        if isinstance(weather_result, Exception):
            logger.error("Weather task failed in gather", exc_info=weather_result)
            weather_result = {"error": "날씨 정보 조회 실패"}
        # The korean weather function returns a tuple
        elif isinstance(weather_result, tuple):
             weather_result = weather_result[0] if weather_result[0] else {"error": weather_result[1]}


        if isinstance(poi_result, Exception):
            logger.error("POI task failed in gather", exc_info=poi_result)
            poi_result = {"error": "주변 장소 정보 조회 실패"}

        if isinstance(events_result, Exception):
            logger.error("Events task failed in gather", exc_info=events_result)
            events_result = {"error": "주변 이벤트 정보 조회 실패"}

        # 3. Aggregate results
        # 여행 정보를 종합하여 포맷팅
        result = f"🌍 {display_name} 여행 정보\n\n"

        # 날씨 정보 추가
        if weather_result and not weather_result.get("error"):
            result += f"🌤️ **날씨**: {weather_result.get('current_weather', '정보 없음')}\n\n"

        # 명소 정보 추가
        poi_places = poi_result.get("places", [])
        if poi_places:
            result += "📍 **주요 명소**:\n"
            for i, place in enumerate(poi_places[:3], 1):
                result += f"   {i}. {place.get('name', 'N/A')}\n"
            result += "\n"

        # 이벤트 정보 추가
        events = events_result.get("events", [])
        if events:
            result += "🎪 **진행 중인 이벤트**:\n"
            for i, event in enumerate(events[:3], 1):
                result += f"   {i}. {event.get('name', 'N/A')}\n"

        return result.strip()

    async def get_current_time(self) -> str:
        """현재 시간과 날짜를 LLM 친화적인 문자열로 반환합니다."""
        current_time = db_utils.get_current_time()
        return f"현재 시간: {current_time}"

    async def get_current_weather(self, location: str = None, day_offset: int = 0) -> str:
        """
        주어진 위치의 날씨 정보를 조회하여 LLM이 이해하기 쉬운 문자열로 반환합니다.
        [수정] 반환 형식을 dict에서 str으로 변경하여 토큰 사용량을 최적화합니다.
        """
        if not self.weather_cog:
            return "날씨 정보 모듈이 준비되지 않아 조회할 수 없습니다."

        location_name = location or config.DEFAULT_LOCATION_NAME
        coords = config.LOCATION_COORDINATES.get(location_name)
        if not coords:
            for key, value in config.LOCATION_COORDINATES.items():
                if location_name in key:
                    coords, location_name = value, key
                    break

        if not coords:
            return f"'{location_name}' 지역의 날씨 정보는 아직 알 수 없습니다."

        nx, ny = str(coords["nx"]), str(coords["ny"])
        weather_data, error_msg = await self.weather_cog.get_formatted_weather_string(day_offset, location_name, nx, ny)

        return weather_data if weather_data else error_msg

    async def get_stock_price(self, stock_name: str) -> str:
        """
        주식명을 기반으로 국내 또는 해외 주식의 시세를 조회하여 LLM 친화적인 문자열로 반환합니다.
        [수정] 반환 형식을 dict에서 str으로 변경하여 토큰 사용량을 최적화합니다.
        """
        if is_korean(stock_name):
            logger.info(f"'{stock_name}'은(는) 한글이 포함되어 KRX API를 호출합니다.")
            return await krx.get_stock_price(stock_name)
        else:
            logger.info(f"'{stock_name}'은(는) Ticker로 간주하여 Finnhub API를 호출합니다.")
            return await finnhub.get_stock_quote(stock_name)

    async def get_company_news(self, stock_name: str, count: int = 3) -> str:
        """
        특정 종목(Ticker Symbol)에 대한 최신 뉴스를 조회하여 LLM 친화적인 문자열로 반환합니다.
        [수정] 반환 형식을 dict에서 str으로 변경하여 토큰 사용량을 최적화합니다.
        """
        return await finnhub.get_company_news(stock_name, count)

    async def search_for_place(self, query: str, page_size: int = 5) -> str:
        """
        키워드로 장소를 검색하여 LLM 친화적인 문자열로 반환합니다.
        [수정] 반환 형식을 dict에서 str으로 변경하여 토큰 사용량을 최적화합니다.
        """
        return await kakao.search_place_by_keyword(query, page_size=page_size)

    async def geocode(self, location_name: str) -> dict:
        """
        장소 이름을 지리적 좌표(위도/경도)로 변환합니다. (내부 사용용 - dict 반환 유지)
        결과가 여러 개일 경우, 사용자에게 선택지를 제공합니다.
        """
        return await nominatim.geocode_location(location_name)

    async def get_foreign_weather(self, lat: float, lon: float) -> dict:
        """
        주어진 위도/경도를 기반으로 해외 날씨 정보를 조회합니다. (내부 사용용 - dict 반환 유지)
        """
        return await openweathermap.get_weather_by_coords(lat, lon)

    async def find_points_of_interest(self, lat: float, lon: float, query: str = None, limit: int = 10) -> dict:
        """
        주어진 위도/경도 주변의 주요 장소(POI)를 검색합니다. (내부 사용용 - dict 반환 유지)
        """
        return await foursquare.get_places_by_coords(lat, lon, query, limit)

    async def find_events(self, lat: float, lon: float, radius: int = 50) -> dict:
        """
        주어진 위도/경도 주변의 이벤트를 검색합니다. (내부 사용용 - dict 반환 유지)
        """
        return await ticketmaster.get_events_by_coords(lat, lon, radius)

    async def get_krw_exchange_rate(self, currency_code: str = "USD") -> str:
        """
        특정 통화의 원화(KRW) 대비 환율을 조회하여 LLM 친화적인 문자열로 반환합니다.
        [수정] 반환 형식을 dict에서 str으로 변경하여 토큰 사용량을 최적화합니다.
        """
        return await exim.get_krw_exchange_rate(currency_code)

    async def get_loan_rates(self) -> str:
        """
        한국수출입은행의 대출 금리 정보를 조회하여 LLM 친화적인 문자열로 반환합니다.
        [수정] 반환 형식을 dict에서 str으로 변경하여 토큰 사용량을 최적화합니다.
        """
        return await exim.get_loan_rates()

    async def get_international_rates(self) -> str:
        """
        한국수출입은행의 국제 금리 정보를 조회하여 LLM 친화적인 문자열로 반환합니다.
        [수정] 반환 형식을 dict에서 str으로 변경하여 토큰 사용량을 최적화합니다.
        """
        return await exim.get_international_rates()

    async def recommend_games(self, ordering: str = '-released', genres: str = None, page_size: int = 5) -> str:
        """
        다양한 조건에 따라 비디오 게임을 추천하여 LLM 친화적인 문자열로 반환합니다.
        [수정] 반환 형식을 dict에서 str으로 변경하여 토큰 사용량을 최적화합니다.
        """
        return await rawg.get_games(ordering=ordering, genres=genres, page_size=page_size)

async def setup(bot: commands.Bot):
    await bot.add_cog(ToolsCog(bot))
    logger.info("ToolsCog 로드 완료.")

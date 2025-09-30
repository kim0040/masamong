# -*- coding: utf-8 -*-
"""
AI 에이전트가 외부 세계와 상호작용하기 위해 사용하는 '도구'들을 모아놓은 Cog입니다.

각 메서드는 특정 작업을 수행하는 도구(Tool)로, AI 핸들러에 의해 호출됩니다.
(예: 날씨 조회, 주식 검색, 환율 정보, 웹 검색 등)
"""

import asyncio
import discord
from discord.ext import commands
import re
import aiohttp

import config
from logger_config import logger
from utils.api_handlers import finnhub, kakao, krx, exim, nominatim, openweathermap
from utils import db as db_utils
from utils import coords as coords_utils
from .weather_cog import WeatherCog

def is_korean(text: str) -> bool:
    """텍스트에 한글이 포함되어 있는지 확인하는 유틸리티 함수입니다."""
    if not text:
        return False
    return bool(re.search("[\uac00-\ud7a3]", text))

class ToolsCog(commands.Cog):
    """AI 에이전트가 사용할 수 있는 도구(Tool)들의 모음입니다."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.weather_cog: WeatherCog = self.bot.get_cog('WeatherCog')
        logger.info("ToolsCog가 성공적으로 초기화되었습니다.")

    # --- 고수준 메타 도구 --- #

    async def get_travel_recommendation(self, location_name: str) -> dict:
        """
        주어진 위치에 대한 여행 정보를 종합하여 반환하는 고수준 메타 도구입니다.
        내부적으로 지오코딩, 날씨 조회 등 여러 도구를 호출하고 결과를 종합합니다.
        """
        # 1. 장소 이름을 좌표로 변환 (지오코딩)
        geo_info = await self.geocode(location_name)
        if "error" in geo_info or "disambiguation" in geo_info:
            return geo_info
        if geo_info.get("status") != "found":
            return {"error": "위치를 찾는 데 실패했습니다."}

        lat, lon, country_code, display_name = geo_info.get("lat"), geo_info.get("lon"), geo_info.get("country_code"), geo_info.get("display_name")
        logger.info(f"여행 추천 생성 중: '{display_name}' ({lat}, {lon})", extra={'guild_id': None})

        # 2. 국가 코드에 따라 적절한 날씨 도구로 라우팅
        weather_result: dict[str, object]
        try:
            if country_code == 'kr':
                if not self.weather_cog:
                    logger.warning("WeatherCog 인스턴스를 찾을 수 없어 국내 날씨 조회를 건너뜁니다.")
                    weather_result = {"error": "날씨 모듈이 준비되지 않았습니다."}
                else:
                    nx, ny = coords_utils.latlon_to_kma_grid(float(lat), float(lon))
                    location_label = (display_name or location_name).split(',')[0]
                    weather_str, error_msg = await self.weather_cog.get_formatted_weather_string(0, location_label, str(nx), str(ny))
                    if error_msg:
                        weather_result = {"error": error_msg}
                    elif weather_str:
                        weather_result = {"current_weather": weather_str, "source": "kma"}
                    else:
                        weather_result = {"error": "날씨 정보 조회에 실패했습니다."}
            else:
                foreign_weather = await self.get_foreign_weather(lat, lon)
                weather_result = foreign_weather if isinstance(foreign_weather, dict) else {"result": str(foreign_weather)}
        except Exception as exc:
            logger.error("여행 추천의 날씨 조회 단계에서 오류 발생", exc_info=True)
            weather_result = {"error": f"날씨 정보 조회에 실패했습니다: {exc}"}

        # 4. 최종 결과 종합
        return {"location_info": geo_info, "weather": weather_result}

    # --- 일반 도구 --- #

    async def get_current_time(self) -> str:
        """현재 시간과 날짜를 KST 기준 문자열로 반환합니다."""
        return f"현재 시간: {db_utils.get_current_time()}"

    async def get_current_weather(self, location: str = None, day_offset: int = 0) -> str:
        """주어진 위치의 날씨 정보를 문자열로 반환합니다."""
        if not self.weather_cog:
            return "날씨 정보 모듈이 준비되지 않았습니다."
        location_name = location or config.DEFAULT_LOCATION_NAME
        coords = await coords_utils.get_coords_from_db(self.bot.db, location_name)
        if not coords:
            return f"'{location_name}' 지역의 날씨 정보는 아직 알 수 없습니다."
        weather_data, error_msg = await self.weather_cog.get_formatted_weather_string(day_offset, location_name, str(coords["nx"]), str(coords["ny"]))
        return weather_data if weather_data else error_msg

    async def get_stock_price(self, stock_name: str) -> str:
        """주식명을 기반으로 국내/해외 주식 시세를 조회합니다. 한글 포함 여부로 국내/해외를 구분합니다."""
        if is_korean(stock_name):
            logger.info(f"'{stock_name}'은(는) 한글명이므로 KRX API를 호출합니다.")
            return await krx.get_stock_price(stock_name)
        else:
            logger.info(f"'{stock_name}'은(는) Ticker이므로 Finnhub API를 호출합니다.")
            return await finnhub.get_stock_quote(stock_name)

    async def get_stock_price_in_krw(self, stock_name: str) -> str:
        """미국 주식의 현재 가격을 조회하고, 환율 API를 통해 원화(KRW)로 변환하여 함께 반환합니다."""
        logger.info(f"미국 주식 원화 가격 조회 실행: '{stock_name}'")
        quote_data = await finnhub.get_raw_stock_quote(stock_name)
        if not quote_data or 'price' not in quote_data:
            return f"'{stock_name}'에 대한 미국 주식 정보를 찾을 수 없습니다."

        stock_price_usd = quote_data['price']
        exchange_rate = await exim.get_raw_exchange_rate("USD")
        if not exchange_rate:
            logger.warning("환율 정보를 가져오지 못해 KRW 계산에 실패했습니다. USD 가격만 반환합니다.")
            return await self.get_stock_price(stock_name)

        stock_price_krw = stock_price_usd * exchange_rate
        change_str = f"{quote_data.get('change', 0):+.2f}"
        return f"{quote_data.get('symbol', stock_name.upper())}: {stock_price_usd:.2f} USD ({change_str}) (약 {stock_price_krw:,.0f}원)"

    async def get_company_news(self, stock_name: str, count: int = 3) -> str:
        """특정 종목(Ticker Symbol)에 대한 최신 뉴스를 조회합니다."""
        return await finnhub.get_company_news(stock_name, count)

    async def search_for_place(self, query: str, page_size: int = 5) -> str:
        """키워드로 장소를 검색합니다."""
        return await kakao.search_place_by_keyword(query, page_size=page_size)

    async def geocode(self, location_name: str) -> dict:
        """장소 이름을 지리적 좌표(위도/경도)로 변환합니다. (주로 다른 도구 내부에서 사용)"""
        return await nominatim.geocode_location(location_name)

    async def get_foreign_weather(self, lat: float, lon: float) -> dict:
        """주어진 위도/경도를 기반으로 해외 날씨 정보를 조회합니다. (주로 다른 도구 내부에서 사용)"""
        return await openweathermap.get_weather_by_coords(lat, lon)

    async def get_krw_exchange_rate(self, currency_code: str = "USD") -> str:
        """특정 통화의 원화(KRW) 대비 환율을 조회합니다."""
        return await exim.get_krw_exchange_rate(currency_code)

    async def get_loan_rates(self) -> str:
        """한국수출입은행의 대출 금리 정보를 조회합니다."""
        return await exim.get_loan_rates()

    async def get_international_rates(self) -> str:
        """한국수출입은행의 국제 금리 정보를 조회합니다."""
        return await exim.get_international_rates()

    async def kakao_web_search(self, query: str) -> str:
        """(폴백용) Kakao API로 웹을 검색하고 결과를 요약하여 반환합니다."""
        logger.info(f"Kakao 웹 검색 실행: '{query}'")
        search_results = await kakao.search_web(query, page_size=3)
        if not search_results:
            return f"'{query}'에 대한 웹 검색 결과가 없습니다."
        
        formatted = [f"{i}. {r.get('title', '제목 없음').replace('<b>','').replace('</b>','')}\n   - {r.get('contents', '내용 없음').replace('<b>','').replace('</b>','')[:250]}..." for i, r in enumerate(search_results, 1)]
        return f"'{query}'에 대한 웹 검색 결과 요약:\n" + "\n".join(formatted)

    async def web_search(self, query: str) -> str:
        """
        Google/SerpAPI를 사용하여 웹 검색을 수행하고, 실패 시 Kakao 검색으로 폴백합니다.
        
        우선순위:
          1) Google Custom Search API (config.GOOGLE_API_KEY & config.GOOGLE_CX)
          2) SerpAPI (config.SERPAPI_KEY)
          3) kakao_web_search()로 폴백
        """
        logger.info(f"웹 검색 실행: '{query}'")
        try:
            # 1. Google Custom Search API
            if getattr(config, 'GOOGLE_API_KEY', None) and getattr(config, 'GOOGLE_CX', None):
                params = {'key': config.GOOGLE_API_KEY, 'cx': config.GOOGLE_CX, 'q': query, 'num': 3}
                async with aiohttp.ClientSession() as session:
                    async with session.get('https://www.googleapis.com/customsearch/v1', params=params, timeout=10) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            items = data.get('items', [])
                            if not items: return f"'{query}'에 대한 검색 결과가 없습니다. (Google CSE)"
                            formatted = []
                            for i, item in enumerate(items, 1):
                                title = item.get('title', '제목 없음')
                                snippet = item.get('snippet', '').replace('\n', ' ')
                                link = item.get('link')
                                formatted.append(f"{i}. {title}\n   - {snippet}\n   - {link}")
                            return f"'{query}'에 대한 웹 검색 결과 (Google CSE):\n" + "\n\n".join(formatted)
                        else:
                            logger.warning(f"Google CSE API가 오류를 반환했습니다 (상태 코드: {resp.status}): {await resp.text()}")

            # 3. Kakao Web Search (최후의 폴백)
            logger.info("Google CSE API 실패, Kakao 웹 검색으로 폴백합니다.")
            return await self.kakao_web_search(query)

        except Exception as e:
            logger.exception("웹 검색 중 예외 발생. Kakao 웹 검색으로 폴백합니다.")
            try:
                return await self.kakao_web_search(query)
            except Exception as final_e:
                return f"모든 웹 검색 시도 중 오류가 발생했습니다: {final_e}"

    async def search_images(self, query: str, count: int = 3) -> str:
        """주어진 쿼리로 이미지를 검색하고, 결과 이미지 URL 목록을 문자열로 반환합니다."""
        logger.info(f"이미지 검색 실행: '{query}'")
        image_results = await kakao.search_image(query, page_size=count)
        if not image_results:
            return f"'{query}'에 대한 이미지를 찾을 수 없습니다."
        
        urls = [result.get('image_url') for result in image_results if result.get('image_url')]
        if not urls:
            return f"'{query}'에 대한 이미지를 찾았지만, 유효한 URL이 없습니다."
        return f"'{query}' 이미지 검색 결과:\n" + "\n".join(urls)

async def setup(bot: commands.Bot):
    """Cog를 봇에 등록하는 함수입니다."""
    await bot.add_cog(ToolsCog(bot))

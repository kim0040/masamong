# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
import re

import config
from logger_config import logger
from utils.api_handlers import finnhub, kakao, exim, rawg
from utils import db as db_utils
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

    async def get_current_time(self) -> dict:
        """현재 시간과 날짜를 반환합니다."""
        return {"current_time": db_utils.get_current_time()}

    async def get_current_weather(self, location: str = None, day_offset: int = 0) -> dict:
        """주어진 위치의 날씨 정보를 조회합니다. (오늘, 내일, 모레까지 가능)"""
        if not self.weather_cog:
            return {"error": "날씨 정보 모듈이 준비되지 않았습니다."}

        location_name = location or config.DEFAULT_LOCATION_NAME

        # 위치 정보로부터 nx, ny 좌표 찾기
        coords = config.LOCATION_COORDINATES.get(location_name)
        if not coords:
            # 부분 일치 검색
            for key, value in config.LOCATION_COORDINATES.items():
                if location_name in key:
                    coords = value
                    location_name = key
                    break

        if not coords:
            return {"error": f"'{location_name}' 지역의 좌표를 찾을 수 없습니다."}

        nx, ny = str(coords["nx"]), str(coords["ny"])

        weather_data, error_msg = await self.weather_cog.get_formatted_weather_string(day_offset, location_name, nx, ny)
        if error_msg:
            return {"error": error_msg}
        return {"weather_info": weather_data}

    async def get_stock_price(self, stock_name: str) -> dict:
        """주식명을 기반으로 해외 주식의 시세를 조회합니다. (현재 한글 종목명 조회는 지원되지 않음)"""
        if is_korean(stock_name):
            logger.warning(f"'{stock_name}'은(는) 한글 종목으로, 현재 지원되지 않습니다.")
            return {"error": f"'{stock_name}'과(와) 같은 한글 종목명 검색은 현재 지원되지 않아. 미안! 대신 미국 주식 티커(예: AAPL)를 알려주면 찾아볼게."}
        else:
            # 해외 주식은 Ticker Symbol로 조회해야 함
            logger.info(f"'{stock_name}'은(는) Ticker로 간주하여 Finnhub API를 호출합니다.")
            return await finnhub.get_stock_quote(stock_name)

    async def get_company_news(self, stock_name: str, count: int = 3) -> dict:
        """특정 종목(Ticker Symbol)에 대한 최신 뉴스를 조회합니다."""
        return await finnhub.get_company_news(stock_name, count)

    async def search_for_place(self, query: str, page_size: int = 5) -> dict:
        """키워드로 장소를 검색하고, 여러 개의 결과를 반환합니다."""
        return await kakao.search_place_by_keyword(query, page_size=page_size)

    async def get_krw_exchange_rate(self, currency_code: str = "USD") -> dict:
        """특정 통화의 원화(KRW) 대비 환율을 조회합니다."""
        return await exim.get_exchange_rate(currency_code)

    async def get_loan_rates(self) -> dict:
        """한국수출입은행의 대출 금리 정보를 조회합니다."""
        return await exim.get_loan_interest_rates()

    async def get_international_rates(self) -> dict:
        """한국수출입은행의 국제 금리 정보를 조회합니다."""
        return await exim.get_international_interest_rates()

    async def recommend_games(self, ordering: str = '-released', genres: str = None, page_size: int = 5) -> dict:
        """다양한 조건에 따라 비디오 게임을 추천합니다."""
        return await rawg.get_games(ordering=ordering, genres=genres, page_size=page_size)

async def setup(bot: commands.Bot):
    await bot.add_cog(ToolsCog(bot))
    logger.info("ToolsCog 로드 완료.")

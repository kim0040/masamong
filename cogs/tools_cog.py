# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
import re

from logger_config import logger
from utils.api_handlers import finnhub, kakao, krx, exim, rawg

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
        logger.info("ToolsCog 초기화 완료.")

    async def get_stock_price(self, stock_name: str) -> dict:
        """주식명을 기반으로 국내 또는 해외 주식의 시세를 조회합니다."""
        if is_korean(stock_name):
            logger.info(f"'{stock_name}'은(는) 한글이 포함되어 KRX API를 호출합니다.")
            return await krx.get_stock_price(stock_name)
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

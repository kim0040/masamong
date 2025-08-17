# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
import re

from logger_config import logger
from utils.api_handlers import riot, finnhub, kakao, krx, exim

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

    async def search_for_place(self, query: str) -> dict:
        """키워드로 장소를 검색합니다."""
        return await kakao.search_place_by_keyword(query)

    async def get_krw_exchange_rate(self, currency_code: str = "USD") -> dict:
        """특정 통화의 원화(KRW) 대비 환율을 조회합니다."""
        return await exim.get_exchange_rate(currency_code)

    async def get_loan_rates(self) -> dict:
        """한국수출입은행의 대출 금리 정보를 조회합니다."""
        return await exim.get_loan_interest_rates()

    async def get_international_rates(self) -> dict:
        """한국수출입은행의 국제 금리 정보를 조회합니다."""
        return await exim.get_international_interest_rates()

    async def get_lol_match_history(self, riot_id: str, count: int = 1) -> dict:
        """Riot ID를 사용하여 LoL 최근 전적을 조회합니다."""
        # Riot ID 형식: "게임이름#태그라인" (예: "Hide on bush#KR1")
        parts = riot_id.split('#')
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return {"error": "Riot ID는 '이름#태그' 형식이어야 합니다. (예: Hide on bush#KR1)"}
        game_name, tag_line = parts[0], parts[1]

        puuid_result = await riot.get_puuid_by_riot_id(game_name, tag_line)
        if puuid_result.get("error"):
            return puuid_result
        puuid = puuid_result["puuid"]

        match_ids_result = await riot.get_match_ids_by_puuid(puuid, count)
        if match_ids_result.get("error"):
            return match_ids_result

        match_details_list = []
        for match_id in match_ids_result["match_ids"]:
            details = await riot.get_match_details_by_id(match_id, puuid)
            if not details.get("error"):
                match_details_list.append(details)

        if not match_details_list:
            return {"error": "상세 전적을 조회하는 데 실패했습니다."}

        return {"matches": match_details_list}

async def setup(bot: commands.Bot):
    await bot.add_cog(ToolsCog(bot))
    logger.info("ToolsCog 로드 완료.")

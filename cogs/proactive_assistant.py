# -*- coding: utf-8 -*-
"""
사용자의 대화에서 잠재적 의도를 파악하여 선제적으로 정보를 제안하는 '능동적 비서' 기능을 담당하는 Cog입니다.
"""

import discord
from discord.ext import commands
from typing import Optional

import config
from logger_config import logger
from .ai_handler import AIHandler

class ProactiveAssistant(commands.Cog):
    """사용자의 잠재적 요구를 파악하고 능동적으로 제안하는 비서 기능 클래스입니다."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ai_handler: Optional[AIHandler] = None
        logger.info("ProactiveAssistant Cog가 성공적으로 초기화되었습니다.")

    async def cog_load(self):
        """Cog가 로드될 때 호출되어 AI 핸들러 참조를 확보합니다."""
        self.ai_handler = self.bot.get_cog('AIHandler')

    async def analyze_user_intent(self, message: discord.Message) -> Optional[str]:
        """사용자 메시지에서 키워드를 기반으로 잠재적 의도를 분석하고, 적절한 제안 메시지를 생성합니다."""
        if not self.ai_handler or not self.ai_handler.is_ready: return None
        content = message.content.lower()
        keyword_map = {
            'travel': ['여행', '휴가', '도쿄', '파리', '뉴욕', '런던', '서울', '부산', '제주'],
            'finance': ['주식', '투자', '애플', '삼성', '테슬라', '나스닥', '코스피', '환율', '달러', '엔화', '유로'],
            'weather': ['날씨', '비', '눈', '맑음', '흐림', '더위', '추위', '우산'],
            'game': ['게임', '스팀', 'ps5', 'xbox', '닌텐도', 'rpg', 'fps']
        }
        for intent, keywords in keyword_map.items():
            if any(keyword in content for keyword in keywords):
                suggestion_method = getattr(self, f"_suggest_{intent}_info", None)
                if suggestion_method: return await suggestion_method(message)
        return None
    
    async def _suggest_travel_info(self, message: discord.Message) -> str:
        """여행 관련 제안 메시지를 생성합니다."""
        content = message.content.lower()
        destinations = ['도쿄', '파리', '뉴욕', '런던', '서울', '부산', '제주', '오사카', '베이징', '상하이']
        detected_destination = next((dest for dest in destinations if dest in content), None)
        if detected_destination:
            return (
                f"오, {detected_destination} 여행 준비 중이네? 🧳\n\n"
                f"`@마사몽 {detected_destination} 맛집 검색해줘` 같은 식으로 말하면 카카오 검색으로 장소 정보를 찾아줄 수 있어."
            )
        return (
            "여행 얘기 나오면 설레지~ ✈️\n\n"
            "가고 싶은 도시가 있으면 `@마사몽 부산 맛집 찾아줘`처럼 말해서 주변 장소를 찾아봐."
        )
    
    async def _suggest_financial_info(self, message: discord.Message) -> str:
        """금융 관련 제안 메시지를 생성합니다."""
        content = message.content.lower()
        if any(word in content for word in ['환율', '달러', '엔화', '유로']):
            return "환율이 궁금해? 💱\n\n`@마사몽 달러 환율 알려줘`처럼 말하면 최신 매매기준율이랑 스프레드까지 알려줄 수 있어."
        if any(word in content for word in ['주식', '투자', '애플', '삼성', '테슬라', '나스닥', '코스피']):
            return "주식 정보를 찾고 있네? 📈\n\n`@마사몽 애플 주가 알려줘`처럼 물어보면 최신 시세랑 관련 뉴스를 알려줄 수 있어."
        return "투자 얘기 중이네! 💼\n\n궁금한 종목이나 환율이 있으면 `@마사몽 삼성전자 주가 알려줘`, `@마사몽 엔화 환율 알려줘`처럼 물어봐 줘. 실시간으로 확인해줄게."
    
    async def _suggest_weather_info(self, message: discord.Message) -> str:
        """날씨 관련 제안 메시지를 생성합니다."""
        return "날씨가 궁금하시군요! 🌤️\n\n`@마사몽 오늘 날씨` 또는 `@마사몽 내일 서울 날씨`라고 물어보시면 상세한 날씨 정보를 제공해드릴게요!"

    async def _suggest_game_info(self, message: discord.Message) -> str:
        """게임 추천 제안 메시지를 생성합니다."""
        return "게임 추천이 필요하시군요! 🎮\n\n`@마사몽 재밌는 RPG 게임 추천해줘`라고 물어보시면 맞춤형 게임 추천을 해드릴게요!"

async def setup(bot: commands.Bot):
    """Cog를 봇에 등록하는 함수입니다."""
    await bot.add_cog(ProactiveAssistant(bot))

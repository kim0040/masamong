# -*- coding: utf-8 -*-
"""
사용자의 대화에서 잠재적 의도를 파악하여 능동적으로 정보를 제안하거나,
사용자가 설정한 개인화된 알림을 주기적으로 확인하고 알려주는 '능동적 비서' 기능을 담당하는 Cog입니다.
"""

import discord
from discord.ext import commands, tasks
from datetime import datetime
import json
from typing import Dict, Any, Optional

import config
from logger_config import logger
from utils import db as db_utils
from utils.api_handlers import exim
from .ai_handler import AIHandler

class ProactiveAssistant(commands.Cog):
    """사용자의 잠재적 요구를 파악하고 능동적으로 제안하는 비서 기능 클래스입니다."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ai_handler: Optional[AIHandler] = None
        self.proactive_monitoring_loop.start()
        logger.info("ProactiveAssistant Cog가 성공적으로 초기화되었습니다.")

    async def cog_load(self):
        """Cog가 로드될 때 호출되어 AI 핸들러 참조를 확보합니다."""
        self.ai_handler = self.bot.get_cog('AIHandler')

    def cog_unload(self):
        """Cog 언로드 시 백그라운드 태스크를 안전하게 중지합니다."""
        self.proactive_monitoring_loop.cancel()

    async def analyze_user_intent(self, message: discord.Message) -> Optional[str]:
        """사용자 메시지에서 키워드를 기반으로 잠재적 의도를 분석하고, 적절한 제안 메시지를 생성합니다."""
        if not self.ai_handler or not self.ai_handler.is_ready: return None
        content = message.content.lower()
        keyword_map = {
            'travel': ['여행', '휴가', '도쿄', '파리', '뉴욕', '런던', '서울', '부산', '제주'],
            'finance': ['환율', '달러', '엔화', '주식', '투자', '대출', '금리'],
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
            return f"오, {detected_destination} 여행 가시는구나! 🧳\n\n현재 {detected_destination}의 날씨와 가볼만한 장소 정보를 알려드릴까요? `@마사몽 {detected_destination} 여행 정보 알려줘`라고 물어보세요!"
        else:
            return "여행 계획 세우고 계시는군요! ✈️\n\n어떤 도시로 가시나요? 날씨, 명소, 이벤트 정보를 종합적으로 알려드릴 수 있어요. `@마사몽 [도시명] 여행 정보 알려줘`라고 말씀해주세요!"
    
    async def _suggest_financial_info(self, message: discord.Message) -> str:
        """금융 관련 제안 메시지를 생성합니다."""
        content = message.content.lower()
        if any(word in content for word in ['환율', '달러', '엔화', '유로']):
            return "환율 정보가 필요하시군요! 💰\n\n`@마사몽 달러 환율 알려줘`라고 물어보시면 상세한 환율 정보를 제공해드릴게요!"
        elif any(word in content for word in ['주식', '투자', '애플', '삼성', '테슬라']):
            return "주식 정보를 찾고 계시는군요! 📈\n\n`@마사몽 애플 주가 알려줘`라고 물어보시면 최신 주식 정보를 제공해드릴게요!"
        else:
            return "금융 정보가 필요하시군요! 💼\n\n환율, 주식, 금리 등 다양한 금융 정보를 제공할 수 있어요. 구체적으로 어떤 정보가 필요하신지 말씀해주세요!"
    
    async def _suggest_weather_info(self, message: discord.Message) -> str:
        """날씨 관련 제안 메시지를 생성합니다."""
        return "날씨가 궁금하시군요! 🌤️\n\n`@마사몽 오늘 날씨` 또는 `@마사몽 내일 서울 날씨`라고 물어보시면 상세한 날씨 정보를 제공해드릴게요!"

    async def _suggest_game_info(self, message: discord.Message) -> str:
        """게임 추천 제안 메시지를 생성합니다."""
        return "게임 추천이 필요하시군요! 🎮\n\n`@마사몽 재밌는 RPG 게임 추천해줘`라고 물어보시면 맞춤형 게임 추천을 해드릴게요!"

    @tasks.loop(minutes=30)
    async def proactive_monitoring_loop(self):
        """30분마다 주기적으로 실행되어, 설정된 모든 알림을 확인하는 메인 루프입니다."""
        await self.bot.wait_until_ready()
        await self._check_exchange_rate_alerts()

    async def _check_exchange_rate_alerts(self):
        """DB에 저장된 모든 환율 알림 설정을 확인하고, 조건 충족 시 DM을 보냅니다."""
        try:
            async with self.bot.db.execute("SELECT user_id, preference_value FROM user_preferences WHERE preference_type = 'exchange_rate_alert'") as cursor:
                alerts = await cursor.fetchall()
            for user_id, alert_data in alerts:
                try:
                    alert_config = json.loads(alert_data)
                    target_currency, target_rate, condition = alert_config.get('currency', 'USD'), alert_config.get('target_rate', 0), alert_config.get('condition', 'below')
                    current_rate = await exim.get_raw_exchange_rate(target_currency)
                    if not current_rate: continue
                    should_alert = (condition == 'below' and current_rate <= target_rate) or (condition == 'above' and current_rate >= target_rate)
                    if should_alert:
                        user = self.bot.get_user(user_id)
                        if user:
                            await user.send(f"🔔 환율 알림\n\n{target_currency} 환율이 {current_rate:,.2f}원에 도달했습니다!\n(설정 목표: {target_rate:,.2f}원 {condition}) ")
                            await db_utils.remove_user_preference(self.bot.db, user_id, 'exchange_rate_alert')
                except (json.JSONDecodeError, KeyError) as e:
                    logger.error(f"환율 알림 처리 오류 (사용자 {user_id}): {e}")
        except Exception as e:
            logger.error(f"환율 알림 확인 중 오류 발생: {e}", exc_info=True)

    @commands.command(name="환율알림", aliases=["환율알림설정", "exchange_alert"])
    @commands.guild_only()
    async def set_exchange_alert(self, ctx: commands.Context, currency: str, target_rate: float, condition: str = "below"):
        """사용자가 특정 통화에 대한 환율 알림을 설정합니다."""
        if condition not in ["below", "above"]: await ctx.send("❌ 조건은 'below' 또는 'above'만 사용할 수 있어요."); return
        alert_config = {'currency': currency.upper(), 'target_rate': target_rate, 'condition': condition}
        try:
            await db_utils.set_user_preference(self.bot.db, ctx.author.id, 'exchange_rate_alert', alert_config)
            condition_text = "이하" if condition == "below" else "이상"
            await ctx.send(f"✅ {currency.upper()} 환율이 {target_rate:,.2f}원 {condition_text}일 때 DM으로 알려드릴게요!")
        except Exception as e:
            logger.error(f"환율 알림 설정 중 오류: {e}", extra={'user_id': ctx.author.id}, exc_info=True)
            await ctx.send("❌ 알림 설정 중 오류가 발생했어요.")
    
    @commands.command(name="알림해제", aliases=["알림삭제", "remove_alert"])
    @commands.guild_only()
    async def remove_alert(self, ctx: commands.Context, alert_type: str):
        """설정한 알림을 해제합니다. (예: `!알림해제 exchange_rate_alert`) """
        try:
            await db_utils.remove_user_preference(self.bot.db, ctx.author.id, alert_type)
            await ctx.send(f"✅ `{alert_type}` 알림이 해제되었습니다.")
        except Exception as e:
            logger.error(f"알림 해제 중 오류: {e}", extra={'user_id': ctx.author.id}, exc_info=True)
            await ctx.send("❌ 알림 해제 중 오류가 발생했어요.")

async def setup(bot: commands.Bot):
    """Cog를 봇에 등록하는 함수입니다."""
    await bot.add_cog(ProactiveAssistant(bot))

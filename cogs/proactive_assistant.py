# -*- coding: utf-8 -*-
"""
능동적 비서 기능을 담당하는 Cog
Phase 3: 지능 - 기능 심화 및 잠재력 극대화
"""

import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import pytz
import asyncio
import json
import re
from typing import Dict, List, Optional, Any

import config
from logger_config import logger
from utils import db as db_utils
from .ai_handler import AIHandler

KST = pytz.timezone('Asia/Seoul')

class ProactiveAssistant(commands.Cog):
    """사용자의 잠재적 요구를 파악하고 능동적으로 제안하는 비서 기능"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ai_handler: Optional[AIHandler] = None
        self.user_preferences: Dict[int, Dict[str, Any]] = {}
        self.notification_queue: asyncio.Queue = asyncio.Queue()
        
    async def cog_load(self):
        """Cog 로드 시 실행"""
        self.ai_handler = self.bot.get_cog('AIHandler')
        if self.ai_handler and self.ai_handler.is_ready:
            self.proactive_monitoring_loop.start()
            logger.info("ProactiveAssistant: 능동적 모니터링 루프 시작")
    
    def cog_unload(self):
        """Cog 언로드 시 실행"""
        if hasattr(self, 'proactive_monitoring_loop') and self.proactive_monitoring_loop.is_running():
            self.proactive_monitoring_loop.cancel()
    
    async def analyze_user_intent(self, message: discord.Message) -> Optional[str]:
        """사용자 메시지에서 잠재적 의도를 분석하고 제안을 생성"""
        if not self.ai_handler or not self.ai_handler.is_ready:
            return None
        
        content = message.content.lower()
        
        # 여행 관련 키워드 감지
        travel_keywords = ['여행', '휴가', '가족여행', '해외여행', '국내여행', '도쿄', '파리', '뉴욕', '런던', '서울', '부산', '제주']
        if any(keyword in content for keyword in travel_keywords):
            return await self._suggest_travel_info(message)
        
        # 금융 관련 키워드 감지
        finance_keywords = ['환율', '달러', '엔화', '유로', '주식', '투자', '저축', '대출', '금리']
        if any(keyword in content for keyword in finance_keywords):
            return await self._suggest_financial_info(message)
        
        # 날씨 관련 키워드 감지
        weather_keywords = ['날씨', '비', '눈', '맑음', '흐림', '더위', '추위', '우산', '외출']
        if any(keyword in content for keyword in weather_keywords):
            return await self._suggest_weather_info(message)
        
        # 게임 관련 키워드 감지
        game_keywords = ['게임', '스팀', 'ps5', 'xbox', '닌텐도', 'pc게임', '모바일게임', 'rpg', 'fps']
        if any(keyword in content for keyword in game_keywords):
            return await self._suggest_game_recommendation(message)
        
        return None
    
    async def _suggest_travel_info(self, message: discord.Message) -> str:
        """여행 관련 제안 생성"""
        content = message.content.lower()
        
        # 목적지 추출 시도
        destinations = ['도쿄', '파리', '뉴욕', '런던', '서울', '부산', '제주', '오사카', '베이징', '상하이']
        detected_destination = None
        for dest in destinations:
            if dest in content:
                detected_destination = dest
                break
        
        if detected_destination:
            return f"오, {detected_destination} 여행 가시는구나! 🧳\n\n현재 {detected_destination}의 날씨와 가볼만한 장소, 그리고 열리는 이벤트 정보를 알려드릴까요? `@마사몽 {detected_destination} 여행 정보 알려줘`라고 물어보시면 상세한 정보를 제공해드릴게요!"
        else:
            return "여행 계획 세우고 계시는군요! ✈️\n\n어떤 도시로 가시나요? 날씨, 명소, 이벤트 정보를 종합적으로 알려드릴 수 있어요. `@마사몽 [도시명] 여행 정보 알려줘`라고 말씀해주세요!"
    
    async def _suggest_financial_info(self, message: discord.Message) -> str:
        """금융 관련 제안 생성"""
        content = message.content.lower()
        
        if any(word in content for word in ['환율', '달러', '엔화', '유로']):
            return "환율 정보가 필요하시군요! 💰\n\n현재 주요 통화의 환율과 송금/현찰 환율 정보를 알려드릴 수 있어요. `@마사몽 달러 환율 알려줘`라고 물어보시면 상세한 환율 정보를 제공해드릴게요!"
        elif any(word in content for word in ['주식', '투자', '애플', '삼성', '테슬라']):
            return "주식 정보를 찾고 계시는군요! 📈\n\n특정 종목의 현재 주가, 뉴스, 변동률 정보를 알려드릴 수 있어요. `@마사몽 애플 주가 알려줘`라고 물어보시면 최신 주식 정보를 제공해드릴게요!"
        else:
            return "금융 정보가 필요하시군요! 💼\n\n환율, 주식, 금리 등 다양한 금융 정보를 제공할 수 있어요. 구체적으로 어떤 정보가 필요하신지 말씀해주세요!"
    
    async def _suggest_weather_info(self, message: discord.Message) -> str:
        """날씨 관련 제안 생성"""
        return "날씨가 궁금하시군요! 🌤️\n\n현재 날씨, 내일/모레 예보, 강수확률 등 상세한 날씨 정보를 알려드릴 수 있어요. `@마사몽 오늘 날씨` 또는 `@마사몽 내일 서울 날씨`라고 물어보시면 정확한 날씨 정보를 제공해드릴게요!"
    
    async def _suggest_game_recommendation(self, message: discord.Message) -> str:
        """게임 추천 제안 생성"""
        return "게임 추천이 필요하시군요! 🎮\n\n장르별, 플랫폼별 게임 추천과 상세 정보(평점, 플레이타임, 메타크리틱 점수 등)를 제공할 수 있어요. `@마사몽 재밌는 RPG 게임 추천해줘`라고 물어보시면 맞춤형 게임 추천을 해드릴게요!"
    
    async def set_user_preference(self, user_id: int, preference_type: str, value: Any):
        """사용자 선호도 저장"""
        if user_id not in self.user_preferences:
            self.user_preferences[user_id] = {}
        
        self.user_preferences[user_id][preference_type] = value
        
        # 데이터베이스에 저장
        try:
            await self.bot.db.execute("""
                INSERT OR REPLACE INTO user_preferences (user_id, preference_type, preference_value, updated_at)
                VALUES (?, ?, ?, ?)
            """, (user_id, preference_type, json.dumps(value), datetime.now(KST).isoformat()))
            await self.bot.db.commit()
        except Exception as e:
            logger.error(f"사용자 선호도 저장 오류: {e}", extra={'user_id': user_id})
    
    async def get_user_preference(self, user_id: int, preference_type: str) -> Optional[Any]:
        """사용자 선호도 조회"""
        try:
            async with self.bot.db.execute("""
                SELECT preference_value FROM user_preferences 
                WHERE user_id = ? AND preference_type = ?
            """, (user_id, preference_type)) as cursor:
                result = await cursor.fetchone()
                if result:
                    return json.loads(result[0])
        except Exception as e:
            logger.error(f"사용자 선호도 조회 오류: {e}", extra={'user_id': user_id})
        return None
    
    @tasks.loop(minutes=30)
    async def proactive_monitoring_loop(self):
        """능동적 모니터링 루프"""
        await self.bot.wait_until_ready()
        
        # 환율 알림 체크
        await self._check_exchange_rate_alerts()
        
        # 날씨 알림 체크
        await self._check_weather_alerts()
    
    async def _check_exchange_rate_alerts(self):
        """환율 알림 체크"""
        try:
            # 환율 알림 설정이 있는 사용자들 조회
            async with self.bot.db.execute("""
                SELECT user_id, preference_value FROM user_preferences 
                WHERE preference_type = 'exchange_rate_alert'
            """) as cursor:
                alerts = await cursor.fetchall()
            
            for user_id, alert_data in alerts:
                try:
                    alert_config = json.loads(alert_data)
                    target_currency = alert_config.get('currency', 'USD')
                    target_rate = alert_config.get('target_rate', 0)
                    condition = alert_config.get('condition', 'below')  # 'below' or 'above'
                    
                    # 현재 환율 조회 (실제 구현에서는 API 호출)
                    # 여기서는 예시로 가정
                    current_rate = 1350.0  # 실제로는 API에서 가져와야 함
                    
                    should_alert = False
                    if condition == 'below' and current_rate <= target_rate:
                        should_alert = True
                    elif condition == 'above' and current_rate >= target_rate:
                        should_alert = True
                    
                    if should_alert:
                        # 사용자에게 DM 전송
                        user = self.bot.get_user(user_id)
                        if user:
                            message = f"🔔 환율 알림\n\n{target_currency} 환율이 {current_rate:,.2f}원에 도달했습니다!\n설정하신 목표: {target_rate:,.2f}원 {condition}"
                            await user.send(message)
                            
                            # 알림 전송 후 설정 삭제
                            await self.bot.db.execute("""
                                DELETE FROM user_preferences 
                                WHERE user_id = ? AND preference_type = 'exchange_rate_alert'
                            """, (user_id,))
                            await self.bot.db.commit()
                            
                except Exception as e:
                    logger.error(f"환율 알림 처리 오류 (사용자 {user_id}): {e}")
                    
        except Exception as e:
            logger.error(f"환율 알림 체크 오류: {e}")
    
    async def _check_weather_alerts(self):
        """날씨 알림 체크"""
        # 날씨 알림 로직은 WeatherCog에서 이미 구현되어 있음
        pass
    
    @commands.command(name="환율알림", aliases=["환율알림설정", "exchange_alert"])
    async def set_exchange_alert(self, ctx: commands.Context, currency: str, target_rate: float, condition: str = "below"):
        """환율 알림 설정"""
        if condition not in ["below", "above"]:
            await ctx.send("❌ 조건은 'below' 또는 'above'만 가능합니다.")
            return
        
        if target_rate <= 0:
            await ctx.send("❌ 목표 환율은 0보다 큰 값이어야 합니다.")
            return
        
        try:
            await self.set_user_preference(
                ctx.author.id, 
                'exchange_rate_alert', 
                {
                    'currency': currency.upper(),
                    'target_rate': target_rate,
                    'condition': condition
                }
            )
            
            condition_text = "이하" if condition == "below" else "이상"
            await ctx.send(f"✅ {currency.upper()} 환율이 {target_rate:,.2f}원 {condition_text}일 때 알림을 보내드릴게요!")
            
        except Exception as e:
            logger.error(f"환율 알림 설정 오류: {e}", extra={'user_id': ctx.author.id})
            await ctx.send("❌ 알림 설정 중 오류가 발생했습니다.")
    
    @commands.command(name="알림해제", aliases=["알림삭제", "remove_alert"])
    async def remove_alert(self, ctx: commands.Context, alert_type: str = "exchange_rate_alert"):
        """알림 설정 해제"""
        try:
            await self.bot.db.execute("""
                DELETE FROM user_preferences 
                WHERE user_id = ? AND preference_type = ?
            """, (ctx.author.id, alert_type))
            await self.bot.db.commit()
            
            await ctx.send(f"✅ {alert_type} 알림이 해제되었습니다.")
            
        except Exception as e:
            logger.error(f"알림 해제 오류: {e}", extra={'user_id': ctx.author.id})
            await ctx.send("❌ 알림 해제 중 오류가 발생했습니다.")

async def setup(bot: commands.Bot):
    await bot.add_cog(ProactiveAssistant(bot))

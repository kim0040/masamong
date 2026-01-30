# -*- coding: utf-8 -*-
"""
날씨 정보와 관련된 모든 기능을 담당하는 Cog입니다.

주요 기능:
- `!날씨` 명령어를 통해 특정 지역의 날씨 정보를 제공합니다.
- AI 채널에서는 날씨 정보를 바탕으로 AI가 창의적인 답변을 생성합니다.
- 주기적으로 강수 예보를 확인하여 비/눈 소식을 알립니다.
- 지정된 시간에 날씨 정보를 포함한 아침/저녁 인사를 보냅니다.
"""

from __future__ import annotations
import discord
from discord.ext import commands, tasks
import asyncio
from datetime import datetime, timedelta, time as dt_time
import pytz

import config
from logger_config import logger
from utils import db as db_utils, weather as weather_utils, coords as coords_utils
from .ai_handler import AIHandler

KST = pytz.timezone('Asia/Seoul')

class WeatherCog(commands.Cog):
    """날씨 조회와 알림 전송을 전담하는 Cog입니다.

    - 명령어(`!날씨`) 실행 시 좌표 변환, KMA 데이터 조회, 응답 포맷팅을 처리합니다.
    - AI 채널에서는 조회 결과를 `AIHandler`에 전달해 문맥 맞춤형 답변을 생성합니다.
    - 주기적으로 비/눈 예보 및 아침·저녁 인사를 전송하는 백그라운드 태스크를 관리합니다.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ai_handler: AIHandler | None = None
        self.notified_rain_event_starts = set()
        self.last_earthquake_time = datetime.now(KST) - timedelta(hours=1) # Only alert recent ones
        logger.info("WeatherCog가 성공적으로 초기화되었습니다.")

    def setup_and_start_loops(self):
        """봇이 준비되면 설정 플래그에 따라 주기 태스크를 기동합니다.

        Rain/Greeting 알림은 각각 별도의 `tasks.loop`로 구현되어 있으며, 필요 없을 때는
        불필요한 리소스를 소비하지 않도록 시작 자체를 건너뜁니다.
        """
        if config.ENABLE_RAIN_NOTIFICATION and config.RAIN_NOTIFICATION_CHANNEL_ID:
            logger.info("주기적 강수 알림 루프를 시작합니다.")
            self.rain_notification_loop.start()
        if config.ENABLE_GREETING_NOTIFICATION and (getattr(config, 'GREETING_NOTIFICATION_CHANNEL_ID', None) or config.RAIN_NOTIFICATION_CHANNEL_ID):
            logger.info("아침/저녁 인사 알림 루프를 시작합니다.")
            self.morning_greeting_loop.start()
            self.evening_greeting_loop.start()
        
        # Earthquake Alert Loop (Always active if notification channel exists)
        if config.RAIN_NOTIFICATION_CHANNEL_ID: # Reuse rain channel for disasters
            logger.info("지진 알림 모니터링 루프를 시작합니다.")
            self.earthquake_alert_loop.start()

    def cog_unload(self):
        """Cog가 언로드될 때, 실행 중인 모든 루프를 안전하게 취소합니다."""
        self.rain_notification_loop.cancel()
        self.morning_greeting_loop.cancel()
        self.evening_greeting_loop.cancel()
        self.earthquake_alert_loop.cancel()

    async def get_mid_term_weather(self, day_offset: int, location_name: str) -> str:
        """중기예보를 조회합니다. (V2 실패 시 V1 Fallback)"""
        try:
            # 1. Try V2 (Flat file)
            # Simple mapping for V2
            v2_code = "11B00000" # Seoul/Incheon/Gyeonggi
            if "광양" in location_name or "전남" in location_name or "광주" in location_name:
                 v2_code = "11F20000" # Jeonnam
            elif "부산" in location_name or "경남" in location_name:
                 v2_code = "11H20000" # Busan/Gyeongnam
            elif "대구" in location_name or "경북" in location_name:
                 v2_code = "11H10000" # Daegu/Gyeongbuk
            
            res = await weather_utils.get_mid_term_forecast_v2(self.bot.db, v2_code)
            if res: return res

            # 2. Fallback to V1 (API)
            # Mappings for V1 (Land, Temp)
            # Land: Wide area code (same as V2 usually)
            # Temp: Specific city code
            v1_land_code = v2_code 
            v1_temp_code = "11B10101" # Seoul Default
            
            if "광양" in location_name or "전남" in location_name:
                v1_land_code = "11F20000" # Jeonnam
                v1_temp_code = "11F20501" # Gwangju (Best proxy if Gwangyang specific unavailable)
                # Note: Known Gwangyang code might be 11F20403 but Gwangju is safer for API availability
            elif "부산" in location_name:
                v1_land_code = "11H20000"
                v1_temp_code = "11H20201" # Busan
            elif "대구" in location_name:
                v1_land_code = "11H10000"
                v1_temp_code = "11H10701" # Daegu
                
            res_v1 = await weather_utils.get_mid_term_forecast(self.bot.db, v1_temp_code, v1_land_code, day_offset, location_name)
            return res_v1 if res_v1 else "중기예보 데이터를 불러올 수 없습니다."
            
        except Exception as e:
            logger.error(f"중기예보 조회 실패: {e}", exc_info=True)
            return config.MSG_WEATHER_FETCH_ERROR

    async def get_formatted_weather_string(self, day_offset: int, location_name: str, nx: str, ny: str) -> tuple[str | None, str | None]:
        """기상청 자료를 조회해 사용자에게 보여줄 문자열을 생성합니다.

        Args:
            day_offset (int): 0=오늘, 1=내일, 2=모레 등 조회할 날짜 오프셋.
            location_name (str): 응답에 표시할 지역명.
            nx (str): 기상청 격자 X 좌표.
            ny (str): 기상청 격자 Y 좌표.

        Returns:
            tuple[str | None, str | None]: (정상 응답 문자열, 오류 메시지).
            성공 시 첫 번째 값이 문자열이고, 문제가 있으면 두 번째 값에 오류 설명이 담깁니다.
        """
        try:
            day_names = ["오늘", "내일", "모레"]
            day_name = day_names[day_offset] if 0 <= day_offset < len(day_names) else f"{day_offset}일 후"
            if day_offset == 0:
                current_weather_data = await weather_utils.get_current_weather_from_kma(self.bot.db, nx, ny)
                if isinstance(current_weather_data, dict) and current_weather_data.get("error"): return None, current_weather_data.get("message", config.MSG_WEATHER_FETCH_ERROR)
                if current_weather_data is None: return None, config.MSG_WEATHER_FETCH_ERROR
                current_weather_str = weather_utils.format_current_weather(current_weather_data)
                short_term_data = await weather_utils.get_short_term_forecast_from_kma(self.bot.db, nx, ny)
                formatted_forecast = weather_utils.format_short_term_forecast(short_term_data, day_name, target_day_offset=0)
                current_weather_str = weather_utils.format_current_weather(current_weather_data)
                short_term_data = await weather_utils.get_short_term_forecast_from_kma(self.bot.db, nx, ny)
                formatted_forecast = weather_utils.format_short_term_forecast(short_term_data, day_name, target_day_offset=0)
                
                # Extended Info (Overview & Typhoon & Warnings & Impact)
                # "Smart Decision": Fetch urgently in parallel to avoid blocking core weather.
                
                async def fetch_optional(coro, name):
                    try:
                        return await coro
                    except Exception as e:
                        # logger.warning(f"Optional weather fetch failed ({name}): {e}")
                        return None

                # Parallel execution with short timeout for optional data
                overview_task = fetch_optional(weather_utils.get_weather_overview(self.bot.db, timeout=5.0), "overview")
                warnings_task = fetch_optional(weather_utils.get_active_warnings(self.bot.db, timeout=5.0), "warnings")
                impact_task = fetch_optional(weather_utils.get_impact_forecast(self.bot.db, timeout=5.0), "impact")
                typhoons_task = fetch_optional(weather_utils.get_typhoons(self.bot.db, timeout=5.0), "typhoons")
                
                results = await asyncio.gather(overview_task, warnings_task, impact_task, typhoons_task)
                overview, warnings, impact, typhoons = results
                
                parts = [f"[{location_name} 상세 날씨 정보 Context]"]
                
                if overview: parts.append(f"📢 **기상 개황**: {overview}")
                if warnings: parts.append(f"🚨 **기상 특보**: {warnings}")
                if impact: parts.append(f"⚠️ **영향 예보**: {impact}")
                if typhoons: parts.append(f"🌀 **태풍 정보**: {typhoons}")
                
                # 5. Core Weather (Current + Short-term)
                parts.append(f"🌡️ **현재 날씨**: {current_weather_str}")
                parts.append(f"📅 **단기 예보**: {formatted_forecast}")
                
                # 6. Mid-term (If explicitly relevant, or just append distinct note)
                # Since day_offset is 0 here (default logic), mid-term might be redundant unless user asked "future".
                # But providing it as context for "outlook" queries helps.
                if day_offset >= 3:
                     # Fetch V2 Mid-term (Land Code needed... e.g., 11B00000)
                     # Mapping logic needed. For now use default Seoul area 11B00000 or derive from coords?
                     # Simplified: Just fetch context if available key allows it.
                     pass 
                
                final_context = "\n".join(parts)
                # Show user what Masamong sees (as requested)
                logger.info(f"☀️ [Weather Context to AI]:\n{final_context}")
                return final_context.strip(), None
            else:
                forecast_data = await weather_utils.get_short_term_forecast_from_kma(self.bot.db, nx, ny)
                if isinstance(forecast_data, dict) and forecast_data.get("error"): return None, forecast_data.get("message", config.MSG_WEATHER_FETCH_ERROR)
                if forecast_data is None: return None, config.MSG_WEATHER_FETCH_ERROR
                formatted_forecast = weather_utils.format_short_term_forecast(forecast_data, day_name, target_day_offset=day_offset)
                return f"[{location_name} 날씨 정보] {formatted_forecast}", None
        except Exception as e:
            logger.error(f"날씨 정보 포맷팅 중 오류: {e}", exc_info=True)
            return None, config.MSG_WEATHER_FETCH_ERROR

    async def prepare_weather_response_for_ai(self, original_message: discord.Message, day_offset: int, location_name: str, nx: str, ny: str, user_original_query: str):
        """날씨 조회 결과를 AI 채널/일반 채널에 맞게 전송합니다.

        Args:
            original_message (discord.Message): 사용자의 원본 메시지 객체.
            day_offset (int): 오늘/내일/모레 구분값.
            location_name (str): 사용자에게 노출할 지역명.
            nx (str): 기상청 격자 X 좌표.
            ny (str): 기상청 격자 Y 좌표.
            user_original_query (str): 사용자가 입력한 원래 질문 텍스트.

        Notes:
            AI 채널에서는 `AIHandler`를 통해 창의적 멘트를 생성하며, 일반 채널에서는 즉시 텍스트를 회신합니다.
        """
        if not weather_utils.get_kma_api_key():
            await original_message.reply(config.MSG_WEATHER_API_KEY_MISSING, mention_author=False)
            return

        async with original_message.channel.typing():
            # 1. 기상 특보 조회
            alerts_data = await weather_utils.get_weather_alerts_from_kma(self.bot.db)
            formatted_alerts = None
            if isinstance(alerts_data, str):
                formatted_alerts = weather_utils.format_weather_alerts(alerts_data)
            
            # 2. 날씨 정보 조회
            weather_data_str, error_message = await self.get_formatted_weather_string(day_offset, location_name, nx, ny)
            if error_message:
                await original_message.reply(error_message, mention_author=False)
                return
            if not weather_data_str:
                await original_message.reply(config.MSG_WEATHER_NO_DATA, mention_author=False)
                return

            # 3. 특보와 날씨 정보 결합
            final_response_str = weather_data_str
            if formatted_alerts:
                final_response_str = f"{formatted_alerts}\n\n---\n\n{weather_data_str}"

            # 4. AI 또는 일반 응답 생성
            self.ai_handler = self.bot.get_cog('AIHandler')
            is_ai_channel_and_enabled = self.ai_handler and self.ai_handler.is_ready and config.CHANNEL_AI_CONFIG.get(original_message.channel.id, {}).get("allowed", False)
            
            if is_ai_channel_and_enabled:
                context = {"location_name": location_name, "weather_data": final_response_str}
                ai_response = await self.ai_handler.generate_creative_text(original_message.channel, original_message.author, "answer_weather", context)
                await original_message.reply(ai_response or config.MSG_AI_ERROR, mention_author=False)
            else:
                await original_message.reply(f"📍 **{location_name}**\n{final_response_str}", mention_author=False)

    @commands.command(name="날씨", aliases=["weather", "현재날씨", "오늘날씨"])
    async def weather_command(self, ctx: commands.Context, *, location_query: str = ""):
        """`!날씨 [날짜] [지역]` 패턴을 해석해 날씨 정보를 제공합니다.

        Args:
            ctx (commands.Context): 명령을 실행한 컨텍스트.
            location_query (str, optional): 사용자가 입력한 지역/날짜 정보.
        """
        user_original_query = location_query.strip() if location_query else "오늘 날씨"
        location_name, nx, ny = config.DEFAULT_LOCATION_NAME, config.DEFAULT_NX, config.DEFAULT_NY
        coords = await coords_utils.get_coords_from_db(self.bot.db, user_original_query.lower())
        if coords: location_name, nx, ny = coords['name'], str(coords['nx']), str(coords['ny'])
        
        # [NEW] Weekly Weather Logic (Short-term + Mid-term)
        if "이번주" in user_original_query or "주간" in user_original_query:
            # 1. Short-term (+1, +2 days)
            short_term_data = await weather_utils.get_short_term_forecast_from_kma(self.bot.db, nx, ny)
            short_term_summary = ""
            if short_term_data and not short_term_data.get("error"):
                 tomorrow_summary = weather_utils.format_short_term_forecast(short_term_data, "내일", 1)
                 dayafter_summary = weather_utils.format_short_term_forecast(short_term_data, "모레", 2)
                 short_term_summary = f"{tomorrow_summary}\n{dayafter_summary}"
            
            # 2. Mid-term (+3 ~ +10 days)
            mid_term_data = await self.get_mid_term_weather(3, location_name)
            
            full_weekly_data = f"--- [단기 예보 (내일/모레)] ---\n{short_term_summary}\n\n--- [중기 예보 (3일 후 ~ 10일 후)] ---\n{mid_term_data}"

            # Send via AI for summarization
            self.ai_handler = self.bot.get_cog('AIHandler')
            is_ai_channel = self.ai_handler and self.ai_handler.is_ready and config.CHANNEL_AI_CONFIG.get(ctx.channel.id, {}).get("allowed", False)
            
            if is_ai_channel:
                 context = {"location_name": location_name, "weather_data": full_weekly_data}
                 ai_response = await self.ai_handler.generate_creative_text(ctx.channel, ctx.author, "answer_weather_weekly", context)
                 await ctx.reply(ai_response or config.MSG_AI_ERROR, mention_author=False)
            else:
                 await ctx.reply(f"📅 **{location_name} 이번 주 날씨 종합**\n{full_weekly_data}", mention_author=False)
            return

        day_offset = 1 if "내일" in user_original_query else 2 if "모레" in user_original_query else 0
        await self.prepare_weather_response_for_ai(ctx.message, day_offset, location_name, nx, ny, user_original_query)

    def _parse_rain_periods(self, forecast_data: dict) -> list:
        """단기예보에서 강수 관련 값을 묶어 강수 구간을 계산합니다.

        Returns:
            list[dict]: `start_dt`, `end_dt`, `type`, `max_pop`, `key` 정보를 담은 기간 목록.
        """
        try:
            items = forecast_data["item"]
        except (KeyError, TypeError):
            return []

        hourly_data: dict[tuple[str, str], dict[str, str]] = {}
        for item in items:
            fcst_date, fcst_time = item.get("fcstDate"), item.get("fcstTime")
            category, value = item.get("category"), item.get("fcstValue")
            if not fcst_date or not fcst_time or not category:
                continue
            if category not in {"PTY", "POP"}:
                continue
            entry = hourly_data.setdefault((fcst_date, fcst_time), {})
            entry[category] = value

        precipitation_periods, current_period = [], None
        for key_time in sorted(hourly_data.keys()):
            data = hourly_data.get(key_time)
            if not data:
                continue

            pty_code = str(data.get("PTY", "0"))
            try:
                pop_value = int(data.get("POP") or 0)
            except (TypeError, ValueError):
                pop_value = 0

            is_raining = pty_code != "0" and pop_value >= config.RAIN_NOTIFICATION_THRESHOLD_POP

            try:
                current_dt = KST.localize(datetime.strptime(f"{key_time[0]}{key_time[1].zfill(4)}", "%Y%m%d%H%M"))
            except (ValueError, TypeError):
                continue

            if is_raining:
                precip_type = "눈" if pty_code in {"3", "7"} else "비"
                if current_period is None or current_period["type"] != precip_type:
                    if current_period:
                        precipitation_periods.append(current_period)
                    current_period = {
                        "type": precip_type,
                        "start_dt": current_dt,
                        "end_dt": current_dt,
                        "max_pop": pop_value,
                        "key": key_time,
                    }
                else:
                    current_period["end_dt"] = current_dt
                    current_period["max_pop"] = max(current_period["max_pop"], pop_value)
            elif current_period:
                precipitation_periods.append(current_period)
                current_period = None

        if current_period:
            precipitation_periods.append(current_period)

        return precipitation_periods

    @tasks.loop(minutes=config.WEATHER_CHECK_INTERVAL_MINUTES)
    async def rain_notification_loop(self):
        """정해진 주기로 강수 예보를 조회하고 필요 시 서버에 알립니다.

        예보상 비/눈 확률이 임계값을 넘으면 채널에 안내 메시지를 전송하고, 동일 시간대 중복 알림을 방지합니다.
        """
        await self.bot.wait_until_ready()
        if not weather_utils.get_kma_api_key(): return
        alert_channel = self.bot.get_channel(config.RAIN_NOTIFICATION_CHANNEL_ID)
        if not alert_channel: return
        forecast = await weather_utils.get_short_term_forecast_from_kma(self.bot.db, config.DEFAULT_NX, config.DEFAULT_NY)
        if not forecast or isinstance(forecast, dict) and forecast.get("error"): return
        self.notified_rain_event_starts = {k for k in self.notified_rain_event_starts if KST.localize(datetime.strptime(f"{k[0]}{k[1]}","%Y%m%d%H%M")) >= datetime.now(KST) - timedelta(days=1)}
        for period in self._parse_rain_periods(forecast):
            if period["start_dt"] >= datetime.now(KST) and period["key"] not in self.notified_rain_event_starts:
                start_display = period["start_dt"].strftime("%m월 %d일 %H시"); end_display = (period["end_dt"] + timedelta(hours=1)).strftime("%H시")
                if period["start_dt"].date() != period["end_dt"].date(): end_display = (period["end_dt"] + timedelta(hours=1)).strftime("%m월 %d일 %H시")
                precip_type = "눈❄️" if period["type"] == "눈" else "비☔"
                alert_info = f"{config.DEFAULT_LOCATION_NAME}에 '{start_display}'부터 '{end_display}'까지 {precip_type}가 올 것으로 예상됩니다. 최대 확률은 {period['max_pop']}%입니다."
                self.ai_handler = self.bot.get_cog('AIHandler')
                ai_msg = await self.ai_handler.generate_system_alert_message(alert_channel.id, alert_info, f"{precip_type} 예보") if self.ai_handler and self.ai_handler.is_ready else None
                await alert_channel.send(ai_msg or f"{precip_type} **{config.DEFAULT_LOCATION_NAME} {precip_type} 예보** {precip_type}\n{alert_info}")
                self.notified_rain_event_starts.add(period["key"])

    async def _send_greeting_notification(self, greeting_type: str):
        """아침/저녁 유형에 맞춰 날씨 요약과 인사 메시지를 전송합니다.

        Args:
            greeting_type (str): "아침" 또는 "저녁" 중 하나.
        """
        await self.bot.wait_until_ready()
        if not weather_utils.get_kma_api_key(): return
        channel_id = getattr(config, 'GREETING_NOTIFICATION_CHANNEL_ID', 0) or config.RAIN_NOTIFICATION_CHANNEL_ID
        alert_channel = self.bot.get_channel(channel_id)
        if not alert_channel: return
        forecast = await weather_utils.get_short_term_forecast_from_kma(self.bot.db, config.DEFAULT_NX, config.DEFAULT_NY)
        summary = weather_utils.format_short_term_forecast(forecast, "오늘", 0) if forecast and not forecast.get("error") else f"오늘 {config.DEFAULT_LOCATION_NAME} 날씨 정보를 가져오는 데 실패했어. 😥"
        if greeting_type == "아침": alert_context = f"좋은 아침! ☀️ 오늘 {config.DEFAULT_LOCATION_NAME} 날씨는 이렇대.\n\n> {summary}\n\n오늘 하루도 활기차게 시작해보자고! 💪"
        else: alert_context = f"오늘 하루도 수고했어! 참고로 오늘 {config.DEFAULT_LOCATION_NAME} 날씨는 이랬어.\n\n> {summary}\n\n이제 편안한 밤 보내고, 내일 또 보자! 잘 자! 🌙"
        self.ai_handler = self.bot.get_cog('AIHandler')
        ai_msg = await self.ai_handler.generate_system_alert_message(channel_id, alert_context, f"{greeting_type} 인사") if self.ai_handler and self.ai_handler.is_ready else None
        await alert_channel.send(ai_msg or alert_context)

    @tasks.loop(time=dt_time(hour=config.MORNING_GREETING_TIME["hour"], minute=config.MORNING_GREETING_TIME["minute"], tzinfo=KST))
    async def morning_greeting_loop(self):
        """매일 아침 지정된 시간에 날씨 정보와 함께 인사말을 보냅니다."""
        await self._send_greeting_notification("아침")

    @tasks.loop(time=dt_time(hour=config.EVENING_GREETING_TIME["hour"], minute=config.EVENING_GREETING_TIME["minute"], tzinfo=KST))
    async def evening_greeting_loop(self):
        """매일 저녁 지정된 시간에 날씨 정보와 함께 인사말을 보냅니다."""
        await self._send_greeting_notification("저녁")

    @tasks.loop(minutes=1)
    async def earthquake_alert_loop(self):
        """1분마다 최근 지진 정보를 확인하고 새로운 지진 발생 시 알립니다."""
        await self.bot.wait_until_ready()
        if not weather_utils.get_kma_api_key(): return
        
        alert_channel_id = config.RAIN_NOTIFICATION_CHANNEL_ID
        if not alert_channel_id: return
        alert_channel = self.bot.get_channel(alert_channel_id)
        if not alert_channel: return
        
        earthquakes = await weather_utils.get_recent_earthquakes(self.bot.db)
        if not earthquakes: return
        
        # Sort by time ascending
        try:
           earthquakes.sort(key=lambda x: str(x.get('tmEqk')))
        except: pass
        
        new_last_time = self.last_earthquake_time
        
        for eqk in earthquakes:
            try:
                tm_str = str(eqk.get('tmEqk'))
                eqk_dt = datetime.strptime(tm_str, "%Y%m%d%H%M%S") if len(tm_str) == 14 else datetime.strptime(tm_str, "%Y%m%d%H%M")
                eqk_dt = KST.localize(eqk_dt) if eqk_dt.tzinfo is None else eqk_dt
                
                # If newer than last checked time
                if eqk_dt > self.last_earthquake_time:
                    # Alert!
                    formatted_msg = weather_utils.format_earthquake_alert(eqk)
                    
                    self.ai_handler = self.bot.get_cog('AIHandler')
                    ai_msg = await self.ai_handler.generate_system_alert_message(
                        alert_channel.id, 
                        formatted_msg, 
                        "지진 발생 알림"
                    ) if self.ai_handler and self.ai_handler.is_ready else None
                    
                    await alert_channel.send(ai_msg or f"🚨 **긴급: 지진 발생**\n{formatted_msg}")
                    
                    if eqk_dt > new_last_time:
                        new_last_time = eqk_dt
            except Exception as e:
                logger.error(f"지진 정보 처리 오류: {e}")
                
        self.last_earthquake_time = new_last_time

async def setup(bot: commands.Bot):
    """Cog를 봇에 등록하는 함수입니다."""
    await bot.add_cog(WeatherCog(bot))

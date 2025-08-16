# -*- coding: utf-8 -*-
import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta, time as dt_time
import pytz
from typing import Literal

# 설정, 로거, 유틸리티, AI 핸들러 가져오기
import config
from logger_config import logger
import utils
from .ai_handler import AIHandler

KST = pytz.timezone('Asia/Seoul')

class WeatherCog(commands.Cog):
    """날씨 정보 제공, AI 연동 응답, 주기적 알림(비, 아침/저녁 인사) 기능을 담당하는 Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ai_handler: AIHandler | None = None
        self.notified_rain_event_starts = set()

    @commands.Cog.listener()
    async def on_ready(self):
        """Cog가 준비되었을 때 AI 핸들러를 가져오고 루프를 시작합니다."""
        self.ai_handler = self.bot.get_cog('AIHandler')
        self.setup_and_start_loops()
        logger.info("WeatherCog 준비 완료 및 루프 시작.")

    def setup_and_start_loops(self):
        """외부에서 호출되어 루프를 안전하게 시작합니다."""
        if config.ENABLE_RAIN_NOTIFICATION and config.RAIN_NOTIFICATION_CHANNEL_ID != 0:
            self.rain_notification_loop.start()
        if config.ENABLE_GREETING_NOTIFICATION:
            if getattr(config, 'GREETING_NOTIFICATION_CHANNEL_ID', 0) or config.RAIN_NOTIFICATION_CHANNEL_ID:
                self.morning_greeting_loop.start()
                self.evening_greeting_loop.start()

    def cog_unload(self):
        """Cog 언로드 시 실행되는 정리 작업입니다."""
        self.rain_notification_loop.cancel()
        self.morning_greeting_loop.cancel()
        self.evening_greeting_loop.cancel()

    async def get_weather_forecast(
        self,
        location: str,
        day: Literal["오늘", "내일", "모레"]
    ) -> str:
        """
        지정된 지역(location)과 날짜(day)의 날씨 예보를 가져옵니다.
        :param location: 날씨를 조회할 지역의 이름 (예: "서울", "부산", "광양").
        :param day: 예보를 조회할 날짜. "오늘", "내일", "모레" 중 하나여야 합니다.
        :return: 조회된 날씨 정보 또는 오류 메시지를 담은 문자열.
        """
        logger.info(f"날씨 도구 실행: location='{location}', day='{day}'")
        if not utils.get_kma_api_key():
            return config.MSG_WEATHER_API_KEY_MISSING

        day_offset_map = {"오늘": 0, "내일": 1, "모레": 2}
        day_offset = day_offset_map.get(day, 0)

        coords = config.LOCATION_COORDINATES.get(location)
        if not coords:
            return config.MSG_LOCATION_NOT_FOUND.format(location_name=location)

        nx, ny = str(coords["nx"]), str(coords["ny"])
        target_date = datetime.now(KST).date() + timedelta(days=day_offset)

        forecast_data = await utils.get_short_term_forecast_from_kma(nx, ny)
        formatted_forecast = utils.format_short_term_forecast(forecast_data, target_date)

        if "오류" in formatted_forecast or "실패" in formatted_forecast or "없어" in formatted_forecast:
            return f"📍 {location} {formatted_forecast}"

        # 오늘 날씨는 현재 날씨 정보 추가
        if day_offset == 0:
            current_weather_data = await utils.get_current_weather_from_kma(nx, ny)
            current_weather_str = utils.format_current_weather(current_weather_data)
            return f"📍 {location} 현재 날씨: {current_weather_str}\n{formatted_forecast}"
        else:
            return f"📍 {location} {formatted_forecast}"

    @commands.command(name="날씨", aliases=["weather", "현재날씨", "오늘날씨"])
    async def weather_command(self, ctx: commands.Context, *, location_query: str = "오늘 광양"):
        """(레거시) 지정된 날짜의 날씨를 알려줍니다. (예: !날씨, !날씨 내일 서울)"""

        query_lower = location_query.lower()
        day: Literal["오늘", "내일", "모레"] = "오늘"
        if "모레" in query_lower: day = "모레"
        elif "내일" in query_lower: day = "내일"

        parsed_location = config.DEFAULT_LOCATION_NAME
        sorted_locations = sorted(config.LOCATION_COORDINATES.keys(), key=len, reverse=True)
        for loc_key in sorted_locations:
            if loc_key in query_lower:
                parsed_location = loc_key
                break

        async with ctx.typing():
            weather_result = await self.get_weather_forecast(location=parsed_location, day=day)
            await ctx.reply(weather_result, mention_author=False)

    def _parse_rain_periods(self, forecast_data: dict) -> list:
        # ... (이하 백그라운드 작업 및 루프 관련 코드는 변경 없음)
        try:
            items = forecast_data['response']['body']['items']['item']
        except (KeyError, TypeError):
            return []

        hourly_data = {}
        for item in items:
            key = (item.get("fcstDate"), item.get("fcstTime"))
            if key not in hourly_data:
                hourly_data[key] = {}
            hourly_data[key][item.get("category")] = item.get("fcstValue")

        precipitation_periods = []
        current_period = None
        
        for key_time in sorted(hourly_data.keys()):
            data = hourly_data[key_time]
            pty_val = data.get("PTY", "0")
            pop_val = int(data.get("POP", 0))

            is_raining = pty_val != "0" and pop_val >= config.RAIN_NOTIFICATION_THRESHOLD_POP
            
            try:
                current_dt = KST.localize(datetime.strptime(f"{key_time[0]}{key_time[1]}", "%Y%m%d%H%M"))
            except (ValueError, TypeError):
                continue

            if is_raining:
                precip_type = "눈" if pty_val == "3" else "비"
                if current_period is None or current_period["type"] != precip_type:
                    if current_period:
                        precipitation_periods.append(current_period)
                    current_period = {
                        "type": precip_type,
                        "start_dt": current_dt,
                        "end_dt": current_dt,
                        "max_pop": pop_val,
                        "key": key_time
                    }
                else:
                    current_period["end_dt"] = current_dt
                    current_period["max_pop"] = max(current_period["max_pop"], pop_val)
            else:
                if current_period:
                    precipitation_periods.append(current_period)
                    current_period = None
        
        if current_period:
            precipitation_periods.append(current_period)
            
        return precipitation_periods

    @tasks.loop(minutes=config.WEATHER_CHECK_INTERVAL_MINUTES)
    async def rain_notification_loop(self):
        await self.bot.wait_until_ready()
        if not utils.get_kma_api_key(): return
        
        alert_channel_id = config.RAIN_NOTIFICATION_CHANNEL_ID
        notification_channel = self.bot.get_channel(alert_channel_id)
        if not notification_channel: return
        
        nx, ny = config.DEFAULT_NX, config.DEFAULT_NY
        forecast_data = await utils.get_short_term_forecast_from_kma(nx, ny)
        
        if not forecast_data or forecast_data.get("error"): return

        precipitation_periods = self._parse_rain_periods(forecast_data)
        now_kst = datetime.now(KST)
        
        cutoff_time = now_kst - timedelta(days=1)
        self.notified_rain_event_starts = {key for key in self.notified_rain_event_starts if KST.localize(datetime.strptime(f"{key[0]}{key[1]}", "%Y%m%d%H%M")) >= cutoff_time}

        for period in precipitation_periods:
            if period["start_dt"] >= now_kst and period["key"] not in self.notified_rain_event_starts:
                start_display = period["start_dt"].strftime("%m월 %d일 %H시")
                end_display = (period["end_dt"] + timedelta(hours=1)).strftime("%H시")
                if period["start_dt"].date() != period["end_dt"].date():
                    end_display = (period["end_dt"] + timedelta(hours=1)).strftime("%m월 %d일 %H시")
                
                precip_type_kor = "눈❄️" if period["type"] == "눈" else "비☔"
                weather_alert_info = f"{config.DEFAULT_LOCATION_NAME}에 '{start_display}'부터 '{end_display}'까지 {precip_type_kor}가 올 것으로 예상됩니다. 최대 강수/강설 확률은 {period['max_pop']}%입니다."
                
                final_message_to_send = f"{precip_type_kor} **{config.DEFAULT_LOCATION_NAME} {precip_type_kor} 예보 알림**\n{weather_alert_info}"
                if self.ai_handler and self.ai_handler.is_ready:
                    # AI를 통해 페르소나에 맞는 말투로 변경
                    final_message_to_send = await self.ai_handler.generate_system_message(
                        text_to_rephrase=weather_alert_info,
                        channel_id=alert_channel_id
                    )
                
                await notification_channel.send(final_message_to_send)
                self.notified_rain_event_starts.add(period["key"])

    async def _send_greeting_notification(self, greeting_type: str):
        await self.bot.wait_until_ready()
        if not utils.get_kma_api_key(): return
        greeting_channel_id = getattr(config, 'GREETING_NOTIFICATION_CHANNEL_ID', 0) or config.RAIN_NOTIFICATION_CHANNEL_ID
        notification_channel = self.bot.get_channel(greeting_channel_id)
        if not notification_channel: return

        nx, ny = config.DEFAULT_NX, config.DEFAULT_NY
        today_forecast_raw = await utils.get_short_term_forecast_from_kma(nx, ny)
        weather_summary = utils.format_short_term_forecast(today_forecast_raw, datetime.now(KST).date())
        
        alert_context = ""
        if greeting_type == "아침":
            alert_context = f"좋은 아침! ☀️ 오늘 {config.DEFAULT_LOCATION_NAME} 날씨는 이렇대.\n\n> {weather_summary}\n\n오늘 하루도 활기차게 시작해보자고! 💪"
        else:
            alert_context = f"오늘 하루도 수고했어! 참고로 오늘 {config.DEFAULT_LOCATION_NAME} 날씨는 이랬어.\n\n> {weather_summary}\n\n이제 편안한 밤 보내고, 내일 또 보자! 잘 자! 🌙"

        final_message_to_send = alert_context
        if self.ai_handler and self.ai_handler.is_ready:
            final_message_to_send = await self.ai_handler.generate_system_message(
                text_to_rephrase=alert_context,
                channel_id=greeting_channel_id
            )

        await notification_channel.send(final_message_to_send)

    @tasks.loop(time=dt_time(hour=config.MORNING_GREETING_TIME["hour"], minute=config.MORNING_GREETING_TIME["minute"], tzinfo=KST))
    async def morning_greeting_loop(self):
        await self._send_greeting_notification("아침")

    @tasks.loop(time=dt_time(hour=config.EVENING_GREETING_TIME["hour"], minute=config.EVENING_GREETING_TIME["minute"], tzinfo=KST))
    async def evening_greeting_loop(self):
        await self._send_greeting_notification("저녁")

async def setup(bot: commands.Bot):
    await bot.add_cog(WeatherCog(bot))

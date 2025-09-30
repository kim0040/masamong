# -*- coding: utf-8 -*-
"""
날씨 정보와 관련된 모든 기능을 담당하는 Cog입니다.

주요 기능:
- `!날씨` 명령어를 통해 특정 지역의 날씨 정보를 제공합니다.
- AI 채널에서는 날씨 정보를 바탕으로 AI가 창의적인 답변을 생성합니다.
- 주기적으로 강수 예보를 확인하여 비/눈 소식을 알립니다.
- 지정된 시간에 날씨 정보를 포함한 아침/저녁 인사를 보냅니다.
"""

import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta, time as dt_time
import pytz

import config
from logger_config import logger
from utils import db as db_utils, weather as weather_utils, coords as coords_utils
from .ai_handler import AIHandler

KST = pytz.timezone('Asia/Seoul')

class WeatherCog(commands.Cog):
    """날씨 정보 제공, AI 연동 응답, 주기적 알림 기능을 담당합니다."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ai_handler: AIHandler | None = None
        self.notified_rain_event_starts = set()
        logger.info("WeatherCog가 성공적으로 초기화되었습니다.")

    def setup_and_start_loops(self):
        """봇이 준비되면(on_ready), 설정에 따라 주기적인 알림 루프들을 시작합니다."""
        if config.ENABLE_RAIN_NOTIFICATION and config.RAIN_NOTIFICATION_CHANNEL_ID:
            logger.info("주기적 강수 알림 루프를 시작합니다.")
            self.rain_notification_loop.start()
        if config.ENABLE_GREETING_NOTIFICATION and (getattr(config, 'GREETING_NOTIFICATION_CHANNEL_ID', None) or config.RAIN_NOTIFICATION_CHANNEL_ID):
            logger.info("아침/저녁 인사 알림 루프를 시작합니다.")
            self.morning_greeting_loop.start()
            self.evening_greeting_loop.start()

    def cog_unload(self):
        """Cog가 언로드될 때, 실행 중인 모든 루프를 안전하게 취소합니다."""
        self.rain_notification_loop.cancel()
        self.morning_greeting_loop.cancel()
        self.evening_greeting_loop.cancel()

    async def get_formatted_weather_string(self, day_offset: int, location_name: str, nx: str, ny: str) -> tuple[str | None, str | None]:
        """날씨 정보를 조회하고 사람이 읽기 좋은 문자열로 포맷팅합니다."""
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
                return f"현재 {current_weather_str}\n{formatted_forecast}".strip(), None
            else:
                forecast_data = await weather_utils.get_short_term_forecast_from_kma(self.bot.db, nx, ny)
                if isinstance(forecast_data, dict) and forecast_data.get("error"): return None, forecast_data.get("message", config.MSG_WEATHER_FETCH_ERROR)
                if forecast_data is None: return None, config.MSG_WEATHER_FETCH_ERROR
                formatted_forecast = weather_utils.format_short_term_forecast(forecast_data, day_name, target_day_offset=day_offset)
                return f"{location_name} {formatted_forecast}", None
        except Exception as e:
            logger.error(f"날씨 정보 포맷팅 중 오류: {e}", exc_info=True)
            return None, config.MSG_WEATHER_FETCH_ERROR

    async def prepare_weather_response_for_ai(self, original_message: discord.Message, day_offset: int, location_name: str, nx: str, ny: str, user_original_query: str):
        """날씨 정보를 조회하고, AI 채널 여부에 따라 AI 응답 또는 일반 응답을 생성합니다."""
        if not weather_utils.get_kma_api_key():
            await original_message.reply(config.MSG_WEATHER_API_KEY_MISSING, mention_author=False)
            return
        async with original_message.channel.typing():
            weather_data_str, error_message = await self.get_formatted_weather_string(day_offset, location_name, nx, ny)
            if error_message: await original_message.reply(error_message, mention_author=False); return
            if not weather_data_str: await original_message.reply(config.MSG_WEATHER_NO_DATA, mention_author=False); return
            self.ai_handler = self.bot.get_cog('AIHandler')
            is_ai_channel_and_enabled = self.ai_handler and self.ai_handler.is_ready and config.CHANNEL_AI_CONFIG.get(original_message.channel.id, {}).get("allowed", False)
            if is_ai_channel_and_enabled:
                context = {"location_name": location_name, "weather_data": weather_data_str}
                ai_response = await self.ai_handler.generate_creative_text(original_message.channel, original_message.author, "answer_weather", context)
                await original_message.reply(ai_response or config.MSG_AI_ERROR, mention_author=False)
            else:
                await original_message.reply(f"📍 **{location_name}**\n{weather_data_str}", mention_author=False)

    @commands.command(name="날씨", aliases=["weather", "현재날씨", "오늘날씨"])
    async def weather_command(self, ctx: commands.Context, *, location_query: str = ""):
        ""`!날씨 [날짜] [지역]` 형식으로 날씨를 조회합니다."""
        user_original_query = location_query.strip() if location_query else "오늘 날씨"
        location_name, nx, ny = config.DEFAULT_LOCATION_NAME, config.DEFAULT_NX, config.DEFAULT_NY
        coords = await coords_utils.get_coords_from_db(self.bot.db, user_original_query.lower())
        if coords: location_name, nx, ny = coords['name'], str(coords['nx']), str(coords['ny'])
        day_offset = 1 if "내일" in user_original_query else 2 if "모레" in user_original_query else 0
        await self.prepare_weather_response_for_ai(ctx.message, day_offset, location_name, nx, ny, user_original_query)

    def _parse_rain_periods(self, forecast_data: dict) -> list:
        """단기예보 원본 데이터에서 '비' 또는 '눈'이 오는 시간대를 파싱하여 리스트로 반환합니다."""
        try: items = forecast_data['item']
        except (KeyError, TypeError): return []
        hourly_data = {}
        for item in items: hourly_data.setdefault((item.get("fcstDate"), item.get("fcstTime")))
        precipitation_periods, current_period = [], None
        for key_time in sorted(hourly_data.keys()):
            data = hourly_data[key_time]
            is_raining = data.get("PTY", "0") != "0" and int(data.get("POP", 0)) >= config.RAIN_NOTIFICATION_THRESHOLD_POP
            try: current_dt = KST.localize(datetime.strptime(f"{key_time[0]}{key_time[1]}%H%M", "%Y%m%d%H%M"))
            except (ValueError, TypeError): continue
            if is_raining:
                precip_type = "눈" if data.get("PTY") == "3" else "비"
                if current_period is None or current_period["type"] != precip_type:
                    if current_period: precipitation_periods.append(current_period)
                    current_period = {"type": precip_type, "start_dt": current_dt, "end_dt": current_dt, "max_pop": int(data.get("POP",0)), "key": key_time}
                else:
                    current_period["end_dt"] = current_dt
                    current_period["max_pop"] = max(current_period["max_pop"], int(data.get("POP",0)))
            else:
                if current_period: precipitation_periods.append(current_period); current_period = None
        if current_period: precipitation_periods.append(current_period)
        return precipitation_periods

    @tasks.loop(minutes=config.WEATHER_CHECK_INTERVAL_MINUTES)
    async def rain_notification_loop(self):
        """주기적으로 강수 예보를 확인하고, 비/눈 소식이 있으면 알림을 보냅니다."""
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
        """날씨 정보를 포함한 아침/저녁 인사를 생성하고 전송합니다."""
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

async def setup(bot: commands.Bot):
    """Cog를 봇에 등록하는 함수입니다."""
    await bot.add_cog(WeatherCog(bot))
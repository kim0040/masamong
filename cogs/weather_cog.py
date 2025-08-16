# -*- coding: utf-8 -*-
import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta, time as dt_time
import pytz

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

    def setup_and_start_loops(self):
        """EventListeners의 on_ready에서 호출되어 루프를 안전하게 시작합니다."""
        if config.ENABLE_RAIN_NOTIFICATION and config.RAIN_NOTIFICATION_CHANNEL_ID != 0:
            logger.info("WeatherCog: 주기적 비 알림 루프를 시작합니다.")
            self.rain_notification_loop.start()

        if config.ENABLE_GREETING_NOTIFICATION:
            greeting_channel_id = getattr(config, 'GREETING_NOTIFICATION_CHANNEL_ID', 0) or config.RAIN_NOTIFICATION_CHANNEL_ID
            if greeting_channel_id != 0:
                logger.info("WeatherCog: 아침/저녁 인사 알림 루프를 시작합니다.")
                self.morning_greeting_loop.start()
                self.evening_greeting_loop.start()
            else:
                logger.error(config.MSG_GREETING_CHANNEL_NOT_SET)

    def cog_unload(self):
        """Cog 언로드 시 실행되는 정리 작업입니다."""
        if hasattr(self, 'rain_notification_loop') and self.rain_notification_loop.is_running():
            self.rain_notification_loop.cancel()
        if hasattr(self, 'morning_greeting_loop') and self.morning_greeting_loop.is_running():
            self.morning_greeting_loop.cancel()
        if hasattr(self, 'evening_greeting_loop') and self.evening_greeting_loop.is_running():
            self.evening_greeting_loop.cancel()

    async def prepare_weather_response_for_ai(self, original_message: discord.Message, day_offset: int, location_name: str, nx: str, ny: str, user_original_query: str):
        """날씨 정보를 가져와 AI에게 전달할 문자열을 준비하고, AI 응답을 요청하거나 직접 응답합니다."""
        context_log = f"[{original_message.guild.name}/{original_message.channel.name}]"
        if not utils.get_kma_api_key():
            await original_message.reply(config.MSG_WEATHER_API_KEY_MISSING, mention_author=False)
            return

        day_names = ["오늘", "내일", "모레"]
        day_name = day_names[day_offset] if 0 <= day_offset < len(day_names) else f"{day_offset}일 후"

        weather_data_str = ""
        fallback_message_content = ""

        async with original_message.channel.typing():
            if day_offset == 0:
                current_weather_data = await utils.get_current_weather_from_kma(nx, ny)
                if isinstance(current_weather_data, dict) and current_weather_data.get("error"):
                    fallback_message_content = current_weather_data.get("message", config.MSG_WEATHER_FETCH_ERROR)
                elif current_weather_data is None:
                    fallback_message_content = config.MSG_WEATHER_FETCH_ERROR
                else:
                    current_weather_str = utils.format_current_weather(current_weather_data)
                    short_term_data = await utils.get_short_term_forecast_from_kma(nx, ny)
                    formatted_forecast = utils.format_short_term_forecast(short_term_data, day_name, target_day_offset=0)
                    weather_data_str = f"{location_name} {day_name} 날씨 정보: 현재 {current_weather_str}\n{formatted_forecast}".strip()
            else:
                forecast_data = await utils.get_short_term_forecast_from_kma(nx, ny)
                if isinstance(forecast_data, dict) and forecast_data.get("error"):
                    fallback_message_content = forecast_data.get("message", config.MSG_WEATHER_FETCH_ERROR)
                elif forecast_data is None:
                     fallback_message_content = config.MSG_WEATHER_FETCH_ERROR
                else:
                    formatted_forecast = utils.format_short_term_forecast(forecast_data, day_name, target_day_offset=day_offset)
                    weather_data_str = f"{location_name} {formatted_forecast}"

            channel_id = original_message.channel.id
            channel_ai_settings = config.CHANNEL_AI_CONFIG.get(channel_id)
            is_ai_channel_and_enabled = self.ai_handler and self.ai_handler.is_ready and channel_ai_settings and channel_ai_settings.get("allowed", False)

            if fallback_message_content:
                logger.info(f"{context_log} 날씨 정보 조회 문제로 직접 응답 - {fallback_message_content}")
                await original_message.reply(fallback_message_content, mention_author=False)
            elif is_ai_channel_and_enabled and weather_data_str:
                logger.info(f"{context_log} AI 응답 요청. 날씨정보: '{weather_data_str[:100]}...'")
                await self.ai_handler.process_ai_message(original_message, weather_info=weather_data_str)
            elif weather_data_str:
                logger.info(f"{context_log} AI 사용 불가 채널이라 직접 날씨 정보 전송.")
                await original_message.reply(f"📍 {weather_data_str}", mention_author=False)
            else:
                 await original_message.reply(config.MSG_WEATHER_NO_DATA, mention_author=False)

    @commands.command(name="날씨", aliases=["weather", "현재날씨", "오늘날씨"])
    async def weather_command(self, ctx: commands.Context, *, location_query: str = ""):
        """지정된 날짜의 날씨를 AI가 페르소나에 맞춰 알려줍니다. (예: !날씨, !날씨 내일 서울)"""
        message = ctx if isinstance(ctx, discord.Message) else ctx.message
        context_log = f"[{message.guild.name}/{message.channel.name}]"

        user_original_query = location_query.strip() if location_query else "오늘 날씨"
        location_name = config.DEFAULT_LOCATION_NAME
        nx, ny = config.DEFAULT_NX, config.DEFAULT_NY

        query_for_loc_check = user_original_query.lower()
        parsed_location_name = None
        sorted_locations = sorted(config.LOCATION_COORDINATES.keys(), key=len, reverse=True)
        for loc_key in sorted_locations:
            if loc_key in query_for_loc_check:
                parsed_location_name = loc_key
                break

        if parsed_location_name:
            location_name = parsed_location_name
            coords = config.LOCATION_COORDINATES[location_name]
            nx, ny = str(coords["nx"]), str(coords["ny"])
            logger.info(f"{context_log} !날씨 명령: 지역 감지 - {location_name} (nx: {nx}, ny: {ny})")

        day_offset = 0
        if "모레" in query_for_loc_check: day_offset = 2
        elif "내일" in query_for_loc_check: day_offset = 1

        await self.prepare_weather_response_for_ai(message, day_offset, location_name, nx, ny, user_original_query)

    def _parse_rain_periods(self, forecast_data: dict) -> list:
        """JSON 단기예보 데이터에서 강수 기간을 추출하여 리스트로 반환합니다."""
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

        context_log = f"[{notification_channel.guild.name}/{notification_channel.name}]"
        logger.info(f"{context_log} 주기적 강수 알림: 날씨 확인 시작...")
        nx, ny = config.DEFAULT_NX, config.DEFAULT_NY
        forecast_today_raw = await utils.get_short_term_forecast_from_kma(nx, ny)

        if not forecast_today_raw or isinstance(forecast_today_raw, dict) and forecast_today_raw.get("error"):
            logger.warning(f"{context_log} 주기적 강수 알림: 오늘 예보 데이터를 가져오지 못했습니다.")
            return

        precipitation_periods = self._parse_rain_periods(forecast_today_raw)
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

                ai_generated_message = None
                if self.ai_handler and self.ai_handler.is_ready:
                    try:
                        ai_generated_message = await self.ai_handler.generate_system_alert_message(alert_channel_id, weather_alert_info, f"{precip_type_kor} 예보 알림")
                    except Exception as ai_err:
                        logger.error(f"{context_log} 주기적 {precip_type_kor} 알림 AI 메시지 생성 중 오류: {ai_err}", exc_info=True)

                final_message_to_send = ai_generated_message or f"{precip_type_kor} **{config.DEFAULT_LOCATION_NAME} {precip_type_kor} 예보 알림** {precip_type_kor}\n{weather_alert_info}"

                try:
                    await notification_channel.send(final_message_to_send)
                    log_message = f"{precip_type_kor} 알림 전송 ({'AI 생성' if ai_generated_message else '기본'}): {final_message_to_send}"
                    logger.info(f"{context_log} " + log_message.replace('\n', ' '))
                    self.notified_rain_event_starts.add(period["key"])
                except Exception as e:
                    logger.error(f"{context_log} {precip_type_kor} 알림 전송 중 오류: {e}", exc_info=True)

    @rain_notification_loop.error
    async def rain_notification_loop_error(self, error):
        logger.error(f"주기적 강수 알림 루프에서 처리되지 않은 오류 발생: {error}", exc_info=True)

    async def _send_greeting_notification(self, greeting_type: str):
        await self.bot.wait_until_ready()
        if not utils.get_kma_api_key(): return

        greeting_channel_id = getattr(config, 'GREETING_NOTIFICATION_CHANNEL_ID', 0) or config.RAIN_NOTIFICATION_CHANNEL_ID
        notification_channel = self.bot.get_channel(greeting_channel_id)
        if not notification_channel: return

        context_log = f"[{notification_channel.guild.name}/{notification_channel.name}]"
        logger.info(f"{context_log} 주기적 {greeting_type} 인사: 날씨 확인 시작...")
        nx, ny = config.DEFAULT_NX, config.DEFAULT_NY
        today_forecast_raw = await utils.get_short_term_forecast_from_kma(nx, ny)

        weather_summary = f"오늘 {config.DEFAULT_LOCATION_NAME} 날씨 정보를 가져오는 데 실패했어. 😥"
        if today_forecast_raw and not isinstance(today_forecast_raw, dict):
            weather_summary = utils.format_short_term_forecast(today_forecast_raw, "오늘", target_day_offset=0)
        elif isinstance(today_forecast_raw, dict) and today_forecast_raw.get("error"):
            weather_summary = today_forecast_raw.get("message", "날씨 정보 조회 중 오류가 발생했어.")

        alert_context = ""
        alert_type_log = ""
        if greeting_type == "아침":
            alert_context = f"좋은 아침! ☀️ 오늘 {config.DEFAULT_LOCATION_NAME} 날씨는 이렇대。\n\n> {weather_summary}\n\n오늘 하루도 활기차게 시작해보자고! 💪"
            alert_type_log = "아침 날씨 인사"
        elif greeting_type == "저녁":
            alert_context = f"오늘 하루도 수고했어! 참고로 오늘 {config.DEFAULT_LOCATION_NAME} 날씨는 이랬어.\n\n> {weather_summary}\n\n이제 편안한 밤 보내고, 내일 또 보자! 잘 자! 🌙"
            alert_type_log = "저녁 날씨 인사"

        ai_generated_message = None
        if self.ai_handler and self.ai_handler.is_ready:
            try:
                ai_generated_message = await self.ai_handler.generate_system_alert_message(
                    channel_id=greeting_channel_id, alert_context_info=alert_context, alert_type=alert_type_log)
            except Exception as ai_err:
                logger.error(f"{context_log} 주기적 {greeting_type} 인사 AI 메시지 생성 중 오류: {ai_err}", exc_info=True)

        final_message_to_send = ai_generated_message or alert_context

        try:
            await notification_channel.send(final_message_to_send)
            log_message = f"{greeting_type} 인사 전송 ({'AI 생성' if ai_generated_message else '기본'}): {final_message_to_send}"
            logger.info(f"{context_log} " + log_message.replace('\n', ' '))
        except Exception as e:
            logger.error(f"{context_log} {greeting_type} 인사 전송 중 오류: {e}", exc_info=True)

    @tasks.loop(time=dt_time(hour=config.MORNING_GREETING_TIME["hour"], minute=config.MORNING_GREETING_TIME["minute"], tzinfo=KST))
    async def morning_greeting_loop(self):
        logger.info(f"아침 인사 루프 실행 (설정 시간: {config.MORNING_GREETING_TIME['hour']}:{config.MORNING_GREETING_TIME['minute']}).")
        await self._send_greeting_notification("아침")

    @morning_greeting_loop.error
    async def morning_greeting_loop_error(self, error):
        logger.error(f"아침 인사 루프에서 처리되지 않은 오류 발생: {error}", exc_info=True)

    @tasks.loop(time=dt_time(hour=config.EVENING_GREETING_TIME["hour"], minute=config.EVENING_GREETING_TIME["minute"], tzinfo=KST))
    async def evening_greeting_loop(self):
        logger.info(f"저녁 인사 루프 실행 (설정 시간: {config.EVENING_GREETING_TIME['hour']}:{config.EVENING_GREETING_TIME['minute']}).")
        await self._send_greeting_notification("저녁")

    @evening_greeting_loop.error
    async def evening_greeting_loop_error(self, error):
        logger.error(f"저녁 인사 루프에서 처리되지 않은 오류 발생: {error}", exc_info=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(WeatherCog(bot))

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
import json
from zoneinfo import ZoneInfo

import config
from logger_config import logger
from utils import (
    db as db_utils,
    weather as weather_utils,
    coords as coords_utils,
)
from utils.discord_helpers import send_split_message, split_message_chunks
from .ai_handler import AIHandler

KST = ZoneInfo("Asia/Seoul")
_RAIN_EVENT_DEDUPE_MAX = max(
    16,
    int(getattr(config, "RAIN_NOTIFICATION_DEDUPE_MAX", 256)),
)
_EARTHQUAKE_WATERMARK_KEY = "earthquake_alert_last_occurred_epoch_v1"
_EARTHQUAKE_MESSAGE_COUNTER_PREFIX = "earthquake_alert_message_v2"

class WeatherCog(commands.Cog):
    """날씨 조회와 알림 전송을 전담하는 Cog입니다.

    - 명령어(`!날씨`) 실행 시 좌표 변환, KMA 데이터 조회, 응답 포맷팅을 처리합니다.
    - AI 채널에서는 조회 결과를 `AIHandler`에 전달해 문맥 맞춤형 답변을 생성합니다.
    - 주기적으로 비/눈 예보 및 아침·저녁 인사를 전송하는 백그라운드 태스크를 관리합니다.
    """

    def __init__(self, bot: commands.Bot):
        """WeatherCog를 초기화하고 상태 변수들을 설정합니다."""
        self.bot = bot
        self.ai_handler: AIHandler | None = None
        self.notified_rain_event_starts = set()
        self.last_earthquake_time = datetime.now(KST) - timedelta(hours=1)
        self._earthquake_watermark_loaded = False
        self._earthquake_watermark_exists = False
        self._earthquake_message_ids: dict[tuple[int, int], int] = {}
        logger.info("WeatherCog가 성공적으로 초기화되었습니다.")

    def setup_and_start_loops(self):
        """봇이 준비되면 설정 플래그에 따라 주기 태스크를 기동합니다.

        Rain/Greeting 알림은 각각 별도의 `tasks.loop`로 구현되어 있으며, 필요 없을 때는
        불필요한 리소스를 소비하지 않도록 시작 자체를 건너뜁니다.
        """
        # 주의: on_ready는 재연결(RESUME)마다 재발화할 수 있다. 이미 실행 중인 loop에
        # start()를 재호출하면 RuntimeError가 발생하므로 is_running()으로 가드한다.
        if config.ENABLE_RAIN_NOTIFICATION and config.RAIN_NOTIFICATION_CHANNEL_ID:
            if not self.rain_notification_loop.is_running():
                logger.info("주기적 강수 알림 루프를 시작합니다.")
                self.rain_notification_loop.start()
        if config.ENABLE_GREETING_NOTIFICATION and (getattr(config, 'GREETING_NOTIFICATION_CHANNEL_ID', None) or config.RAIN_NOTIFICATION_CHANNEL_ID):
            if not self.morning_greeting_loop.is_running():
                logger.info("아침/저녁 인사 알림 루프를 시작합니다.")
                self.morning_greeting_loop.start()
            if not self.evening_greeting_loop.is_running():
                self.evening_greeting_loop.start()
        
        # Earthquake Alert Loop
        has_fallback_channel = bool(getattr(config, "RAIN_NOTIFICATION_CHANNEL_ID", 0))
        has_ai_channels = bool(getattr(config, "CHANNEL_AI_CONFIG", {}))
        if getattr(config, "ENABLE_EARTHQUAKE_ALERT", True) and (has_fallback_channel or has_ai_channels):
            if not self.earthquake_alert_loop.is_running():
                logger.info(
                    "지진 알림 모니터링 루프를 시작합니다. 주기: %d초",
                    getattr(config, "EARTHQUAKE_CHECK_INTERVAL_SECONDS", 60),
                )
                self.earthquake_alert_loop.start()

    def cog_unload(self):
        """Cog가 언로드될 때, 실행 중인 모든 루프를 안전하게 취소합니다."""
        self.rain_notification_loop.cancel()
        self.morning_greeting_loop.cancel()
        self.evening_greeting_loop.cancel()
        self.earthquake_alert_loop.cancel()

    def _is_ai_enabled_for_channel(self, channel: discord.abc.Messageable) -> bool:
        """DB 서버 정책을 우선해 해당 서버/채널의 AI 활성 여부를 판정합니다."""
        guild = getattr(channel, "guild", None)
        channel_id = getattr(channel, "id", None)
        if guild is None or channel_id is None:
            return False
        policy_check = getattr(self.bot, "is_ai_channel_allowed", None)
        if callable(policy_check):
            return bool(policy_check(int(guild.id), int(channel_id)))
        return bool(
            config.CHANNEL_AI_CONFIG.get(int(channel_id), {}).get("allowed", False)
        )

    async def get_mid_term_weather(self, day_offset: int, location_name: str) -> str:
        """공식 JSON 중기예보에서 지정 날짜를 조회합니다."""
        try:
            result = await weather_utils.get_mid_term_forecast(
                self.bot.db,
                location_name,
                day_offset,
            )
            return result or "중기예보 데이터를 불러올 수 없습니다."
        except Exception as e:
            logger.error(f"중기예보 조회 실패: {e}", exc_info=True)
            return config.MSG_WEATHER_FETCH_ERROR

    async def get_formatted_weather_string(
        self,
        day_offset: int,
        location_name: str,
        nx: str,
        ny: str,
        user_query: str = "",
    ) -> tuple[str | None, str | None]:
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
            normalized_query = str(user_query or "").lower()

            async def fetch_optional(coro):
                try:
                    return await asyncio.wait_for(coro, timeout=8)
                except Exception:
                    return None

            if day_offset == 0:
                current_weather_data, ultra_short_data, short_term_data = await asyncio.gather(
                    weather_utils.get_current_weather_from_kma(self.bot.db, nx, ny),
                    weather_utils.get_ultra_short_forecast_from_kma(
                        self.bot.db,
                        nx,
                        ny,
                    ),
                    weather_utils.get_short_term_forecast_from_kma(self.bot.db, nx, ny),
                )
                if isinstance(current_weather_data, dict) and current_weather_data.get("error"): return None, current_weather_data.get("message", config.MSG_WEATHER_FETCH_ERROR)
                if current_weather_data is None: return None, config.MSG_WEATHER_FETCH_ERROR
                current_weather_str = weather_utils.format_current_weather(current_weather_data)
                ultra_short_str = weather_utils.format_ultra_short_forecast(
                    ultra_short_data
                )
                formatted_forecast = weather_utils.format_short_term_forecast(short_term_data, day_name, target_day_offset=0)

                # 국가 개황·영향예보·태풍은 모든 일상 조회에 필요한 자료가 아니다.
                # 질문이 명시한 경우에만 호출하고 응답 캐시는 다른 사용자와 공유한다.
                optional_names: list[str] = []
                optional_calls: list = []
                if any(
                    token in normalized_query
                    for token in ("개황", "전망", "기상 상황")
                ):
                    optional_names.append("overview")
                    optional_calls.append(
                        fetch_optional(
                            weather_utils.get_weather_overview(
                                self.bot.db,
                                timeout=5.0,
                            )
                        )
                    )
                if any(
                    token in normalized_query
                    for token in ("폭염", "한파", "영향예보", "영향 예보")
                ):
                    optional_names.append("impact")
                    optional_calls.append(
                        fetch_optional(
                            weather_utils.get_impact_forecast(
                                self.bot.db,
                                timeout=5.0,
                            )
                        )
                    )
                if "태풍" in normalized_query:
                    optional_names.append("typhoon")
                    optional_calls.append(
                        fetch_optional(
                            weather_utils.get_typhoons(
                                self.bot.db,
                                timeout=5.0,
                            )
                        )
                    )
                optional_values = (
                    await asyncio.gather(*optional_calls)
                    if optional_calls
                    else []
                )
                optional = dict(zip(optional_names, optional_values))
                parts = [f"[{location_name} 상세 날씨 정보 Context]"]
                if optional.get("overview"):
                    parts.append(f"📢 **기상 개황:** {optional['overview']}")
                if optional.get("impact"):
                    parts.append(f"⚠️ **영향 예보:** {optional['impact']}")
                if optional.get("typhoon"):
                    parts.append(f"🌀 **태풍 정보:** {optional['typhoon']}")
                parts.append(current_weather_str)
                if ultra_short_str:
                    parts.append(ultra_short_str)
                parts.append(formatted_forecast)
                final_context = "\n".join(parts)
                logger.info(
                    "☀️ Weather context prepared. context_chars=%d",
                    len(final_context),
                )
                return final_context.strip(), None
            else:
                forecast_data = await weather_utils.get_short_term_forecast_from_kma(self.bot.db, nx, ny)
                if isinstance(forecast_data, dict) and forecast_data.get("error"): return None, forecast_data.get("message", config.MSG_WEATHER_FETCH_ERROR)
                if forecast_data is None: return None, config.MSG_WEATHER_FETCH_ERROR
                formatted_forecast = weather_utils.format_short_term_forecast(forecast_data, day_name, target_day_offset=day_offset)
                parts = [f"[{location_name} 날씨 정보]"]
                # "내일 태풍"처럼 날짜가 붙은 질문도 현재 태풍 분석·공식 전망을
                # 빠뜨리지 않는다. 명시 질문에만 조회하며 결과는 15분 캐시된다.
                if "태풍" in normalized_query:
                    typhoon = await fetch_optional(
                        weather_utils.get_typhoons(
                            self.bot.db,
                            timeout=5.0,
                        )
                    )
                    if typhoon:
                        parts.append(f"🌀 **태풍 정보:** {typhoon}")
                parts.append(formatted_forecast)
                return "\n".join(parts), None
        except Exception as e:
            logger.error(f"날씨 정보 포맷팅 중 오류: {e}", exc_info=True)
            return None, config.MSG_WEATHER_FETCH_ERROR

    async def prepare_weather_response_for_ai(self, original_message: discord.Message, day_offset: int, location_name: str, nx: str, ny: str, user_original_query: str, status_msg: discord.Message = None):
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
            if status_msg: await status_msg.edit(content=config.MSG_WEATHER_API_KEY_MISSING)
            else: await original_message.channel.send(config.MSG_WEATHER_API_KEY_MISSING)
            return

        async with original_message.channel.typing():
            # 특보와 본 날씨 조회는 서로 독립적이므로 함께 시작한다.
            alerts_data, weather_result = await asyncio.gather(
                weather_utils.get_weather_alerts_from_kma(self.bot.db),
                self.get_formatted_weather_string(
                    day_offset,
                    location_name,
                    nx,
                    ny,
                    user_original_query,
                ),
            )
            formatted_alerts = None
            if isinstance(alerts_data, str):
                formatted_alerts = weather_utils.format_weather_alerts(
                    alerts_data,
                    location_name,
                )
            
            weather_data_str, error_message = weather_result
            if error_message:
                if status_msg: await status_msg.edit(content=error_message)
                else: await original_message.channel.send(error_message)
                return
            if not weather_data_str:
                if status_msg: await status_msg.edit(content=config.MSG_WEATHER_NO_DATA)
                else: await original_message.channel.send(config.MSG_WEATHER_NO_DATA)
                return

            # 3. 특보와 날씨 정보 결합
            final_response_str = weather_data_str
            if formatted_alerts:
                final_response_str = f"{formatted_alerts}\n\n---\n\n{weather_data_str}"

            # 4. AI 또는 일반 응답 생성
            # 4. AI 또는 일반 응답 생성
            self.ai_handler = self.bot.get_cog('AIHandler')
            is_ai_channel_and_enabled = (
                self.ai_handler
                and self.ai_handler.is_ready
                and self._is_ai_enabled_for_channel(original_message.channel)
            )
            
            # [Refactor] Data First, then AI Briefing
            rendered_data = f"📍 **{location_name}**\n{final_response_str}"
            if status_msg:
                chunks = split_message_chunks(rendered_data) or [rendered_data]
                await status_msg.edit(
                    content=chunks[0],
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                for chunk in chunks[1:]:
                    await status_msg.channel.send(
                        chunk,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                data_msg = status_msg
            else:
                sent = await send_split_message(
                    original_message.channel,
                    rendered_data,
                )
                data_msg = sent[0] if sent else original_message

            if is_ai_channel_and_enabled:
                context = {"location_name": location_name, "weather_data": final_response_str}
                # AI Briefing as follow-up
                async with original_message.channel.typing():
                    ai_response = await self.ai_handler.generate_creative_text(original_message.channel, original_message.author, "answer_weather", context)
                    if ai_response and ai_response != config.MSG_AI_ERROR:
                        await send_split_message(data_msg.channel, ai_response)

    @commands.command(name="날씨", aliases=["weather", "현재날씨", "오늘날씨"])
    async def weather_command(self, ctx: commands.Context, *, location_query: str = ""):
        """날씨 정보를 조회합니다. (서버/DM 가능)

        사용법:
        - `!날씨` : 기본 지역의 오늘 날씨
        - `!날씨 서울` : 특정 지역의 오늘 날씨
        - `!날씨 내일 부산` : 날짜 + 지역
        - `!날씨 이번주 광주` : 주간 예보 요약

        예시:
        - `!날씨`
        - `!날씨 내일 대구`
        - `!날씨 이번주 제주`
        """
        user_original_query = location_query.strip() if location_query else "오늘 날씨"
        location_name, nx, ny = config.DEFAULT_LOCATION_NAME, config.DEFAULT_NX, config.DEFAULT_NY
        coords = await coords_utils.get_coords_from_db(self.bot.db, user_original_query.lower())
        if coords: location_name, nx, ny = coords['name'], str(coords['nx']), str(coords['ny'])
        status_msg = await ctx.send(f"🌤️ `{location_name}`의 날씨 정보를 가져오는 중이야...")
        
        # [NEW] Weekly Weather Logic (Short-term + Mid-term)
        if "이번주" in user_original_query or "주간" in user_original_query:
            # 단기/중기 예보는 독립 API이므로 병렬 조회한다.
            short_term_data, mid_term_data = await asyncio.gather(
                weather_utils.get_short_term_forecast_from_kma(self.bot.db, nx, ny),
                weather_utils.get_mid_term_weekly_forecast(
                    self.bot.db,
                    location_name,
                ),
            )
            short_term_summary = ""
            if short_term_data and not short_term_data.get("error"):
                 tomorrow_summary = weather_utils.format_short_term_forecast(short_term_data, "내일", 1)
                 dayafter_summary = weather_utils.format_short_term_forecast(short_term_data, "모레", 2)
                 short_term_summary = f"{tomorrow_summary}\n{dayafter_summary}"
            
            full_weekly_data = f"--- [단기 예보 (내일/모레)] ---\n{short_term_summary}\n\n--- [중기 예보 (3일 후 ~ 10일 후)] ---\n{mid_term_data}"

            # [Refactor] Data First, then AI Briefing
            weekly_rendered = (
                f"📅 **{location_name} 이번 주 날씨 종합**\n"
                f"{full_weekly_data}"
            )
            weekly_chunks = split_message_chunks(weekly_rendered) or [
                weekly_rendered
            ]
            await status_msg.edit(
                content=weekly_chunks[0],
                allowed_mentions=discord.AllowedMentions.none(),
            )
            for chunk in weekly_chunks[1:]:
                await status_msg.channel.send(
                    chunk,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

            # Send via AI for summarization
            self.ai_handler = self.bot.get_cog('AIHandler')
            is_ai_channel = (
                self.ai_handler
                and self.ai_handler.is_ready
                and self._is_ai_enabled_for_channel(ctx.channel)
            )
            
            if is_ai_channel:
                 context = {"location_name": location_name, "weather_data": full_weekly_data}
                 async with ctx.channel.typing():
                     ai_response = await self.ai_handler.generate_creative_text(ctx.channel, ctx.author, "answer_weather_weekly", context)
                     if ai_response and ai_response != config.MSG_AI_ERROR:
                         await send_split_message(status_msg.channel, ai_response)
            return

        day_offset = 1 if "내일" in user_original_query else 2 if "모레" in user_original_query else 0
        await self.prepare_weather_response_for_ai(ctx.message, day_offset, location_name, nx, ny, user_original_query, status_msg)

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
                current_dt = datetime.strptime(
                    f"{key_time[0]}{key_time[1].zfill(4)}",
                    "%Y%m%d%H%M",
                ).replace(tzinfo=KST)
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

    @staticmethod
    def _rain_event_datetime(event_key: tuple[str, str]) -> datetime | None:
        """강수 이벤트 키를 KST 시각으로 변환합니다."""
        try:
            date_part, time_part = event_key
            return datetime.strptime(
                f"{date_part}{str(time_part).zfill(4)}",
                "%Y%m%d%H%M",
            ).replace(tzinfo=KST)
        except (TypeError, ValueError):
            return None

    def _prune_rain_event_dedupe(self, now_kst: datetime | None = None) -> None:
        """오래되거나 과도하게 쌓인 강수 dedupe 키를 제거합니다."""
        now_kst = now_kst or datetime.now(KST)
        cutoff = now_kst - timedelta(days=1)
        retained: list[tuple[datetime, tuple[str, str]]] = []
        for event_key in self.notified_rain_event_starts:
            event_dt = self._rain_event_datetime(event_key)
            if event_dt is not None and event_dt >= cutoff:
                retained.append((event_dt, event_key))

        # KMA 단기예보 범위를 훨씬 웃도는 상한이지만, 비정상 응답에도
        # 프로세스 수명 동안 set이 무한히 커지지 않게 한다.
        retained.sort(key=lambda item: item[0], reverse=True)
        self.notified_rain_event_starts = {
            event_key for _, event_key in retained[:_RAIN_EVENT_DEDUPE_MAX]
        }

    def _remember_rain_event(
        self,
        event_key: tuple[str, str],
        *,
        now_kst: datetime | None = None,
    ) -> None:
        """외부 호출 전에 강수 이벤트를 소비 처리하여 반복 과금을 막습니다."""
        self.notified_rain_event_starts.add(event_key)
        self._prune_rain_event_dedupe(now_kst)

    async def _generate_system_alert_safely(
        self,
        channel_id: int,
        context: str,
        alert_type: str,
    ) -> str | None:
        """알림용 LLM 실패를 폴백 메시지 전송과 분리합니다."""
        self.ai_handler = self.bot.get_cog("AIHandler")
        if not self.ai_handler or not self.ai_handler.is_ready:
            return None
        try:
            return await self.ai_handler.generate_system_alert_message(
                channel_id,
                context,
                alert_type,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "%s LLM 메시지 생성 실패(고정 문구로 계속 전송): %s",
                alert_type,
                exc,
                exc_info=True,
            )
            return None

    async def _send_alert_to_channels(
        self,
        channel_ids: set[int],
        payload: str,
        *,
        alert_type: str,
    ) -> tuple[int, int]:
        """각 채널 전송을 격리하고 성공/실패 수를 반환합니다."""
        sent_count = 0
        failed_count = 0
        for channel_id in sorted(channel_ids):
            alert_channel = self.bot.get_channel(channel_id)
            if not alert_channel:
                failed_count += 1
                logger.warning(
                    "%s 채널을 찾을 수 없어 건너뜁니다. channel_id=%s",
                    alert_type,
                    channel_id,
                )
                continue
            try:
                await alert_channel.send(
                    payload,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                sent_count += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failed_count += 1
                logger.error(
                    "%s 채널 전송 실패(다른 채널은 계속 처리): channel_id=%s error=%s",
                    alert_type,
                    channel_id,
                    exc,
                    exc_info=True,
                )
        return sent_count, failed_count

    async def _load_earthquake_watermark(self) -> bool:
        """재기동 뒤 같은 지진을 다시 보내지 않도록 마지막 발생시각을 복원합니다."""
        if self._earthquake_watermark_loaded:
            return self._earthquake_watermark_exists
        # 조회 실패가 반복되어 매분 같은 예외를 남기지 않도록 이번 프로세스에서는
        # 한 번만 시도한다. 인메모리 watermark는 계속 동작한다.
        self._earthquake_watermark_loaded = True
        try:
            async with self.bot.db.execute(
                """
                SELECT counter_value
                FROM system_counters
                WHERE counter_name = ?
                """,
                (_EARTHQUAKE_WATERMARK_KEY,),
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                return False
            stored = datetime.fromtimestamp(int(row[0]), tz=KST)
            if stored > self.last_earthquake_time:
                self.last_earthquake_time = stored
            self._earthquake_watermark_exists = True
        except Exception as exc:
            logger.warning(
                "지진 중복 방지 시각 복원 실패(인메모리 방식으로 계속): %s",
                exc,
            )
        return self._earthquake_watermark_exists

    async def _persist_earthquake_watermark(
        self,
        occurred_at: datetime,
    ) -> None:
        """Discord 전송 전에 지진 발생시각을 저장해 재기동 중복 전송을 막습니다."""
        try:
            await self.bot.db.execute(
                """
                INSERT OR REPLACE INTO system_counters
                    (counter_name, counter_value, last_reset_at)
                VALUES (?, ?, ?)
                """,
                (
                    _EARTHQUAKE_WATERMARK_KEY,
                    int(occurred_at.timestamp()),
                    discord.utils.utcnow().isoformat(),
                ),
            )
            await self.bot.db.commit()
        except Exception as exc:
            try:
                await self.bot.db.rollback()
            except Exception:
                logger.critical(
                    "지진 중복 방지 시각 저장 실패 후 rollback도 실패했습니다.",
                    exc_info=True,
                )
            # 전송 자체를 막으면 실제 경보를 놓치므로, 저장 실패 시에는 현재
            # 프로세스의 watermark만 유지하고 정직하게 운영 로그를 남긴다.
            logger.error(
                "지진 중복 방지 시각 저장 실패(알림 전송은 계속): %s",
                exc,
                exc_info=True,
            )

    @staticmethod
    def _earthquake_message_counter_name(
        incident_epoch: int,
        channel_id: int,
    ) -> str:
        return (
            f"{_EARTHQUAKE_MESSAGE_COUNTER_PREFIX}:"
            f"{int(incident_epoch)}:{int(channel_id)}"
        )

    async def _load_earthquake_message_id(
        self,
        *,
        incident_epoch: int,
        channel_id: int,
    ) -> int | None:
        """현재 지진군의 채널별 원본 Discord 메시지 ID를 복원합니다."""
        cache_key = (int(incident_epoch), int(channel_id))
        cached = self._earthquake_message_ids.get(cache_key)
        if cached:
            return cached
        try:
            counter_name = self._earthquake_message_counter_name(
                incident_epoch,
                channel_id,
            )
            async with self.bot.db.execute(
                """
                SELECT counter_value
                FROM system_counters
                WHERE counter_name = ?
                """,
                (counter_name,),
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                return None
            message_id = int(row[0])
            if message_id <= 0:
                return None
            self._earthquake_message_ids[cache_key] = message_id
            return message_id
        except Exception as exc:
            logger.warning(
                "지진 현황 메시지 ID 복원 실패: incident=%s channel=%s error=%s",
                incident_epoch,
                channel_id,
                exc,
            )
            return None

    async def _persist_earthquake_message_id(
        self,
        *,
        incident_epoch: int,
        channel_id: int,
        message_id: int,
    ) -> None:
        """새로 보낸 지진군 메시지 ID를 재기동 가능한 counter로 저장합니다."""
        cache_key = (int(incident_epoch), int(channel_id))
        self._earthquake_message_ids[cache_key] = int(message_id)
        try:
            await self.bot.db.execute(
                """
                INSERT OR REPLACE INTO system_counters
                    (counter_name, counter_value, last_reset_at)
                VALUES (?, ?, ?)
                """,
                (
                    self._earthquake_message_counter_name(
                        incident_epoch,
                        channel_id,
                    ),
                    int(message_id),
                    discord.utils.utcnow().isoformat(),
                ),
            )
            await self.bot.db.commit()
        except Exception as exc:
            try:
                await self.bot.db.rollback()
            except Exception:
                logger.critical(
                    "지진 현황 메시지 ID 저장 실패 후 rollback도 실패했습니다.",
                    exc_info=True,
                )
            logger.error(
                "지진 현황 메시지 ID 저장 실패(인메모리 편집은 계속): "
                "incident=%s channel=%s error=%s",
                incident_epoch,
                channel_id,
                exc,
                exc_info=True,
            )

    async def _send_or_edit_earthquake_incident(
        self,
        channel_ids: set[int],
        *,
        incident_epoch: int,
        payload: str,
    ) -> tuple[int, int, int]:
        """같은 지진군은 원본 메시지를 수정하고 새 지진군만 새로 보냅니다."""
        sent_count = 0
        edited_count = 0
        failed_count = 0
        for channel_id in sorted(channel_ids):
            alert_channel = self.bot.get_channel(channel_id)
            if not alert_channel:
                failed_count += 1
                logger.warning(
                    "지진 알림 채널을 찾을 수 없어 건너뜁니다. channel_id=%s",
                    channel_id,
                )
                continue

            message_id = await self._load_earthquake_message_id(
                incident_epoch=incident_epoch,
                channel_id=channel_id,
            )
            if message_id:
                try:
                    # ID를 이미 영속화했으므로 GET으로 원문을 다시 가져오지 않고
                    # Discord PATCH를 바로 보낸다. 지진마다 API 호출을 하나 줄이고
                    # Read Message History 권한이 없어도 봇 자신의 글을 수정할 수 있다.
                    if hasattr(alert_channel, "get_partial_message"):
                        message = alert_channel.get_partial_message(message_id)
                    else:
                        message = await alert_channel.fetch_message(message_id)
                    await message.edit(
                        content=payload,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    edited_count += 1
                    continue
                except discord.NotFound:
                    # 사용자가 원본을 삭제한 경우에만 대체 현황을 새로 보낸다.
                    self._earthquake_message_ids.pop(
                        (int(incident_epoch), int(channel_id)),
                        None,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # timeout/권한 오류에서 새 메시지를 보내면 중복될 수 있으므로
                    # 해당 tick은 실패로 끝내고 다음 신규 통보 때 다시 편집한다.
                    failed_count += 1
                    logger.error(
                        "지진 현황 메시지 수정 실패(중복 방지를 위해 신규 전송 안 함): "
                        "incident=%s channel=%s message=%s error=%s",
                        incident_epoch,
                        channel_id,
                        message_id,
                        exc,
                        exc_info=True,
                    )
                    continue

            try:
                sent_message = await alert_channel.send(
                    payload,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                sent_count += 1
                new_message_id = int(getattr(sent_message, "id", 0) or 0)
                if new_message_id > 0:
                    await self._persist_earthquake_message_id(
                        incident_epoch=incident_epoch,
                        channel_id=channel_id,
                        message_id=new_message_id,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failed_count += 1
                logger.error(
                    "지진 현황 메시지 신규 전송 실패: incident=%s "
                    "channel=%s error=%s",
                    incident_epoch,
                    channel_id,
                    exc,
                    exc_info=True,
                )
        return sent_count, edited_count, failed_count

    @tasks.loop(minutes=config.WEATHER_CHECK_INTERVAL_MINUTES)
    async def rain_notification_loop(self):
        """정해진 주기로 강수 예보를 조회하고 필요 시 서버에 알립니다.

        예보상 비/눈 확률이 임계값을 넘으면 채널에 안내 메시지를 전송하고, 동일 시간대 중복 알림을 방지합니다.
        """
        try:
            await self.bot.wait_until_ready()
            if not weather_utils.get_kma_api_key(): return
            rain_channel_id = getattr(config, "RAIN_NOTIFICATION_CHANNEL_ID", 0)
            greeting_channel_id = getattr(config, "GREETING_NOTIFICATION_CHANNEL_ID", 0)
            greeting_enabled = getattr(config, "ENABLE_GREETING_NOTIFICATION", False) and greeting_channel_id
            if not rain_channel_id and not greeting_enabled:
                return
            forecast = await weather_utils.get_short_term_forecast_from_kma(self.bot.db, config.DEFAULT_NX, config.DEFAULT_NY)
            if not forecast or isinstance(forecast, dict) and forecast.get("error"): return
            now_kst = datetime.now(KST)
            self._prune_rain_event_dedupe(now_kst)
            for period in self._parse_rain_periods(forecast):
                if period["start_dt"] >= now_kst and period["key"] not in self.notified_rain_event_starts:
                    start_display = period["start_dt"].strftime("%m월 %d일 %H시"); end_display = (period["end_dt"] + timedelta(hours=1)).strftime("%H시")
                    if period["start_dt"].date() != period["end_dt"].date(): end_display = (period["end_dt"] + timedelta(hours=1)).strftime("%m월 %d일 %H시")
                    precip_type = "눈❄️" if period["type"] == "눈" else "비☔"
                    alert_info = f"{config.DEFAULT_LOCATION_NAME}에 '{start_display}'부터 '{end_display}'까지 {precip_type}가 올 것으로 예상됩니다. 최대 확률은 {period['max_pop']}%입니다."
                    channel_ids = set()
                    if rain_channel_id:
                        channel_ids.add(rain_channel_id)
                    if greeting_enabled and period["max_pop"] >= config.RAIN_NOTIFICATION_GREETING_THRESHOLD_POP:
                        channel_ids.add(greeting_channel_id)
                    if not channel_ids:
                        continue

                    # LLM 또는 Discord가 실패해도 같은 이벤트를 매 주기마다 다시
                    # 과금/전송하지 않도록 첫 외부 await 전에 소비 처리한다.
                    self._remember_rain_event(period["key"], now_kst=now_kst)
                    primary_channel_id = sorted(channel_ids)[0]
                    ai_msg = await self._generate_system_alert_safely(
                        primary_channel_id,
                        alert_info,
                        f"{precip_type} 예보",
                    )
                    fallback_msg = f"{precip_type} **{config.DEFAULT_LOCATION_NAME} {precip_type} 예보** {precip_type}\n{alert_info}"
                    sent_count, failed_count = await self._send_alert_to_channels(
                        channel_ids,
                        ai_msg or fallback_msg,
                        alert_type=f"{precip_type} 예보",
                    )
                    logger.info(
                        "강수 알림 이벤트 처리 완료: key=%s sent=%d failed=%d",
                        period["key"],
                        sent_count,
                        failed_count,
                    )
        except Exception as e:
            # 일시적 오류(네트워크/KMA/파싱)로 루프가 영구 정지되지 않도록 방어한다.
            logger.error(f"강수 알림 루프 처리 중 오류(무시하고 다음 주기 진행): {e}", exc_info=True)

    async def _send_greeting_notification(self, greeting_type: str):
        """아침/저녁 유형에 맞춰 날씨 요약과 인사 메시지를 전송합니다.

        Args:
            greeting_type (str): "아침" 또는 "저녁" 중 하나.
        """
        try:
            await self.bot.wait_until_ready()
            if not weather_utils.get_kma_api_key(): return
            channel_id = getattr(config, 'GREETING_NOTIFICATION_CHANNEL_ID', 0) or config.RAIN_NOTIFICATION_CHANNEL_ID
            alert_channel = self.bot.get_channel(channel_id)
            if not alert_channel: return
            forecast = await weather_utils.get_short_term_forecast_from_kma(self.bot.db, config.DEFAULT_NX, config.DEFAULT_NY)
            summary = weather_utils.format_short_term_forecast(forecast, "오늘", 0) if forecast and not forecast.get("error") else f"오늘 {config.DEFAULT_LOCATION_NAME} 날씨 정보를 가져오는 데 실패했어. 😥"
            if greeting_type == "아침": alert_context = f"좋은 아침! ☀️ 오늘 {config.DEFAULT_LOCATION_NAME} 날씨는 이렇대.\n\n> {summary}\n\n오늘 하루도 활기차게 시작해보자고! 💪"
            else: alert_context = f"오늘 하루도 수고했어! 참고로 오늘 {config.DEFAULT_LOCATION_NAME} 날씨는 이랬어.\n\n> {summary}\n\n이제 편안한 밤 보내고, 내일 또 보자! 잘 자! 🌙"
            ai_msg = await self._generate_system_alert_safely(
                channel_id,
                alert_context,
                f"{greeting_type} 인사",
            )
            await alert_channel.send(ai_msg or alert_context, allowed_mentions=discord.AllowedMentions.none())
        except Exception as e:
            # 일시적 오류(네트워크/KMA/Discord)로 루프가 영구 정지되지 않도록 방어한다.
            logger.error(f"{greeting_type} 인사 알림 처리 중 오류(무시하고 다음 주기 진행): {e}", exc_info=True)

    @tasks.loop(time=dt_time(hour=config.MORNING_GREETING_TIME["hour"], minute=config.MORNING_GREETING_TIME["minute"], tzinfo=KST))
    async def morning_greeting_loop(self):
        """매일 아침 지정된 시간에 날씨 정보와 함께 인사말을 보냅니다."""
        await self._send_greeting_notification("아침")

    @tasks.loop(time=dt_time(hour=config.EVENING_GREETING_TIME["hour"], minute=config.EVENING_GREETING_TIME["minute"], tzinfo=KST))
    async def evening_greeting_loop(self):
        """매일 저녁 지정된 시간에 날씨 정보와 함께 인사말을 보냅니다."""
        await self._send_greeting_notification("저녁")

    @tasks.loop(seconds=config.EARTHQUAKE_CHECK_INTERVAL_SECONDS)
    async def earthquake_alert_loop(self):
        """설정된 주기마다 최근 지진 정보를 확인하고 새로운 지진 발생 시 알립니다."""
        await self.bot.wait_until_ready()
        if not weather_utils.get_kma_api_key(): return
        await self._load_earthquake_watermark()

        channel_ids: set[int] = set()
        try:
            # Prefer DB-registered channels (AI allowed channels)
            async with self.bot.db.execute(
                "SELECT ai_allowed_channels FROM guild_settings WHERE ai_allowed_channels IS NOT NULL"
            ) as cursor:
                rows = await cursor.fetchall()
            for (raw_json,) in rows:
                try:
                    channels = json.loads(raw_json) if raw_json else []
                except json.JSONDecodeError:
                    channels = []
                for cid in channels:
                    try:
                        channel_ids.add(int(cid))
                    except (TypeError, ValueError):
                        continue
        except Exception as e:
            logger.debug(f"지진 알림 채널 조회 실패: {e}")

        # Fallback to prompt-config registered channels
        for cid, meta in getattr(config, "CHANNEL_AI_CONFIG", {}).items():
            if meta.get("allowed", False):
                channel_ids.add(int(cid))

        if not channel_ids and getattr(config, "RAIN_NOTIFICATION_CHANNEL_ID", 0):
            channel_ids.add(int(config.RAIN_NOTIFICATION_CHANNEL_ID))

        if not channel_ids:
            return
        
        try:
            earthquakes = await weather_utils.get_recent_earthquakes(self.bot.db)
        except Exception as e:
            # 일시적 오류로 루프가 영구 정지되지 않도록 방어한다.
            logger.error(f"지진 정보 조회 중 오류(무시하고 다음 주기 진행): {e}", exc_info=True)
            return
        if earthquakes is None:
            return
        if not earthquakes:
            if not self._earthquake_watermark_exists:
                # 정상적인 빈 응답이라면 기동 시각을 기준점으로 남긴다. 이 처리가
                # 없으면 며칠간 지진이 없던 신규 설치에서 첫 실제 지진을
                # "기존 사건"으로 오인해 건너뛸 수 있다.
                baseline = datetime.now(KST)
                self.last_earthquake_time = baseline
                self._earthquake_watermark_exists = True
                await self._persist_earthquake_watermark(baseline)
                logger.info(
                    "지진 알림 최초 빈 기준점 설정: occurred_at=%s",
                    baseline.isoformat(),
                )
            return
        
        clusters = weather_utils.cluster_earthquake_events(
            earthquakes,
            sequence_window_hours=getattr(
                config,
                "EARTHQUAKE_SEQUENCE_WINDOW_HOURS",
                72,
            ),
            sequence_radius_km=getattr(
                config,
                "EARTHQUAKE_SEQUENCE_RADIUS_KM",
                150,
            ),
        )

        if not self._earthquake_watermark_exists:
            # 이 중복 방지 키가 처음 도입되었거나 DB 조회가 실패한 기동에서는,
            # 이미 KMA에 게시된 최신 사건을 기준점으로만 기록한다. 배포·재기동
            # 직후 과거 지진/여진을 다시 방송하지 않고 다음 신규 사건부터 알린다.
            latest_existing: datetime | None = None
            for eqk in earthquakes:
                parsed = weather_utils.earthquake_event_datetime(eqk)
                if parsed is None:
                    continue
                if latest_existing is None or parsed > latest_existing:
                    latest_existing = parsed
            if latest_existing is not None:
                self.last_earthquake_time = latest_existing
                self._earthquake_watermark_exists = True
                await self._persist_earthquake_watermark(latest_existing)
                logger.info(
                    "지진 알림 최초 기준점 설정(기존 사건 미전송): occurred_at=%s",
                    latest_existing.isoformat(),
                )
            return

        affected_clusters: list[tuple[datetime, list[dict]]] = []
        newest_event = self.last_earthquake_time
        for cluster in clusters:
            event_times = [
                occurred
                for event in cluster
                if (occurred := weather_utils.earthquake_event_datetime(event))
                is not None
            ]
            new_times = [
                occurred
                for occurred in event_times
                if occurred > self.last_earthquake_time
            ]
            if not new_times or not event_times:
                continue
            affected_clusters.append((min(event_times), cluster))
            newest_event = max(newest_event, *new_times)

        if not affected_clusters:
            return

        # Discord 전송·수정 전에 watermark를 전진시켜 재기동이나 일부 채널
        # 실패가 과거 지진 메시지의 반복 전송으로 이어지지 않게 한다.
        self.last_earthquake_time = newest_event
        await self._persist_earthquake_watermark(newest_event)

        for incident_start, cluster in affected_clusters:
            try:
                formatted_msg = weather_utils.format_earthquake_incident_alert(
                    cluster,
                    max_followups=getattr(
                        config,
                        "EARTHQUAKE_SEQUENCE_MAX_DISPLAY_EVENTS",
                        6,
                    ),
                )
                sent_count, edited_count, failed_count = (
                    await self._send_or_edit_earthquake_incident(
                        channel_ids,
                        incident_epoch=int(incident_start.timestamp()),
                        payload=formatted_msg,
                    )
                )
                logger.info(
                    "지진군 현황 처리 완료: incident=%s events=%d "
                    "sent=%d edited=%d failed=%d",
                    incident_start.isoformat(),
                    len(cluster),
                    sent_count,
                    edited_count,
                    failed_count,
                )
            except Exception as exc:
                logger.error(
                    "지진군 현황 처리 오류: incident=%s error=%s",
                    incident_start.isoformat(),
                    exc,
                    exc_info=True,
                )

async def setup(bot: commands.Bot):
    """Cog를 봇에 등록하는 함수입니다."""
    await bot.add_cog(WeatherCog(bot))

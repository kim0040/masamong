"""날씨 스케줄/알림/응답 계약 회귀 테스트."""

from datetime import date, datetime, timedelta

import pytest

import config
from cogs.weather_cog import KST, WeatherCog
from utils import weather as weather_utils


class _FakeChannel:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.attempts = 0
        self.payloads: list[str] = []

    async def send(self, content, **_kwargs):
        self.attempts += 1
        if self.fail:
            raise RuntimeError("discord send failed")
        self.payloads.append(content)


class _FakeAI:
    is_ready = True

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    async def generate_system_alert_message(self, *_args):
        self.calls += 1
        if self.fail:
            raise RuntimeError("llm failed")
        return "AI 알림"


class _BrokenDb:
    def execute(self, *_args, **_kwargs):
        raise RuntimeError("db unavailable in unit test")


class _FakeBot:
    def __init__(self, channels: dict[int, _FakeChannel], ai: _FakeAI) -> None:
        self.db = _BrokenDb()
        self.channels = channels
        self.ai = ai

    async def wait_until_ready(self):
        return None

    def get_cog(self, name: str):
        return self.ai if name == "AIHandler" else None

    def get_channel(self, channel_id: int):
        return self.channels.get(channel_id)


def test_daily_greeting_schedule_uses_real_kst_offset():
    """pytz LMT(+08:28)가 아니라 Asia/Seoul(+09:00)로 예약되어야 한다."""
    assert getattr(KST, "key", None) == "Asia/Seoul"
    for loop in (
        WeatherCog.morning_greeting_loop,
        WeatherCog.evening_greeting_loop,
    ):
        for scheduled_time in loop.time:
            scheduled = datetime.combine(date(2026, 1, 1), scheduled_time)
            assert scheduled.utcoffset() == timedelta(hours=9)


@pytest.mark.asyncio
async def test_rain_partial_channel_and_llm_failure_are_consumed_once(monkeypatch):
    """LLM/한 채널 실패가 있어도 성공 채널 중복 전송과 LLM 재호출을 막는다."""
    ok_channel = _FakeChannel()
    failed_channel = _FakeChannel(fail=True)
    ai = _FakeAI(fail=True)
    bot = _FakeBot({10: ok_channel, 20: failed_channel}, ai)
    cog = WeatherCog(bot)

    now_kst = datetime.now(KST)
    start_dt = (now_kst + timedelta(hours=1)).replace(second=0, microsecond=0)
    event_key = (start_dt.strftime("%Y%m%d"), start_dt.strftime("%H%M"))
    period = {
        "type": "비",
        "start_dt": start_dt,
        "end_dt": start_dt,
        "max_pop": 80,
        "key": event_key,
    }

    async def fake_forecast(*_args, **_kwargs):
        return {"item": []}

    monkeypatch.setattr(weather_utils, "get_kma_api_key", lambda: "test-key")
    monkeypatch.setattr(
        weather_utils,
        "get_short_term_forecast_from_kma",
        fake_forecast,
    )
    monkeypatch.setattr(cog, "_parse_rain_periods", lambda _forecast: [period])
    monkeypatch.setattr(config, "RAIN_NOTIFICATION_CHANNEL_ID", 10)
    monkeypatch.setattr(config, "GREETING_NOTIFICATION_CHANNEL_ID", 20)
    monkeypatch.setattr(config, "ENABLE_GREETING_NOTIFICATION", True)
    monkeypatch.setattr(config, "RAIN_NOTIFICATION_GREETING_THRESHOLD_POP", 70)

    await WeatherCog.rain_notification_loop.coro(cog)
    await WeatherCog.rain_notification_loop.coro(cog)

    assert ai.calls == 1
    assert ok_channel.attempts == 1
    assert failed_channel.attempts == 1
    assert len(ok_channel.payloads) == 1
    assert event_key in cog.notified_rain_event_starts


@pytest.mark.asyncio
async def test_earthquake_partial_channel_failure_advances_watermark(monkeypatch):
    """한 채널 전송 실패가 다음 polling에서 같은 지진을 재과금하지 않는다."""
    ok_channel = _FakeChannel()
    failed_channel = _FakeChannel(fail=True)
    ai = _FakeAI()
    bot = _FakeBot({10: ok_channel, 20: failed_channel}, ai)
    cog = WeatherCog(bot)

    occurred_at = datetime.now(KST).replace(microsecond=0)
    cog.last_earthquake_time = occurred_at - timedelta(hours=1)
    quake = {
        "tmEqk": occurred_at.strftime("%Y%m%d%H%M%S"),
        "loc": "테스트 지역",
        "mt": "4.2",
        "rem": "테스트",
    }

    async def fake_earthquakes(*_args, **_kwargs):
        return [dict(quake)]

    monkeypatch.setattr(weather_utils, "get_kma_api_key", lambda: "test-key")
    monkeypatch.setattr(weather_utils, "get_recent_earthquakes", fake_earthquakes)
    monkeypatch.setattr(
        config,
        "CHANNEL_AI_CONFIG",
        {10: {"allowed": True}, 20: {"allowed": True}},
    )
    monkeypatch.setattr(config, "RAIN_NOTIFICATION_CHANNEL_ID", 0)

    await WeatherCog.earthquake_alert_loop.coro(cog)
    await WeatherCog.earthquake_alert_loop.coro(cog)

    assert ai.calls == 1
    assert ok_channel.attempts == 1
    assert failed_channel.attempts == 1
    assert cog.last_earthquake_time == occurred_at


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"item": [{"wfSv1": "전국이 대체로 맑겠습니다."}]},
        {
            "response": {
                "body": {
                    "items": {
                        "item": [{"wfSv1": "전국이 대체로 맑겠습니다."}]
                    }
                }
            }
        },
        {
            "response": {
                "body": {
                    "items": [{"wfSv1": "전국이 대체로 맑겠습니다."}]
                }
            }
        },
    ],
)
async def test_weather_overview_accepts_normalized_and_raw_contract(
    monkeypatch,
    payload,
):
    async def fake_fetch(*_args, **_kwargs):
        return payload

    monkeypatch.setattr(weather_utils, "_fetch_kma_api", fake_fetch)

    assert (
        await weather_utils.get_weather_overview(object())
        == "전국이 대체로 맑겠습니다."
    )


@pytest.mark.asyncio
async def test_mid_term_v2_uses_shared_region_mapping(monkeypatch):
    captured: list[str] = []

    async def fake_v2(_db, region_code):
        captured.append(region_code)
        return "제주 중기예보"

    monkeypatch.setattr(weather_utils, "get_mid_term_forecast_v2", fake_v2)
    cog = WeatherCog(_FakeBot({}, _FakeAI()))

    assert await cog.get_mid_term_weather(3, "제주시") == "제주 중기예보"
    assert captured == ["11G00000"]


def test_short_term_forecast_preserves_zero_degree_and_missing_pop():
    target_date = datetime.now(weather_utils.KST).strftime("%Y%m%d")
    payload = {
        "item": [
            {
                "fcstDate": target_date,
                "fcstTime": "0600",
                "category": "TMN",
                "fcstValue": "0",
            },
            {
                "fcstDate": target_date,
                "fcstTime": "1500",
                "category": "TMX",
                "fcstValue": "2",
            },
            {
                "fcstDate": target_date,
                "fcstTime": "1200",
                "category": "SKY",
                "fcstValue": "1",
            },
        ]
    }

    rendered = weather_utils.format_short_term_forecast(payload, "오늘", 0)

    assert "0.0°C ~ 2.0°C" in rendered
    assert "강수확률: ~0%" in rendered

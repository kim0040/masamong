"""날씨 스케줄/알림/응답 계약 회귀 테스트."""

import asyncio
from datetime import date, datetime, timedelta

import aiosqlite
import pytest

import config
from cogs.weather_cog import KST, WeatherCog
from utils import weather as weather_utils


class _FakeChannel:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.attempts = 0
        self.edits = 0
        self.fetches = 0
        self.payloads: list[str] = []
        self.messages: dict[int, "_FakeSentMessage"] = {}

    async def send(self, content, **_kwargs):
        self.attempts += 1
        if self.fail:
            raise RuntimeError("discord send failed")
        self.payloads.append(content)
        message_id = 10_000 + len(self.messages)
        message = _FakeSentMessage(
            message_id,
            self,
            payload_index=len(self.payloads) - 1,
        )
        self.messages[message_id] = message
        return message

    async def fetch_message(self, message_id: int):
        self.fetches += 1
        return self.messages[int(message_id)]

    def get_partial_message(self, message_id: int):
        return self.messages[int(message_id)]


class _FakeSentMessage:
    def __init__(
        self,
        message_id: int,
        channel: _FakeChannel,
        *,
        payload_index: int,
    ) -> None:
        self.id = int(message_id)
        self.channel = channel
        self.payload_index = int(payload_index)

    async def edit(self, *, content: str, **_kwargs):
        self.channel.edits += 1
        self.channel.payloads[self.payload_index] = content
        return self


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
    """지진은 LLM 없이 고정 형식으로 한 번만 보내고 실패 채널을 격리한다."""
    ok_channel = _FakeChannel()
    failed_channel = _FakeChannel(fail=True)
    ai = _FakeAI()
    bot = _FakeBot({10: ok_channel, 20: failed_channel}, ai)
    cog = WeatherCog(bot)
    cog._earthquake_watermark_exists = True

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

    assert ai.calls == 0
    assert ok_channel.attempts == 1
    assert failed_channel.attempts == 1
    assert cog.last_earthquake_time == occurred_at
    assert "**🚨 지진 발생 알림**" in ok_channel.payloads[0]
    assert "테스트 지역" in ok_channel.payloads[0]
    assert "AI 알림" not in ok_channel.payloads[0]


@pytest.mark.asyncio
async def test_earthquake_first_start_seeds_latest_without_replaying(monkeypatch):
    """새 watermark의 첫 기동은 기존 사건을 기준점으로만 저장하고 보내지 않는다."""
    ok_channel = _FakeChannel()
    ai = _FakeAI()
    bot = _FakeBot({10: ok_channel}, ai)
    cog = WeatherCog(bot)

    older = datetime.now(KST).replace(microsecond=0) - timedelta(minutes=20)
    latest = older + timedelta(minutes=10)
    events = [
        {
            "tmEqk": occurred.strftime("%Y%m%d%H%M%S"),
            "loc": "기존 사건",
            "mt": "4.2",
            "rem": "테스트",
        }
        for occurred in (older, latest)
    ]
    persisted: list[datetime] = []

    async def fake_earthquakes(*_args, **_kwargs):
        return [dict(item) for item in events]

    async def fake_persist(occurred_at):
        persisted.append(occurred_at)

    monkeypatch.setattr(weather_utils, "get_kma_api_key", lambda: "test-key")
    monkeypatch.setattr(weather_utils, "get_recent_earthquakes", fake_earthquakes)
    monkeypatch.setattr(cog, "_persist_earthquake_watermark", fake_persist)
    monkeypatch.setattr(config, "CHANNEL_AI_CONFIG", {10: {"allowed": True}})
    monkeypatch.setattr(config, "RAIN_NOTIFICATION_CHANNEL_ID", 0)

    await WeatherCog.earthquake_alert_loop.coro(cog)

    assert ok_channel.attempts == 0
    assert ai.calls == 0
    assert persisted == [latest]
    assert cog.last_earthquake_time == latest
    assert cog._earthquake_watermark_exists is True


@pytest.mark.asyncio
async def test_earthquake_empty_first_start_creates_baseline(monkeypatch):
    """과거 사건이 없어도 기준점을 만들어 이후 첫 실제 지진은 놓치지 않는다."""
    bot = _FakeBot({10: _FakeChannel()}, _FakeAI())
    cog = WeatherCog(bot)
    persisted: list[datetime] = []

    async def fake_earthquakes(*_args, **_kwargs):
        return []

    async def fake_persist(occurred_at):
        persisted.append(occurred_at)

    monkeypatch.setattr(weather_utils, "get_kma_api_key", lambda: "test-key")
    monkeypatch.setattr(weather_utils, "get_recent_earthquakes", fake_earthquakes)
    monkeypatch.setattr(cog, "_persist_earthquake_watermark", fake_persist)
    monkeypatch.setattr(config, "CHANNEL_AI_CONFIG", {10: {"allowed": True}})
    monkeypatch.setattr(config, "RAIN_NOTIFICATION_CHANNEL_ID", 0)

    before = datetime.now(KST)
    await WeatherCog.earthquake_alert_loop.coro(cog)

    assert len(persisted) == 1
    assert persisted[0] >= before
    assert cog._earthquake_watermark_exists is True


@pytest.mark.asyncio
async def test_earthquake_aftershock_edits_original_message(monkeypatch):
    """같은 지진군의 후속 지진은 새 메시지 대신 원본 현황을 수정한다."""
    channel = _FakeChannel()
    ai = _FakeAI()
    bot = _FakeBot({10: channel}, ai)
    cog = WeatherCog(bot)
    cog._earthquake_watermark_exists = True

    main_at = datetime.now(KST).replace(microsecond=0)
    events = [
        {
            "tmEqk": main_at.strftime("%Y%m%d%H%M%S"),
            "tmFc": main_at.strftime("%Y%m%d%H%M"),
            "loc": "일본 구마모토현 남쪽 20km 지역",
            "lat": "32.60",
            "lon": "130.70",
            "mt": "7.1",
            "dep": "10",
            "rem": "국내 일부 지역에서 지진동을 느낄 수 있음",
        }
    ]
    cog.last_earthquake_time = main_at - timedelta(minutes=1)

    async def fake_earthquakes(*_args, **_kwargs):
        return [dict(item) for item in events]

    async def fake_persist(_occurred_at):
        return None

    monkeypatch.setattr(weather_utils, "get_kma_api_key", lambda: "test-key")
    monkeypatch.setattr(weather_utils, "get_recent_earthquakes", fake_earthquakes)
    monkeypatch.setattr(cog, "_persist_earthquake_watermark", fake_persist)
    monkeypatch.setattr(config, "CHANNEL_AI_CONFIG", {10: {"allowed": True}})
    monkeypatch.setattr(config, "RAIN_NOTIFICATION_CHANNEL_ID", 0)

    await WeatherCog.earthquake_alert_loop.coro(cog)
    assert channel.attempts == 1
    assert channel.edits == 0

    aftershock_at = main_at + timedelta(minutes=12)
    events.append(
        {
            "tmEqk": aftershock_at.strftime("%Y%m%d%H%M%S"),
            "tmFc": aftershock_at.strftime("%Y%m%d%H%M"),
            "loc": "일본 구마모토현 남쪽 25km 지역",
            "lat": "32.64",
            "lon": "130.75",
            "mt": "4.8",
            "dep": "12",
            "rem": "국내 일부 지역에서 지진동을 느낄 수 있음",
        }
    )
    await WeatherCog.earthquake_alert_loop.coro(cog)

    assert channel.attempts == 1
    assert channel.edits == 1
    assert channel.fetches == 0
    assert "**🚨 지진 연속 발생 현황**" in channel.payloads[0]
    assert "총 2건" in channel.payloads[0]
    assert "규모 **4.8**" in channel.payloads[0]
    assert ai.calls == 0


@pytest.mark.asyncio
async def test_earthquake_restart_restores_message_id_and_edits(monkeypatch):
    """재기동 후에도 DB counter의 원본 메시지 ID를 복원해 같은 글을 수정한다."""
    channel = _FakeChannel()
    bot = _FakeBot({10: channel}, _FakeAI())
    bot.db = await aiosqlite.connect(":memory:")
    await bot.db.execute(
        """
        CREATE TABLE system_counters (
            counter_name TEXT PRIMARY KEY,
            counter_value INTEGER NOT NULL,
            last_reset_at TEXT NOT NULL
        )
        """
    )
    await bot.db.commit()

    main_at = datetime.now(KST).replace(microsecond=0)
    events = [
        {
            "tmEqk": main_at.strftime("%Y%m%d%H%M%S"),
            "loc": "일본 구마모토현",
            "lat": "32.60",
            "lon": "130.70",
            "mt": "7.1",
            "rem": "국내 영향 가능",
        }
    ]

    async def fake_earthquakes(*_args, **_kwargs):
        return [dict(item) for item in events]

    monkeypatch.setattr(weather_utils, "get_kma_api_key", lambda: "test-key")
    monkeypatch.setattr(weather_utils, "get_recent_earthquakes", fake_earthquakes)
    monkeypatch.setattr(config, "CHANNEL_AI_CONFIG", {10: {"allowed": True}})
    monkeypatch.setattr(config, "RAIN_NOTIFICATION_CHANNEL_ID", 0)

    first_cog = WeatherCog(bot)
    first_cog._earthquake_watermark_loaded = True
    first_cog._earthquake_watermark_exists = True
    first_cog.last_earthquake_time = main_at - timedelta(minutes=1)
    await WeatherCog.earthquake_alert_loop.coro(first_cog)
    assert channel.attempts == 1

    aftershock_at = main_at + timedelta(minutes=9)
    events.append(
        {
            "tmEqk": aftershock_at.strftime("%Y%m%d%H%M%S"),
            "loc": "일본 구마모토현 남쪽 15km",
            "lat": "32.63",
            "lon": "130.73",
            "mt": "4.6",
            "rem": "국내 영향 가능",
        }
    )

    restarted_cog = WeatherCog(bot)
    await WeatherCog.earthquake_alert_loop.coro(restarted_cog)

    assert channel.attempts == 1
    assert channel.edits == 1
    assert channel.fetches == 0
    assert "총 2건" in channel.payloads[0]
    await bot.db.close()


@pytest.mark.asyncio
async def test_unrelated_earthquake_starts_new_message(monkeypatch):
    """시간이 가깝더라도 먼 진앙의 독립 지진은 별도 현황 메시지를 시작한다."""
    channel = _FakeChannel()
    bot = _FakeBot({10: channel}, _FakeAI())
    cog = WeatherCog(bot)
    cog._earthquake_watermark_exists = True
    first_at = datetime.now(KST).replace(microsecond=0)
    events = [
        {
            "tmEqk": first_at.strftime("%Y%m%d%H%M%S"),
            "loc": "일본 구마모토현",
            "lat": "32.60",
            "lon": "130.70",
            "mt": "7.1",
            "rem": "국내 영향 가능",
        }
    ]
    cog.last_earthquake_time = first_at - timedelta(minutes=1)

    async def fake_earthquakes(*_args, **_kwargs):
        return [dict(item) for item in events]

    async def fake_persist(_occurred_at):
        return None

    monkeypatch.setattr(weather_utils, "get_kma_api_key", lambda: "test-key")
    monkeypatch.setattr(weather_utils, "get_recent_earthquakes", fake_earthquakes)
    monkeypatch.setattr(cog, "_persist_earthquake_watermark", fake_persist)
    monkeypatch.setattr(config, "CHANNEL_AI_CONFIG", {10: {"allowed": True}})
    monkeypatch.setattr(config, "RAIN_NOTIFICATION_CHANNEL_ID", 0)

    await WeatherCog.earthquake_alert_loop.coro(cog)
    second_at = first_at + timedelta(minutes=20)
    events.append(
        {
            "tmEqk": second_at.strftime("%Y%m%d%H%M%S"),
            "loc": "대만 동부 해역",
            "lat": "24.00",
            "lon": "122.00",
            "mt": "6.2",
            "rem": "국내 영향 가능",
        }
    )
    await WeatherCog.earthquake_alert_loop.coro(cog)

    assert channel.attempts == 2
    assert channel.edits == 0
    assert len(channel.payloads) == 2


def test_earthquake_clustering_uses_distance_and_dedupes_corrections():
    events = [
        {
            "tmEqk": "20260728162700",
            "tmFc": "202607281634",
            "tmSeq": "1",
            "loc": "일본 구마모토현 남쪽 20km",
            "lat": "32.60",
            "lon": "130.70",
            "mt": "7.0",
        },
        {
            "tmEqk": "20260728162700",
            "tmFc": "202607281636",
            "tmSeq": "2",
            "loc": "일본 구마모토현 남쪽 20km",
            "lat": "32.60",
            "lon": "130.70",
            "mt": "7.1",
            "cor": "규모 상향",
        },
        {
            "tmEqk": "20260728170800",
            "tmFc": "202607281712",
            "loc": "일본 구마모토현 남쪽 25km",
            "lat": "32.64",
            "lon": "130.75",
            "mt": "4.8",
        },
        {
            "tmEqk": "20260728172000",
            "tmFc": "202607281724",
            "loc": "대만 동부 해역",
            "lat": "24.00",
            "lon": "122.00",
            "mt": "6.0",
        },
    ]

    clusters = weather_utils.cluster_earthquake_events(events)

    assert [len(cluster) for cluster in clusters] == [2, 1]
    assert clusters[0][0]["mt"] == "7.1"
    assert clusters[0][0]["cor"] == "규모 상향"


def test_earthquake_incident_render_is_formal_and_discord_sized():
    events = [
        {
            "tmEqk": f"2026072816{27 + index:02d}00",
            "tmFc": f"2026072816{34 + index:02d}",
            "loc": f"일본 구마모토현 남쪽 {20 + index}km 지역",
            "lat": str(32.60 + index * 0.01),
            "lon": str(130.70 + index * 0.01),
            "mt": "7.1" if index == 0 else f"4.{index}",
            "dep": "10",
            "rem": "국내 일부 지역에서 지진동을 느낄 수 있음",
        }
        for index in range(8)
    ]

    payload = weather_utils.format_earthquake_incident_alert(events)

    assert "**🚨 지진 연속 발생 현황**" in payload
    assert "총 8건" in payload
    assert "공식 여진 판정이 아닙니다" in payload
    assert "지금 해야 할 일" in payload
    assert "마사몽" not in payload
    assert len(payload) <= 1950


def test_earthquake_alert_is_official_fixed_format_with_useful_fields():
    payload = weather_utils.format_earthquake_alert(
        {
            "tmEqk": "20260728162700",
            "tmFc": "202607281634",
            "loc": "일본 규슈 구마모토현 인근",
            "mt": "6.1",
            "dep": "10",
            "inT": "최대진도 Ⅳ",
            "rem": "국내 일부 지역에서 지진동을 느낄 수 있음",
            "cor": "없음",
        }
    )

    assert "2026년 07월 28일 16시 27분 00초" in payload
    assert "**기상청 발표:** 2026년 07월 28일 16시 34분" in payload
    assert "**깊이:** 10 km" in payload
    assert "**계기진도:** 최대진도 Ⅳ" in payload
    assert "엘리베이터를 사용하지 말고 계단" in payload
    assert "최신 기상청·재난문자 안내가 우선" in payload
    assert "대피 요망" not in payload


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


_ACTIVE_TYPHOON_LIST = """#START7777
# YY SEQ NOW EFF TM_ST TM_ED TYP_NAME TYP_EN REM
2026 13 1 4 202607270600 210012310000 돌핀 DOLPHIN 제13호 태풍
#7777END"""

_ACTIVE_TYPHOON_DETAIL = """#START7777
# FT YY TYP SEQ TMD TYP_TM FT_TM LAT LON DIR SP PS WS RAD15 RAD25 RAD ED15 ER15 LOC ED25 ER25
0 2026 13 10 0 202607291200 202607291200 15.2 167.6 NW 23 935 49 330 90 0 SW 230 괌 동쪽 약 2470 km 부근 해상 SW,60,
1 2026 13 0 12 202607291200 202607300000 16.3 165.7 WNW 20 925 51 350 110 40 SW 250 괌 동쪽 약 2270 km 부근 해상 SW,90,
1 2026 13 0 24 202607291200 202607301200 17.3 163.8 WNW 19 915 55 370 120 80 SW 270 괌 동북동쪽 약 2080 km 부근 해상 SW,100,
#7777END"""


def test_typhoon_formatter_uses_latest_official_analysis_and_forecast():
    rendered = weather_utils.format_typhoon_list(
        _ACTIVE_TYPHOON_LIST,
        _ACTIVE_TYPHOON_DETAIL,
    )

    assert "**제13호 태풍 돌핀(DOLPHIN)** · 활동 중" in rendered
    assert "07/29 21:00 KST" in rendered
    assert "괌 동쪽 약 2470 km 부근 해상" in rendered
    assert "중심기압 935 hPa" in rendered
    assert "최대풍속 49 m/s" in rendered
    assert "북서쪽 23 km/h" in rendered
    assert "**한반도 영향:** 현재 영향 없음" in rendered
    assert "**24시간 전망:** 07/30 21:00 KST" in rendered
    assert "최대풍속 55 m/s" in rendered


def test_typhoon_formatter_distinguishes_no_active_storm_from_api_failure():
    ended = _ACTIVE_TYPHOON_LIST.replace(" 1 4 ", " 2 4 ")

    assert (
        weather_utils.format_typhoon_list(ended)
        == "현재 기상청 목록에 활동 중인 태풍이 없습니다."
    )
    assert weather_utils.format_typhoon_list("invalid response") is None


@pytest.mark.asyncio
async def test_typhoon_detail_is_only_fetched_when_active(monkeypatch):
    calls: list[tuple[str, dict]] = []

    async def fake_cached(
        _db,
        _endpoint,
        params,
        *,
        api_type,
        **_kwargs,
    ):
        calls.append((api_type, dict(params)))
        if api_type == "typhoon":
            return _ACTIVE_TYPHOON_LIST
        return _ACTIVE_TYPHOON_DETAIL

    monkeypatch.setattr(weather_utils, "_fetch_kma_cached", fake_cached)

    rendered = await weather_utils.get_typhoons(object(), timeout=1.0)

    assert "제13호 태풍 돌핀" in rendered
    assert [api_type for api_type, _params in calls] == [
        "typhoon",
        "typhoon_detail",
    ]
    assert calls[1][1]["mode"] == "2"
    assert len(calls[1][1]["tm"]) == 12


@pytest.mark.asyncio
async def test_future_weather_question_keeps_typhoon_context(monkeypatch):
    async def fake_forecast(*_args, **_kwargs):
        return {"item": []}

    async def fake_typhoons(*_args, **_kwargs):
        return "공식 태풍 분석"

    monkeypatch.setattr(
        weather_utils,
        "get_short_term_forecast_from_kma",
        fake_forecast,
    )
    monkeypatch.setattr(
        weather_utils,
        "format_short_term_forecast",
        lambda *_args, **_kwargs: "내일 예보",
    )
    monkeypatch.setattr(weather_utils, "get_typhoons", fake_typhoons)
    cog = WeatherCog(_FakeBot({}, _FakeAI()))

    rendered, error = await cog.get_formatted_weather_string(
        1,
        "서울",
        "60",
        "127",
        "내일 태풍 영향 어때?",
    )

    assert error is None
    assert "🌀 **태풍 정보:** 공식 태풍 분석" in rendered
    assert "내일 예보" in rendered


@pytest.mark.asyncio
async def test_mid_term_weather_uses_structured_json_path(monkeypatch):
    captured: list[tuple[str, int]] = []

    async def fake_mid(_db, location_name, day_offset):
        captured.append((location_name, day_offset))
        return "제주 중기예보"

    monkeypatch.setattr(weather_utils, "get_mid_term_forecast", fake_mid)
    cog = WeatherCog(_FakeBot({}, _FakeAI()))

    assert await cog.get_mid_term_weather(3, "제주시") == "제주 중기예보"
    assert captured == [("제주시", 3)]


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


def test_weather_formats_all_useful_observation_and_short_range_fields():
    observed = weather_utils.format_current_weather(
        {
            "item": [
                {
                    "baseDate": "20260728",
                    "baseTime": "1700",
                    "category": "T1H",
                    "obsrValue": "31.2",
                },
                {"category": "REH", "obsrValue": "72"},
                {"category": "PTY", "obsrValue": "1"},
                {"category": "RN1", "obsrValue": "3.0"},
                {"category": "VEC", "obsrValue": "225"},
                {"category": "WSD", "obsrValue": "6.4"},
            ]
        }
    )

    assert "07/28 17:00 관측" in observed
    assert "31.2°C" in observed
    assert "습도:** 72%" in observed
    assert "1시간 3.0 mm" in observed
    assert "남서풍 6.4 m/s" in observed

    nowcast = weather_utils.format_ultra_short_forecast(
        {
            "item": [
                {
                    "fcstDate": "20260728",
                    "fcstTime": "1800",
                    "category": "PTY",
                    "fcstValue": "1",
                },
                {
                    "fcstDate": "20260728",
                    "fcstTime": "1800",
                    "category": "POP",
                    "fcstValue": "80",
                },
                {
                    "fcstDate": "20260728",
                    "fcstTime": "1800",
                    "category": "RN1",
                    "fcstValue": "5.0",
                },
                {
                    "fcstDate": "20260728",
                    "fcstTime": "1800",
                    "category": "LGT",
                    "fcstValue": "1",
                },
                {
                    "fcstDate": "20260728",
                    "fcstTime": "1800",
                    "category": "VEC",
                    "fcstValue": "90",
                },
                {
                    "fcstDate": "20260728",
                    "fcstTime": "1800",
                    "category": "WSD",
                    "fcstValue": "9.1",
                },
                {
                    "fcstDate": "20260728",
                    "fcstTime": "1800",
                    "category": "REH",
                    "fcstValue": "85",
                },
            ]
        }
    )

    assert "18시 비 80%" in nowcast
    assert "5.0 mm" in nowcast
    assert "동풍 9.1 m/s" in nowcast
    assert "낙뢰 신호:** 있음" in nowcast


def test_active_warning_format_prefers_requested_location():
    payload = """# REG_UP,REG_UP_KO,REG_ID,REG_KO,TM_FC,TM_EF,WRN,LVL,CMD
#START7777
L1000000,전라북도,L1010000,전라북도 전주시,202607281600,202607281700,R,3,1
L2000000,부산광역시,L2010000,부산광역시 해운대구,202607281500,202607281600,W,2,1
#7777END"""

    rendered = weather_utils.format_weather_alerts(payload, "전주시")

    assert "전라북도 전주시: 호우 경보" in rendered
    assert "부산" not in rendered
    assert "07/28 17:00 발효" in rendered


@pytest.mark.asyncio
async def test_kma_cache_collapses_concurrent_identical_requests(monkeypatch):
    weather_utils._KMA_RESPONSE_CACHE.clear()
    weather_utils._KMA_INFLIGHT.clear()
    calls = 0

    async def fake_fetch(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {"item": [{"category": "T1H", "obsrValue": "20"}]}

    monkeypatch.setattr(weather_utils, "_fetch_kma_api", fake_fetch)
    results = await asyncio.gather(
        weather_utils._fetch_kma_cached(
            object(),
            "getUltraSrtNcst",
            {},
            api_type="forecast",
            cache_key="same-request",
            ttl_seconds=60,
        ),
        weather_utils._fetch_kma_cached(
            object(),
            "getUltraSrtNcst",
            {},
            api_type="forecast",
            cache_key="same-request",
            ttl_seconds=60,
        ),
    )

    assert calls == 1
    assert results[0] == results[1]

"""지진 알림 규모 하한(국내 4.0 / 국외 7.0) 회귀 테스트."""

import asyncio

import pytest

import config
from utils import weather as weather_utils


def _run(coro):
    return asyncio.run(coro)


def _event(
    *,
    tm: str,
    mt: str,
    loc: str,
    lat: str | None = None,
    lon: str | None = None,
    rem: str = "",
) -> dict:
    item = {
        "tmFc": tm,
        "tmEqk": tm,
        "mt": mt,
        "loc": loc,
        "rem": rem,
    }
    if lat is not None:
        item["lat"] = lat
    if lon is not None:
        item["lon"] = lon
    return item


# --- 국내/국외 분류 ---------------------------------------------------------


@pytest.mark.parametrize(
    "lat, lon",
    [
        ("35.80", "129.19"),  # 경주
        ("37.57", "126.98"),  # 서울
        ("33.45", "126.57"),  # 제주
        ("37.24", "131.87"),  # 울릉도·독도 인근
        ("36.10", "124.20"),  # 서해 해역
    ],
)
def test_korean_coordinates_are_domestic(lat, lon):
    assert weather_utils.is_domestic_earthquake(
        _event(tm="20260817120000", mt="4.0", loc="지역", lat=lat, lon=lon)
    )


@pytest.mark.parametrize(
    "lat, lon",
    [
        ("32.79", "130.74"),  # 일본 구마모토
        ("23.70", "121.50"),  # 대만
        ("38.50", "142.00"),  # 일본 동북 해역
        ("30.00", "120.00"),  # 중국 내륙
        ("61.00", "-147.00"),  # 알래스카
    ],
)
def test_foreign_coordinates_are_overseas(lat, lon):
    assert not weather_utils.is_domestic_earthquake(
        _event(tm="20260817120000", mt="7.0", loc="지역", lat=lat, lon=lon)
    )


def test_location_name_classifies_when_coordinates_missing():
    domestic = _event(tm="20260817120000", mt="4.1", loc="경북 경주시 남남서쪽 8km 지역")
    overseas = _event(tm="20260817120000", mt="7.1", loc="일본 구마모토현 남쪽 20km 지역")
    assert weather_utils.is_domestic_earthquake(domestic)
    assert not weather_utils.is_domestic_earthquake(overseas)


def test_unclassifiable_event_falls_back_to_domestic():
    """분류 실패 시에는 하한이 낮은 국내로 간주해 알림을 놓치지 않는다."""
    assert weather_utils.is_domestic_earthquake(
        _event(tm="20260817120000", mt="4.5", loc="")
    )


def test_location_name_wins_over_coordinates():
    """기상청 위치명이 좌표보다 국내/국외 구분을 명확히 담고 있다."""
    item = _event(
        tm="20260817120000",
        mt="7.1",
        loc="일본 구마모토현",
        lat="35.80",  # 좌표만 보면 국내 범위
        lon="129.19",
    )
    assert not weather_utils.is_domestic_earthquake(item)


def test_overseas_token_beats_domestic_sea_token():
    """'일본 ... 해역'처럼 두 토큰이 겹칠 때 국외 판정이 우선한다."""
    assert not weather_utils.is_domestic_earthquake(
        _event(tm="20260817120000", mt="6.0", loc="일본 도쿄 남쪽 해역")
    )


def test_domestic_sea_area_is_domestic():
    assert weather_utils.is_domestic_earthquake(
        _event(tm="20260817120000", mt="4.2", loc="서해 백령도 남서쪽 30km 해역")
    )


def test_invalid_coordinates_without_location_hint_default_to_domestic():
    item = _event(
        tm="20260817120000",
        mt="4.5",
        loc="알 수 없음",
        lat="999",
        lon="999",
    )
    assert weather_utils.is_domestic_earthquake(item)


# --- 하한 적용 --------------------------------------------------------------


def test_thresholds_differ_by_region():
    domestic = _event(tm="20260817120000", mt="4.0", loc="경북 경주시", lat="35.80", lon="129.19")
    overseas = _event(tm="20260817120000", mt="7.0", loc="일본", lat="32.79", lon="130.74")
    assert weather_utils.earthquake_alert_threshold(domestic) == 4.0
    assert weather_utils.earthquake_alert_threshold(overseas) == 7.0


def test_domestic_magnitude_4_is_kept():
    items = [
        _event(tm="20260817120000", mt="4.0", loc="경북 경주시", lat="35.80", lon="129.19")
    ]
    assert weather_utils.filter_earthquakes_by_magnitude(items)


def test_domestic_below_4_is_dropped():
    items = [
        _event(tm="20260817120000", mt="3.9", loc="경북 경주시", lat="35.80", lon="129.19")
    ]
    assert weather_utils.filter_earthquakes_by_magnitude(items) == []


def test_overseas_below_7_is_dropped():
    """이전 규칙(4.0 단일 하한)이라면 알림이 갔을 국외 중규모 지진."""
    items = [
        _event(tm="20260817120000", mt="6.9", loc="일본 구마모토현", lat="32.79", lon="130.74"),
        _event(tm="20260817110000", mt="5.4", loc="대만 동부 해역", lat="23.70", lon="121.50"),
        _event(tm="20260817100000", mt="4.2", loc="일본 도쿄 남쪽 해역", lat="34.00", lon="139.50"),
    ]
    assert weather_utils.filter_earthquakes_by_magnitude(items) == []


def test_overseas_magnitude_7_is_kept():
    items = [
        _event(tm="20260817120000", mt="7.0", loc="일본 구마모토현", lat="32.79", lon="130.74")
    ]
    assert len(weather_utils.filter_earthquakes_by_magnitude(items)) == 1


def test_unparseable_magnitude_is_dropped():
    items = [
        _event(tm="20260817120000", mt="확인 중", loc="경북 경주시", lat="35.80", lon="129.19")
    ]
    assert weather_utils.filter_earthquakes_by_magnitude(items) == []


def test_mixed_batch_keeps_only_qualifying_events():
    items = [
        _event(tm="20260817120000", mt="4.3", loc="경북 경주시", lat="35.80", lon="129.19"),
        _event(tm="20260817110000", mt="6.1", loc="일본 구마모토현", lat="32.79", lon="130.74"),
        _event(tm="20260817100000", mt="7.4", loc="대만 동부 해역", lat="23.70", lon="121.50"),
        _event(tm="20260817090000", mt="2.8", loc="전남 신안군", lat="34.83", lon="126.10"),
    ]
    kept = {item["mt"] for item in weather_utils.filter_earthquakes_by_magnitude(items)}
    assert kept == {"4.3", "7.4"}


def test_overseas_aftershocks_ride_along_with_qualifying_mainshock():
    """기준 지진이 하한을 넘으면 같은 지진군의 후속 지진은 규모가 낮아도 남긴다.

    후속 지진은 새 알림이 아니라 기존 메시지 수정으로 표시되므로 알림 건수를
    늘리지 않는다.
    """
    items = [
        _event(tm="20260817120000", mt="7.3", loc="일본 구마모토현 남쪽 20km", lat="32.79", lon="130.74"),
        _event(tm="20260817121500", mt="5.1", loc="일본 구마모토현 남쪽 25km", lat="32.80", lon="130.76"),
        _event(tm="20260817123000", mt="4.4", loc="일본 구마모토현 남쪽 22km", lat="32.78", lon="130.75"),
    ]
    kept = {item["mt"] for item in weather_utils.filter_earthquakes_by_magnitude(items)}
    assert kept == {"7.3", "5.1", "4.4"}


def test_unrelated_overseas_cluster_is_not_rescued_by_distant_mainshock():
    items = [
        _event(tm="20260817120000", mt="7.3", loc="일본 구마모토현", lat="32.79", lon="130.74"),
        _event(tm="20260817121500", mt="5.1", loc="대만 동부 해역", lat="23.70", lon="121.50"),
    ]
    kept = {item["mt"] for item in weather_utils.filter_earthquakes_by_magnitude(items)}
    assert kept == {"7.3"}


def test_domestic_aftershocks_ride_along_with_qualifying_mainshock():
    items = [
        _event(tm="20260817120000", mt="4.6", loc="경북 경주시 남남서쪽 8km", lat="35.80", lon="129.19"),
        _event(tm="20260817121500", mt="2.9", loc="경북 경주시 남남서쪽 9km", lat="35.81", lon="129.20"),
    ]
    kept = {item["mt"] for item in weather_utils.filter_earthquakes_by_magnitude(items)}
    assert kept == {"4.6", "2.9"}


def test_domestic_influence_marker_is_still_excluded(monkeypatch):
    """'국내영향없음' 통보는 규모와 무관하게 제외된다."""
    captured: dict = {}

    async def fake_fetch(_db, _endpoint, _params, api_type=None):
        captured["api_type"] = api_type
        return {
            "item": [
                _event(
                    tm="20260817120000",
                    mt="8.1",
                    loc="칠레 중부",
                    lat="-33.00",
                    lon="-71.00",
                    rem="국내영향없음",
                ),
            ]
        }

    monkeypatch.setattr(weather_utils, "_fetch_kma_api", fake_fetch)
    result = _run(weather_utils.get_recent_earthquakes(None))
    assert result == []
    assert captured["api_type"] == "eqk"


def test_get_recent_earthquakes_applies_regional_thresholds(monkeypatch):
    async def fake_fetch(_db, _endpoint, _params, api_type=None):
        return {
            "item": [
                _event(tm="20260817120000", mt="6.5", loc="일본 구마모토현", lat="32.79", lon="130.74"),
                _event(tm="20260817110000", mt="4.1", loc="경북 경주시", lat="35.80", lon="129.19"),
            ]
        }

    monkeypatch.setattr(weather_utils, "_fetch_kma_api", fake_fetch)
    result = _run(weather_utils.get_recent_earthquakes(None))
    assert [item["mt"] for item in result] == ["4.1"]


def test_thresholds_are_configurable(monkeypatch):
    monkeypatch.setattr(config, "EARTHQUAKE_MIN_MAGNITUDE_OVERSEAS", 5.0, raising=False)
    items = [
        _event(tm="20260817120000", mt="5.2", loc="일본 구마모토현", lat="32.79", lon="130.74")
    ]
    assert len(weather_utils.filter_earthquakes_by_magnitude(items)) == 1

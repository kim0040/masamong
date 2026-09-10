import pytest

from utils.weather_forecast import build_agent_forecast


class _Bot:
    def __init__(self, db):
        self.db = db


class _WeatherCog:
    def __init__(self, db):
        self.bot = _Bot(db)

    async def get_mid_term_weather(self, day_offset, location_name):
        return f"mid:{day_offset}:{location_name}"


@pytest.mark.asyncio
async def test_agent_forecast_uses_weather_cog_not_a_second_kma_stack(monkeypatch):
    calls = []

    async def fake_coords(_db, location_name):
        calls.append(("coords", location_name))
        return {"nx": 60, "ny": 120}

    async def fake_current(_db, nx, ny):
        calls.append(("current", nx, ny))
        return {"sky": "맑음"}

    async def fake_short(_db, nx, ny):
        calls.append(("short", nx, ny))
        return {"item": [{"fcst": "맑음"}]}

    monkeypatch.setattr(
        "utils.weather_forecast.coords_utils.get_coords_from_db",
        fake_coords,
    )
    monkeypatch.setattr(
        "utils.weather_forecast.weather_utils.get_current_weather_from_kma",
        fake_current,
    )
    monkeypatch.setattr(
        "utils.weather_forecast.weather_utils.get_short_term_forecast_from_kma",
        fake_short,
    )
    monkeypatch.setattr(
        "utils.weather_forecast.weather_utils.format_current_weather",
        lambda _data: "맑음 20도",
    )

    result = await build_agent_forecast(_WeatherCog(object()), "전주", 0)

    assert result["location"] == "전주"
    assert result["current_weather"] == "맑음 20도"
    assert result["forecast_items"] == [{"fcst": "맑음"}]
    assert ("coords", "전주") in calls
    assert ("current", "60", "120") in calls

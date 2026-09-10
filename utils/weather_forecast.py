# -*- coding: utf-8 -*-
"""에이전트 날씨 도구 결과. 슬래시/명령 경로와 KMA 호출을 한곳에서 모읍니다."""

from __future__ import annotations

import asyncio
from typing import Any

import config
from utils import coords as coords_utils
from utils import weather as weather_utils


async def build_agent_forecast(
    weather_cog,
    location: str | None = None,
    day_offset: int = 0,
) -> str | dict[str, Any]:
    """ToolsCog가 쓰던 구조화 예보를 WeatherCog 경유로 만듭니다."""
    location_name = location or config.DEFAULT_LOCATION_NAME
    db = weather_cog.bot.db

    if day_offset >= 3:
        coords = await coords_utils.get_coords_from_db(db, location_name)
        nx, ny = config.DEFAULT_NX, config.DEFAULT_NY
        if coords:
            nx, ny = str(coords["nx"]), str(coords["ny"])

        short_term_data, mid_term_data = await asyncio.gather(
            weather_utils.get_short_term_forecast_from_kma(db, nx, ny),
            weather_cog.get_mid_term_weather(day_offset, location_name),
        )
        short_term_summary = ""
        if short_term_data and not short_term_data.get("error"):
            tomorrow_summary = weather_utils.format_short_term_forecast(
                short_term_data, "내일", 1
            )
            dayafter_summary = weather_utils.format_short_term_forecast(
                short_term_data, "모레", 2
            )
            short_term_summary = f"{tomorrow_summary}\n{dayafter_summary}"

        return (
            "--- [단기 예보 (내일/모레)] ---\n"
            f"{short_term_summary}\n\n"
            "--- [중기 예보 (3일 후 ~ 10일 후)] ---\n"
            f"{mid_term_data}"
        )

    coords = await coords_utils.get_coords_from_db(db, location_name)
    if not coords:
        return f"'{location_name}' 지역의 날씨 정보는 아직 알 수 없습니다."

    nx, ny = str(coords["nx"]), str(coords["ny"])
    current_data, forecast_data = await asyncio.gather(
        weather_utils.get_current_weather_from_kma(db, nx, ny),
        weather_utils.get_short_term_forecast_from_kma(db, nx, ny),
    )
    current_str = (
        weather_utils.format_current_weather(current_data)
        if current_data
        else "정보 없음"
    )
    items_list = []
    if forecast_data and "item" in forecast_data:
        items_list = forecast_data["item"]

    return {
        "location": location_name,
        "current_weather": current_str,
        "forecast_items": items_list,
        "summary": f"{location_name} 현재: {current_str}",
    }

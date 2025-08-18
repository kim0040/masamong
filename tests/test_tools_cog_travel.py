import pytest
from unittest.mock import AsyncMock, MagicMock
from cogs.tools_cog import ToolsCog

# Mock bot and other dependencies needed by ToolsCog
@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.get_cog.return_value = MagicMock()
    return bot

@pytest.mark.asyncio
async def test_travel_recommendation_korea(mock_bot, mocker):
    """
    Test the intelligent router for a location within South Korea.
    It should use the KMA weather service via the coordinate translator.
    """
    tools_cog = ToolsCog(mock_bot)

    # Mock the individual tools that the router calls
    mocker.patch.object(tools_cog, 'geocode', new_callable=AsyncMock, return_value={
        "status": "found", "lat": 37.56, "lon": 126.97, "country_code": "kr", "display_name": "Seoul, South Korea"
    })
    # Mock the internal weather cog function that gets called for KR locations
    mocker.patch.object(tools_cog.weather_cog, 'get_formatted_weather_string', new_callable=AsyncMock, return_value=("맑음", None))
    mocker.patch.object(tools_cog, 'find_points_of_interest', new_callable=AsyncMock, return_value={"places": [{"name": "Gyeongbok Palace"}]})
    mocker.patch.object(tools_cog, 'find_events', new_callable=AsyncMock, return_value={"events": [{"name": "Seoul Jazz Festival"}]})

    # Mock the coordinate translator
    mocker.patch('utils.coords.latlon_to_kma_grid', return_value=(60, 127))

    result = await tools_cog.get_travel_recommendation("서울")

    # Assertions
    assert "error" not in result
    assert result["location_info"]["country_code"] == "kr"
    assert result["weather"] == "맑음"
    assert len(result["points_of_interest"]) == 1
    assert result["points_of_interest"][0]["name"] == "Gyeongbok Palace"
    assert len(result["events"]) == 1
    assert result["events"][0]["name"] == "Seoul Jazz Festival"

    # Verify that the correct functions were called
    tools_cog.geocode.assert_called_once_with("서울")
    tools_cog.weather_cog.get_formatted_weather_string.assert_called_once()
    tools_cog.find_points_of_interest.assert_called_once()
    tools_cog.find_events.assert_called_once()


@pytest.mark.asyncio
async def test_travel_recommendation_foreign(mock_bot, mocker):
    """
    Test the intelligent router for a location outside of South Korea.
    It should use the OpenWeatherMap (foreign) weather service.
    """
    tools_cog = ToolsCog(mock_bot)

    mocker.patch.object(tools_cog, 'geocode', new_callable=AsyncMock, return_value={
        "status": "found", "lat": 48.85, "lon": 2.29, "country_code": "fr", "display_name": "Paris, France"
    })
    mocker.patch.object(tools_cog, 'get_foreign_weather', new_callable=AsyncMock, return_value={"description": "clear sky"})
    mocker.patch.object(tools_cog, 'find_points_of_interest', new_callable=AsyncMock, return_value={"places": [{"name": "Eiffel Tower"}]})
    mocker.patch.object(tools_cog, 'find_events', new_callable=AsyncMock, return_value={"events": []}) # No events found

    result = await tools_cog.get_travel_recommendation("Paris")

    # Assertions
    assert "error" not in result
    assert result["location_info"]["country_code"] == "fr"
    assert result["weather"]["description"] == "clear sky"
    assert len(result["points_of_interest"]) == 1
    assert result["points_of_interest"][0]["name"] == "Eiffel Tower"
    assert len(result["events"]) == 0

    # Verify that the correct functions were called
    tools_cog.geocode.assert_called_once_with("Paris")
    tools_cog.get_foreign_weather.assert_called_once()
    tools_cog.find_points_of_interest.assert_called_once()
    tools_cog.find_events.assert_called_once()

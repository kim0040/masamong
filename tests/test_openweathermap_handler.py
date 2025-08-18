import pytest
from unittest.mock import MagicMock
from utils.api_handlers import openweathermap

@pytest.mark.asyncio
async def test_get_weather_by_coords_success(mocker):
    """Test successful weather data retrieval."""
    mocker.patch('config.OPENWEATHERMAP_API_KEY', 'DUMMY_API_KEY')
    mock_response_data = {
        "weather": [{"description": "clear sky"}],
        "main": {"temp": 25, "feels_like": 26, "humidity": 50},
        "wind": {"speed": 5},
        "sys": {"sunrise": 1622582400, "sunset": 1622634000}
    }
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.json.return_value = mock_response_data
    mock_session.get.return_value = mock_response
    mocker.patch('utils.http.get_modern_tls_session', return_value=mock_session)

    result = await openweathermap.get_weather_by_coords(35.68, 139.69)

    assert "error" not in result
    assert result["description"] == "clear sky"
    assert result["temp"] == 25

import requests

@pytest.mark.asyncio
async def test_get_weather_by_coords_api_error(mocker):
    """Test API error during weather data retrieval."""
    mocker.patch('config.OPENWEATHERMAP_API_KEY', 'DUMMY_API_KEY')
    mock_session = MagicMock()
    mock_session.get.side_effect = requests.exceptions.RequestException("Network Error")
    mocker.patch('utils.http.get_modern_tls_session', return_value=mock_session)

    result = await openweathermap.get_weather_by_coords(35.68, 139.69)

    assert "error" in result
    assert "네트워크 오류" in result["error"]

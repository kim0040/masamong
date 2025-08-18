import pytest
from unittest.mock import MagicMock
from utils.api_handlers import foursquare

@pytest.mark.asyncio
async def test_get_places_by_coords_success(mocker):
    """Test successful place data retrieval."""
    mocker.patch('config.FOURSQUARE_API_KEY', 'DUMMY_API_KEY')
    mock_response_data = {
        "results": [
            {"name": "Tokyo Tower", "location": {"formatted_address": "Tokyo, Japan"}, "categories": [{"name": "Landmark"}]}
        ]
    }
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.json.return_value = mock_response_data
    mock_session.get.return_value = mock_response
    mocker.patch('utils.http.get_modern_tls_session', return_value=mock_session)

    result = await foursquare.get_places_by_coords(35.68, 139.69)

    assert "error" not in result
    assert len(result["places"]) == 1
    assert result["places"][0]["name"] == "Tokyo Tower"

import requests

@pytest.mark.asyncio
async def test_get_places_by_coords_api_error(mocker):
    """Test API error during place data retrieval."""
    mocker.patch('config.FOURSQUARE_API_KEY', 'DUMMY_API_KEY')
    mock_session = MagicMock()
    mock_session.get.side_effect = requests.exceptions.RequestException("Network Error")
    mocker.patch('utils.http.get_modern_tls_session', return_value=mock_session)

    result = await foursquare.get_places_by_coords(35.68, 139.69)

    assert "error" in result
    assert "네트워크 오류" in result["error"]

import pytest
from unittest.mock import MagicMock
from utils.api_handlers import ticketmaster

@pytest.mark.asyncio
async def test_get_events_by_coords_success(mocker):
    """Test successful event data retrieval."""
    mocker.patch('config.TICKETMASTER_API_KEY', 'DUMMY_API_KEY')
    mock_response_data = {
        "_embedded": {
            "events": [
                {"name": "Rock Festival", "url": "http://example.com/rock", "dates": {"start": {"localDate": "2025-09-01"}}}
            ]
        }
    }
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.json.return_value = mock_response_data
    mock_session.get.return_value = mock_response
    mocker.patch('utils.http.get_modern_tls_session', return_value=mock_session)

    result = await ticketmaster.get_events_by_coords(35.68, 139.69)

    assert "error" not in result
    assert len(result["events"]) == 1
    assert result["events"][0]["name"] == "Rock Festival"

import requests

@pytest.mark.asyncio
async def test_get_events_by_coords_api_error(mocker):
    """Test API error during event data retrieval."""
    mocker.patch('config.TICKETMASTER_API_KEY', 'DUMMY_API_KEY')
    mock_session = MagicMock()
    mock_session.get.side_effect = requests.exceptions.RequestException("Network Error")
    mocker.patch('utils.http.get_modern_tls_session', return_value=mock_session)

    result = await ticketmaster.get_events_by_coords(35.68, 139.69)

    assert "error" in result
    assert "네트워크 오류" in result["error"]

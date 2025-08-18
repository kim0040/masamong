import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

from utils.api_handlers import nominatim
import config

# Reset the cache before each test to ensure isolation
@pytest.fixture(autouse=True)
def clear_nominatim_cache():
    nominatim._geocode_cache.clear()

@pytest.mark.asyncio
async def test_geocode_success_single_result(mocker):
    """
    Test successful geocoding with a single, clear result.
    """
    query = "Eiffel Tower"
    mock_response_data = [{
        "place_id": 1, "lat": "48.8583701", "lon": "2.2944813", "display_name": "Eiffel Tower, Paris, France",
        "address": {"country_code": "fr"}
    }]

    # Mock the session and its get method
    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.json.return_value = mock_response_data
    mock_session.get.return_value = mock_response
    mocker.patch('utils.http.get_modern_tls_session', return_value=mock_session)

    result = await nominatim.geocode_location(query)

    assert result['status'] == 'found'
    assert result['lat'] == 48.8583701
    assert result['lon'] == 2.2944813
    assert result['country_code'] == 'fr'
    assert "Eiffel Tower" in result['display_name']

@pytest.mark.asyncio
async def test_geocode_disambiguation_multiple_results(mocker):
    """
    Test geocoding for an ambiguous query that returns multiple results.
    """
    query = "Springfield"
    mock_response_data = [
        {"display_name": "Springfield, Illinois, USA"},
        {"display_name": "Springfield, Massachusetts, USA"}
    ]

    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.json.return_value = mock_response_data
    mock_session.get.return_value = mock_response
    mocker.patch('utils.http.get_modern_tls_session', return_value=mock_session)

    result = await nominatim.geocode_location(query)

    assert result['status'] == 'disambiguation'
    assert "어떤 'Springfield'를 말씀하시는 건가요?" in result['message']
    assert len(result['options']) == 2

@pytest.mark.asyncio
async def test_geocode_not_found(mocker):
    """
    Test geocoding for a location that cannot be found.
    """
    query = "a_made_up_place_12345"
    mock_response_data = []

    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.json.return_value = mock_response_data
    mock_session.get.return_value = mock_response
    mocker.patch('utils.http.get_modern_tls_session', return_value=mock_session)

    result = await nominatim.geocode_location(query)

    assert "error" in result
    assert "찾을 수 없습니다" in result['error']

@pytest.mark.asyncio
async def test_caching_logic(mocker):
    """
    Test that a successful API call caches the result and subsequent calls use the cache.
    """
    query = "Tokyo"
    mock_response_data = [{
        "place_id": 2, "lat": "35.6895", "lon": "139.6917", "display_name": "Tokyo, Japan",
        "address": {"country_code": "jp"}
    }]

    mock_session = MagicMock()
    mock_response = MagicMock()
    mock_response.json.return_value = mock_response_data
    mock_session.get.return_value = mock_response
    mocker.patch('utils.http.get_modern_tls_session', return_value=mock_session)

    # First call - should trigger API call
    result1 = await nominatim.geocode_location(query)
    assert result1['status'] == 'found'
    assert result1['country_code'] == 'jp'

    # Verify the API was called once
    mock_session.get.assert_called_once()

    # Second call - should use cache
    result2 = await nominatim.geocode_location(query)
    assert result2 == result1 # Should be the exact same result object from cache

    # Verify the API was NOT called a second time
    mock_session.get.assert_called_once()

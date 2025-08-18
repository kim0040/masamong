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


@pytest.mark.asyncio
async def test_ai_handler_uses_strict_prompt_for_travel(mocker):
    """
    Verify that the AIHandler uses the specialized travel prompt
    when the 'get_travel_recommendation' tool is called.
    """
    # --- Setup ---
    # Mock AIHandler and its dependencies
    from cogs.ai_handler import AIHandler
    mock_bot = MagicMock()
    mock_bot.db = MagicMock()
    mock_tools_cog = MagicMock()
    mock_bot.get_cog.return_value = mock_tools_cog

    ai_handler = AIHandler(mock_bot)
    ai_handler.gemini_configured = True # Set the underlying property instead of the read-only one

    # Mock internal helper functions
    mocker.patch.object(ai_handler, '_get_rag_context', new_callable=AsyncMock, return_value=("", []))
    mocker.patch.object(ai_handler, '_get_recent_history', new_callable=AsyncMock, return_value=[])

    # Mock the tool execution result
    travel_data = {"location_info": {"display_name": "Testville"}, "weather": "Sunny", "points_of_interest": [], "events": []}
    mocker.patch.object(ai_handler, '_execute_tool', new_callable=AsyncMock, return_value=travel_data)

    # --- Mock the two-step LLM calls ---
    # 1. Lite model returns a tool call
    lite_response_mock = MagicMock()
    lite_response_mock.text = '<tool_call>{"tool_to_use": "get_travel_recommendation", "parameters": {"location_name": "Testville"}}</tool_call>'

    # 2. Main model returns a final answer
    main_response_mock = MagicMock()
    main_response_mock.text = "Here is your travel summary."

    # Mock the underlying Gemini API call
    safe_generate_mock = mocker.patch.object(ai_handler, '_safe_generate_content', new_callable=AsyncMock, side_effect=[
        lite_response_mock,
        main_response_mock
    ])

    # --- Execution ---
    mock_message = MagicMock()
    mock_message.content = "Tell me about Testville"
    mock_message.guild.id = 123
    mock_message.channel.id = 456
    mock_message.author.id = 789
    # Configure the 'typing' context manager
    mock_message.channel.typing = MagicMock()
    mock_message.channel.typing.return_value.__aenter__ = AsyncMock(return_value=None)
    mock_message.channel.typing.return_value.__aexit__ = AsyncMock(return_value=None)
    mock_message.reply = AsyncMock()

    await ai_handler.process_agent_message(mock_message)

    # --- Assertions ---
    # Check that the final reply was called
    mock_message.reply.assert_called_once_with("Here is your travel summary.", mention_author=False)

    # Crucially, check the prompt sent to the MAIN model (the second call)
    assert safe_generate_mock.call_count == 2
    main_model_call_args = safe_generate_mock.call_args_list[1]

    # The prompt is the second positional argument (index 1) in the call to _safe_generate_content
    main_prompt_arg = main_model_call_args.args[1]

    # Verify it's using the specialized prompt
    assert "너는 오직 아래 [제공된 정보]만을 사용하여" in main_prompt_arg
    assert "Testville" in main_prompt_arg # Check that the tool data was included
    assert "AGENT_SYSTEM_PROMPT" not in main_prompt_arg # Ensure default prompt was NOT used

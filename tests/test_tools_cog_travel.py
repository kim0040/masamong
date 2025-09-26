import pytest
from unittest.mock import AsyncMock, MagicMock
from cogs.tools_cog import ToolsCog
import google.generativeai as genai

# Mock bot and other dependencies needed by ToolsCog
@pytest.fixture
def mock_bot():
    bot = MagicMock()
    # Mock the weather_cog dependency
    weather_cog_mock = MagicMock()
    weather_cog_mock.get_formatted_weather_string = AsyncMock()
    
    # Set up the bot to return the mocked cog
    def get_cog_side_effect(name):
        if name == 'WeatherCog':
            return weather_cog_mock
        return MagicMock()
        
    bot.get_cog.side_effect = get_cog_side_effect
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
    tools_cog.weather_cog.get_formatted_weather_string.return_value = ("맑음", None)

    # Mock the coordinate translator
    mocker.patch('utils.coords.latlon_to_kma_grid', return_value=(60, 127))

    result = await tools_cog.get_travel_recommendation("서울")

    # Assertions
    assert "error" not in result
    assert result["location_info"]["country_code"] == "kr"
    assert result["weather"]["current_weather"] == "맑음"

    # Verify that the correct functions were called
    tools_cog.geocode.assert_called_once_with("서울")
    tools_cog.weather_cog.get_formatted_weather_string.assert_called_once()


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

    result = await tools_cog.get_travel_recommendation("Paris")

    # Assertions
    assert "error" not in result
    assert result["location_info"]["country_code"] == "fr"
    assert result["weather"]["description"] == "clear sky"

    # Verify that the correct functions were called
    tools_cog.geocode.assert_called_once_with("Paris")
    tools_cog.get_foreign_weather.assert_called_once()


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
    # Use AsyncMock for the database to handle await calls
    mock_bot.db = AsyncMock()
    # Set up the execute method on the mock db
    mock_bot.db.execute = AsyncMock()
    mock_tools_cog = MagicMock()
    mock_bot.get_cog.return_value = mock_tools_cog

    ai_handler = AIHandler(mock_bot)
    ai_handler.gemini_configured = True

    # Mock internal helper functions
    mocker.patch('utils.db.check_api_rate_limit', new_callable=AsyncMock, return_value=False)
    mocker.patch('utils.db.get_guild_setting', new_callable=AsyncMock, return_value=None) # Mock guild settings to avoid another db call
    mocker.patch.object(ai_handler, '_get_rag_context', new_callable=AsyncMock, return_value=("", []))
    mocker.patch.object(ai_handler, '_get_recent_history', new_callable=AsyncMock, return_value=[])

    # Mock the tool execution result
    travel_data = {"location_info": {"display_name": "Testville"}, "weather": "Sunny", "points_of_interest": [], "events": []}
    mocker.patch.object(ai_handler, '_execute_tool', new_callable=AsyncMock, return_value=travel_data)

    # --- Mock the two-step LLM calls ---
    # Mock the GenerativeModel class itself
    mock_generative_model_class = mocker.patch('google.generativeai.GenerativeModel')
    
    # Create mock instances for the two models (lite and main)
    mock_lite_model_instance = AsyncMock()
    mock_main_model_instance = AsyncMock()
    
    # The class will return the lite model first, then the main model
    mock_generative_model_class.side_effect = [mock_lite_model_instance, mock_main_model_instance]

    # 1. Lite model returns a tool call
    lite_response_mock = MagicMock()
    lite_response_mock.text = '<tool_call>{"tool_to_use": "get_travel_recommendation", "parameters": {"location_name": "Testville"}}</tool_call>'
    mock_lite_model_instance.generate_content_async.return_value = lite_response_mock

    # 2. Main model returns a final answer
    main_response_mock = MagicMock()
    main_response_mock.text = "Here is your travel summary."
    mock_main_model_instance.generate_content_async.return_value = main_response_mock

    # --- Execution ---
    mock_message = MagicMock()
    mock_message.content = "Tell me about Testville"
    mock_message.guild.id = 123
    mock_message.channel.id = 456
    mock_message.author.id = 789
    mock_message.channel.typing.return_value.__aenter__ = AsyncMock(return_value=None)
    mock_message.channel.typing.return_value.__aexit__ = AsyncMock(return_value=None)
    mock_message.reply = AsyncMock()

    await ai_handler.process_agent_message(mock_message)

    # --- Assertions ---
    # Check that the final reply was called
    mock_message.reply.assert_called_once_with("Here is your travel summary.", mention_author=False)

    # Crucially, check the system prompt sent to the MAIN model
    assert mock_generative_model_class.call_count == 2
    main_model_init_kwargs = mock_generative_model_class.call_args_list[1].kwargs
    main_system_prompt = main_model_init_kwargs.get("system_instruction", "")
    
    # Verify it's using the specialized prompt
    assert "너는 오직 아래 [제공된 정보]만을 사용하여" in main_system_prompt
    assert "Testville" in main_system_prompt # Check that the tool data was included
    
    # Verify the user prompt for the main model was empty
    main_model_call_args = mock_main_model_instance.generate_content_async.call_args
    main_user_prompt = main_model_call_args.args[0]
    assert main_user_prompt == ""

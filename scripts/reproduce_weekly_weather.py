
import sys
import os
import asyncio

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from utils import weather
import config

async def test_command_logic(query):
    print(f"Testing logic for query: '{query}'")
    
    # Updated logic in WeatherCog.weather_command
    loc = "서울"
    
    # [NEW] Weekly determination
    if "이번주" in query or "주간" in query:
        print(f" -> [Logic] 'This Week' detected! Logic will call 'get_mid_term_weather(3, {loc})'")
        return

    day_offset = 1 if "내일" in query else 2 if "모레" in query else 0
    print(f" -> Computed day_offset: {day_offset}")

    # If day_offset is 0, it calls get_current_weather...
    if day_offset == 0:
        print(" -> Action: Will fetch TODAY's weather (Short-term).")
    else:
        print(f" -> Action: Will fetch +{day_offset} day weather.")

if __name__ == "__main__":
    asyncio.run(test_command_logic("이번주 날씨"))
    print("\n--- Test 2: Normal Weather ---")
    asyncio.run(test_command_logic("오늘 날씨"))

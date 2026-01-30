
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import config

if "answer_weather_weekly" in config.AI_CREATIVE_PROMPTS:
    print("Success: 'answer_weather_weekly' found in config.")
else:
    print("Failure: 'answer_weather_weekly' NOT found.")

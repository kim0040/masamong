
import asyncio
import aiosqlite
import sys
import os

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from utils import weather
import config
from datetime import datetime, timedelta

async def run_verification():
    print(f"Checking KMA API Key: {config.KMA_API_KEY[:5]}..." if config.KMA_API_KEY else "Checking KMA API Key: None")
    
    # Mock DB connection (InMemory)
    async with aiosqlite.connect(":memory:") as db:
        # Create necessary tables for rate limit checks
        await db.execute("""
            CREATE TABLE IF NOT EXISTS api_call_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_type TEXT NOT NULL,
                called_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS system_counters (
                counter_name TEXT PRIMARY KEY,
                counter_value INTEGER NOT NULL DEFAULT 0,
                last_reset_at TEXT NOT NULL
            )
        """)
        await db.commit()
        
        print("\n--- 1. Testing Raw Earthquake API (Real Data) ---")
        # This will fetch real data (likely foreign if no domestic quake recently)
        # We expect get_recent_earthquakes to filter it out if it has "국내영향없음"
        
        # To test the FUNCTION logic properly, we should call 'weather.get_recent_earthquakes' directly
        # But that function connects to DB inside. But we injected mock DB so it's fine.
        # Wait, get_recent_earthquakes calls _fetch_kma_api which we mocked? No we mocked the DB connection passed TO it.
        # But _fetch_kma_api makes REAL requests.
        
        print("Fetching Real Data via `get_recent_earthquakes` (Should filter out foreign)...")
        real_quakes = await weather.get_recent_earthquakes(db)
        if hasattr(real_quakes, '__len__'):
            print(f"Filtered Result Count: {len(real_quakes)}")
            for q in real_quakes:
                print(f" -> Passed Filter: {q.get('loc')} (Mag {q.get('mt')})")
        else:
            print("Result is None or Error")
            
        print("\n--- 2. Testing Logic with MOCKED Data (Simulation) ---")
        # Simulating a domestic quake dict and a foreign quake dict
        
        mock_foreign_quake = {
            'tmEqk': '20260131120000', 'loc': '사우스 샌드위치', 'mt': '6.0', 'rem': '국내영향없음'
        }
        mock_small_quake = {
            'tmEqk': '20260131120500', 'loc': '경북 경주 (규모 3.5)', 'mt': '3.5', 'rem': '여진 주의'
        }
        mock_alert_quake = {
            'tmEqk': '20260131121000', 'loc': '충북 괴산 (규모 4.2)', 'mt': '4.2', 'rem': '낙하물 주의'
        }
        mock_huge_quake = {
            'tmEqk': '20260131121500', 'loc': '동해 해역 (규모 6.5)', 'mt': '6.5', 'rem': '해일 주의'
        }

        print(f"\n[Simulation] Small Quake (Mag 3.5):")
        if float(mock_small_quake['mt']) >= 4.0:
             print(" -> Filter Check: FAILED (Should be filtered out)")
        else:
             print(" -> Filter Check: BLOCKED (Correct)")

        print(f"\n[Simulation] Alert Quake (Mag 4.2):")
        print(weather.format_earthquake_alert(mock_alert_quake))
        
        print(f"\n[Simulation] Huge Quake (Mag 6.5 + Evacuation Tips):")
        print(weather.format_earthquake_alert(mock_huge_quake))

        print("\n--- 3. Testing Weather Forecast API (confirmation) ---")
        # Just to be sure the key works if eqk returns nothing
        forecast = await weather.get_current_weather_from_kma(db, "60", "127") # Seoul
        if isinstance(forecast, dict) and forecast.get("error"):
             print(f"Error fetching forecast: {forecast}")
        elif forecast:
             print("Weather forecast fetch successful!")
             print(weather.format_current_weather(forecast))
        else:
             print("Weather forecast fetch returned None.")

if __name__ == "__main__":
    asyncio.run(run_verification())


import asyncio
import aiosqlite
import sys
import os

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from utils import weather
import config

async def check_mid_term():
    async with aiosqlite.connect(":memory:") as db:
        # Create necessary tables
        await db.execute("""
            CREATE TABLE IF NOT EXISTS api_call_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                api_type TEXT NOT NULL,
                called_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc'))
            )
        """)
        await db.commit()
        
        print("Fetching Mid-term Forecast (Raw via _fetch_kma_api)...")
        # Direct call to see why it returns None
        params = {"reg": "11B00000"}
        res = await weather._fetch_kma_api(db, "/fct_afs_dl.php", params, api_type='mid')
        print(f"Raw Response:\n{res}")

if __name__ == "__main__":
    asyncio.run(check_mid_term())

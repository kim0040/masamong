
import sys
import os
import asyncio
import logging

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from dotenv import load_dotenv
load_dotenv()

from utils.api_handlers import finnhub
from logger_config import logger

# Set logger to info to see finnhub internal logs
logger.setLevel(logging.INFO)

async def test_finnhub_search():
    queries = ["스타벅스", "유니티"] # Test cases that previously failed or are new
    
    print("🔎 Testing Finnhub Symbol Search (with Web Fallback)...\n")

    for q in queries:
        print(f"--- Query: '{q}' ---")
        
        result = await finnhub.get_raw_stock_quote(q)
        if result:
            print(f"✅ Found: {result['symbol']} (Price: {result['price']})")
        else:
            print(f"❌ Failed to find '{q}'")
        print("")

if __name__ == "__main__":
    asyncio.run(test_finnhub_search())

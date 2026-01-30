
import sys
import os
import asyncio
import logging

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from dotenv import load_dotenv
load_dotenv()

from cogs.tools_cog import ToolsCog
from discord.ext import commands
import discord

# Mock Bot
class MockBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())
    
    def get_cog(self, name):
        return None # WeatherCog not needed for stock test

async def test_cross_market():
    print("🔎 Testing Cross-Market Fallback (KRX -> Finnhub)...\n")
    
    bot = MockBot()
    tools = ToolsCog(bot)
    
    # Test Case: "코인베이스" (Korean name for US stock)
    # Expected: KRX fails -> Fallback to Finnhub -> Web Search finds COIN -> Success
    query = "코인베이스"
    print(f"--- Query: '{query}' ---")
    
    result = await tools.get_stock_price(query)
    print(f"Result:\n{result}")
    
    if "COIN" in result or "Coinbase" in result or "코인베이스" in result:
        print("\n✅ Success: Found Coinbase info via fallback!")
    else:
        print("\n❌ Failed: Did not find Coinbase info.")

if __name__ == "__main__":
    asyncio.run(test_cross_market())

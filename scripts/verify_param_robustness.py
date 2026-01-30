
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
        return None 

async def test_robustness():
    print("🔎 Testing Tool Parameter Robustness...\n")
    
    bot = MockBot()
    tools = ToolsCog(bot)
    
    # Test 1: Call with 'stock_name' (Old LLM behavior)
    print("--- Test 1: Calling with stock_name='MCD' ---")
    try:
        # We expect this to work (or at least NOT raise TypeError)
        # It might fail due to missing API keys or other logic, but TypeError is what we fixed.
        # MCD -> Finnhub call.
        result = await tools.get_stock_price(stock_name="MCD")
        print(f"✅ Result: {result[:50]}...") # Show start of result
    except TypeError as e:
        print(f"❌ TypeError Failed: {e}")
    except Exception as e:
        print(f"⚠️ Other Error (Expected): {e}")

    # Test 2: Call with 'symbol' (New Correct behavior)
    print("\n--- Test 2: Calling with symbol='MCD' ---")
    try:
        result = await tools.get_stock_price(symbol="MCD")
        print(f"✅ Result: {result[:50]}...")
    except TypeError as e:
        print(f"❌ TypeError Failed: {e}")

if __name__ == "__main__":
    asyncio.run(test_robustness())

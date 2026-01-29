import asyncio
import os
import sys
import logging
import time
import requests
from datetime import datetime, timedelta
import pytz

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
import config

load_dotenv()

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

async def run_diagnostics():
    print("🚀 Starting Deep Weather API Diagnostics...")
    
    api_key = config.KMA_API_KEY
    base_url = config.KMA_BASE_URL
    timeout = getattr(config, 'KMA_API_TIMEOUT', 30)
    
    print(f"   Config: Timeout={timeout}s, BaseURL={base_url}")
    
    # Test 1: Simple Auth/Connectivity Check (Typ01 URL base check)
    print("\n1. Testing Basic Connectivity (Typ01 Base)...")
    try:
        url = "https://apihub.kma.go.kr/api/typ01/url/typ_lst.php"
        params = {"authKey": api_key, "disp": "0", "help": "0"}
        start = time.time()
        res = requests.get(url, params=params, timeout=10)
        end = time.time()
        print(f"   Status: {res.status_code}, Time: {end-start:.2f}s")
        if res.status_code == 200:
            print("   ✅ Basic Connectivity OK")
        else:
            print(f"   ❌ Connectivity Failed: {res.text[:100]}")
    except Exception as e:
        print(f"   ❌ Exception: {e}")

    # Test 2: UltraShort Forecast (Real Scenario)
    print("\n2. Testing UltraShort Forecast (Heavy Load)...")
    try:
        now = datetime.now(pytz.timezone('Asia/Seoul'))
        base_date = now.strftime("%Y%m%d")
        base_time = (now - timedelta(hours=1)).strftime("%H00")
        
        # Construct exact URL used in bot
        typ02_base = "https://apihub.kma.go.kr/api/typ02/openApi/VilageFcstInfoService_2.0"
        endpoint = "getUltraSrtNcst"
        full_url = f"{typ02_base}/{endpoint}"
        
        params = {
            "authKey": api_key,
            "base_date": base_date,
            "base_time": base_time,
            "nx": "60",
            "ny": "127",
            "pageNo": "1", 
            "numOfRows": "1000", 
            "dataType": "JSON"
        }
        
        print(f"   URL: {full_url}")
        # print(f"   Params: {params}") # Don't print key in logs usually, but for local diag ok
        
        start = time.time()
        # Use session to mimic internal logic
        res = requests.get(full_url, params=params, timeout=timeout)
        end = time.time()
        
        print(f"   Status: {res.status_code}, Time: {end-start:.2f}s")
        if res.status_code == 200:
             print(f"   Response Preview: {res.text[:200]}")
             if "item" in res.text:
                 print("   ✅ Data Found (Flat Format Confirmed)")
             elif "response" in res.text:
                 print("   ✅ Data Found (Standard Format Confirmed)")
             else:
                 print("   ⚠️ 200 OK but unusual body.")
        else:
             print(f"   ❌ Failed: {res.text[:200]}")

    except Exception as e:
        print(f"   ❌ Exception: {e}")

if __name__ == "__main__":
    asyncio.run(run_diagnostics())

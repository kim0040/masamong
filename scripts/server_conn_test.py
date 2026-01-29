import requests
import time
import sys
import os
import logging
from dotenv import load_dotenv

# Add project root
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from utils import http

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ConnTest")

URL = "https://apihub.kma.go.kr/api/typ01/url/typ_lst.php" # Lightweight endpoint
PARAMS = {"authKey": config.KMA_API_KEY, "disp": "0", "help": "0"}

def test_session(name, session_factory):
    print(f"\n🧪 Testing {name}...")
    try:
        if session_factory:
            session = session_factory()
        else:
            session = requests.Session()
            
        start = time.time()
        # Set a short timeout for the test to fail fast if it hangs
        res = session.get(URL, params=PARAMS, timeout=5) 
        end = time.time()
        
        print(f"   ✅ Status: {res.status_code}, Time: {end-start:.4f}s")
        print(f"   Preview: {res.text[:50]}...")
    except Exception as e:
        print(f"   ❌ Failed: {e}")

if __name__ == "__main__":
    print(f"🚀 KMA Connectivity Test (Server Side)")
    print(f"   Target: {URL}")
    print(f"   API Key: {config.KMA_API_KEY[:5]}...")

    # 1. Standard Requests (No Adapter)
    test_session("Standard requests.Session()", None)

    # 2. Modern TLS Adapter (Currently used in weather.py)
    test_session("http.get_modern_tls_session()", http.get_modern_tls_session)

    # 3. TLS v1.2 Adapter
    test_session("http.get_tlsv12_session()", http.get_tlsv12_session)

    # 4. Insecure Session (Verify False)
    test_session("http.get_insecure_session()", http.get_insecure_session)

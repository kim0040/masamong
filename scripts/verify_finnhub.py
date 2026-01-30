
import sys
import os
import asyncio
import json

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from dotenv import load_dotenv
load_dotenv()

from utils.api_handlers import finnhub

async def test_finnhub():
    symbol = "NVDA"
    print(f"🔎 Testing Finnhub Rich Context for '{symbol}'...\n")
    print("="*60)
    
    # Simulate cogs/tools_cog.py logic
    price_task = finnhub.get_stock_quote(symbol)
    profile_task = finnhub.get_company_profile(symbol)
    news_task = finnhub.get_company_news(symbol, count=3)
    reco_task = finnhub.get_recommendation_trends(symbol)
    
    results = await asyncio.gather(price_task, profile_task, news_task, reco_task, return_exceptions=True)
    price_res, profile_res, news_res, reco_res = results
    
    output_parts = [f"## 💰 시세 정보:\n{price_res}"]

    # 2. Company Profile
    if isinstance(profile_res, dict):
        mcap = f"{profile_res.get('market_cap', 0):,.0f}" if profile_res.get('market_cap') else "N/A"
        profile_str = (f"- 기업명: {profile_res.get('name')}\n"
                       f"- 산업: {profile_res.get('industry')}\n"
                       f"- 시가총액: ${mcap} Million\n"
                       f"- 웹사이트: {profile_res.get('website')}")
        output_parts.append(f"## 🏢 기업 개요:\n{profile_str}")

    # 3. Recommendation Trends
    if isinstance(reco_res, str) and "실패" not in reco_res:
        output_parts.append(f"## 📊 애널리스트 투자의견:\n{reco_res}")

    # 4. News
    if isinstance(news_res, str) and "찾을 수 없습니다" not in news_res:
        output_parts.append(f"## 📰 관련 뉴스:\n{news_res}")
        
    final_output = f"'{symbol}'에 대한 종합 주식 리포트 (Finnhub):\n\n" + "\n\n".join(output_parts)
    print(final_output)
    print("="*60)

if __name__ == "__main__":
    asyncio.run(test_finnhub())

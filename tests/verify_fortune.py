import sys
import os
import asyncio
from datetime import datetime
import pytz

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.fortune import FortuneCalculator
from logger_config import logger

async def test_fortune():
    print(">>> FortuneCalculator 검증 시작")
    calc = FortuneCalculator()
    
    # 1. Saju Calculation Test
    print("\n[1] 사주 데이터 테스트")
    saju = calc._get_saju_palja(2024, 1, 1)
    print(f"2024-01-01 사주: {saju}")
    
    # 2. Astrology Chart Test (Ephem)
    print("\n[2] 점성술 차트 테스트 (Ephem)")
    now = datetime.now(pytz.timezone('Asia/Seoul'))
    astro = calc._get_astrology_chart(now)
    print(f"현재 점성술 정보: {astro}")
    
    # 3. Comprehensive Info Test
    print("\n[3] 종합 데이터 생성 테스트")
    full_info = calc.get_comprehensive_info("1990-01-01", "12:00")
    print(f"PROMPT DATA:\n{full_info}")
    
    if "서양 점성술 정보 없음" in astro:
        print("\n[WARNING] Ephem 라이브러리가 로드되지 않았거나 작동하지 않습니다.")
    else:
        print("\n[SUCCESS] Ephem 점성술 데이터가 정상적으로 생성되었습니다.")

if __name__ == "__main__":
    asyncio.run(test_fortune())

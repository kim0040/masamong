# -*- coding: utf-8 -*-
"""
좌표 변환 관련 유틸리티 함수를 제공하는 모듈입니다.

- 위경도(WGS84) <-> 기상청 격자(X, Y) 좌표 상호 변환
- 데이터베이스에서 지역명으로 격자 좌표 조회

좌표 변환 코드는 아래 Gist를 원본으로 하되, 원본 Javascript 소스와의
차이점을 교정하여 정확도를 높였습니다.
참고: https://gist.github.com/fronteer-kr/14d7f779d52a21ac2f16
"""

import math
import aiosqlite
from typing import Dict, Optional

# --- 기상청 격자 변환을 위한 상수 --- #
RE = 6371.00877  # 지구 반경 (km)
GRID = 5.0       # 격자 간격 (km)
SLAT1 = 30.0     # 표준 위도 1 (deg)
SLAT2 = 60.0     # 표준 위도 2 (deg)
OLON = 126.0     # 기준 경도 (deg)
OLAT = 38.0      # 기준 위도 (deg)
XO = 43.0        # 기준점 X좌표 (격자)
YO = 136.0       # 기준점 Y좌표 (격자)

# --- 성능 최적화를 위한 사전 계산값 --- #
PI = math.pi
DEGRAD = PI / 180.0
RADDEG = 180.0 / PI

re = RE / GRID
slat1 = SLAT1 * DEGRAD
slat2 = SLAT2 * DEGRAD
olon = OLON * DEGRAD
olat = OLAT * DEGRAD

sn = math.tan(PI * 0.25 + slat2 * 0.5) / math.tan(PI * 0.25 + slat1 * 0.5)
sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(sn)
sf = math.tan(PI * 0.25 + slat1 * 0.5)
sf = math.pow(sf, sn) * math.cos(slat1) / sn
ro = math.tan(PI * 0.25 + olat * 0.5)
ro = re * sf / math.pow(ro, sn)

async def get_coords_from_db(db: aiosqlite.Connection, location_name: str) -> Optional[Dict[str, int]]:
    """
    데이터베이스의 `locations` 테이블에서 지역 이름으로 기상청 격자 좌표(nx, ny)를 조회합니다.
    
    1.  먼저 지역명과 정확히 일치하는 데이터를 찾습니다.
    2.  정확히 일치하는 데이터가 없으면, 부분 일치(LIKE) 검색을 시도하여 첫 번째 결과를 반환합니다.
    """
    if not db:
        return None
    
    # 1. 정확한 이름으로 검색
    async with db.execute("SELECT name, nx, ny FROM locations WHERE name = ?", (location_name,)) as cursor:
        result = await cursor.fetchone()
        if result:
            return {'name': result['name'], 'nx': result['nx'], 'ny': result['ny']}

    # 2. 부분 일치로 검색 (LIKE)
    async with db.execute("SELECT name, nx, ny FROM locations WHERE ? LIKE '%' || name || '%'", (location_name,)) as cursor:
        result = await cursor.fetchone()
        if result:
            return {'name': result['name'], 'nx': result['nx'], 'ny': result['ny']}
            
    return None

def latlon_to_kma_grid(lat: float, lon: float) -> tuple[int, int]:
    """위경도(WGS84) 좌표를 기상청 격자 좌표(X, Y)로 변환합니다."""
    ra = math.tan(PI * 0.25 + lat * DEGRAD * 0.5)
    ra = re * sf / math.pow(ra, sn)
    theta = lon * DEGRAD - olon
    if theta > PI:
        theta -= 2.0 * PI
    if theta < -PI:
        theta += 2.0 * PI
    theta *= sn
    x = int(ra * math.sin(theta) + XO + 0.5)
    y = int(ro - ra * math.cos(theta) + YO + 0.5)
    return x, y

def kma_grid_to_latlon(x: int, y: int) -> tuple[float, float]:
    """기상청 격자 좌표(X, Y)를 위경도(WGS84) 좌표로 변환합니다."""
    xn = x - XO
    yn = ro - y + YO
    ra = math.sqrt(xn * xn + yn * yn)
    if sn < 0.0:
        ra = -ra
    alat = math.pow((re * sf / ra), (1.0 / sn))
    alat = 2.0 * math.atan(alat) - PI * 0.5

    if abs(xn) <= 0.0:
        theta = 0.0
    else:
        if abs(yn) <= 0.0:
            theta = PI * 0.5
            if xn < 0.0:
                theta = -theta
        else:
            theta = math.atan2(xn, yn)

    alon = theta / sn + olon
    lat = alat * RADDEG
    lon = alon * RADDEG
    return lat, lon

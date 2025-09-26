# -*- coding: utf-8 -*-
import math
import aiosqlite
from typing import Dict, Optional

# This code is adapted from the Gist found at:
# https://gist.github.com/fronteer-kr/14d7f779d52a21ac2f16
# The Python version in the Gist had some discrepancies with the original
# Javascript version. This implementation uses the constants and logic from
# the more reliable Javascript source.

# --- Constants for KMA Grid Conversion ---
RE = 6371.00877  # Earth radius (km)
GRID = 5.0       # Grid interval (km)
SLAT1 = 30.0     # Standard latitude 1 (deg)
SLAT2 = 60.0     # Standard latitude 2 (deg)
OLON = 126.0     # Reference longitude (deg)
OLAT = 38.0      # Reference latitude (deg)
XO = 43.0        # Reference X coordinate (GRID) - From JS version
YO = 136.0       # Reference Y coordinate (GRID) - From JS version

# --- Pre-calculated values for performance ---
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
    데이터베이스의 locations 테이블에서 지역 이름으로 좌표(nx, ny)를 조회합니다.
    정확히 일치하는 이름이 없으면, 이름의 일부가 일치하는 첫 번째 지역을 반환합니다.
    """
    if not db:
        return None
    
    # 1. 정확한 이름으로 검색
    async with db.execute("SELECT nx, ny FROM locations WHERE name = ?", (location_name,)) as cursor:
        result = await cursor.fetchone()
        if result:
            return {'nx': result['nx'], 'ny': result['ny']}

    # 2. 부분 일치로 검색 (LIKE)
    async with db.execute("SELECT nx, ny FROM locations WHERE name LIKE ?", (f'%{location_name}%',)) as cursor:
        result = await cursor.fetchone()
        if result:
            return {'nx': result['nx'], 'ny': result['ny']}
            
    return None

def latlon_to_kma_grid(lat: float, lon: float) -> tuple[int, int]:
    """
    Converts latitude and longitude (WGS84) to KMA grid coordinates (X, Y).
    """
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
    """
    Converts KMA grid coordinates (X, Y) to latitude and longitude (WGS84).
    """
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
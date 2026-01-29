# -*- coding: utf-8 -*-
"""
KMA Mid-term Forecast Region Codes
Maps location names to KMA Mid-term Land/Temp codes.
"""

# Land Forecast Zones (Weather Condition)
MID_TERM_LAND_CODES = {
    # Seoul / Incheon / Gyeonggi
    "서울": "11B00000", "인천": "11B00000", "경기": "11B00000",
    "수원": "11B00000", "성남": "11B00000", "용인": "11B00000",
    
    # Gangwon
    "춘천": "11D10000", "원주": "11D10000", "강릉": "11D20000", "속초": "11D20000",
    
    # Daejeon / Chungnam / Sejong
    "대전": "11C20000", "세종": "11C20000", "충남": "11C20000", "천안": "11C20000",
    
    # Chungbuk
    "청주": "11C10000", "충주": "11C10000",
    
    # Gwangju / Jeonnam
    "광주": "11F20000", "전남": "11F20000", 
    "여수": "11F20000", "순천": "11F20000", "광양": "11F20000", "목포": "11F20000",
    
    # Jeonbuk
    "전주": "11F10000", "군산": "11F10000",
    
    # Daegu / Gyeongbuk
    "대구": "11H10000", "경북": "11H10000", "포항": "11H10000", "안동": "11H10000",
    
    # Busan / Ulsan / Gyeongnam
    "부산": "11H20000", "울산": "11H20000", "경남": "11H20000",
    "창원": "11H20000", "진주": "11H20000",
    
    # Jeju
    "제주": "11G00000", "서귀포": "11G00000"
}

# Temperature Zones (Specific City Codes)
MID_TERM_TEMP_CODES = {
    "서울": "11B10101",
    "인천": "11B20201",
    "수원": "11B20601",
    "춘천": "11D10301",
    "강릉": "11D20501",
    "대전": "11C20401",
    "청주": "11C10301",
    "광주": "11F20501",
    "전주": "11F10201",
    "부산": "11H20201",
    "울산": "11H20101",
    "대구": "11H10701",
    "제주": "11G00201",
    "서귀포": "11G00401",
    "여수": "11F20402",
    "순천": "11F20405", # Approximate (uses Suncheon)
    "광양": "11F20404", # Kwangyang City Code
    "목포": "11F20503",
    "창원": "11H20301"
}

def get_land_code(location_name: str) -> str:
    """Finds best matching land code."""
    for key, code in MID_TERM_LAND_CODES.items():
        if key in location_name:
            return code
    return "11B00000" # Default to Seoul if unknown

def get_temp_code(location_name: str) -> str:
    """Finds best matching temp code."""
    for key, code in MID_TERM_TEMP_CODES.items():
        if key in location_name:
            return code
    return "11B10101" # Default to Seoul if unknown

# -*- coding: utf-8 -*-
"""
운세 데이터를 계산하고 분석하는 핵심 모듈입니다.
서양 점성술(ephem)과 동양 사주(korean-lunar-calendar)를 사용하여 종합적인 운세 정보를 생성합니다.
"""

import logging
from datetime import datetime, time
import pytz
import math
from typing import Dict, Any, Optional

from logger_config import logger

# Ephem Import (Western Astrology)
try:
    import ephem
    EPHEM_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    EPHEM_AVAILABLE = False

# Korean Lunar Calendar Import (Eastern Saju)
try:
    from korean_lunar_calendar import KoreanLunarCalendar
except ImportError:
    pass

# 서울 위경도 (점성술 차트 계산용)
SEOUL_LAT = 37.5665
SEOUL_LON = 126.9780

def get_zodiac_sign(lon_deg: float) -> str:
    """황도 경도(degree)를 별자리 이름으로 변환합니다."""
    signs = [
        "양자리", "황소자리", "쌍둥이자리", "게자리", "사자자리", "처녀자리",
        "천칭자리", "전갈자리", "사수자리", "염소자리", "물병자리", "물고기자리"
    ]
    index = int(lon_deg / 30)
    return signs[index % 12]

class FortuneCalculator:
    """운세 계산 및 데이터 생성을 담당하는 클래스"""

    def __init__(self):
        try:
            self.calendar = KoreanLunarCalendar()
            logger.info("FortuneCalculator 초기화 완료")
        except NameError:
             logger.error("KoreanLunarCalendar not installed.")

    def _get_saju_palja(self, year: int, month: int, day: int) -> str:
        """
        변경된 날짜(양력)를 기준으로 일진(일주) 등 사주 정보를 텍스트로 반환합니다.
        Note: 시주(시간)는 복잡하여 제외하고 연/월/일주 위주로 제공합니다.
        """
        try:
            self.calendar.setSolarDate(year, month, day)
            ganji = self.calendar.getGapJaString() 
            lunar_date = f"{self.calendar.lunarYear}-{self.calendar.lunarMonth:02d}-{self.calendar.lunarDay:02d}"
            return f"음력: {lunar_date}, 간지: {ganji}"
        except Exception as e:
            logger.error(f"사주 계산 중 오류: {e}")
            return "사주 정보 산출 실패"

    def _get_astrology_chart(self, dt: datetime) -> str:
        """
        현재 시각 기준 서울 상공의 행성 배치(Transit) 정보를 요약하여 반환합니다.
        (ephem 라이브러리 사용)
        """
        if not EPHEM_AVAILABLE:
            return "서양 점성술 정보 없음 (라이브러리 미설치)"

        try:
            # ephem 설정
            observer = ephem.Observer()
            observer.lat = str(SEOUL_LAT)
            observer.lon = str(SEOUL_LON)
            # datetime을 UTC로 변환하여 전달 (ephem은 UTC 기준)
            dt_utc = dt.astimezone(pytz.utc)
            observer.date = dt_utc

            # 행성 객체 생성
            planets = {
                "태양": ephem.Sun(),
                "달": ephem.Moon(),
                "수성": ephem.Mercury(),
                "금성": ephem.Venus(),
                "화성": ephem.Mars(),
                "목성": ephem.Jupiter(),
                "토성": ephem.Saturn()
            }
            
            result_parts = []
            for name, body in planets.items():
                body.compute(observer)
                # 황도 좌표계(Ecliptic Coordinate)로 변환하여 경도(Longitude) 추출
                ecl_lon = ephem.Ecliptic(body).lon
                degree = math.degrees(ecl_lon)
                sign = get_zodiac_sign(degree)
                result_parts.append(f"{name}: {sign}") 

            return ", ".join(result_parts)

        except Exception as e:
            logger.error(f"점성술 차트 계산 중 오류: {e}")
            return "천체 배치 정보 산출 실패"

    def get_comprehensive_info(self, birth_date: str, birth_time: str = "12:00") -> str:
        """
        AI 프롬프트에 주입할 Raw Data를 생성합니다.
        
        Args:
            birth_date: "YYYY-MM-DD"
            birth_time: "HH:MM"
            
        Returns:
            str: 분석용 텍스트 데이터
        """
        try:
            # 현재 시각 (KST)
            now_kst = datetime.now(pytz.timezone('Asia/Seoul'))
            
            # 1. 오늘의 사주 (일진)
            saju_info = self._get_saju_palja(now_kst.year, now_kst.month, now_kst.day)
            
            # 2. 오늘의 점성술 (Transit Chart)
            astro_info = self._get_astrology_chart(now_kst)
            
            # 3. 사용자 정보 요약 (Removed 'UserBirth' from here as it's just raw data context)
            
            # 종합 데이터 문자열 구성 (Token Efficient Format)
            raw_data = (
                f"[Feature: Fortune]\n"
                f"Time: {now_kst.strftime('%Y-%m-%d %H:%M')}\n"
                f"UserBirth: {birth_date} {birth_time}\n"
                f"Saju: {saju_info}\n"
                f"Astro: {astro_info}\n"
            )
            
            return raw_data
            
        except Exception as e:
            logger.error(f"운세 데이터 통합 생성 중 오류: {e}", exc_info=True)
            return "운세 데이터를 불러오는 데 실패했습니다."

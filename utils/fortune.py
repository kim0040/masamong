# -*- coding: utf-8 -*-
"""
운세 데이터를 계산하고 분석하는 핵심 모듈입니다.
서양 점성술(flatlib)과 동양 사주(korean-lunar-calendar)를 사용하여 종합적인 운세 정보를 생성합니다.
"""

import logging
from datetime import datetime, time
import pytz
from typing import Dict, Any, Optional

try:
    from flatlib.datetime import Datetime as FlatDatetime
    from flatlib.geopos import GeoPos
    from flatlib.chart import Chart
    from flatlib import const
except ImportError:
    pass

try:
    from korean_lunar_calendar import KoreanLunarCalendar
except ImportError:
    pass

from logger_config import logger

# 서울 위경도 (점성술 차트 계산용)
SEOUL_LAT = 37.5665
SEOUL_LON = 126.9780

class FortuneCalculator:
    """운세 계산 및 데이터 생성을 담당하는 클래스"""

    def __init__(self):
        self.calendar = KoreanLunarCalendar()
        logger.info("FortuneCalculator 초기화 완료")

    def _get_saju_palja(self, year: int, month: int, day: int) -> str:
        """
        변경된 날짜(양력)를 기준으로 일진(일주) 등 사주 정보를 텍스트로 반환합니다.
        Note: 시주(시간)는 복잡하여 제외하고 연/월/일주 위주로 제공합니다.
        """
        try:
            self.calendar.setSolarDate(year, month, day)
            
            # 간지(GapJa) 정보 가져오기
            # KoreanLunarCalendar 라이브러리는 
            # getGapJaString() -> "갑자" 형태
            # getChineseGapJaString() -> "甲子" 형태 등을 제공함.
            
            # 라이브러리 버전에 따라 메서드명이 다를 수 있으므로 확인 필요하지만,
            # 통상적인 사용법: setSolarDate 후 getGapJaString() 호출
            
            ganji = self.calendar.getGapJaString() # ex: "갑자(년) 을축(월) 병인(일)" 형태일 수 있음. 확인 필요.
            # 실제 라이브러리는 년/월/일 간지를 개별로 줄 수도 있음.
            # 하지만 여기서는 라이브러리가 제공하는 문자열을 그대로 사용.
            
            lunar_date = f"{self.calendar.lunarYear}-{self.calendar.lunarMonth:02d}-{self.calendar.lunarDay:02d}"
            return f"음력: {lunar_date}, 간지: {ganji}"
        except Exception as e:
            logger.error(f"사주 계산 중 오류: {e}")
            return "사주 정보 산출 실패"

    def _get_astrology_chart(self, dt: datetime) -> str:
        """
        현재 시각 기준 서울 상공의 행성 배치(Transit) 정보를 요약하여 반환합니다.
        """
        try:
            # flatlib 날짜 객체 생성
            date = FlatDatetime(dt.strftime('%Y/%m/%d'), dt.strftime('%H:%M'), '+09:00')
            pos = GeoPos(SEOUL_LAT, SEOUL_LON)
            chart = Chart(date, pos)

            # 주요 행성 위치 추출
            sun = chart.get(const.SUN)
            moon = chart.get(const.MOON)
            mercury = chart.get(const.MERCURY)
            venus = chart.get(const.VENUS)
            mars = chart.get(const.MARS)
            jupiter = chart.get(const.JUPITER)
            saturn = chart.get(const.SATURN)

            return (
                f"태양: {sun.sign} {int(sun.lon)}도, "
                f"달: {moon.sign} {int(moon.lon)}도, "
                f"수성: {mercury.sign}, "
                f"금성: {venus.sign}, "
                f"화성: {mars.sign}, "
                f"목성: {jupiter.sign}, "
                f"토성: {saturn.sign}"
            )
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
            
            # 3. 사용자 정보 요약
            user_info_str = f"사용자 생시: {birth_date} {birth_time}"
            
            # 종합 데이터 문자열 구성
            raw_data = (
                f"[운세 분석용 데이터]\n"
                f"기준 시각: {now_kst.strftime('%Y-%m-%d %H:%M')}\n"
                f"{user_info_str}\n"
                f"오늘의 동양 사주(만세력): {saju_info}\n"
                f"현재 천체 배치(Western Transit): {astro_info}\n"
            )
            
            return raw_data
            
        except Exception as e:
            logger.error(f"운세 데이터 통합 생성 중 오류: {e}", exc_info=True)
            return "운세 데이터를 불러오는 데 실패했습니다."

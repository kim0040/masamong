"""운세 계산기 회귀 테스트.

파일명이 pytest 기본 수집 규칙(`test_*.py`)을 따르도록 유지합니다. 이 테스트는
외부 LLM이나 운영 DB를 사용하지 않고 로컬 계산 라이브러리만 검증합니다.
"""

from datetime import date, datetime

import pytz

from utils.fortune import FortuneCalculator


def test_fortune_calculator_generates_core_sections():
    calc = FortuneCalculator()

    saju = calc._get_saju_palja(2024, 1, 1)
    astro = calc._get_astrology_chart(
        pytz.timezone("Asia/Seoul").localize(datetime(2024, 1, 1, 12, 0))
    )

    assert "음력:" in saju
    assert "간지:" in saju
    assert astro
    assert "산출 실패" not in astro


def test_comprehensive_fortune_context_keeps_user_birth_data():
    calc = FortuneCalculator()

    full_info = calc.get_comprehensive_info("1990-01-01", "12:00")

    assert "[Feature: Fortune]" in full_info
    assert "UserBirth: 1990-01-01 12:00" in full_info
    assert "Saju:" in full_info
    assert "Astro:" in full_info


def test_unknown_birth_time_is_not_replaced_with_noon_and_target_date_is_used():
    calc = FortuneCalculator()

    full_info = calc.get_comprehensive_info(
        "1990-01-01",
        None,
        target_date=date(2026, 7, 29),
    )

    assert "UserBirth: 1990-01-01 [time not provided]" in full_info
    assert "UserBirth: 1990-01-01 12:00" not in full_info
    assert "Time: 2026-07-29 08:00" in full_info

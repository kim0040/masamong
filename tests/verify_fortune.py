from datetime import datetime

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

import pytest

import config
from utils.api_handlers import exchange_rate


@pytest.mark.asyncio
async def test_get_krw_exchange_rate_success(monkeypatch):
    sample_records = [
        {
            "cur_unit": "USD",
            "cur_nm": "미국 달러",
            "deal_bas_r": "1,352.50",
            "ttb": "1,329.12",
            "tts": "1,375.88",
        }
    ]

    async def fake_fetch():
        return sample_records

    monkeypatch.setattr(config, "EXIM_API_KEY_KR", "DUMMY_KEY")
    monkeypatch.setattr(exchange_rate, "_fetch_latest_exchange_rates", fake_fetch)

    result = await exchange_rate.get_krw_exchange_rate("usd")
    assert "매매기준율" in result
    assert "미국 달러" in result


@pytest.mark.asyncio
async def test_get_krw_exchange_rate_not_found(monkeypatch):
    async def fake_fetch():
        return [
            {
                "cur_unit": "USD",
                "deal_bas_r": "1,352.50",
            }
        ]

    monkeypatch.setattr(config, "EXIM_API_KEY_KR", "DUMMY_KEY")
    monkeypatch.setattr(exchange_rate, "_fetch_latest_exchange_rates", fake_fetch)

    result = await exchange_rate.get_krw_exchange_rate("EUR")
    assert "찾을 수 없습니다" in result


@pytest.mark.asyncio
async def test_get_raw_exchange_rate_returns_float(monkeypatch):
    sample_records = [
        {
            "cur_unit": "JPY(100)",
            "deal_bas_r": "925.50",
        }
    ]

    async def fake_fetch():
        return sample_records

    monkeypatch.setattr(config, "EXIM_API_KEY_KR", "DUMMY_KEY")
    monkeypatch.setattr(exchange_rate, "_fetch_latest_exchange_rates", fake_fetch)

    value = await exchange_rate.get_raw_exchange_rate("JPY(100)")
    assert value == pytest.approx(925.50)

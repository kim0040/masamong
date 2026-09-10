"""ExchangeRate-API v6 응답 형식과 한도 오류 처리."""

import pytest

from utils.api_handlers import fx_rates


def test_extract_rate_map_prefers_conversion_rates():
    rates = fx_rates.extract_rate_map(
        {
            "conversion_rates": {"KRW": 1338.8, "JPY": 153.5, "USD": 1},
            "rates": {"KRW": 1},
        }
    )
    assert rates["KRW"] == 1338.8
    assert rates["JPY"] == 153.5


def test_extract_rate_map_falls_back_to_open_access_rates():
    rates = fx_rates.extract_rate_map({"rates": {"KRW": 8.72}})
    assert rates["KRW"] == 8.72


@pytest.mark.asyncio
async def test_quota_reached_is_provider_failure(monkeypatch):
    def _fail(_base):
        raise fx_rates.ExchangeRateApiError("quota-reached", "quota-reached")

    monkeypatch.setattr(fx_rates, "_load_rates", _fail)
    result = await fx_rates.get_fx_quote("USD", "KRW")
    assert result["failure_kind"] == "provider_error"
    assert "한도" in result["error"]


@pytest.mark.asyncio
async def test_unsupported_code_is_input_failure(monkeypatch):
    def _fail(_base):
        raise fx_rates.ExchangeRateApiError("unsupported-code", "unsupported-code")

    monkeypatch.setattr(fx_rates, "_load_rates", _fail)
    result = await fx_rates.get_fx_quote("USD", "ZZZ")
    assert result["failure_kind"] == "invalid_symbol"
    assert result["provider_failure"] is False


@pytest.mark.asyncio
async def test_keyed_latest_uses_bearer_and_conversion_rates(monkeypatch):
    fx_rates._CACHE.clear()
    monkeypatch.setattr(fx_rates, "_api_key", lambda: "test-key")
    captured = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {
                "result": "success",
                "base_code": "JPY",
                "conversion_rates": {"KRW": 8.719, "USD": 0.006511},
                "time_next_update_unix": 2_000_000_000,
            }

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, url, headers=None, timeout=10):
            captured["url"] = url
            captured["headers"] = headers
            captured["timeout"] = timeout
            return _Resp()

    monkeypatch.setattr(fx_rates.http, "get_modern_tls_session", lambda: _Session())

    payload = fx_rates._load_rates("JPY")
    assert captured["url"] == "https://v6.exchangerate-api.com/v6/latest/JPY"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert "test-key" not in captured["url"]
    assert payload["rates"]["KRW"] == 8.719

    result = await fx_rates.get_fx_quote("JPY", "KRW")
    assert result["status"] == "success"
    assert result["price"] == 8.719
    assert "1 JPY" in result["summary"]


@pytest.mark.asyncio
async def test_tools_cog_krw_rate_uses_exchangerate_api(monkeypatch):
    from cogs.tools_cog import ToolsCog

    class _FakeBot:
        def get_cog(self, _name):
            return None

    async def fake_quote(base, quote):
        assert base == "EUR"
        assert quote == "KRW"
        return {"status": "success", "summary": "1 EUR = 1,500.0000 KRW"}

    monkeypatch.setattr(fx_rates, "get_fx_quote", fake_quote)
    text = await ToolsCog(_FakeBot()).get_krw_exchange_rate("EUR")
    assert "1 EUR" in text

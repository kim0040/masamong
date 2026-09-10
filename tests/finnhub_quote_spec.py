"""Finnhub 무료 플랜 범위와 유연한 환율 페어."""

import pytest

import config
from cogs.tools_cog import ToolsCog
from utils.api_handlers import finnhub, fx_rates
from utils.finance_query import detect_fx_pair, split_quote_query


def test_fx_pairs_cover_krw_jpy_usd_and_crosses():
    assert detect_fx_pair("엔화 환율 알려줘") == ("JPY", "KRW")
    assert detect_fx_pair("환율 엔화 알려줘") == ("JPY", "KRW")
    assert detect_fx_pair("엔화 알려달라고") == ("JPY", "KRW")
    assert detect_fx_pair("엔만") == ("JPY", "KRW")
    assert detect_fx_pair("한화로 달러 얼마야") == ("USD", "KRW")
    assert detect_fx_pair("달러 엔 환율") == ("USD", "JPY")
    assert detect_fx_pair("EUR/KRW") == ("EUR", "KRW")
    assert detect_fx_pair("USDKRW=X") == ("USD", "KRW")
    assert detect_fx_pair("엔비디아 주가") is None
    assert detect_fx_pair("오늘 환율 어때") == ("USD", "KRW")
    assert detect_fx_pair("THB 환율") == ("THB", "KRW")


def test_split_quote_query_keeps_real_tickers_and_drops_names():
    query, ticker = split_quote_query(user_query="엔비디아 주가", symbol="NVDA")
    assert query == "엔비디아 주가"
    assert ticker == "NVDA"
    query, ticker = split_quote_query(symbol="엔비디아")
    assert query == "엔비디아"
    assert ticker == ""
    query, ticker = split_quote_query(symbol="BINANCE:BTCUSDT")
    assert ticker == "BINANCE:BTCUSDT"


@pytest.mark.asyncio
async def test_fx_quote_formats_both_directions(monkeypatch):
    monkeypatch.setattr(
        fx_rates,
        "_load_rates",
        lambda _base: {"result": "success", "rates": {"KRW": 8.72}},
    )
    result = await fx_rates.get_fx_quote("JPY", "KRW")
    assert result["status"] == "success"
    assert result["price"] == 8.72
    assert result["pair"]["base"] == "JPY"
    assert "1 JPY" in result["summary"]


@pytest.mark.asyncio
async def test_finnhub_lookup_uses_search_then_quote(monkeypatch):
    async def fake_request(path, extra):
        if path == "/search":
            return 200, {
                "result": [
                    {
                        "description": "NVIDIA Corp",
                        "symbol": "NVDA",
                        "type": "Common Stock",
                    }
                ]
            }
        return 200, {"c": 223.67, "d": -2.06, "dp": -0.91, "t": 1}

    monkeypatch.setattr(finnhub, "_get_client", lambda: {"token": "x"})
    monkeypatch.setattr(finnhub, "_request_json", fake_request)

    result = await finnhub.lookup_quote("NVIDIA Corp")
    assert result["status"] == "success"
    assert result["symbol"] == "NVDA"
    assert result["price"] == 223.67


@pytest.mark.asyncio
async def test_finnhub_korean_listing_is_unsupported(monkeypatch):
    called = []

    async def fake_request(path, extra):
        called.append(path)
        if path == "/search":
            return 200, {
                "result": [
                    {
                        "description": "Samsung Electronics Co Ltd",
                        "symbol": "005930.KS",
                        "type": "Common Stock",
                    }
                ]
            }
        raise AssertionError("국내 상장은 quote를 호출하면 안 됩니다")

    monkeypatch.setattr(finnhub, "_get_client", lambda: {"token": "x"})
    monkeypatch.setattr(finnhub, "_request_json", fake_request)

    result = await finnhub.lookup_quote("Samsung Electronics")
    assert result["failure_kind"] == "unsupported_market"
    assert "/quote" not in called


@pytest.mark.asyncio
async def test_tools_cog_routes_yen_to_fx_not_finnhub(monkeypatch):
    monkeypatch.setattr(config, "USE_YFINANCE", False)

    class _Bot:
        def get_cog(self, _name):
            return None

    async def fake_fx(base, quote):
        assert (base, quote) == ("JPY", "KRW")
        return {
            "status": "success",
            "price": 9.1,
            "symbol": "JPYKRW=X",
            "source_urls": ["https://www.exchangerate-api.com"],
        }

    monkeypatch.setattr(fx_rates, "get_fx_quote", fake_fx)
    cog = ToolsCog(_Bot())
    result = await cog.get_stock_price(user_query="엔화 환율 알려줘")
    assert result["status"] == "success"
    assert result["symbol"] == "JPYKRW=X"


@pytest.mark.asyncio
async def test_hangul_name_skips_empty_search(monkeypatch):
    async def fake_request(path, extra):
        raise AssertionError(f"한글 전용 질의는 검색하지 않습니다: {path}")

    monkeypatch.setattr(finnhub, "_get_client", lambda: {"token": "x"})
    monkeypatch.setattr(finnhub, "_request_json", fake_request)
    result = await finnhub.lookup_quote("엔비디아 주가")
    assert result["failure_kind"] == "invalid_symbol"


@pytest.mark.asyncio
async def test_crypto_search_prefers_exchange_symbol(monkeypatch):
    async def fake_request(path, extra):
        if path == "/search":
            return 200, {
                "result": [
                    {
                        "description": "Bitcoin USD",
                        "symbol": "BINANCE:BTCUSDT",
                        "type": "Crypto",
                    },
                    {
                        "description": "Bitcoin Cash",
                        "symbol": "BCH",
                        "type": "Common Stock",
                    },
                ]
            }
        return 200, {"c": 64000.0, "d": 100.0, "dp": 0.15, "t": 1}

    monkeypatch.setattr(finnhub, "_get_client", lambda: {"token": "x"})
    monkeypatch.setattr(finnhub, "_request_json", fake_request)
    result = await finnhub.lookup_quote("Bitcoin")
    assert result["status"] == "success"
    assert result["symbol"] == "BINANCE:BTCUSDT"

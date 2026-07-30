import pandas as pd
import pytest

from utils.api_handlers import yfinance_handler


@pytest.mark.asyncio
async def test_stock_lookup_classifies_missing_symbol_as_input_failure(
    monkeypatch,
):
    class _MissingTicker:
        @property
        def info(self):
            raise RuntimeError("Quote not found for symbol: SKHYNX")

        @property
        def fast_info(self):
            raise RuntimeError("Quote not found for symbol: SKHYNX")

        def history(self, **_kwargs):
            raise RuntimeError(
                "possibly delisted; no price data found"
            )

    monkeypatch.setattr(
        yfinance_handler.yf,
        "Ticker",
        lambda _ticker: _MissingTicker(),
    )

    result = await yfinance_handler.get_stock_info("SKHYNX")

    assert result["status"] == "error"
    assert result["failure_kind"] == "invalid_symbol"
    assert result["provider_failure"] is False


@pytest.mark.asyncio
async def test_stock_lookup_success_includes_verifiable_source(monkeypatch):
    class _FastInfo:
        last_price = 215.32
        previous_close = 210.0

    class _Ticker:
        info = {
            "currency": "USD",
            "shortName": "Apple Inc.",
            "marketCap": 123,
        }
        fast_info = _FastInfo()

    monkeypatch.setattr(
        yfinance_handler.yf,
        "Ticker",
        lambda _ticker: _Ticker(),
    )

    result = await yfinance_handler.get_stock_info("AAPL")

    assert result["status"] == "success"
    assert result["price"] == 215.32
    assert result["source_urls"] == [
        "https://finance.yahoo.com/quote/AAPL/"
    ]


@pytest.mark.asyncio
async def test_market_snapshot_batches_indices_and_calculates_changes(monkeypatch):
    columns = pd.MultiIndex.from_product(
        [
            ["Close"],
            ["^KS11", "^KQ11"],
        ]
    )
    frame = pd.DataFrame(
        [
            [6023.66, 705.85],
            [5663.24, 662.68],
        ],
        index=pd.to_datetime(["2026-07-28", "2026-07-29"]),
        columns=columns,
    )
    captured = {}

    def _fake_download(tickers, **kwargs):
        captured["tickers"] = tickers
        captured["kwargs"] = kwargs
        return frame

    monkeypatch.setattr(yfinance_handler.yf, "download", _fake_download)

    result = await yfinance_handler.get_market_snapshot("kr")

    assert result["status"] == "success"
    assert result["region"] == "kr"
    assert captured["tickers"] == ["^KS11", "^KQ11"]
    assert captured["kwargs"]["threads"] is False
    assert captured["kwargs"]["timeout"] == 10
    assert len(result["indices"]) == 2

    kospi = result["indices"][0]
    assert kospi["name"] == "코스피"
    assert kospi["market_date"] == "2026-07-29"
    assert kospi["value"] == 5663.24
    assert kospi["change"] == pytest.approx(-360.42)
    assert kospi["change_percent"] == pytest.approx(-5.9834)
    assert kospi["source_url"].startswith("https://finance.yahoo.com/quote/")


@pytest.mark.asyncio
async def test_market_snapshot_rejects_unknown_region_without_extra_indices(
    monkeypatch,
):
    columns = pd.MultiIndex.from_product(
        [
            ["Close"],
            ["^KS11", "^KQ11", "^DJI", "^GSPC", "^IXIC"],
        ]
    )
    frame = pd.DataFrame(
        [[1, 2, 3, 4, 5], [2, 3, 4, 5, 6]],
        index=pd.to_datetime(["2026-07-28", "2026-07-29"]),
        columns=columns,
    )

    monkeypatch.setattr(
        yfinance_handler.yf,
        "download",
        lambda *_args, **_kwargs: frame,
    )

    result = await yfinance_handler.get_market_snapshot("invalid")

    assert result["region"] == "global"
    assert len(result["indices"]) == 5

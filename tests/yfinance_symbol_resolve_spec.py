"""Yahoo 상장 심볼 해석은 회사 별칭 표가 아니라 검색 결과를 따른다."""

from unittest.mock import AsyncMock

import pytest

import config
from cogs.tools_cog import ToolsCog
from utils.api_handlers import yfinance_handler


def _quote(symbol, shortname, quote_type="EQUITY", exchange="NMS"):
    return {
        "symbol": symbol,
        "shortname": shortname,
        "longname": shortname,
        "quoteType": quote_type,
        "exchange": exchange,
        "exchDisp": exchange,
    }


def test_fx_pair_uses_iso_codes_not_company_aliases():
    assert yfinance_handler.detect_fx_symbol("지금 달러 환율") == "USDKRW=X"
    assert yfinance_handler.detect_fx_symbol("엔화 환율") == "JPYKRW=X"
    assert yfinance_handler.detect_fx_symbol("엔비디아 주가") is None


def test_two_korean_companies_are_ambiguous_before_search():
    planned = yfinance_handler.build_symbol_search_terms(
        "엔비디아랑 삼성전자 중에 뭐가 나아"
    )
    assert planned["failure_kind"] == "ambiguous_symbol"
    assert planned["terms"] == []


def test_nvidia_primary_listing_beats_foreign_duplicates():
    picked = yfinance_handler.pick_listed_quote(
        [
            _quote("NVD.DE", "NVIDIA CORP", exchange="GER"),
            _quote("NVDA", "NVIDIA Corporation", exchange="NMS"),
            _quote("NVDC34.SA", "NVIDIA CORP DRN", exchange="SAO"),
        ],
        query="NVIDIA",
        search_term="NVIDIA",
    )
    assert picked["symbol"] == "NVDA"


def test_korean_listings_are_ignored():
    picked = yfinance_handler.pick_listed_quote(
        [
            _quote("005930.KS", "Samsung Electronics Co.", exchange="KSC"),
        ],
        query="삼성전자 주가 알려줘",
        search_term="Samsung Electronics",
    )
    assert picked is None


def test_close_unrelated_matches_stay_ambiguous():
    picked = yfinance_handler.pick_listed_quote(
        [
            _quote("AAA", "Acme One", exchange="NMS"),
            _quote("BBB", "Acme Two", exchange="NYQ"),
        ],
        query="Acme",
        search_term="Acme",
    )
    assert picked["ambiguous"] is True


@pytest.mark.asyncio
async def test_resolve_uses_search_hits_not_llm_tickers(monkeypatch):
    yfinance_handler._SEARCH_CACHE.clear()
    monkeypatch.setattr(
        yfinance_handler,
        "search_yahoo_quotes",
        lambda term: (
            [_quote("NVDA", "NVIDIA Corporation")]
            if term.casefold() == "nvidia"
            else []
        ),
    )

    empty = await yfinance_handler.resolve_listed_symbol("엔비디아 지금 주가")
    assert empty["status"] == "error"

    resolved = await yfinance_handler.resolve_listed_symbol(
        "NVIDIA",
        original_query="엔비디아 지금 주가",
    )
    assert resolved["status"] == "success"
    assert resolved["symbol"] == "NVDA"


@pytest.mark.asyncio
async def test_get_stock_price_confirms_search_before_quote(monkeypatch):
    monkeypatch.setattr(config, "USE_YFINANCE", True)
    yfinance_handler._SEARCH_CACHE.clear()

    class _AI:
        async def extract_finance_search_term_with_llm(self, query):
            assert "엔비디아" in query
            return "NVIDIA"

    class _Bot:
        def get_cog(self, name):
            return _AI() if name == "AIHandler" else None

    calls = []

    async def _resolve(query, **kwargs):
        calls.append((query, kwargs))
        if query == "NVIDIA":
            return {
                "status": "success",
                "symbol": "NVDA",
            }
        return {
            "status": "error",
            "failure_kind": "invalid_symbol",
            "provider_failure": False,
        }

    handler = type("Handler", (), {})()
    handler.resolve_listed_symbol = _resolve
    handler.get_stock_info = AsyncMock(
        return_value={
            "status": "success",
            "price": 223.67,
            "symbol": "NVDA",
            "source_urls": ["https://finance.yahoo.com/quote/NVDA/"],
        }
    )

    cog = ToolsCog(_Bot())
    cog._yfinance_handler = handler

    result = await cog.get_stock_price(user_query="엔비디아 지금 시세 얼마야")

    assert result["status"] == "success"
    assert result["symbol"] == "NVDA"
    assert calls[0][0] == "엔비디아 지금 시세 얼마야"
    assert calls[1][0] == "NVIDIA"
    handler.get_stock_info.assert_awaited_once_with("NVDA")

"""환율 질문 시나리오: 페어 해석, 도구 인자, 직접 렌더, 수치 가드."""

from cogs.ai_handler import AIHandler
from utils.finance_query import (
    detect_fx_pair,
    format_fx_user_reply,
    format_quote_user_reply,
    looks_like_fx_quote,
)
from utils.intent_analyzer import IntentAnalyzer


_JPY_QUOTE = {
    "status": "success",
    "symbol": "JPYKRW=X",
    "name": "JPY/KRW",
    "price": 9.1876,
    "currency": "KRW",
    "provider": "exchangerate-api",
    "checked_at_kst": "2026-09-10T15:04:00",
    "source_urls": ["https://www.exchangerate-api.com"],
    "pair": {
        "base": "JPY",
        "quote": "KRW",
        "rate": 9.1876,
        "inverse": 1.0 / 9.1876,
    },
    "summary": "1 JPY = 9.1876 KRW, 1 KRW = 0.108842 JPY",
}


def _handler() -> AIHandler:
    return AIHandler.__new__(AIHandler)


def test_fx_pair_scenarios_cover_casual_korean_and_tickers():
    cases = {
        "환율 엔화 알려줘": ("JPY", "KRW"),
        "엔화 알려달라고": ("JPY", "KRW"),
        "엔화 알려줘": ("JPY", "KRW"),
        "엔만": ("JPY", "KRW"),
        "100엔이면 얼마야": ("JPY", "KRW"),
        "달러 환율": ("USD", "KRW"),
        "오늘 환율 어때": ("USD", "KRW"),
        "USD/JPY": ("USD", "JPY"),
        "JPYKRW=X": ("JPY", "KRW"),
        "유로 환율": ("EUR", "KRW"),
        "THB 환율": ("THB", "KRW"),
        "1달러 몇 원": ("USD", "KRW"),
    }
    for query, expected in cases.items():
        assert detect_fx_pair(query) == expected, query

    assert detect_fx_pair("엔비디아 주가") is None
    assert detect_fx_pair("삼성전자 시세") is None


def test_fx_user_reply_shows_yen_and_hundred_yen():
    text = format_fx_user_reply(_JPY_QUOTE, "환율 엔화 알려줘")
    assert "1엔" in text
    assert "9.1876원" in text
    assert "100엔" in text
    assert "918.76원" in text
    assert "2026-09-10T15:04:00" in text


def test_fx_user_reply_converts_amount_in_query():
    text = format_fx_user_reply(_JPY_QUOTE, "100엔이면 얼마야")
    assert "100엔" in text.replace(" ", "")
    assert "918.76" in text


def test_looks_like_fx_quote_rejects_equity():
    assert looks_like_fx_quote(_JPY_QUOTE) is True
    assert looks_like_fx_quote(
        {
            "status": "success",
            "symbol": "NVDA",
            "price": 180.0,
            "currency": "USD",
        }
    ) is False


def test_keyword_and_sanitize_keep_user_query_for_yen():
    handler = _handler()
    for query in (
        "환율 엔화 알려줘",
        "엔화 알려달라고",
        "엔만",
        "100엔이면 얼마야",
    ):
        plan = handler._detect_tools_by_keyword(query)
        assert plan, query
        assert plan[0]["tool_to_use"] == "get_stock_price", query
        assert plan[0]["parameters"]["user_query"] == query, query

    sanitized = handler._sanitize_tool_plan(
        "환율 엔화 알려줘",
        [
            {
                "tool_to_use": "get_stock_price",
                "parameters": {"symbol": "JPYKRW=X"},
            }
        ],
        rag_top_score=0.0,
        log_extra=None,
        trust_llm=True,
    )
    assert sanitized[0]["parameters"]["user_query"] == "환율 엔화 알려줘"
    assert sanitized[0]["parameters"]["symbol"] == "JPYKRW=X"


def test_prompt_evidence_includes_hundred_yen_so_llm_rounding_can_pass():
    evidence = AIHandler._format_tool_results_for_prompt(
        [{"tool_name": "get_stock_price", "result": _JPY_QUOTE}]
    )
    assert "9.1876" in evidence
    assert "918.76" in evidence
    assert "Yahoo Finance" not in evidence
    assert AIHandler._unsupported_finance_numbers(
        "엔화는 1엔에 9.1876원, 100엔이면 918.76원이에요.",
        evidence,
        "환율 엔화 알려줘",
    ) == []


def test_number_guard_falls_back_to_verified_fx_not_empty_snapshot():
    handler = _handler()
    rendered = handler._format_verified_quote_fallback(
        _JPY_QUOTE,
        None,
        query="환율 엔화 알려줘",
        note="뉴스 요약에서 원자료로 확인되지 않는 수치가 감지되어 해당 내용은 제외했어요.",
    )
    assert "1엔" in rendered
    assert "918.76원" in rendered
    assert "지금 시세를 제대로 확인하지 못했어요" not in rendered


def test_equity_quote_fallback_keeps_verified_price():
    text = format_quote_user_reply(
        {
            "status": "success",
            "symbol": "NVDA",
            "name": "NVIDIA",
            "price": 180.25,
            "currency": "USD",
            "change_percent": -1.2,
            "checked_at_kst": "2026-09-10T10:00:00",
        }
    )
    assert "NVIDIA" in text
    assert "180.25" in text
    assert "-1.20%" in text


def test_intent_analyzer_quote_params_never_drop_user_query():
    analyzer = IntentAnalyzer(db=None, llm_client=None, tools_cog=None)
    params = analyzer._quote_tool_parameters(
        "엔화 알려달라고",
        {"symbol": "JPY"},
    )
    assert params["user_query"] == "엔화 알려달라고"
    assert params["symbol"] == "JPY"

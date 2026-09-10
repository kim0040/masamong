"""사용자가 실제로 던지는 질문 단위로 기존 기능 라우팅·렌더를 고정합니다.

환율은 fx_reply_scenarios_spec이 맡습니다. 여기서는 날씨, 장소, 이미지,
미국 주식, 국장 거절, 시황, 웹검색, 잡담이 서로 섞이지 않는지를 봅니다.
"""

import pytest

from cogs.ai_handler import AIHandler
from cogs.tools_cog import ToolsCog
from utils.finance_query import KR_MARKET_UNSUPPORTED, format_quote_user_reply
from utils.intent_analyzer import IntentAnalyzer


def _handler() -> AIHandler:
    return AIHandler.__new__(AIHandler)


def _tool(plan: list[dict]) -> str:
    return str((plan or [{}])[0].get("tool_to_use") or "")


def test_weather_scenarios_use_kma_tool_and_day_offset():
    handler = _handler()
    cases = {
        "오늘 날씨 어때": 0,
        "내일 날씨": 1,
        "모레 서울 날씨 알려줘": 2,
        "우산 챙겨야 할까": 0,
        "내일 태풍 영향은 어때?": 1,
    }
    for query, offset in cases.items():
        plan = handler._detect_tools_by_keyword(query)
        assert _tool(plan) == "get_weather_forecast", query
        assert plan[0]["parameters"]["day_offset"] == offset, query


def test_weather_sanitize_fills_location_from_query_or_default():
    analyzer = IntentAnalyzer(db=None, llm_client=None, tools_cog=None)
    analyzer.location_cache = {"서울", "광양", "전주"}
    handler = _handler()
    handler.intent_analyzer = analyzer

    seoul = handler._sanitize_tool_plan(
        "모레 서울 날씨",
        [{"tool_to_use": "get_weather_forecast", "parameters": {}}],
        rag_top_score=0.0,
        log_extra=None,
        trust_llm=True,
    )
    assert seoul[0]["parameters"]["location"] == "서울"
    assert seoul[0]["parameters"]["day_offset"] == 2

    defaulted = handler._sanitize_tool_plan(
        "오늘 날씨",
        [{"tool_to_use": "get_weather_forecast", "parameters": {}}],
        rag_top_score=0.0,
        log_extra=None,
        trust_llm=True,
    )
    assert defaulted[0]["parameters"]["location"]
    assert defaulted[0]["parameters"]["day_offset"] == 0


def test_place_and_image_and_smalltalk_stay_on_their_lanes():
    handler = _handler()

    place = handler._detect_tools_by_keyword("홍대 근처 맛집 추천해줘")
    assert _tool(place) == "web_search"
    assert "맛집" in place[0]["parameters"]["query"]

    cafe = handler._detect_tools_by_keyword("강남 카페 어디가 괜찮아")
    assert _tool(cafe) == "web_search"

    image = handler._detect_tools_by_keyword("파란 고양이 그려줘")
    assert _tool(image) == "generate_image"
    assert image[0]["parameters"]["prompt"] == "파란 고양이 그려줘"

    sanitized_image = handler._sanitize_tool_plan(
        "파란 고양이 그려줘",
        [{"tool_to_use": "generate_image", "parameters": {}}],
        rag_top_score=0.0,
        log_extra=None,
        trust_llm=True,
    )
    assert sanitized_image[0]["parameters"]["prompt"] == "파란 고양이 그려줘"

    assert handler._detect_tools_by_keyword("안녕") == []
    assert handler._detect_tools_by_keyword("ㅋㅋ 뭐해") == []


def test_us_equity_and_crypto_go_to_quote_tool_not_fx():
    handler = _handler()
    for query in (
        "엔비디아 주가",
        "NVDA 시세 얼마야",
        "애플 현재 주가 알려줘",
        "비트코인 시세",
    ):
        plan = handler._detect_tools_by_keyword(query)
        assert _tool(plan) == "get_stock_price", query
        assert plan[0]["parameters"] == {"user_query": query}, query


def test_korean_listing_and_kospi_go_to_web_search_not_quote_tools():
    handler = _handler()
    for query in (
        "오늘 국장 어때",
        "코스피 지금 몇이야",
        "코스닥 시세",
        "005930.KS 주가",
        "국내 상장 종목 시세 알려줘",
    ):
        plan = handler._detect_tools_by_keyword(query)
        tools = [item["tool_to_use"] for item in plan]
        assert tools == ["web_search"], query
        assert "get_stock_price" not in tools
        assert "get_market_snapshot" not in tools

    samsung = handler._detect_tools_by_keyword("삼성전자 지금 주가 얼마야")
    assert _tool(samsung) == "get_stock_price"

    nasdaq = handler._detect_tools_by_keyword("나스닥 지금 어때")
    tools = [item["tool_to_use"] for item in nasdaq]
    assert "get_market_snapshot" in tools
    assert "web_search" in tools


def test_conceptual_finance_and_charts_stay_on_web_search():
    handler = _handler()
    for query in (
        "달러 강세인 이유가 뭐야",
        "금리가 오르면 환율은 어떻게 돼?",
        "엔비디아 주가 차트 보여줘",
        "테슬라 과거 주가 추이 보여줘",
    ):
        plan = handler._detect_tools_by_keyword(query)
        assert _tool(plan) == "web_search", query


def test_explicit_web_search_and_news_get_search_on_evidence_path():
    analyzer = IntentAnalyzer(db=None, llm_client=None, tools_cog=None)
    for query in (
        "이거 검색해줘",
        "최신 뉴스 찾아줘",
        "공식 문서 링크 있어?",
    ):
        plan = analyzer._enforce_evidence_tool_plan(
            query,
            "외부 자료 확인",
            analyzer._detect_tools_by_keyword(query),
            requires_external_evidence=True,
        )
        assert _tool(plan) == "web_search", query


def test_weather_and_equity_prompt_formatters_keep_verified_numbers():
    weather = AIHandler._format_tool_results_for_prompt(
        [
            {
                "tool_name": "get_weather_forecast",
                "result": {
                    "location": "광양",
                    "current_weather": "맑음 20도",
                    "forecast_items": [
                        {
                            "fcstTime": "1500",
                            "TMP": "21",
                            "SKY": "맑음",
                            "POP": "10",
                        }
                    ],
                },
            }
        ]
    )
    assert "광양" in weather
    assert "맑음 20도" in weather
    assert "21도" in weather

    equity = format_quote_user_reply(
        {
            "status": "success",
            "symbol": "NVDA",
            "name": "NVIDIA",
            "price": 180.25,
            "currency": "USD",
            "change_percent": 1.5,
            "checked_at_kst": "2026-09-10T10:00:00",
        }
    )
    assert "180.25" in equity
    assert "+1.50%" in equity
    assert "지금 시세를 제대로 확인하지 못했어요" not in equity


@pytest.mark.asyncio
async def test_korean_market_paths_are_rejected_without_provider_calls(monkeypatch):
    called = []

    class _Bot:
        def get_cog(self, _name):
            return None

    cog = ToolsCog.__new__(ToolsCog)
    cog.bot = _Bot()
    cog.weather_cog = None

    async def boom(*_args, **_kwargs):
        called.append(True)
        raise AssertionError("국장 경로는 외부 시세 API를 부르면 안 됩니다.")

    monkeypatch.setattr("utils.api_handlers.finnhub.lookup_quote", boom)
    monkeypatch.setattr(cog, "_load_yfinance_snapshot", boom)

    snapshot = await cog.get_market_snapshot("kr")
    assert snapshot["failure_kind"] == "unsupported_market"
    assert KR_MARKET_UNSUPPORTED in snapshot["error"]

    quote = await cog.get_stock_price(symbol="005930.KS")
    assert quote["failure_kind"] == "unsupported_market"
    assert not called

import pytest

import config
from cogs.ai_handler import AIHandler


def _build_handler_without_init() -> AIHandler:
    return AIHandler.__new__(AIHandler)


def test_select_tool_plan_routes_factual_query_to_web_search():
    handler = _build_handler_without_init()

    plan = handler._select_tool_plan_without_intent_llm(
        "아브라함 링컨은 언제 태어났어?",
        rag_top_score=0.1,
        log_extra=None,
    )

    assert isinstance(plan, list)
    assert plan
    assert plan[0]["tool_to_use"] == "web_search"


@pytest.mark.asyncio
async def test_should_use_web_search_for_factual_query_when_rag_is_weak():
    handler = _build_handler_without_init()

    should_search = await handler._should_use_web_search(
        "파이썬 3.14는 언제 출시돼?",
        rag_top_score=0.1,
        history=None,
    )

    assert should_search is True


def test_local_memory_query_is_not_treated_as_external_fact():
    handler = _build_handler_without_init()
    assert handler._looks_like_external_fact_query("내가 어제 말했던 계획 기억나?") is False


@pytest.mark.asyncio
async def test_fast_thinking_path_uses_routing_lane_only():
    handler = _build_handler_without_init()

    class _DummyBot:
        db = None

    handler.bot = _DummyBot()
    lane_calls = []
    routing_calls = []

    def _fake_get_lane_targets(lane: str, model_override=None):
        lane_calls.append((lane, model_override))
        if lane == "routing":
            return [{"name": "routing.primary"}]
        return [{"name": "main.primary"}]

    async def _fake_call_routing_lane_target(target, *, prompt: str, log_extra: dict):
        routing_calls.append((target.get("name"), prompt, dict(log_extra)))
        return "intent-json"

    handler._get_lane_targets = _fake_get_lane_targets
    handler._call_routing_lane_target = _fake_call_routing_lane_target

    result = await handler._cometapi_fast_generate_text(
        "이번 전북대 축제 라인업 어떰?",
        None,
        {"trace_id": "t1"},
        trace_key="cometapi_fast_intent",
    )

    assert result == "intent-json"
    assert lane_calls == [("routing", None)]
    assert routing_calls
    assert routing_calls[0][0] == "routing.primary"


@pytest.mark.asyncio
async def test_detect_tools_by_llm_runs_for_smalltalk_when_always_run_enabled(monkeypatch):
    handler = _build_handler_without_init()
    handler.use_cometapi = True

    monkeypatch.setattr(config, "INTENT_LLM_ENABLED", True)
    monkeypatch.setattr(config, "INTENT_LLM_ALWAYS_RUN", True)

    called = {"value": False}

    async def _fake_fast(prompt, model, log_extra, trace_key="cometapi_fast"):
        _ = prompt, model, log_extra, trace_key
        called["value"] = True
        return '{"intent":"인사/잡담","reasoning":"일반 대화","tools":[]}'

    handler._cometapi_fast_generate_text = _fake_fast

    plan = await handler._detect_tools_by_llm("안녕 마사몽", {"trace_id": "s1"}, history=None)

    assert called["value"] is True
    assert plan == []


def test_detect_tools_by_keyword_place_routes_to_web_search():
    handler = _build_handler_without_init()

    plan = handler._detect_tools_by_keyword("홍대 근처 맛집 추천해줘")

    assert isinstance(plan, list)
    assert plan
    assert plan[0]["tool_to_use"] == "web_search"
    assert "맛집" in plan[0]["parameters"]["query"]


def test_sanitize_tool_plan_filters_out_non_allowed_tools():
    handler = _build_handler_without_init()

    plan = handler._sanitize_tool_plan(
        "홍대 맛집 추천해줘",
        [
            {"tool_to_use": "search_for_place", "parameters": {"query": "홍대 맛집"}},
            {"tool_to_use": "generate_image", "parameters": {"user_query": "고양이"}},
            {"tool_to_use": "web_search", "parameters": {"query": "홍대 맛집"}},
        ],
        rag_top_score=0.2,
        log_extra=None,
    )

    assert len(plan) == 1
    assert plan[0]["tool_to_use"] == "web_search"


def test_sanitize_tool_plan_keeps_place_web_search_even_when_rag_is_strong():
    handler = _build_handler_without_init()

    plan = handler._sanitize_tool_plan(
        "홍대 근처 맛집 추천해줘",
        [{"tool_to_use": "web_search", "parameters": {"query": "홍대 근처 맛집 추천"}}],
        rag_top_score=config.RAG_STRONG_SIMILARITY_THRESHOLD + 0.1,
        log_extra=None,
    )

    assert plan
    assert plan[0]["tool_to_use"] == "web_search"


@pytest.mark.asyncio
async def test_execute_tool_rejects_disabled_tool():
    handler = _build_handler_without_init()
    handler.tools_cog = type("DummyTools", (), {})()

    result = await handler._execute_tool(
        {"tool_to_use": "generate_image", "parameters": {"user_query": "고양이"}},
        guild_id=0,
        user_query="고양이 그려줘",
        channel_id=0,
    )

    assert "비활성화" in result.get("error", "")


def test_finance_disambiguation_does_not_treat_apple_music_as_finance():
    handler = _build_handler_without_init()

    assert handler._looks_like_finance_query("애플 뮤직 호환성 문제에 대해 알려줘") is False
    assert handler._looks_like_external_fact_query("애플 뮤직 호환성 문제에 대해 알려줘") is True
    assert handler._detect_tools_by_keyword("애플 뮤직 호환성 문제에 대해 알려줘") == []


def test_finance_disambiguation_still_routes_real_stock_questions():
    handler = _build_handler_without_init()

    assert handler._looks_like_finance_query("애플 주가 알려줘") is True
    plan = handler._detect_tools_by_keyword("애플 주가 알려줘")
    assert plan
    assert plan[0]["tool_to_use"] == "web_search"
    assert "금융 뉴스" in plan[0]["parameters"]["query"]

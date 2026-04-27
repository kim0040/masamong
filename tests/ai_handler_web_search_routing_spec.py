import pytest

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

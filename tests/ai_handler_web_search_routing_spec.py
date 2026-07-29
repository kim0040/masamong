import pytest

import config
from cogs.ai_handler import AIHandler
from utils.intent_analyzer import IntentAnalyzer


def _build_handler_without_init() -> AIHandler:
    return AIHandler.__new__(AIHandler)


def test_routing_json_parser_accepts_reasoning_prefix_without_eval():
    result = IntentAnalyzer._parse_routing_json(
        '<think>짧은 판단</think>\n'
        '{"intent":"날씨","needs_memory":false,"tools":[]}'
    )

    assert result["intent"] == "날씨"
    assert result["needs_memory"] is False


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

    async def _fake_call_routing_lane_target(
        target,
        *,
        prompt: str,
        log_extra: dict,
        max_tokens=None,
    ):
        _ = max_tokens
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
async def test_detect_tools_by_llm_runs_for_smalltalk(monkeypatch):
    handler = _build_handler_without_init()
    handler.use_cometapi = True

    monkeypatch.setattr(config, "INTENT_LLM_ENABLED", True)

    called = {"value": False}

    async def _fake_fast(
        prompt,
        model,
        log_extra,
        trace_key="cometapi_fast",
        max_tokens=None,
    ):
        _ = prompt, model, log_extra, trace_key, max_tokens
        called["value"] = True
        return '{"intent":"인사/잡담","reasoning":"일반 대화","tools":[]}'

    handler._cometapi_fast_generate_text = _fake_fast

    plan = await handler._detect_tools_by_llm("안녕 마사몽", {"trace_id": "s1"}, history=None)

    assert called["value"] is True
    assert plan == []


@pytest.mark.asyncio
async def test_semantic_router_controls_tool_choice_without_keyword_override():
    handler = _build_handler_without_init()
    handler.use_cometapi = True
    captured = {}

    async def _fake_fast(
        prompt,
        model,
        log_extra,
        trace_key="cometapi_fast",
        max_tokens=None,
    ):
        _ = model, log_extra, trace_key
        captured["prompt"] = prompt
        captured["max_tokens"] = max_tokens
        return (
            '{"intent":"시각 자료 생성","needs_memory":false,'
            '"tools":[{"tool":"generate_image",'
            '"params":{"prompt":"도시 성장 과정을 보여주는 인포그래픽"}}]}'
        )

    handler._cometapi_fast_generate_text = _fake_fast
    decision = await handler._route_tools(
        "도시가 커지는 과정을 시각 자료로 부탁해",
        {"trace_id": "semantic-router"},
        history=[],
    )
    plan = handler._sanitize_tool_plan(
        "도시가 커지는 과정을 시각 자료로 부탁해",
        decision.plan,
        rag_top_score=0.0,
        trust_llm=decision.source == "llm",
    )

    assert decision.source == "llm"
    assert decision.needs_memory is True
    assert plan == [
        {
            "tool_to_use": "generate_image",
            "tool_name": "generate_image",
            "parameters": {
                "prompt": "도시 성장 과정을 보여주는 인포그래픽"
            },
        }
    ]
    assert "Examples:" not in captured["prompt"]
    assert "단어 포함 여부가 아니라" in captured["prompt"]
    assert captured["max_tokens"] == config.SEMANTIC_ROUTER_MAX_TOKENS


@pytest.mark.asyncio
async def test_semantic_router_can_request_memory_without_external_tool():
    handler = _build_handler_without_init()
    handler.use_cometapi = True

    async def _fake_fast(*_args, **_kwargs):
        return (
            '{"intent":"이전 계획 회상","needs_memory":true,'
            '"tools":[]}'
        )

    handler._cometapi_fast_generate_text = _fake_fast
    decision = await handler._route_tools(
        "전에 정한 여행 계획 다시 알려줘",
        {"trace_id": "memory-router"},
        history=[],
    )

    assert decision.source == "llm"
    assert decision.needs_memory is True
    assert decision.plan == []


@pytest.mark.asyncio
async def test_semantic_router_no_tool_is_not_replaced_by_keyword_plan():
    handler = _build_handler_without_init()
    handler.use_cometapi = True

    async def _fake_fast(*_args, **_kwargs):
        return (
            '{"intent":"검색 개념에 대한 일반 대화",'
            '"needs_memory":false,"tools":[]}'
        )

    handler._cometapi_fast_generate_text = _fake_fast
    decision = await handler._route_tools(
        "검색이라는 개념에 대한 네 생각만 말해줘",
        {"trace_id": "no-tool-router"},
        history=[],
    )

    assert decision.source == "llm"
    assert decision.plan == []


@pytest.mark.asyncio
async def test_semantic_router_compacts_only_older_history(monkeypatch):
    handler = _build_handler_without_init()
    handler.use_cometapi = True
    monkeypatch.setattr(config, "AI_CONTEXT_RECENT_TURNS", 4)
    monkeypatch.setattr(config, "AI_CONTEXT_COMPACTION_TRIGGER_CHARS", 1_000)
    monkeypatch.setattr(config, "AI_CONTEXT_COMPACTION_SOURCE_MAX_CHARS", 2_000)
    monkeypatch.setattr(config, "AI_CONTEXT_DIGEST_MAX_CHARS", 240)
    captured = {}

    history = [
        {
            "role": "user",
            "speaker": "민수",
            "is_current_user": True,
            "parts": [f"오래된 계획 {index} " + ("내용 " * 70)],
        }
        for index in range(7)
    ]
    history.extend(
        {
            "role": "model",
            "speaker": "Masamong",
            "parts": [f"최신 답변 {index}"],
        }
        for index in range(4)
    )

    async def _fake_fast(prompt, *_args, **_kwargs):
        captured["prompt"] = prompt
        return (
            '{"intent":"계획 이어가기","needs_memory":false,'
            '"context_digest":"민수는 자가용을 쓰지 않기로 했고 KTX 예약은 미정이다.",'
            '"tools":[]}'
        )

    handler._cometapi_fast_generate_text = _fake_fast
    decision = await handler._route_tools(
        "그 계획 계속 정리해줘",
        {"trace_id": "context-digest"},
        history=history,
    )

    assert decision.source == "llm"
    assert decision.context_digest == (
        "민수는 자가용을 쓰지 않기로 했고 KTX 예약은 미정이다."
    )
    assert "압축할 오래된 대화:" in captured["prompt"]
    assert "오래된 계획" in captured["prompt"]
    assert "최신 답변 3" in captured["prompt"]


@pytest.mark.asyncio
async def test_semantic_router_ignores_unsolicited_digest_for_short_history():
    handler = _build_handler_without_init()
    handler.use_cometapi = True

    async def _fake_fast(*_args, **_kwargs):
        return (
            '{"intent":"인사","needs_memory":false,'
            '"context_digest":"모델이 임의로 만든 오래된 사실","tools":[]}'
        )

    handler._cometapi_fast_generate_text = _fake_fast
    decision = await handler._route_tools(
        "안녕",
        {"trace_id": "no-context-digest"},
        history=[],
    )

    assert decision.context_digest == ""


def test_detect_tools_by_keyword_place_routes_to_web_search():
    handler = _build_handler_without_init()

    plan = handler._detect_tools_by_keyword("홍대 근처 맛집 추천해줘")

    assert isinstance(plan, list)
    assert plan
    assert plan[0]["tool_to_use"] == "web_search"
    assert "맛집" in plan[0]["parameters"]["query"]


def test_sanitize_tool_plan_keeps_supported_place_tool():
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

    assert {item["tool_to_use"] for item in plan} == {
        "search_for_place",
        "web_search",
    }
    place = next(item for item in plan if item["tool_to_use"] == "search_for_place")
    assert place["parameters"]["page_size"] == 5


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


@pytest.mark.asyncio
async def test_execute_web_search_raw_does_not_call_answer_llm():
    handler = _build_handler_without_init()

    class _Tools:
        async def web_search_rag(self, query, **_kwargs):
            assert query == "최신 모델"
            return {
                "status": "success",
                "context": "[출처 1] 공식 문서\n본문",
                "source_urls": ["https://example.com/docs"],
                "provider": "linkup",
            }

    handler.tools_cog = _Tools()
    handler._cometapi_generate_content = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("raw 검색 단계에서 LLM을 호출하면 안 됩니다")
    )

    result = await handler._execute_web_search_raw("최신 모델", {})

    assert result["result"] == "[출처 1] 공식 문서\n본문"
    assert result["source_urls"] == ["https://example.com/docs"]


def test_short_web_followup_uses_only_same_users_previous_turn():
    handler = _build_handler_without_init()
    history = [
        {
            "role": "user",
            "speaker": "다른 사람",
            "is_current_user": False,
            "parts": ["제주도 항공편"],
        },
        {
            "role": "user",
            "speaker": "질문자",
            "is_current_user": True,
            "parts": ["OpenAI 새 모델 발표 알려줘"],
        },
    ]

    query = handler._contextualize_web_query("가격은?", "가격은?", history)

    assert "OpenAI 새 모델" in query
    assert "제주도" not in query


def test_rag_search_context_is_derived_from_already_loaded_history():
    history = [
        {
            "role": "user",
            "speaker": "다른 사람",
            "is_current_user": False,
            "parts": ["다른 사람의 주제"],
        },
        {
            "role": "user",
            "speaker": "질문자",
            "is_current_user": True,
            "parts": ["내가 전에 정한 일정"],
        },
        {
            "role": "model",
            "speaker": "Masamong",
            "is_current_user": False,
            "parts": ["지난 답변"],
        },
    ]

    assert AIHandler._recent_search_messages_from_history(history) == [
        "내가 전에 정한 일정",
        "지난 답변",
    ]


def test_discord_source_footer_is_deduplicated_and_suppresses_embeds():
    footer = AIHandler._format_web_source_footer(
        [
            "https://a.example.com",
            "https://a.example.com",
            "javascript:alert(1)",
            "https://b.example.com",
        ]
    )

    assert footer == (
        "\n\n**출처**\n"
        "1. <https://a.example.com>\n"
        "2. <https://b.example.com>"
    )


def test_intent_analyzer_initializes_runtime_caches():
    analyzer = IntentAnalyzer(None, None, None)

    assert analyzer._auto_web_search_last_used == {}
    assert analyzer.location_cache == set()
    assert analyzer._location_cache_loaded is False

import pytest
from types import SimpleNamespace

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
async def test_main_wrapper_forwards_request_reasoning_without_extra_call():
    handler = _build_handler_without_init()
    captured = []

    class _LLMClient:
        async def generate_content(self, *args, **kwargs):
            captured.append((args, kwargs))
            return "완료"

    handler.llm_client = _LLMClient()
    result = await handler._cometapi_generate_content(
        "system",
        "user",
        {"trace_id": "dynamic-reasoning"},
        stop_on_bounded_failure=True,
        reasoning_effort_override="high",
    )

    assert result == "완료"
    assert len(captured) == 1
    assert captured[0][1]["reasoning_effort_override"] == "high"
    assert captured[0][1]["raise_on_bounded_failure"] is True


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
    assert decision.reasoning_level == "low"
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
    assert "서버 구성원·지인" in captured["prompt"]
    assert '"reasoning_level":"low"' in captured["prompt"]
    assert "여러 제약을 동시에 풀거나" in captured["prompt"]
    assert captured["max_tokens"] == config.SEMANTIC_ROUTER_MAX_TOKENS


@pytest.mark.asyncio
async def test_semantic_router_selects_high_reasoning_for_complex_request(
    monkeypatch,
):
    handler = _build_handler_without_init()
    handler.use_cometapi = True
    monkeypatch.setattr(config, "LLM_DYNAMIC_REASONING_ENABLED", True)
    monkeypatch.setattr(config, "LLM_DYNAMIC_REASONING_DEFAULT", "low")

    async def _fake_fast(*_args, **_kwargs):
        return (
            '{"intent":"충돌하는 기억과 제약 비교",'
            '"needs_memory":true,"reasoning_level":"high","tools":[]}'
        )

    handler._cometapi_fast_generate_text = _fake_fast
    decision = await handler._route_tools(
        "두 사람이 말한 일정이 충돌하는데 조건을 모두 맞춰 정리해줘",
        {"trace_id": "high-reasoning-router"},
        history=[],
    )

    assert decision.source == "llm"
    assert decision.reasoning_level == "high"


@pytest.mark.asyncio
async def test_semantic_router_invalid_reasoning_fails_down_to_low(
    monkeypatch,
):
    handler = _build_handler_without_init()
    handler.use_cometapi = True
    monkeypatch.setattr(config, "LLM_DYNAMIC_REASONING_ENABLED", True)
    monkeypatch.setattr(config, "LLM_DYNAMIC_REASONING_DEFAULT", "low")

    async def _fake_fast(*_args, **_kwargs):
        return (
            '{"intent":"일반 대화","needs_memory":false,'
            '"reasoning_level":"unlimited","tools":[]}'
        )

    handler._cometapi_fast_generate_text = _fake_fast
    decision = await handler._route_tools(
        "가볍게 이야기해줘",
        {"trace_id": "invalid-reasoning-router"},
        history=[],
    )

    assert decision.reasoning_level == "low"


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
async def test_market_news_router_cannot_skip_verified_market_tools():
    handler = _build_handler_without_init()
    handler.use_cometapi = True

    async def _fake_fast(*_args, **_kwargs):
        return (
            '{"intent":"오늘의 주요 주식 시황과 이슈 확인",'
            '"needs_memory":false,"requires_external_evidence":false,'
            '"tools":[]}'
        )

    handler._cometapi_fast_generate_text = _fake_fast
    decision = await handler._route_tools(
        "오늘 중요한 주식 소식 알려줘",
        {"trace_id": "market-missing-tools"},
        history=[],
    )

    assert decision.requires_external_evidence is True
    assert decision.needs_memory is False
    assert [item["tool_to_use"] for item in decision.plan] == [
        "get_market_snapshot",
        "web_search",
    ]
    assert decision.plan[0]["parameters"]["region"] == "kr"
    search_query = decision.plan[1]["parameters"]["query"]
    assert "대상 시장: 한국 증시" in search_query
    assert "기준일:" in search_query
    assert "거래소" in search_query
    assert "커뮤니티" in search_query


@pytest.mark.asyncio
async def test_short_us_market_followup_uses_us_snapshot_even_if_router_only_searches():
    handler = _build_handler_without_init()
    handler.use_cometapi = True

    async def _fake_fast(*_args, **_kwargs):
        return (
            '{"intent":"미국 주식 시장의 오늘 주요 흐름과 소식",'
            '"needs_memory":true,"requires_external_evidence":true,'
            '"tools":[{"tool":"web_search","params":{"query":"미국 증시 오늘"}}]}'
        )

    handler._cometapi_fast_generate_text = _fake_fast
    decision = await handler._route_tools(
        "미국은?",
        {"trace_id": "us-followup"},
        history=[
            {
                "role": "user",
                "speaker": "질문자",
                "is_current_user": True,
                "parts": ["오늘 중요한 주식 소식 알려줘"],
            }
        ],
    )

    assert decision.needs_memory is False
    assert [item["tool_to_use"] for item in decision.plan] == [
        "get_market_snapshot",
        "web_search",
    ]
    assert decision.plan[0]["parameters"]["region"] == "us"
    assert "대상 시장: 미국 증시" in decision.plan[1]["parameters"]["query"]
    assert "한국 증시" not in decision.plan[1]["parameters"]["query"]


@pytest.mark.asyncio
async def test_external_claim_verification_cannot_end_with_empty_tool_plan():
    handler = _build_handler_without_init()
    handler.use_cometapi = True

    async def _fake_fast(*_args, **_kwargs):
        return (
            '{"intent":"여수에서 엑스포 영향으로 버스 외형 변화가 있는지 확인",'
            '"needs_memory":true,"requires_external_evidence":false,'
            '"tools":[]}'
        )

    handler._cometapi_fast_generate_text = _fake_fast
    decision = await handler._route_tools(
        "여수 버스 생김새 바뀐 거 알아? 엑스포 때문에",
        {"trace_id": "bus-fact-check"},
        history=[],
    )

    assert decision.requires_external_evidence is True
    assert decision.needs_memory is False
    assert [item["tool_to_use"] for item in decision.plan] == ["web_search"]
    assert "여수 버스" in decision.plan[0]["parameters"]["query"]


@pytest.mark.asyncio
async def test_semantic_router_compacts_only_older_history(monkeypatch):
    handler = _build_handler_without_init()
    handler.use_cometapi = True
    monkeypatch.setattr(config, "AI_CONTEXT_RECENT_TURNS", 4)
    monkeypatch.setattr(config, "AI_CONTEXT_COMPACTION_TRIGGER_CHARS", 1_000)
    monkeypatch.setattr(config, "AI_CONTEXT_COMPACTION_SOURCE_MAX_CHARS", 2_000)
    monkeypatch.setattr(config, "AI_CONTEXT_DIGEST_MAX_CHARS", 240)
    monkeypatch.setattr(config, "SEMANTIC_ROUTER_MAX_TOKENS", 384)
    monkeypatch.setattr(
        config,
        "SEMANTIC_ROUTER_COMPACTION_MAX_TOKENS",
        768,
    )
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
        captured["max_tokens"] = _kwargs.get("max_tokens")
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
    assert captured["max_tokens"] == 768


@pytest.mark.asyncio
async def test_semantic_router_marks_fortune_context_only_when_requested():
    handler = _build_handler_without_init()
    handler.use_cometapi = True

    async def _fake_fast(*_args, **_kwargs):
        return (
            '{"intent":"직전 운세 기반 조언","needs_memory":false,'
            '"needs_fortune_context":true,'
            '"requires_external_evidence":false,"tools":[]}'
        )

    handler._cometapi_fast_generate_text = _fake_fast
    decision = await handler._route_tools(
        "아까 본 운세를 토대로 오늘 조언해줘",
        {"trace_id": "fortune-context"},
        history=[],
    )

    assert decision.needs_fortune_context is True


@pytest.mark.asyncio
async def test_bare_identity_question_prefers_scoped_memory_over_web():
    handler = _build_handler_without_init()
    handler.use_cometapi = True

    async def _fake_fast(*_args, **_kwargs):
        return (
            '{"intent":"특정 인물의 신원 확인","needs_memory":false,'
            '"needs_fortune_context":false,'
            '"requires_external_evidence":true,'
            '"tools":[{"tool":"web_search",'
            '"params":{"query":"김재원 누구"}}]}'
        )

    handler._cometapi_fast_generate_text = _fake_fast
    decision = await handler._route_tools(
        "김재원이 누구야?",
        {"trace_id": "bare-identity"},
        history=[],
    )

    assert decision.plan == []
    assert decision.needs_memory is True
    assert decision.requires_external_evidence is False


def test_described_or_explicitly_searched_identity_is_not_forced_local():
    assert not IntentAnalyzer._looks_like_bare_identity_question(
        "축구선수 호날두가 누구야?"
    )
    assert not IntentAnalyzer._looks_like_bare_identity_question(
        "김재원이 누군지 웹에서 검색해줘"
    )


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


def test_no_tool_conversation_gets_bounded_passive_memory_search(monkeypatch):
    monkeypatch.setattr(config, "RAG_PASSIVE_NO_TOOL_SEARCH_ENABLED", True)

    assert not AIHandler._should_search_memory(
        SimpleNamespace(needs_memory=False, plan=[], source="llm")
    )
    assert AIHandler._should_search_memory(
        SimpleNamespace(
            needs_memory=False,
            plan=[],
            source="error_fallback",
        )
    )
    assert not AIHandler._should_search_memory(
        SimpleNamespace(
            needs_memory=False,
            plan=[{"tool_to_use": "get_weather_forecast"}],
            source="error_fallback",
        )
    )
    assert AIHandler._should_search_memory(
        SimpleNamespace(
            needs_memory=True,
            plan=[{"tool_to_use": "generate_image"}],
            source="llm",
        )
    )


def test_final_history_keeps_short_source_window_without_digest(monkeypatch):
    monkeypatch.setattr(config, "AI_CONTEXT_SOURCE_HISTORY_LIMIT", 24)
    monkeypatch.setattr(config, "AI_CONTEXT_RECENT_TURNS", 8)
    history = [
        {"role": "user", "parts": [f"짧은 메시지 {index}"]}
        for index in range(24)
    ]

    kept = AIHandler._select_final_history(
        history,
        SimpleNamespace(context_digest=""),
    )
    compacted = AIHandler._select_final_history(
        history,
        SimpleNamespace(context_digest="압축됨"),
    )

    assert len(kept) == 24
    assert len(compacted) == 8


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


@pytest.mark.asyncio
async def test_execute_tool_does_not_promote_failure_text_to_evidence():
    handler = _build_handler_without_init()

    class _DummyTools:
        async def get_stock_price(self, **_parameters):
            return "'AAPL' 조회 실패: 시세 정보를 가져올 수 없습니다."

        async def execute_guarded(self, _tool_name, operation):
            return await operation()

        @staticmethod
        def result_has_external_evidence(tool_name, result):
            from cogs.tools_cog import ToolsCog

            return ToolsCog.result_has_external_evidence(tool_name, result)

    handler.tools_cog = _DummyTools()

    result = await handler._execute_tool(
        {
            "tool_to_use": "get_stock_price",
            "parameters": {"symbol": "AAPL"},
        },
        guild_id=1,
        user_query="애플 주가 알려줘",
        channel_id=2,
        user_id=3,
    )

    assert "error" in result
    assert "result" not in result


def test_external_evidence_predicate_rejects_empty_or_error_results():
    from cogs.tools_cog import ToolsCog

    assert (
        ToolsCog.result_has_external_evidence(
            "get_stock_price",
            "'AAPL' 조회 실패: 시세 정보를 가져올 수 없습니다.",
        )
        is False
    )
    assert (
        ToolsCog.result_has_external_evidence(
            "get_stock_price",
            "AAPL 현재가: 215.32 USD (+1.20%)",
        )
        is True
    )
    assert (
        ToolsCog.result_has_external_evidence(
            "get_market_snapshot",
            {"error": "provider unavailable"},
        )
        is False
    )


def test_finance_disambiguation_does_not_treat_apple_music_as_finance():
    handler = _build_handler_without_init()

    assert handler._looks_like_finance_query("애플 뮤직 호환성 문제에 대해 알려줘") is False
    assert handler._looks_like_external_fact_query("애플 뮤직 호환성 문제에 대해 알려줘") is True
    assert handler._detect_tools_by_keyword("애플 뮤직 호환성 문제에 대해 알려줘") == []


def test_visual_material_is_not_misclassified_as_opening_price():
    handler = _build_handler_without_init()

    assert handler._looks_like_finance_query(
        "도시 성장 과정을 시각 자료로 보여줘"
    ) is False


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
        "\n\n📰 **뉴스 출처**\n"
        "1. <https://a.example.com>\n"
        "2. <https://b.example.com>"
    )


@pytest.mark.asyncio
async def test_news_sources_are_hidden_until_user_reacts_and_hidden_again():
    class _Message:
        def __init__(self, message_id, content):
            self.id = message_id
            self.content = content
            self.reactions = []
            self.added_reactions = []
            self.channel = None

        async def add_reaction(self, emoji):
            self.added_reactions.append(emoji)
            self.reactions = [SimpleNamespace(emoji=emoji, count=1)]

        async def edit(self, *, content, **_kwargs):
            self.content = content
            return self

    long_message = _Message(10, "긴 답변 " * 150)
    short_message = _Message(11, "마지막 답변")

    class _Channel:
        async def fetch_message(self, message_id):
            assert message_id == short_message.id
            return short_message

    channel = _Channel()
    long_message.channel = channel
    short_message.channel = channel
    handler = object.__new__(AIHandler)
    handler.bot = SimpleNamespace(
        user=SimpleNamespace(id=999),
        get_channel=lambda channel_id: channel if channel_id == 22 else None,
    )
    handler._news_source_cache = {}
    handler._news_source_locks = {}

    anchor = await handler._register_news_source_reaction(
        [long_message, short_message],
        ["https://news.example/a", "https://news.example/a"],
    )

    assert anchor is short_message
    assert short_message.content == "마지막 답변"
    assert short_message.added_reactions == ["📰"]
    assert handler._news_source_cache[short_message.id] == [
        "https://news.example/a"
    ]

    short_message.reactions[0].count = 2
    payload = SimpleNamespace(
        user_id=123,
        channel_id=22,
        message_id=short_message.id,
        emoji="📰",
    )
    await handler.on_raw_reaction_add(payload)
    assert "📰 **뉴스 출처**" in short_message.content
    assert "<https://news.example/a>" in short_message.content

    short_message.reactions[0].count = 1
    await handler.on_raw_reaction_remove(payload)
    assert short_message.content == "마지막 답변"


@pytest.mark.asyncio
async def test_bot_news_reaction_does_not_reveal_sources():
    handler = object.__new__(AIHandler)
    handler.bot = SimpleNamespace(user=SimpleNamespace(id=999))
    handler._news_source_cache = {10: ["https://news.example/a"]}
    handler._news_source_locks = {}

    await handler.on_raw_reaction_add(
        SimpleNamespace(
            user_id=999,
            channel_id=22,
            message_id=10,
            emoji="📰",
        )
    )

    assert handler._news_source_cache[10] == ["https://news.example/a"]


def test_intent_analyzer_initializes_runtime_caches():
    analyzer = IntentAnalyzer(None, None, None)

    assert analyzer._auto_web_search_last_used == {}
    assert analyzer.location_cache == set()
    assert analyzer._location_cache_loaded is False


def test_finance_numeric_grounding_rejects_numbers_missing_from_tool_evidence():
    evidence = (
        "[get_market_snapshot] 코스피: 5,663.24, -360.42 (-5.98%), "
        "최신 가용 거래일 2026-07-29"
    )

    assert AIHandler._unsupported_finance_numbers(
        "코스피는 5,663.24로 -5.98% 하락했어요.",
        evidence,
    ) == []
    unsupported = AIHandler._unsupported_finance_numbers(
        "코스피는 2,710.24로 -0.12% 하락했어요.",
        evidence,
    )
    assert 2710.24 in unsupported
    assert -0.12 in unsupported


def test_market_snapshot_fallback_uses_only_verified_values():
    text = AIHandler._format_market_snapshot_fallback(
        {
            "indices": [
                {
                    "name": "코스피",
                    "market_date": "2026-07-29",
                    "value": 5663.24,
                    "change": -360.42,
                    "change_percent": -5.9849,
                }
            ]
        },
        note="뉴스 검색은 실패했어요.",
    )

    assert "5,663.24" in text
    assert "-5.98%" in text
    assert "2026-07-29" in text
    assert "뉴스 검색은 실패했어요." in text


def test_unexecuted_future_search_promise_is_replaced_but_verified_answer_is_kept():
    promise = "나도 한번 찾아볼게! 다음에 알려줄게."

    replaced = AIHandler._replace_unexecuted_lookup_promise(
        promise,
        has_external_evidence=False,
    )
    kept = AIHandler._replace_unexecuted_lookup_promise(
        promise,
        has_external_evidence=True,
    )

    assert "실제로 확인하지 못했어요" in replaced
    assert "찾아볼게" not in replaced
    assert kept == promise

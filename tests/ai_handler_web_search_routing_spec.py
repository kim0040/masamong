import logging

import pytest
from types import SimpleNamespace

import config
from cogs.ai_handler import AIHandler
from utils.intent_analyzer import IntentAnalyzer


def _build_handler_without_init() -> AIHandler:
    return AIHandler.__new__(AIHandler)


def test_tool_outcome_logging_is_bounded_and_omits_result_content(caplog):
    with caplog.at_level(logging.INFO):
        AIHandler._log_tool_execution_outcome(
            "web_search",
            {
                "result": "사용자 질문과 검색 본문은 로그에 남기면 안 됩니다.",
                "source_urls": ["https://example.com/a", "https://example.com/b"],
            },
            {"trace_id": "tool-success"},
            duration_ms=123,
            step=1,
            step_count=2,
        )
        AIHandler._log_tool_execution_outcome(
            "get_weather_forecast",
            {
                "error": "공급자 원문 오류도 로그에 복사하지 않습니다.",
                "failure_kind": "empty_result",
            },
            {"trace_id": "tool-failure"},
            duration_ms=456,
            step=2,
            step_count=2,
        )

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert (
        "tool=web_search outcome=succeeded "
        "duration_ms=123 source_count=2"
        in rendered
    )
    assert (
        "tool=get_weather_forecast outcome=failed "
        "failure_kind=empty_result duration_ms=456"
        in rendered
    )
    success_record, failure_record = caplog.records[-2:]
    assert success_record.event == "tool_execution_completed"
    assert success_record.step == 1
    assert success_record.step_count == 2
    assert failure_record.failure_kind == "empty_result"
    assert "사용자 질문" not in rendered
    assert "공급자 원문 오류" not in rendered


def test_agent_terminal_log_is_structured_and_content_free(caplog):
    with caplog.at_level(logging.INFO):
        AIHandler._log_agent_execution_outcome(
            {"trace_id": "agent-finish"},
            started_at=0.0,
            outcome="failed",
            stage="delivery",
            tool_count=2,
            error_kind="HTTPException",
        )

    record = caplog.records[-1]
    assert record.event == "agent_completed"
    assert record.outcome == "failed"
    assert record.stage == "delivery"
    assert record.tool_count == 2
    assert record.error_kind == "HTTPException"
    assert "query" not in record.getMessage().lower()


def test_malformed_legacy_tool_plan_log_does_not_copy_payload(caplog):
    handler = _build_handler_without_init()
    sensitive_payload = '{"tool_name":"web_search","query":"민감한 원문",}'

    with caplog.at_level(logging.WARNING):
        result = handler._parse_tool_calls(
            f"<tool_call>{sensitive_payload}</tool_call>"
        )

    assert result == []
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert "민감한 원문" not in rendered
    assert "payload_chars=" in rendered


def test_final_reasoning_progress_text_distinguishes_high_requests():
    assert (
        AIHandler._final_reasoning_progress_text("high")
        == "🧠 마사몽이 여러 내용을 살펴보며 조금 더 오래 고민 중이에요..."
    )
    assert "답변을 작성 중" in AIHandler._final_reasoning_progress_text("low")
    assert "답변을 작성 중" in AIHandler._final_reasoning_progress_text(None)


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


@pytest.mark.asyncio
async def test_semantic_router_preserves_bounded_linkup_depth_hint():
    handler = _build_handler_without_init()
    handler.use_cometapi = True
    captured_prompt = {}

    async def _fake_fast(prompt, *_args, **_kwargs):
        captured_prompt["text"] = prompt
        return (
            '{"intent":"오늘 발표된 단일 결과 확인",'
            '"needs_memory":false,"requires_external_evidence":true,'
            '"reasoning_level":"low",'
            '"tools":[{"tool":"web_search","params":{'
            '"query":"오늘 삼성전기 실적 발표 결과","depth":"fast"}}]}'
        )

    handler._cometapi_fast_generate_text = _fake_fast
    decision = await handler._route_tools(
        "오늘 삼성전기 실적 발표 결과 확인해줘",
        {"trace_id": "semantic-depth"},
        history=[],
    )

    assert decision.plan == [
        {
            "tool_to_use": "web_search",
            "tool_name": "web_search",
            "parameters": {
                "query": "오늘 삼성전기 실적 발표 결과",
                "depth": "fast",
            },
        }
    ]
    assert "fast는 한 대상의 한 가지 수치·결과" in captured_prompt["text"]
    assert "단순 최신 질문을 deep으로 올리지 않는다" in captured_prompt["text"]


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
    assert decision.reasoning_level == "none"
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
    assert '"reasoning_level":"none"' in captured["prompt"]
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
async def test_shared_history_and_external_evidence_are_kept_separate():
    handler = _build_handler_without_init()
    handler.use_cometapi = True
    call_count = 0
    captured = {}

    async def _fake_fast(
        prompt,
        *_args,
        **_kwargs,
    ):
        nonlocal call_count
        call_count += 1
        captured["prompt"] = prompt
        return (
            '{"intent":"이전에 함께 말한 서비스 증설과 현재 장애 원인 검증",'
            '"needs_memory":false,"references_shared_history":false,'
            '"requires_external_evidence":false,"reasoning_level":"low",'
            '"tools":[]}'
        )

    handler._cometapi_fast_generate_text = _fake_fast
    decision = await handler._route_tools(
        "얘네 서버 증설 엄청 했잖아. 그런데도 계속 장애가 난다고?",
        {"trace_id": "shared-history-and-web"},
        history=[],
    )

    assert call_count == 1
    assert decision.references_shared_history is True
    assert decision.needs_memory is True
    assert decision.requires_external_evidence is True
    assert decision.reasoning_level == "high"
    assert [item["tool_to_use"] for item in decision.plan] == ["web_search"]
    assert len(
        [
            item
            for item in decision.plan
            if item["tool_to_use"] == "web_search"
        ]
    ) == 1
    assert '"references_shared_history":false' in captured["prompt"]
    assert "기억은 과거 대화의 연속성에, 웹은 외부 사실 검증에 쓴다" in captured[
        "prompt"
    ]


@pytest.mark.asyncio
async def test_shared_history_flag_enables_memory_without_forcing_web():
    handler = _build_handler_without_init()
    handler.use_cometapi = True

    async def _fake_fast(*_args, **_kwargs):
        return (
            '{"intent":"이전에 함께 정한 모임 규칙 회상",'
            '"needs_memory":false,"references_shared_history":true,'
            '"requires_external_evidence":false,"reasoning_level":"low",'
            '"tools":[]}'
        )

    handler._cometapi_fast_generate_text = _fake_fast
    decision = await handler._route_tools(
        "우리 그때 정한 규칙 있었잖아. 뭐였지?",
        {"trace_id": "shared-history-only"},
        history=[],
    )

    assert decision.references_shared_history is True
    assert decision.needs_memory is True
    assert decision.requires_external_evidence is False
    assert decision.plan == []
    assert decision.reasoning_level == "low"


@pytest.mark.asyncio
async def test_rhetorical_common_knowledge_does_not_trigger_shared_memory():
    handler = _build_handler_without_init()
    handler.use_cometapi = True

    async def _fake_fast(*_args, **_kwargs):
        return (
            '{"intent":"안정적인 일반 상식 설명",'
            '"needs_memory":false,"references_shared_history":false,'
            '"requires_external_evidence":false,"reasoning_level":"low",'
            '"tools":[]}'
        )

    handler._cometapi_fast_generate_text = _fake_fast
    decision = await handler._route_tools(
        "지구는 둥글잖아. 쉽게 설명해줘.",
        {"trace_id": "rhetorical-common-knowledge"},
        history=[],
    )

    assert decision.references_shared_history is False
    assert decision.needs_memory is False
    assert decision.plan == []


@pytest.mark.asyncio
async def test_realtime_public_cause_cannot_skip_external_evidence():
    handler = _build_handler_without_init()
    handler.use_cometapi = True

    async def _fake_fast(*_args, **_kwargs):
        return (
            '{"intent":"새 서비스 지연 원인 설명",'
            '"needs_memory":false,"references_shared_history":false,'
            '"requires_external_evidence":false,"reasoning_level":"low",'
            '"tools":[]}'
        )

    handler._cometapi_fast_generate_text = _fake_fast
    decision = await handler._route_tools(
        "오늘 공개된 새 AI 서비스가 계속 느리다는데 원인이 뭐야?",
        {"trace_id": "realtime-public-cause"},
        history=[],
    )

    assert decision.references_shared_history is False
    assert decision.needs_memory is False
    assert decision.requires_external_evidence is True
    assert [item["tool_to_use"] for item in decision.plan] == ["web_search"]


def test_shared_history_structural_guard_avoids_generic_rhetorical_claims():
    assert IntentAnalyzer._looks_like_shared_history_reference(
        "얘네 서버 증설 엄청 했잖아. 그런데도 장애가 난다고?"
    )
    assert IntentAnalyzer._looks_like_shared_history_reference(
        "우리 그때 정한 규칙 있었잖아"
    )
    assert IntentAnalyzer._looks_like_shared_history_reference(
        "내가 전에 말했던 여행지 기억나?"
    )
    assert not IntentAnalyzer._looks_like_shared_history_reference(
        "지구는 둥글잖아. 쉽게 설명해줘"
    )
    assert not IntentAnalyzer._looks_like_shared_history_reference(
        "공룡은 멸종했잖아. 왜 멸종했어?"
    )


def test_emergency_router_preserves_shared_memory_and_external_verification():
    handler = _build_handler_without_init()
    analyzer = handler._ensure_intent_analyzer()

    decision = analyzer._emergency_routing_decision(
        "얘네 서버 증설 엄청 했잖아. 그런데도 계속 장애가 난 원인이 뭐야?",
        source="fallback",
    )

    assert decision.references_shared_history is True
    assert decision.needs_memory is True
    assert decision.requires_external_evidence is True
    assert [item["tool_to_use"] for item in decision.plan] == ["web_search"]


def test_emergency_router_verifies_public_incident_with_explicit_memory_hint():
    handler = _build_handler_without_init()
    analyzer = handler._ensure_intent_analyzer()

    decision = analyzer._emergency_routing_decision(
        "저번에 말한 서비스 장애 원인이 지금도 같은지 알려줘",
        source="fallback",
    )

    assert decision.references_shared_history is True
    assert decision.needs_memory is True
    assert decision.requires_external_evidence is True
    assert [item["tool_to_use"] for item in decision.plan] == ["web_search"]


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
    assert decision.reasoning_level == "high"
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
    assert decision.reasoning_level == "high"
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
    assert decision.reasoning_level == "high"


@pytest.mark.asyncio
async def test_scoped_person_profile_does_not_leak_into_public_web_search():
    handler = _build_handler_without_init()
    handler.use_cometapi = True

    async def _fake_fast(*_args, **_kwargs):
        return (
            '{"intent":"서버 내부 인물의 스펙 브리핑","needs_memory":true,'
            '"needs_fortune_context":false,'
            '"requires_external_evidence":true,"reasoning_level":"low",'
            '"tools":[{"tool":"web_search",'
            '"params":{"query":"김재원 스펙"}}]}'
        )

    handler._cometapi_fast_generate_text = _fake_fast
    decision = await handler._route_tools(
        "김재원 스펙 브리핑해줘",
        {"trace_id": "scoped-person-profile"},
        history=[],
    )

    assert decision.plan == []
    assert decision.needs_memory is True
    assert decision.requires_external_evidence is False


@pytest.mark.asyncio
async def test_explicit_public_person_research_keeps_web_search():
    handler = _build_handler_without_init()
    handler.use_cometapi = True

    async def _fake_fast(*_args, **_kwargs):
        return (
            '{"intent":"서버에서 언급된 인물의 공개 수상 기록 조사",'
            '"needs_memory":true,"references_shared_history":true,'
            '"requires_external_evidence":true,"reasoning_level":"high",'
            '"tools":[{"tool":"web_search",'
            '"params":{"query":"가천대 홍민석 ICPC 뉴스"}}]}'
        )

    handler._cometapi_fast_generate_text = _fake_fast
    decision = await handler._route_tools(
        "가천대 홍민석 ICPC 뉴스 찾아줘",
        {"trace_id": "public-person-research"},
        history=[],
    )

    assert [item["tool_to_use"] for item in decision.plan] == ["web_search"]
    assert decision.needs_memory is True
    assert decision.requires_external_evidence is True


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


def test_detect_tools_by_keyword_typhoon_routes_to_kma_weather():
    handler = _build_handler_without_init()

    plan = handler._detect_tools_by_keyword("내일 태풍 영향은 어때?")

    assert plan
    assert plan[0]["tool_to_use"] == "get_weather_forecast"
    assert plan[0]["parameters"]["day_offset"] == 1


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


def test_main_prompt_labels_memory_as_unverified_conversation():
    handler = _build_handler_without_init()
    message = SimpleNamespace(
        author=SimpleNamespace(display_name="질문자"),
    )

    prompt = handler._compose_main_prompt(
        message,
        user_query="전에 이야기했던 서버 증설 내용 기억나?",
        rag_blocks=["User(질문자): 예전에 서버 증설 이야기를 나눴다."],
        tool_results_block=None,
        recent_history=[],
    )

    assert "[과거 대화 기억 (선택 참고)]" in prompt
    assert "당시 대화 기록이며 외부 사실을 검증한 자료가 아닙니다" in prompt


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


@pytest.mark.asyncio
async def test_ticker_extraction_uses_fast_bounded_lane():
    handler = AIHandler.__new__(AIHandler)
    handler.use_cometapi = True

    async def _fast(
        prompt,
        model,
        log_extra,
        *,
        trace_key,
        max_tokens,
    ):
        assert "Never invent an ADR" in prompt
        assert model is None
        assert log_extra["mode"] == "ticker_extraction"
        assert trace_key == "ticker_extraction"
        assert max_tokens == 24
        return "AAPL"

    handler._cometapi_fast_generate_text = _fast

    ticker = await handler.extract_ticker_with_llm(
        "애플 현재 주가 알려줘"
    )

    assert ticker == "AAPL"


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
    assert (
        ToolsCog.result_has_external_evidence(
            "get_stock_price",
            {
                "status": "success",
                "price": 215.32,
                "source_urls": [
                    "https://finance.yahoo.com/quote/AAPL/"
                ],
            },
        )
        is True
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


def test_stock_chart_request_is_redirected_to_web_capability():
    handler = _build_handler_without_init()

    plan = handler._sanitize_tool_plan(
        "T3 디펜스 주가 일봉 그래프를 토스증권 API로 보여줘",
        [
            {
                "tool_to_use": "get_stock_price",
                "parameters": {"user_query": "T3 디펜스"},
            }
        ],
        rag_top_score=0.0,
        log_extra=None,
        trust_llm=True,
    )

    assert len(plan) == 1
    assert plan[0]["tool_to_use"] == "web_search"
    assert "추측하지 말고" in plan[0]["parameters"]["query"]


def test_plain_current_stock_request_keeps_quote_tool():
    handler = _build_handler_without_init()

    plan = handler._sanitize_tool_plan(
        "애플 현재 주가 알려줘",
        [
            {
                "tool_to_use": "get_stock_price",
                "parameters": {"symbol": "AAPL"},
            }
        ],
        rag_top_score=0.0,
        log_extra=None,
        trust_llm=True,
    )

    assert plan == [
        {
            "tool_to_use": "get_stock_price",
            "tool_name": "get_stock_price",
            "parameters": {"symbol": "AAPL"},
        }
    ]


@pytest.mark.asyncio
async def test_execute_web_search_raw_does_not_call_answer_llm():
    handler = _build_handler_without_init()

    class _Tools:
        async def web_search_rag(self, query, **_kwargs):
            assert query == "최신 모델"
            assert _kwargs["depth_hint"] == "fast"
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

    result = await handler._execute_web_search_raw(
        "최신 모델",
        {},
        depth_hint="fast",
    )

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


def test_finance_numeric_grounding_allows_verified_exchange_arithmetic():
    evidence = (
        "[web_search] USD/KRW 매매기준율은 1달러당 "
        "1,385.50원으로 확인됨"
    )
    query = "957달러를 현재 환율로 원화 환산하면 얼마야?"
    converted = 957 * 1385.50

    assert AIHandler._unsupported_finance_numbers(
        f"957달러는 약 {converted:,.0f}원이에요.",
        evidence,
        query,
    ) == []
    assert AIHandler._unsupported_finance_numbers(
        "957달러는 2,000,000원이에요.",
        evidence,
        query,
    ) == [2000000.0]


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

    # 검색을 약속하는 대신, 확인되지 않았다는 사실을 그대로 밝혀야 한다.
    assert "지금 확인이 안 됐어요" in replaced
    assert "찾아볼게" not in replaced
    assert kept == promise


def _analyzer() -> IntentAnalyzer:
    return object.__new__(IntentAnalyzer)


# 어휘 표 보정이 장난까지 "검증 필수"로 올리면, 조회할 공개 자료가 없으므로
# fail-closed 문구가 나가고 사용자에게는 봇이 농담을 거부한 것으로 보인다.
_PLAYFUL_QUERIES = (
    "야 너 스펙이 어떻게 됨?",
    "우리 중에 누가 더 빠름?",
    "나 오늘 최고 기록 세웠다 ㅋㅋ",
    "이 가격 실화냐고",
    "너 출력 좀 올려봐라",
    "마사몽 제원 좀 알려줘 ㅋㅋ",
    "야 나랑 너랑 성능 비교하면?",
    "내 연애 기록 좀 평가해줘",
)

_FACTUAL_QUERIES = (
    "아이폰 17 프로 가격 얼마야",
    "M4랑 M3 성능 비교해줘",
    "포르쉐 911 제로백 몇 초야",
    "쏘나타 연비 얼마나 나옴?",
)


@pytest.mark.parametrize("query", _PLAYFUL_QUERIES)
def test_playful_query_is_not_forced_into_external_verification(query):
    """라우터가 잡담이라고 판정하면 어휘 표로 뒤집지 않는다."""
    analyzer = _analyzer()

    assert (
        analyzer._derive_external_evidence_requirement(
            query,
            intent="인사/잡담",
            declared=False,
        )
        is False
    )


@pytest.mark.parametrize("query", _PLAYFUL_QUERIES)
def test_playful_query_still_guarded_when_router_output_is_missing(query):
    """라우터 응답이 없으면 기존 어휘 안전망이 그대로 동작해야 한다."""
    analyzer = _analyzer()

    assert analyzer._derive_external_evidence_requirement(
        query,
        intent="",
        declared=None,
    )


@pytest.mark.parametrize("query", _FACTUAL_QUERIES)
def test_factual_query_keeps_verification_even_if_router_declines(query):
    """의도가 사실 조회면 declared=False여도 어휘 안전망을 유지한다."""
    analyzer = _analyzer()

    assert analyzer._derive_external_evidence_requirement(
        query,
        intent="가격 조회",
        declared=False,
    )


def test_finance_and_requested_lookup_ignore_casual_intent_claim():
    """금융·명시적 조회 요청은 잡담 판정으로도 우회할 수 없다."""
    analyzer = _analyzer()

    for query in ("오늘 애플 주가 알려줘", "환율 지금 어때", "테슬라 소식 검색해줘"):
        assert analyzer._derive_external_evidence_requirement(
            query,
            intent="장난",
            declared=False,
        ), query


def test_topic_words_alone_are_not_treated_as_requested_lookup():
    """'비교'·'발표' 같은 주제어는 사용자가 조회를 요청한 표현이 아니다."""
    pattern = IntentAnalyzer._REQUESTED_WEB_LOOKUP_PATTERN

    assert not pattern.search("야 나랑 너랑 성능 비교하면?")
    assert not pattern.search("어제 발표 어땠음?")
    assert pattern.search("이거 검색해줘")
    assert pattern.search("출처 좀")


def test_lookup_promise_removal_keeps_the_surrounding_conversation():
    """약속 한 마디 때문에 잡담 전체가 안내문으로 바뀌면 안 된다."""
    replaced = AIHandler._replace_unexecuted_lookup_promise(
        "오 그거 재밌겠다ㅋㅋ 나도 한번 찾아볼게",
        has_external_evidence=False,
    )

    assert replaced == "오 그거 재밌겠다ㅋㅋ"
    assert "찾아볼게" not in replaced
    assert "확인이 안 됐어요" not in replaced


def test_lookup_promise_removal_handles_text_without_punctuation():
    """Discord 잡담은 문장 부호가 없어도 약속 구간만 걷어내야 한다."""
    replaced = AIHandler._replace_unexecuted_lookup_promise(
        "헐 대박 개웃기네 오빠 그거 진짜임 나중에 알려줄게",
        has_external_evidence=False,
    )

    assert "헐 대박 개웃기네" in replaced
    assert "알려줄게" not in replaced


def test_promise_only_response_still_discloses_missing_verification():
    """약속만 있고 내용이 없으면 확인하지 못했다고 밝힌다."""
    for promise in ("확인해볼게요.", "ㅇㅇ 내가 찾아볼게"):
        replaced = AIHandler._replace_unexecuted_lookup_promise(
            promise,
            has_external_evidence=False,
        )
        assert "지금 확인이 안 됐어요" in replaced, promise


def test_casual_reply_without_promise_is_untouched():
    text = "ㅋㅋㅋ 뭐래 그건 나도 모르겠음"

    assert (
        AIHandler._replace_unexecuted_lookup_promise(
            text,
            has_external_evidence=False,
        )
        == text
    )


@pytest.mark.asyncio
async def test_current_discord_scope_name_overrides_unrelated_public_search():
    """현재 서버 이름을 말한 내부 떡밥은 동명의 공개 웹 사건으로 새면 안 된다."""
    handler = _build_handler_without_init()
    handler.use_cometapi = True
    captured = {}

    async def _fake_fast(prompt, *_args, **_kwargs):
        captured["prompt"] = prompt
        return (
            '{"intent":"마사모 부정선거 논란에 대한 사실·경위 확인",'
            '"needs_memory":false,"references_shared_history":false,'
            '"requires_external_evidence":true,"reasoning_level":"low",'
            '"tools":[{"tool":"web_search",'
            '"params":{"query":"마사모 부정선거 논란"}}]}'
        )

    handler._cometapi_fast_generate_text = _fake_fast
    decision = await handler._route_tools(
        "마사모 부정선거 논란",
        {"trace_id": "current-scope-banter"},
        history=[],
        conversation_scope="마사모",
    )

    assert decision.plan == []
    assert decision.needs_memory is True
    assert decision.references_shared_history is True
    assert decision.requires_external_evidence is False
    assert '"마사모"' in captured["prompt"]
    assert "공개 웹 사건으로 바꾸지 말고" in captured["prompt"]


@pytest.mark.asyncio
async def test_explicit_public_lookup_keeps_web_even_for_current_scope_name():
    handler = _build_handler_without_init()
    handler.use_cometapi = True

    async def _fake_fast(*_args, **_kwargs):
        return (
            '{"intent":"마사모 관련 공개 기사 검색",'
            '"needs_memory":false,"references_shared_history":false,'
            '"requires_external_evidence":true,"reasoning_level":"low",'
            '"tools":[{"tool":"web_search",'
            '"params":{"query":"마사모 관련 기사"}}]}'
        )

    handler._cometapi_fast_generate_text = _fake_fast
    decision = await handler._route_tools(
        "마사모 관련 기사 웹에서 검색해줘",
        {"trace_id": "current-scope-public-lookup"},
        history=[],
        conversation_scope="마사모",
    )

    assert [item["tool_to_use"] for item in decision.plan] == ["web_search"]
    assert decision.requires_external_evidence is True


def test_creative_intent_is_not_reclassified_by_factual_vocabulary():
    analyzer = _analyzer()
    query = (
        "과거 연애 기록과 두 사람의 성격 차이를 반영해서 "
        "오피스물 소설 다음화 이어서 써줘"
    )

    assert (
        analyzer._derive_external_evidence_requirement(
            query,
            intent="현대 오피스물 소설의 다음화 창작 이어쓰기",
            declared=False,
        )
        is False
    )


def test_emergency_router_keeps_creative_request_off_the_web():
    analyzer = _analyzer()
    decision = analyzer._emergency_routing_decision(
        "과거 연애 기록과 성격 차이를 반영해서 소설 다음화 이어서 써줘",
        source="error_fallback",
    )

    assert decision.plan == []
    assert decision.requires_external_evidence is False
    assert decision.intent == "창작·잡담"


def test_emergency_router_keeps_local_bot_banter_off_the_web():
    analyzer = _analyzer()
    decision = analyzer._emergency_routing_decision(
        "야 너 스펙이 어떻게 됨? ㅋㅋ",
        source="error_fallback",
    )

    assert decision.plan == []
    assert decision.requires_external_evidence is False


def test_creative_response_keeps_fictional_future_dialogue():
    text = '현민이 말했다. "그 비밀은 나중에 알려줄게." 예은은 고개를 끄덕였다.'

    assert (
        AIHandler._replace_unexecuted_lookup_promise(
            text,
            has_external_evidence=False,
            creative_response=True,
        )
        == text
    )


def test_main_prompt_allows_playful_uncertainty_without_repeated_lectures():
    handler = _build_handler_without_init()
    message = SimpleNamespace(
        author=SimpleNamespace(display_name="질문자"),
    )

    prompt = handler._compose_main_prompt(
        message,
        user_query="마사모 부정선거 논란 ㅋㅋ",
        rag_blocks=[],
        tool_results_block=None,
        recent_history=[],
    )

    assert "친구끼리 하는 잡담에서는 모든 문장에 근거" in prompt
    assert "사용자가 정정하면 변명하지 말고 가볍게 인정" in prompt
    assert "같은 경고를 반복하거나 길게 훈계하지 말고" in prompt

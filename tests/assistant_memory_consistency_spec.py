"""봇 자기 일관성·근거 가드·응답 기억의 운영 회귀 테스트."""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import numpy as np
import pytest

import config
from cogs.ai_handler import AIHandler
from utils.hybrid_search import HybridSearchEngine
from utils.memory_units import (
    build_assistant_memory_unit,
    build_structured_memory_units,
    classify_memory_type,
)
from utils.rag_manager import RAGManager


def _bot_payload() -> list[dict]:
    return [
        {
            "message_id": 201,
            "user_id": 999,
            "user_name": "마사몽",
            "content": "내 선택은 AMG GT 블랙시리즈야.",
            "is_bot": True,
            "created_at": "2026-07-30T10:00:00+00:00",
        },
        {
            "message_id": 202,
            "user_id": 999,
            "user_name": "마사몽",
            "content": "둘 중 고르라면 그 선택을 유지할게.",
            "is_bot": True,
            "created_at": "2026-07-30T10:00:01+00:00",
        },
    ]


def test_assistant_chunks_become_one_server_scoped_commitment():
    unit = build_assistant_memory_unit(
        _bot_payload(),
        channel_id=77,
        memory_scope="guild",
    )

    assert unit is not None
    assert unit.memory_id == "assistant:77:201:202"
    assert unit.memory_scope == "guild"
    assert unit.memory_type == "assistant_commitment"
    assert unit.owner_user_id is None
    assert unit.source_message_ids == [201, 202]
    assert "블랙시리즈" in unit.memory_text


def test_memory_classifier_does_not_turn_banter_into_user_preference():
    assert classify_memory_type(
        "너 발키리 좋아하네 ㅋㅋ",
        speaker_count=1,
        owner_specific=True,
    ) == "conversation"
    assert classify_memory_type(
        "나는 떡볶이를 좋아해",
        speaker_count=1,
        owner_specific=True,
    ) == "preference"
    assert classify_memory_type(
        "이건 준비해야 할 것 같아",
        speaker_count=1,
        owner_specific=True,
    ) == "conversation"
    assert classify_memory_type(
        "내일 오전 9시에 발표 준비할게",
        speaker_count=1,
        owner_specific=True,
    ) == "plan"


def test_bot_turn_is_not_attributed_as_a_user_memory():
    units = build_structured_memory_units(
        [
            {
                "message_id": 1,
                "user_id": 10,
                "user_name": "철수",
                "content": "둘 중 뭐가 더 좋아?",
                "is_bot": False,
                "created_at": "2026-07-30T10:00:00+00:00",
            },
            *_bot_payload(),
        ],
        channel_id=77,
        shared_scope="guild",
        user_scope="guild_user",
    )

    assistant_units = [
        unit for unit in units if unit.memory_type == "assistant_commitment"
    ]
    assert len(assistant_units) == 1
    assert assistant_units[0].memory_scope == "guild"
    assert assistant_units[0].owner_user_id is None


@pytest.mark.asyncio
async def test_delivered_bot_response_is_persisted_without_window_recursion(
    monkeypatch,
):
    db = await aiosqlite.connect(":memory:")
    await db.executescript(
        Path("database/schema.sql").read_text(encoding="utf-8")
    )
    monkeypatch.setattr(config, "AI_MEMORY_ENABLED", True)
    monkeypatch.setattr(config, "EMBEDDING_ENABLED", True)

    manager = RAGManager.__new__(RAGManager)
    manager.db = db
    manager.bot = SimpleNamespace(
        is_ai_channel_allowed=lambda _guild, _channel: True
    )
    scheduled: list[str] = []

    def _schedule(coroutine, *, log_extra, task_kind):
        scheduled.append(task_kind)
        coroutine.close()
        return True

    manager._schedule_background_task = _schedule
    channel = SimpleNamespace(id=77)
    guild = SimpleNamespace(id=88)
    author = SimpleNamespace(
        id=999,
        bot=True,
        display_name="마사몽",
        name="masamong",
    )
    messages = [
        SimpleNamespace(
            id=item["message_id"],
            guild=guild,
            channel=channel,
            author=author,
            content=item["content"],
            attachments=[],
            embeds=[],
            stickers=[],
            created_at=datetime.now(timezone.utc),
        )
        for item in _bot_payload()
    ]

    stored = await manager.record_delivered_bot_messages(messages)

    async with db.execute(
        "SELECT message_id, is_bot, content FROM conversation_history "
        "ORDER BY message_id"
    ) as cursor:
        rows = await cursor.fetchall()
    assert stored == [201, 202]
    assert [(row[0], bool(row[1])) for row in rows] == [
        (201, True),
        (202, True),
    ]
    assert scheduled == ["봇 응답 임베딩"]
    async with db.execute(
        "SELECT COUNT(*) FROM conversation_windows"
    ) as cursor:
        assert (await cursor.fetchone())[0] == 0
    await db.close()


@pytest.mark.asyncio
async def test_assistant_embedding_uses_local_model_without_llm_summary(
    monkeypatch,
):
    unit = build_assistant_memory_unit(
        _bot_payload(),
        channel_id=77,
        memory_scope="guild",
    )
    assert unit is not None
    stored: list[dict] = []

    class _Store:
        async def upsert_memory_entry(self, **kwargs):
            stored.append(kwargs)

    class _NoLLM:
        def __getattr__(self, name):
            raise AssertionError(f"LLM must not be touched: {name}")

    manager = RAGManager.__new__(RAGManager)
    manager.embedding_store = _Store()
    manager.llm_client = _NoLLM()

    async def _token_limit():
        return 512

    async def _local_embedding(*_args, **_kwargs):
        return np.zeros(384, dtype=np.float32)

    manager._embedding_token_limit = _token_limit
    manager._generate_local_embedding = _local_embedding
    monkeypatch.setattr(
        "utils.rag_manager.count_embedding_tokens",
        lambda *_args, **_kwargs: _async_value(20),
    )

    await manager._create_assistant_memory_embedding(88, 77, unit)

    assert len(stored) == 1
    assert stored[0]["memory_type"] == "assistant_commitment"
    assert stored[0]["memory_scope"] == "guild"
    assert stored[0]["owner_user_id"] is None


@pytest.mark.asyncio
async def test_assistant_embedding_delay_keeps_raw_commit_path_separate(
    monkeypatch,
):
    unit = build_assistant_memory_unit(
        _bot_payload(),
        channel_id=77,
        memory_scope="guild",
    )
    assert unit is not None
    manager = RAGManager.__new__(RAGManager)
    events: list[tuple[str, float | None]] = []

    async def _sleep(seconds):
        events.append(("sleep", seconds))

    async def _embed(*_args):
        events.append(("embed", None))

    monkeypatch.setattr(config, "ASSISTANT_MEMORY_EMBEDDING_DELAY_SECONDS", 2)
    monkeypatch.setattr("utils.rag_manager.asyncio.sleep", _sleep)
    manager._create_assistant_memory_embedding = _embed

    await manager._create_assistant_memory_embedding_after_delay(
        88,
        77,
        unit,
    )

    assert events == [("sleep", 2.0), ("embed", None)]


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_router_restores_memory_for_assistant_preference_followup():
    handler = AIHandler.__new__(AIHandler)
    handler.use_cometapi = True

    async def _fake_fast(*_args, **_kwargs):
        return (
            '{"intent":"자동차 취향 질문","needs_memory":false,'
            '"references_shared_history":false,'
            '"requires_external_evidence":false,"tools":[]}'
        )

    handler._cometapi_fast_generate_text = _fake_fast
    decision = await handler._route_tools(
        "너가 AMG GT 블랙시리즈보다 좋아하는 차가 있긴 하냐?",
        {"trace_id": "assistant-choice"},
        history=[],
    )

    assert decision.needs_memory is True
    assert decision.references_shared_history is True
    assert decision.plan == []


@pytest.mark.asyncio
async def test_objective_vehicle_record_forces_one_web_search():
    handler = AIHandler.__new__(AIHandler)
    handler.use_cometapi = True

    async def _fake_fast(*_args, **_kwargs):
        return (
            '{"intent":"차량 질문","needs_memory":false,'
            '"requires_external_evidence":false,"tools":[]}'
        )

    handler._cometapi_fast_generate_text = _fake_fast
    decision = await handler._route_tools(
        "둘 중 누가 더 빠르고 공식 랩타임 기록은 몇 초야?",
        {"trace_id": "objective-comparison"},
        history=[],
    )

    assert decision.requires_external_evidence is True
    assert [item["tool_to_use"] for item in decision.plan] == ["web_search"]


def test_subjective_choice_prompt_forbids_unverified_spec_table():
    handler = AIHandler.__new__(AIHandler)
    handler._get_custom_emoji_instruction = lambda *_args: ""
    message = SimpleNamespace(
        author=SimpleNamespace(display_name="tester"),
    )

    prompt = handler._compose_main_prompt(
        message,
        user_query="GT3 RS vs 테메라리오, 너의 선택은?",
        rag_blocks=[],
        tool_results_block=None,
        recent_history=[],
    )

    assert "정확한 수치·가격·기록·출력·제원을 새로 제시하지 마세요" in prompt
    assert "과거의 내 선택 기억" in prompt
    assert "현재 선택을 고정하는 규칙이 아닙니다" in prompt
    assert "현재 질문에 주어진 조건과 대화 흐름을 먼저" in prompt
    assert "특별한 이유 없이 반대로 바꾸지 말고" not in prompt


def test_rag_memory_is_framed_as_context_not_a_binding_rule():
    handler = AIHandler.__new__(AIHandler)
    handler._get_custom_emoji_instruction = lambda *_args: ""
    message = SimpleNamespace(
        author=SimpleNamespace(display_name="tester"),
    )

    prompt = handler._compose_main_prompt(
        message,
        user_query="이번 조건이라면 뭐가 더 나아?",
        rag_blocks=["마사몽: 예전에는 A가 더 좋다고 답했다."],
        tool_results_block=None,
        recent_history=[],
    )

    assert "현재 질문과 최근 대화에 관련될 때만 참고" in prompt
    assert "현재 답변을 구속하는 규칙으로 쓰지 마세요" in prompt


def test_system_prompt_prioritizes_current_context_over_past_stance():
    handler = AIHandler.__new__(AIHandler)
    handler._get_channel_system_prompt = lambda *_args, **_kwargs: "친근하게 답한다."
    handler._get_custom_emoji_instruction = lambda *_args: ""
    message = SimpleNamespace(
        channel=SimpleNamespace(id=77),
        guild=SimpleNamespace(id=88),
    )

    prompt = handler._compose_main_system_prompt(
        message,
        user_query="지금은 생각이 달라졌어?",
    )

    assert "현재 답변을 구속하지 않는다" in prompt
    assert "현재 질문과 최근 대화 흐름, 새로 확인된 정보를 먼저" in prompt
    assert "사용자가 일관성을 물을 때만" in prompt


def test_explicit_memory_search_keeps_one_lexically_matching_other_source(
    monkeypatch,
):
    monkeypatch.setattr(config, "RAG_MEMORY_RELATIVE_FLOOR", 0.94)
    monkeypatch.setattr(config, "RAG_EXPLICIT_MEMORY_GATE_SCORE", 0.58)
    monkeypatch.setattr(config, "RAG_SOURCE_DIVERSITY_ENABLED", True)
    monkeypatch.setattr(
        config,
        "RAG_SOURCE_DIVERSITY_RELATIVE_FLOOR",
        0.72,
    )
    monkeypatch.setattr(
        config,
        "RAG_SOURCE_DIVERSITY_LEXICAL_FLOOR",
        0.02,
    )
    engine = HybridSearchEngine.__new__(HybridSearchEngine)
    entries = [
        {
            "origin": "Discord",
            "combined_score": 0.832,
            "semantic_similarity": 0.832,
            "lexical_score": 0.04,
            "message": "재원이 얘기",
        },
        {
            "origin": "Discord",
            "combined_score": 0.810,
            "semantic_similarity": 0.810,
            "lexical_score": 0.04,
            "message": "재원이 다른 얘기",
        },
        {
            "origin": "Kakao",
            "combined_score": 0.636,
            "semantic_similarity": 0.616,
            "lexical_score": 0.02,
            "message": "김재원 카카오 대화",
        },
    ]

    selected = engine._gate_by_relevance(
        "재원이 누구야?",
        entries,
        deep_search=True,
    )

    assert [entry["origin"] for entry in selected[:2]] == [
        "Discord",
        "Kakao",
    ]

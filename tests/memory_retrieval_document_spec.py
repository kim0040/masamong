from __future__ import annotations

import pytest

from utils.memory_units import (
    build_structured_memory_units,
    compose_memory_text,
)
from utils.rag_manager import RAGManager


def _payload():
    return [
        {
            "message_id": 101,
            "user_id": 1,
            "user_name": "민수",
            "content": "7월 31일 부산역에서 오전 10시에 만나자.",
            "is_bot": False,
            "created_at": "2026-07-28T12:00:00+00:00",
        },
        {
            "message_id": 102,
            "user_id": 2,
            "user_name": "지연",
            "content": "좋아. 다만 자가용은 이용하지 말고 KTX로 가자.",
            "is_bot": False,
            "created_at": "2026-07-28T12:01:00+00:00",
        },
    ]


def test_structured_memory_builds_lean_standalone_retrieval_document():
    units = build_structured_memory_units(
        _payload(),
        channel_id=77,
        max_summary_chars=320,
        max_context_chars=1200,
    )

    shared = units[0]
    assert shared.memory_scope == "channel"
    assert shared.summary_text.startswith("민수, 지연의 대화:")
    assert "핵심 키워드:" not in shared.summary_text
    assert shared.memory_text.startswith(shared.summary_text)
    assert "기억 유형:" not in shared.memory_text
    assert "핵심 표현:" not in shared.memory_text
    assert "부산역" in shared.memory_text
    assert "KTX" in shared.memory_text
    assert "자가용은 이용하지 말고" in shared.memory_text
    # 검색 metadata는 벡터 본문이 아니라 별도 열로 보존한다.
    assert shared.memory_type
    assert shared.speaker_names == ["민수", "지연"]
    assert shared.keywords
    assert shared.timestamp_iso.startswith("2026-07-28")


def test_retrieval_document_uses_independent_summary_without_raw_context():
    rendered = compose_memory_text(
        "민수가 토요일 등산 계획을 취소했다.",
        "",
        limit=500,
        keywords=["토요일", "등산", "취소"],
        speaker_names=["민수"],
        memory_type="plan",
        timestamp_iso="2026-07-28T09:00:00+09:00",
    )

    assert rendered == "민수가 토요일 등산 계획을 취소했다."


@pytest.mark.asyncio
async def test_overlong_memory_summary_prompt_prioritizes_retrieval_facts(
    monkeypatch,
):
    monkeypatch.setattr(
        "config.STRUCTURED_MEMORY_MAX_SUMMARY_CHARS",
        320,
    )
    captured = {}

    class _LLM:
        use_cometapi = True

        async def generate_content(
            self,
            system_prompt,
            user_prompt,
            log_extra,
        ):
            captured["system"] = system_prompt
            captured["user"] = user_prompt
            captured["extra"] = log_extra
            return "부산역 약속은 취소되었고 새 일정은 미정이다."

    manager = RAGManager.__new__(RAGManager)
    manager.llm_client = _LLM()
    summary = await manager._summarize_content("긴 대화 " * 120)

    assert summary == "부산역 약속은 취소되었고 새 일정은 미정이다."
    assert "부정 표현" in captured["system"]
    assert "아직 미정인 항목" in captured["system"]
    assert "분위기" in captured["system"]
    assert "필요한 경우가 아니면 버린다" in captured["system"]
    assert "최대 320자" in captured["system"]

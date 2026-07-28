from __future__ import annotations

from types import SimpleNamespace

import pytest

import config
from cogs.ai_handler import AIHandler
from utils.llm_client import LLMClient, LLMProviderTimeoutError


def _handler() -> AIHandler:
    handler = object.__new__(AIHandler)
    handler.bot = SimpleNamespace()
    return handler


def _message(*, guild: bool = True):
    return SimpleNamespace(
        channel=SimpleNamespace(id=11),
        guild=SimpleNamespace(id=22) if guild else None,
        author=SimpleNamespace(display_name="테스트 사용자"),
    )


def test_main_prompt_reserves_question_and_tool_before_optional_context(monkeypatch):
    monkeypatch.setattr(config, "COMETAPI_USER_PROMPT_MAX_CHARS", 1_600)
    monkeypatch.setattr(config, "MAX_RAG_BLOCK_CHARS", 20_000)

    handler = _handler()
    prompt = handler._compose_main_prompt(
        _message(),
        user_query="질문 앞부분 " + ("Q" * 300) + " CURRENT_QUESTION_SENTINEL",
        tool_results_block="TOOL_RESULT_SENTINEL: 17도",
        fortune_context="FORTUNE " + ("F" * 20_000),
        recent_history=[
            {"role": "user", "parts": ["H" * 20_000]},
            {"role": "model", "parts": ["LATEST_HISTORY_SENTINEL"]},
        ],
        rag_blocks=["R" * 20_000, "RAG_SENTINEL"],
    )

    assert len(prompt) <= 1_600
    assert "CURRENT_QUESTION_SENTINEL" in prompt
    assert "TOOL_RESULT_SENTINEL: 17도" in prompt
    assert "[현재 질문]" in prompt
    assert "[도구 실행 결과 (최우선 정보)]" in prompt


def test_persona_exists_only_in_system_role(monkeypatch):
    monkeypatch.setattr(config, "COMETAPI_SYSTEM_PROMPT_MAX_CHARS", 6_000)
    monkeypatch.setattr(config, "COMETAPI_USER_PROMPT_MAX_CHARS", 2_000)

    handler = _handler()
    handler._get_channel_system_prompt = (
        lambda channel_id, guild_id=None: "UNIQUE_PERSONA_SENTINEL\n채널 규칙"
    )
    handler._get_custom_emoji_instruction = lambda guild, query: ""

    message = _message()
    system_prompt = handler._compose_main_system_prompt(
        message,
        user_query="현재 질문",
    )
    user_prompt = handler._compose_main_prompt(
        message,
        user_query="현재 질문",
        rag_blocks=[],
        tool_results_block=None,
    )

    assert system_prompt.count("UNIQUE_PERSONA_SENTINEL") == 1
    assert "UNIQUE_PERSONA_SENTINEL" not in user_prompt
    assert len(system_prompt) <= config.COMETAPI_SYSTEM_PROMPT_MAX_CHARS
    assert len(user_prompt) <= config.COMETAPI_USER_PROMPT_MAX_CHARS


@pytest.mark.asyncio
async def test_llm_client_final_guard_keeps_latest_question_without_external_call(
    monkeypatch,
):
    monkeypatch.setattr(config, "COMETAPI_USER_PROMPT_MAX_CHARS", 900)
    monkeypatch.setattr(config, "COMETAPI_SYSTEM_PROMPT_MAX_CHARS", 500)

    client = LLMClient(db=None)
    monkeypatch.setattr(
        client,
        "get_lane_targets",
        lambda lane, model_override=None: [
            {
                "provider": "test",
                "name": "main.test",
                "model": "fake",
            }
        ],
    )

    captured: dict[str, str] = {}

    async def fake_call(
        target,
        *,
        system_prompt,
        user_prompt,
        log_extra,
        max_tokens,
    ):
        captured["system"] = system_prompt
        captured["user"] = user_prompt
        return "응답"

    monkeypatch.setattr(client, "call_main_lane_target", fake_call)

    response = await client.generate_content(
        "SYSTEM_HEAD " + ("S" * 2_000) + " SYSTEM_TAIL",
        ("과거 RAG와 대화 " * 1_000)
        + "\n\n[현재 질문]\nCURRENT_QUESTION_SENTINEL",
        {"trace_id": "prompt-budget-test"},
    )

    assert response == "응답"
    assert len(captured["system"]) <= 500
    assert len(captured["user"]) <= 900
    assert "CURRENT_QUESTION_SENTINEL" in captured["user"]


def test_optional_context_has_independent_hard_budgets(monkeypatch):
    monkeypatch.setattr(config, "COMETAPI_USER_PROMPT_MAX_CHARS", 20_000)
    monkeypatch.setattr(config, "MAX_RAG_BLOCK_CHARS", 50_000)

    handler = _handler()
    prompt = handler._compose_main_prompt(
        _message(),
        user_query="짧은 현재 질문",
        rag_blocks=["R" * 50_000],
        tool_results_block=None,
        fortune_context="F" * 50_000,
        recent_history=[{"role": "user", "parts": ["H" * 50_000]}],
    )

    # 제목/생략 마커가 더해지므로 각 다음 섹션의 위치로 실제 콘텐츠 구간을 잰다.
    history = prompt.split("[최근 대화 흐름 (선택 참고)]\n", 1)[1]
    history = history.split("\n\n[과거 대화 기억 (선택 참고)]", 1)[0]
    fortune = prompt.split("[운세 참고 (선택 참고)]\n", 1)[1]
    fortune = fortune.split("\n\n[최근 대화 흐름 (선택 참고)]", 1)[0]
    rag = prompt.split("[과거 대화 기억 (선택 참고)]\n", 1)[1]
    rag = rag.split("\n\n[현재 질문]", 1)[0]

    assert len(history) <= handler._RECENT_HISTORY_PROMPT_MAX_CHARS
    assert len(fortune) <= handler._FORTUNE_PROMPT_MAX_CHARS
    assert len(rag) <= handler._RAG_PROMPT_MAX_CHARS
    assert len(prompt) <= config.COMETAPI_USER_PROMPT_MAX_CHARS


@pytest.mark.asyncio
async def test_image_prompt_timeout_uses_raw_prompt_without_direct_llm_fallback():
    handler = _handler()
    handler.use_cometapi = True
    direct_calls = 0

    async def timed_out(*_args, **kwargs):
        assert kwargs["stop_on_bounded_failure"] is True
        raise LLMProviderTimeoutError("timed out")

    async def unexpected_direct(*_args, **_kwargs):
        nonlocal direct_calls
        direct_calls += 1
        raise AssertionError("direct fallback must not run after timeout")

    handler._cometapi_generate_content = timed_out
    handler._can_use_direct_gemini = lambda: True
    handler._safe_generate_content = unexpected_direct

    assert await handler._generate_image_prompt("파란 고양이", {}) is None
    assert direct_calls == 0

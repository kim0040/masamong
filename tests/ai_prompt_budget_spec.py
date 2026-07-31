from __future__ import annotations

from types import SimpleNamespace

import pytest

import config
from cogs.ai_handler import AIHandler
from utils.llm_client import LLMClient


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


def test_main_system_prompt_ends_with_provider_neutral_style_contract(
    monkeypatch,
):
    monkeypatch.setattr(config, "COMETAPI_SYSTEM_PROMPT_MAX_CHARS", 10_000)

    handler = _handler()
    handler._get_channel_system_prompt = (
        lambda channel_id, guild_id=None: "SERVER_PERSONA\nSERVER_RULES"
    )
    handler._get_custom_emoji_instruction = lambda guild, query: ""

    system_prompt = handler._compose_main_system_prompt(
        _message(),
        user_query="UML이 뭐야?",
    )

    assert system_prompt.count("### 말투 유지 계약") == 1
    assert system_prompt.rfind("### 말투 유지 계약") > system_prompt.rfind(
        "### 외부 자료 처리 규칙"
    )
    assert "고객센터, 교과서, 보고서처럼 딱딱한" in system_prompt
    assert "최근 대화의 Bot 문장" in system_prompt
    # 공용 계약은 특정 운영 서버의 고유 말투나 호칭을 하드코딩하지 않는다.
    assert "마사모 서버" not in config.MODEL_STYLE_FIDELITY_PROMPT
    assert "연사모" not in config.MODEL_STYLE_FIDELITY_PROMPT
    assert "오빠" not in config.MODEL_STYLE_FIDELITY_PROMPT


def test_style_contract_survives_tight_system_prompt_budget(monkeypatch):
    monkeypatch.setattr(config, "COMETAPI_SYSTEM_PROMPT_MAX_CHARS", 700)

    handler = _handler()
    handler._get_channel_system_prompt = (
        lambda channel_id, guild_id=None: (
            "PERSONA_HEAD_SENTINEL " + ("P" * 3_000)
        )
    )
    handler._get_custom_emoji_instruction = lambda guild, query: ""

    system_prompt = handler._compose_main_system_prompt(
        _message(),
        user_query="짧은 질문",
    )

    assert len(system_prompt) <= 700
    assert "PERSONA_HEAD_SENTINEL" in system_prompt
    # keep="both"가 맨 뒤 계약의 핵심 안전 우선 문장을 보존한다.
    assert "정확성·안전·개인정보 규칙" in system_prompt


def test_main_prompt_keeps_digest_separate_from_recent_verbatim(monkeypatch):
    monkeypatch.setattr(config, "COMETAPI_USER_PROMPT_MAX_CHARS", 4_000)

    handler = _handler()
    prompt = handler._compose_main_prompt(
        _message(),
        user_query="그 계획에서 아직 안 정한 게 뭐야?",
        rag_blocks=[],
        tool_results_block=None,
        context_digest=(
            "민수는 부산 이동에 자가용을 쓰지 않기로 했고 KTX 예약은 미정이다."
        ),
        recent_history=[
            {
                "role": "user",
                "speaker": "민수",
                "is_current_user": True,
                "parts": ["회의는 8월 3일 오후 2시 부산역이야."],
            }
        ],
    )

    assert "[이전 대화 압축본 (선택 참고)]" in prompt
    assert "자가용을 쓰지 않기로" in prompt
    assert "[최근 대화 흐름 (선택 참고)]" in prompt
    assert "8월 3일 오후 2시 부산역" in prompt


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


def test_recent_channel_history_keeps_speaker_identity_separate(monkeypatch):
    monkeypatch.setattr(config, "COMETAPI_USER_PROMPT_MAX_CHARS", 4_000)
    handler = _handler()

    prompt = handler._compose_main_prompt(
        _message(),
        user_query="내가 말한 건 뭐였지?",
        rag_blocks=[],
        tool_results_block=None,
        recent_history=[
            {
                "role": "user",
                "speaker": "민수",
                "is_current_user": False,
                "parts": ["부산으로 가자"],
            },
            {
                "role": "user",
                "speaker": "테스트 사용자",
                "is_current_user": True,
                "parts": ["서울로 가자"],
            },
        ],
    )

    assert "User(민수): 부산으로 가자" in prompt
    assert "User(테스트 사용자·현재 질문자): 서울로 가자" in prompt


@pytest.mark.asyncio
async def test_image_prompt_preserves_korean_without_prompt_llm():
    handler = _handler()
    handler.use_cometapi = True
    handler._debug = lambda *_args, **_kwargs: None
    handler.llm_client = SimpleNamespace(
        truncate_for_debug=lambda value: str(value)[:200]
    )

    async def unexpected_llm(*_args, **_kwargs):
        raise AssertionError("이미지 프롬프트 구성에 LLM을 호출하면 안 됩니다")

    handler._cometapi_generate_content = unexpected_llm
    prompt = await handler._generate_image_prompt(
        "파란 고양이를 수채화로 그려줘",
        {},
        rag_context="고양이는 초록색 목걸이를 좋아한다.",
        interpreted_query="파란 고양이 수채화",
    )

    assert prompt is not None
    assert "파란 고양이를 수채화로 그려줘" in prompt
    assert "초록색 목걸이" in prompt
    assert "exactly one final" in prompt
    assert "collage" in prompt

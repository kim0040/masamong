from types import SimpleNamespace

import pytest

import config
from cogs.ai_handler import AIHandler


def _build_handler():
    bot_user = SimpleNamespace(
        id=999999,
        name="Masamong",
        display_name="마사몽",
        global_name="Masamong",
    )
    bot = SimpleNamespace(
        user=bot_user,
        get_cog=lambda name: None,
        db=None,
    )
    handler = AIHandler(bot)
    handler.gemini_configured = True
    return handler, bot_user


def _make_message(content: str, mentions, guild_display: str = "마사몽"):
    guild = SimpleNamespace(
        id=123,
        me=SimpleNamespace(
            display_name=guild_display,
            roles=[],  # 역할 목록 (멘션 패턴 생성용)
        ),
    )
    return SimpleNamespace(
        content=content,
        mentions=list(mentions),
        guild=guild,
        channel=SimpleNamespace(id=456),
        author=SimpleNamespace(id=789),
    )


def test_message_has_valid_mention_via_id():
    handler, bot_user = _build_handler()
    message = _make_message(
        content=f"<@{bot_user.id}> 안녕?",
        mentions=[SimpleNamespace(id=bot_user.id)],
    )
    assert handler._message_has_valid_mention(message) is True


def test_message_has_valid_mention_via_alias():
    handler, _ = _build_handler()
    message = _make_message(
        content="@Masamong 도와줘",
        mentions=[],
    )
    assert handler._message_has_valid_mention(message) is True


def test_prepare_user_query_removes_mentions():
    handler, bot_user = _build_handler()
    message = _make_message(
        content=f"<@!{bot_user.id}>  테스트 부탁해",
        mentions=[SimpleNamespace(id=bot_user.id)],
    )
    log_extra = {"guild_id": 123, "channel_id": 456, "user_id": 789}
    assert handler._prepare_user_query(message, log_extra) == "테스트 부탁해"


def test_prepare_user_query_without_mention_returns_none():
    handler, _ = _build_handler()
    message = _make_message(
        content="그냥 이야기",
        mentions=[],
    )
    log_extra = {"guild_id": 123, "channel_id": 456, "user_id": 789}
    assert handler._prepare_user_query(message, log_extra) is None


def test_interaction_analytics_omits_message_content_by_default(monkeypatch):
    monkeypatch.setattr(config, "ANALYTICS_STORE_CONTENT", False)
    message = _make_message(content="민감한 원문 메시지", mentions=[])

    details = AIHandler._build_interaction_analytics(
        message=message,
        trace_id="trace-1",
        user_query="민감한 원문 메시지",
        final_response="민감한 응답",
        tool_plan=[
            {"tool_name": "weather"},
            {"tool_name": "weather"},
            {"tool_to_use": "generate_image"},
        ],
    )

    assert details == {
        "guild_id": 123,
        "user_id": 789,
        "channel_id": 456,
        "trace_id": "trace-1",
        "user_query_chars": len("민감한 원문 메시지"),
        "final_response_chars": len("민감한 응답"),
        "tools": ["weather", "generate_image"],
    }


def test_dm_prompt_is_selected_by_missing_guild_not_channel_id(monkeypatch):
    handler, _ = _build_handler()
    monkeypatch.setattr(
        config,
        "CHANNEL_AI_CONFIG",
        {456: {"persona": "길드 전용 페르소나", "rules": "길드 전용 규칙"}},
    )

    prompt = handler._get_channel_system_prompt(456, guild_id=None)

    assert "개인 비서이자 친구" in prompt
    assert "길드 전용 페르소나" not in prompt


def test_guild_runtime_persona_takes_priority_over_channel_default(monkeypatch):
    handler, _ = _build_handler()
    handler.bot.get_guild_persona = lambda guild_id: (
        "서버 맞춤 페르소나" if guild_id == 123 else None
    )
    monkeypatch.setattr(
        config,
        "CHANNEL_AI_CONFIG",
        {456: {"persona": "채널 기본 페르소나", "rules": "테스트 규칙"}},
    )

    prompt = handler._get_channel_system_prompt(456, guild_id=123)

    assert "서버 맞춤 페르소나" in prompt
    assert "채널 기본 페르소나" not in prompt


def test_guild_personas_are_strictly_isolated(monkeypatch):
    handler, _ = _build_handler()
    personas = {
        123: "A 서버 전용 말투",
        987: "B 서버 전용 말투",
    }
    handler.bot.get_guild_persona = lambda guild_id: personas.get(guild_id)
    monkeypatch.setattr(
        config,
        "CHANNEL_AI_CONFIG",
        {
            456: {"persona": "정적 기본 말투", "rules": "A 규칙"},
            654: {"persona": "정적 기본 말투", "rules": "B 규칙"},
        },
    )

    prompt_a = handler._get_channel_system_prompt(456, guild_id=123)
    prompt_b = handler._get_channel_system_prompt(654, guild_id=987)

    assert "A 서버 전용 말투" in prompt_a
    assert "B 서버 전용 말투" not in prompt_a
    assert "B 서버 전용 말투" in prompt_b
    assert "A 서버 전용 말투" not in prompt_b

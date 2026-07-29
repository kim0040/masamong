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

    assert "오랜 친구" in prompt
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


def _bot_with_channels(handler, channel_to_guild: dict[int, int]) -> None:
    """configured channel_id -> guild_id 매핑을 가진 Discord 캐시를 흉내낸다."""

    def get_channel(channel_id: int):
        guild_id = channel_to_guild.get(int(channel_id))
        if guild_id is None:
            return None
        return SimpleNamespace(id=int(channel_id), guild=SimpleNamespace(id=guild_id))

    handler.bot.get_channel = get_channel


def test_guild_personas_are_strictly_isolated(monkeypatch):
    """운영 경로(get_guild_persona=None)에서도 서버별 말투가 섞이지 않는다."""
    handler, _ = _build_handler()
    # 운영 봇의 get_guild_persona는 항상 None을 반환한다. 그 상태를 그대로 둔다.
    monkeypatch.setattr(
        config,
        "CHANNEL_AI_CONFIG",
        {
            456: {"persona": "A 서버 전용 말투", "rules": "A 규칙"},
            654: {"persona": "B 서버 전용 말투", "rules": "B 규칙"},
        },
    )
    _bot_with_channels(handler, {456: 123, 654: 987})

    prompt_a = handler._get_channel_system_prompt(456, guild_id=123)
    prompt_b = handler._get_channel_system_prompt(654, guild_id=987)

    assert "A 서버 전용 말투" in prompt_a
    assert "A 규칙" in prompt_a
    assert "B 서버 전용 말투" not in prompt_a
    assert "B 규칙" not in prompt_a
    assert "B 서버 전용 말투" in prompt_b
    assert "B 규칙" in prompt_b
    assert "A 서버 전용 말투" not in prompt_b
    assert "A 규칙" not in prompt_b


def test_unconfigured_channel_inherits_same_guild_persona(monkeypatch):
    """`!관리`로 켠 미등록 채널이 전역 기본값 대신 그 서버 말투를 쓴다."""
    handler, _ = _build_handler()
    monkeypatch.setattr(
        config,
        "CHANNEL_AI_CONFIG",
        {456: {"persona": "A 서버 전용 말투", "rules": "A 규칙"}},
    )
    # 777은 prompts.json에 없지만 123 서버에 속한 채널이다.
    _bot_with_channels(handler, {456: 123, 777: 123})

    prompt = handler._get_channel_system_prompt(777, guild_id=123)

    assert "A 서버 전용 말투" in prompt
    assert "A 규칙" in prompt
    assert config.DEFAULT_TSUNDERE_PERSONA not in prompt


def test_unconfigured_channel_never_inherits_other_guild_persona(monkeypatch):
    """다른 서버에만 설정이 있으면 물려받지 않고 전역 기본값으로 남는다."""
    handler, _ = _build_handler()
    monkeypatch.setattr(
        config,
        "CHANNEL_AI_CONFIG",
        {456: {"persona": "A 서버 전용 말투", "rules": "A 규칙"}},
    )
    # 888은 A 서버(123)와 무관한 다른 서버(987)의 채널이다.
    _bot_with_channels(handler, {456: 123, 888: 987})

    prompt = handler._get_channel_system_prompt(888, guild_id=987)

    assert "A 서버 전용 말투" not in prompt
    assert "A 규칙" not in prompt


def test_configured_channel_wins_over_guild_fallback(monkeypatch):
    """명시 설정된 채널은 같은 서버의 다른 채널 말투로 덮이지 않는다."""
    handler, _ = _build_handler()
    monkeypatch.setattr(
        config,
        "CHANNEL_AI_CONFIG",
        {
            456: {"persona": "먼저 정의된 말투", "rules": "먼저 정의된 규칙"},
            654: {"persona": "이 채널만의 말투", "rules": "이 채널만의 규칙"},
        },
    )
    _bot_with_channels(handler, {456: 123, 654: 123})

    prompt = handler._get_channel_system_prompt(654, guild_id=123)

    assert "이 채널만의 말투" in prompt
    assert "먼저 정의된 말투" not in prompt

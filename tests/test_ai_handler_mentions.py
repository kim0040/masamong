from types import SimpleNamespace

import pytest

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
        me=SimpleNamespace(display_name=guild_display),
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

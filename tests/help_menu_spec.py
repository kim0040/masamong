"""통합 메뉴의 기능 탐색 UX 명세."""

from types import SimpleNamespace

import discord
import pytest

import config
from cogs.help_cog import CategoryView, MasamongHomeView, ServerMenuLauncherView


def test_home_menu_exposes_only_grouped_categories():
    bot = SimpleNamespace(get_cog=lambda _name: object())
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=123),
        clean_prefix="!",
        guild=None,
    )

    view = MasamongHomeView(bot, ctx)

    buttons = {
        child.label
        for child in view.children
        if isinstance(child, discord.ui.Button)
    }
    selects = [
        child
        for child in view.children
        if isinstance(child, discord.ui.Select)
    ]
    assert buttons == set()
    assert len(selects) == 1
    assert {option.value for option in selects[0].options} == {
        "ai",
        "weather",
        "school",
        "fortune",
        "community",
        "personal",
        "help",
    }


def test_home_menu_shows_admin_only_to_configured_superadmin(monkeypatch):
    monkeypatch.setattr(config, "SUPERADMIN_USER_IDS", frozenset({123}))
    bot = SimpleNamespace(get_cog=lambda _name: object())
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=123),
        clean_prefix="!",
        guild=SimpleNamespace(id=456),
    )

    view = MasamongHomeView(bot, ctx)
    select = next(
        child for child in view.children if isinstance(child, discord.ui.Select)
    )

    assert "admin" in {option.value for option in select.options}


def test_server_school_category_disables_dm_only_actions():
    bot = SimpleNamespace(get_cog=lambda _name: object())
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=123),
        clean_prefix="!",
        guild=SimpleNamespace(id=456),
    )

    view = CategoryView(bot, ctx, "school")
    buttons = {
        child.label: child
        for child in view.children
        if isinstance(child, discord.ui.Button)
    }
    assert buttons["학교 공지 · DM"].disabled is True
    assert buttons["편입 공지 · DM"].disabled is True
    assert buttons["개인 설정"].disabled is False
    assert buttons["뒤로"].disabled is False


@pytest.mark.asyncio
async def test_server_launcher_opens_ephemeral_owner_menu():
    sent = []

    class _Response:
        async def send_message(self, **kwargs):
            sent.append(kwargs)

    bot = SimpleNamespace(get_cog=lambda _name: object())
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=123),
        clean_prefix="!",
        guild=SimpleNamespace(id=456),
    )
    launcher = ServerMenuLauncherView(bot, ctx)
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=123),
        response=_Response(),
    )
    button = next(
        child
        for child in launcher.children
        if isinstance(child, discord.ui.Button)
    )

    await button.callback(interaction)

    assert sent and sent[0]["ephemeral"] is True
    assert sent[0]["embed"].title == "🤖 마사몽 메뉴"
    private_view = sent[0]["view"]
    assert isinstance(private_view, MasamongHomeView)
    assert private_view.server_mode is True


@pytest.mark.asyncio
async def test_category_command_defers_before_slow_command_callback():
    events = []

    class _Response:
        def is_done(self):
            return bool(events)

        async def defer(self, **kwargs):
            events.append(("defer", kwargs))

    class _Followup:
        async def send(self, content=None, **kwargs):
            events.append(("send", content, kwargs))
            return SimpleNamespace()

    class _Command:
        cog = object()

        async def callback(self, _cog, ctx, **_kwargs):
            assert events and events[0][0] == "defer"
            await ctx.send("완료")

    bot = SimpleNamespace(
        get_cog=lambda _name: object(),
        get_command=lambda _name: _Command(),
    )
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=123),
        clean_prefix="!",
        guild=SimpleNamespace(id=456),
    )
    interaction = SimpleNamespace(
        response=_Response(),
        followup=_Followup(),
    )

    await CategoryView(bot, ctx, "weather")._invoke_command(
        interaction,
        "날씨",
        location_query="부산",
    )

    assert events[0] == (
        "defer",
        {"ephemeral": False, "thinking": True},
    )
    assert events[1][0:2] == ("send", "완료")
    assert events[1][2]["ephemeral"] is False

"""통합 메뉴의 기능 탐색 UX 명세."""

from types import SimpleNamespace

import discord
import pytest

from cogs.help_cog import MasamongHomeView, ServerMenuLauncherView


def test_home_menu_exposes_buttons_and_diverse_quick_guides():
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
    assert buttons == {
        "학교 공지",
        "편입 공지",
        "오늘 운세",
        "개인정보",
        "전체 도움말",
    }
    assert len(selects) == 1
    assert {option.value for option in selects[0].options} == {
        "ai",
        "weather",
        "school",
        "transfer",
        "fortune",
        "creative",
        "community",
        "admin",
        "privacy",
    }


def test_server_menu_disables_dm_only_buttons_and_keeps_features_visible():
    bot = SimpleNamespace(get_cog=lambda _name: object())
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=123),
        clean_prefix="!",
        guild=SimpleNamespace(id=456),
    )

    view = MasamongHomeView(bot, ctx)
    buttons = {
        child.label: child
        for child in view.children
        if isinstance(child, discord.ui.Button)
    }
    select = next(
        child for child in view.children if isinstance(child, discord.ui.Select)
    )

    assert buttons["학교 공지 · DM"].disabled is True
    assert buttons["편입 공지 · DM"].disabled is True
    assert buttons["개인정보 · DM"].disabled is True
    assert buttons["오늘 운세 안내"].disabled is False
    assert "dm_only" in {option.value for option in select.options}
    assert "school" not in {option.value for option in select.options}
    assert "transfer" not in {option.value for option in select.options}


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

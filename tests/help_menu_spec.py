"""통합 메뉴의 기능 탐색 UX 명세."""

from types import SimpleNamespace

import discord

from cogs.help_cog import MasamongHomeView


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

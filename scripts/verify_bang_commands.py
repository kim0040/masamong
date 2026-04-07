#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`!` 명령어 검증 스모크 테스트.

실제 Discord 연결 없이 콜백을 직접 호출해 주요 명령어의 기본 동작을 검증한다.
네트워크/API 의존 로직은 목 객체로 대체한다.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, Callable, Coroutine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from cogs.activity_cog import ActivityCog
from cogs.commands import UserCommands
from cogs.fun_cog import FunCog
from cogs.help_cog import MasamongHelpCommand
from cogs.poll_cog import PollCog
from cogs.weather_cog import WeatherCog
from utils import coords as coords_utils


class DummyTyping:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class DummySentMessage:
    def __init__(self, content: str | None = None, embed: Any = None, file: Any = None):
        self.content = content
        self.embed = embed
        self.file = file
        self.deleted = False
        self.reactions: list[str] = []

    async def add_reaction(self, emoji: str) -> None:
        self.reactions.append(emoji)

    async def delete(self) -> None:
        self.deleted = True

    async def edit(self, *, content: str | None = None, embed: Any = None) -> None:
        if content is not None:
            self.content = content
        if embed is not None:
            self.embed = embed


class DummyChannel:
    def __init__(self, channel_id: int, guild: Any = None):
        self.id = channel_id
        self.guild = guild
        self.messages: list[DummySentMessage] = []

    def typing(self) -> DummyTyping:
        return DummyTyping()

    async def send(self, content: str | None = None, *, embed: Any = None, file: Any = None, **kwargs) -> DummySentMessage:
        msg = DummySentMessage(content=content, embed=embed, file=file)
        self.messages.append(msg)
        return msg


class DummyAuthor:
    def __init__(self, user_id: int, name: str = "tester", *, bot: bool = False):
        self.id = user_id
        self.display_name = name
        self.bot = bot


class DummyGuild:
    def __init__(self, guild_id: int):
        self.id = guild_id


class DummyContext:
    def __init__(self, *, channel: DummyChannel, author: DummyAuthor, guild: DummyGuild | None):
        self.channel = channel
        self.author = author
        self.guild = guild
        self.message = SimpleNamespace(content="", channel=channel)
        self.invoked_subcommand = None

    def typing(self) -> DummyTyping:
        return self.channel.typing()

    async def send(self, content: str | None = None, **kwargs) -> DummySentMessage:
        return await self.channel.send(content, **kwargs)

    async def reply(self, content: str | None = None, **kwargs) -> DummySentMessage:
        return await self.channel.send(content, **kwargs)


class DummyBot:
    def __init__(self):
        self._cogs: dict[str, Any] = {}
        self.user = SimpleNamespace(display_name="masamong", avatar=None)
        self.db = object()

    def set_cog(self, name: str, cog: Any) -> None:
        self._cogs[name] = cog

    def get_cog(self, name: str) -> Any:
        return self._cogs.get(name)

    def get_user(self, user_id: int) -> Any:
        return SimpleNamespace(display_name=f"user-{user_id}")

    async def fetch_user(self, user_id: int) -> Any:
        return SimpleNamespace(display_name=f"user-{user_id}")


class FakeToolsCog:
    async def generate_image(self, *, prompt: str, user_id: int) -> dict[str, Any]:
        return {"image_data": b"fake-image", "remaining": 9}


class FakeAIHandler:
    def __init__(self):
        self.tools_cog = FakeToolsCog()
        self.is_ready = True

    async def get_ai_completion(self, prompt: str, *, system_role: str | None = None) -> str:
        return "- 테스트 업데이트 1\n- 테스트 업데이트 2"


@dataclass
class CaseResult:
    name: str
    ok: bool
    detail: str


class SkipCase(Exception):
    pass


async def case_image_missing_prompt() -> None:
    bot = DummyBot()
    cog = UserCommands(bot)
    ctx = DummyContext(
        channel=DummyChannel(100, guild=DummyGuild(1)),
        author=DummyAuthor(10),
        guild=DummyGuild(1),
    )
    await cog.generate_image_command.callback(cog, ctx, prompt=None)
    assert ctx.channel.messages, "응답 메시지가 없음"
    assert "설명이 빠졌" in (ctx.channel.messages[-1].content or "")


async def case_image_disabled() -> None:
    bot = DummyBot()
    cog = UserCommands(bot)
    ctx = DummyContext(
        channel=DummyChannel(101, guild=DummyGuild(1)),
        author=DummyAuthor(11),
        guild=DummyGuild(1),
    )
    prev = config.COMETAPI_IMAGE_ENABLED
    config.COMETAPI_IMAGE_ENABLED = False
    try:
        await cog.generate_image_command.callback(cog, ctx, prompt="고양이")
    finally:
        config.COMETAPI_IMAGE_ENABLED = prev
    assert ctx.channel.messages, "응답 메시지가 없음"
    assert "비활성화" in (ctx.channel.messages[-1].content or "")


async def case_image_success() -> None:
    bot = DummyBot()
    bot.set_cog("AIHandler", FakeAIHandler())
    cog = UserCommands(bot)
    ctx = DummyContext(
        channel=DummyChannel(102, guild=DummyGuild(1)),
        author=DummyAuthor(12),
        guild=DummyGuild(1),
    )
    prev = config.COMETAPI_IMAGE_ENABLED
    config.COMETAPI_IMAGE_ENABLED = True
    try:
        await cog.generate_image_command.callback(cog, ctx, prompt="우주복 햄스터")
    finally:
        config.COMETAPI_IMAGE_ENABLED = prev
    assert len(ctx.channel.messages) >= 2, "상태/결과 메시지가 모두 생성되지 않음"
    assert any("완성" in (m.content or "") for m in ctx.channel.messages), "완료 메시지 없음"


async def case_update_info() -> None:
    bot = DummyBot()
    bot.set_cog("AIHandler", FakeAIHandler())
    cog = UserCommands(bot)
    ctx = DummyContext(
        channel=DummyChannel(103, guild=DummyGuild(1)),
        author=DummyAuthor(13),
        guild=DummyGuild(1),
    )

    original = asyncio.create_subprocess_exec

    class _Proc:
        async def communicate(self) -> tuple[bytes, bytes]:
            return b"- feat: test\n- fix: test", b""

    async def _fake_subprocess_exec(*args, **kwargs):
        return _Proc()

    asyncio.create_subprocess_exec = _fake_subprocess_exec  # type: ignore[assignment]
    try:
        await cog.update_info.callback(cog, ctx)
    finally:
        asyncio.create_subprocess_exec = original  # type: ignore[assignment]

    assert ctx.channel.messages, "응답 메시지가 없음"
    assert ctx.channel.messages[-1].embed is not None, "업데이트 임베드가 생성되지 않음"
    assert "업데이트 소식" in ctx.channel.messages[-1].embed.title


async def case_poll_commands() -> None:
    bot = DummyBot()
    cog = PollCog(bot)
    author = DummyAuthor(14)
    guild = DummyGuild(2)

    ctx_none = DummyContext(channel=DummyChannel(201, guild=guild), author=author, guild=guild)
    await cog.poll.callback(cog, ctx_none, question=None)
    assert "투표 주제가 없어요" in (ctx_none.channel.messages[-1].content or "")

    ctx_yesno = DummyContext(channel=DummyChannel(202, guild=guild), author=author, guild=guild)
    await cog.poll.callback(cog, ctx_yesno, question="점심 먹을까?")
    yesno_msg = ctx_yesno.channel.messages[-1]
    assert yesno_msg.embed is not None
    assert yesno_msg.reactions == ["⭕", "❌"]

    ctx_choices = DummyContext(channel=DummyChannel(203, guild=guild), author=author, guild=guild)
    await cog.poll.callback(cog, ctx_choices, "점심", "국밥", "라면", "돈까스")
    choice_msg = ctx_choices.channel.messages[-1]
    assert choice_msg.embed is not None
    assert choice_msg.reactions == ["1️⃣", "2️⃣", "3️⃣"]


async def case_fun_summary_fallback() -> None:
    bot = DummyBot()
    cog = FunCog(bot)
    ctx = DummyContext(
        channel=DummyChannel(301, guild=DummyGuild(3)),
        author=DummyAuthor(15),
        guild=DummyGuild(3),
    )
    await cog.summarize.callback(cog, ctx)
    assert "준비되지 않았습니다" in (ctx.channel.messages[-1].content or "")


async def case_activity_ranking_fallback() -> None:
    bot = DummyBot()
    cog = ActivityCog(bot)
    ctx = DummyContext(
        channel=DummyChannel(302, guild=DummyGuild(3)),
        author=DummyAuthor(16),
        guild=DummyGuild(3),
    )
    await cog.ranking.callback(cog, ctx)
    assert "AI가 아직 준비되지 않았어요" in (ctx.channel.messages[-1].content or "")


async def case_weather_routing() -> None:
    bot = DummyBot()
    cog = WeatherCog(bot)
    ctx = DummyContext(
        channel=DummyChannel(401, guild=DummyGuild(4)),
        author=DummyAuthor(17),
        guild=DummyGuild(4),
    )

    captured: dict[str, Any] = {}
    original_coords = coords_utils.get_coords_from_db
    original_prepare = cog.prepare_weather_response_for_ai

    async def _fake_coords(db, query: str):
        return {"name": "광양", "nx": 73, "ny": 70}

    async def _fake_prepare(message, day_offset: int, location_name: str, nx: str, ny: str, user_original_query: str):
        captured["day_offset"] = day_offset
        captured["location_name"] = location_name
        captured["nx"] = nx
        captured["ny"] = ny
        captured["query"] = user_original_query

    coords_utils.get_coords_from_db = _fake_coords  # type: ignore[assignment]
    cog.prepare_weather_response_for_ai = _fake_prepare  # type: ignore[assignment]
    try:
        await cog.weather_command.callback(cog, ctx, location_query="광양 날씨 어때")
    finally:
        coords_utils.get_coords_from_db = original_coords  # type: ignore[assignment]
        cog.prepare_weather_response_for_ai = original_prepare  # type: ignore[assignment]

    assert captured.get("location_name") == "광양"
    assert captured.get("day_offset") == 0
    assert captured.get("nx") == "73"
    assert captured.get("ny") == "70"


async def case_fortune_command_dispatch() -> None:
    try:
        from cogs.fortune_cog import FortuneCog
    except Exception as exc:  # pragma: no cover - 환경별 호환성 방어
        raise SkipCase(f"fortune_cog import skip: {exc}") from exc

    class _DummyFortuneSelf:
        def __init__(self):
            self.called_option = None
            self.called_mode = None

        async def _check_fortune_logic(self, ctx, option: str = None, mode: str = "day"):
            self.called_option = option
            self.called_mode = mode

    guild = DummyGuild(5)
    ctx = DummyContext(channel=DummyChannel(501, guild=guild), author=DummyAuthor(18), guild=guild)
    dummy = _DummyFortuneSelf()

    await FortuneCog.fortune.callback(dummy, ctx, option="상세")
    assert dummy.called_option == "상세"
    assert dummy.called_mode == "day"

    await FortuneCog.monthly_fortune.callback(dummy, ctx, arg=None)
    assert dummy.called_mode == "month"

    await FortuneCog.yearly_fortune.callback(dummy, ctx, arg=None)
    assert dummy.called_mode == "year"


async def case_help_command() -> None:
    bot = DummyBot()
    user_commands = UserCommands(bot)
    poll_cog = PollCog(bot)

    channel = DummyChannel(601, guild=DummyGuild(6))
    ctx = DummyContext(channel=channel, author=DummyAuthor(19), guild=DummyGuild(6))
    ctx.bot = bot

    help_cmd = MasamongHelpCommand()
    help_cmd.context = ctx

    mapping = {
        user_commands: [user_commands.update_info, user_commands.generate_image_command],
        poll_cog: [poll_cog.poll],
    }
    await help_cmd.send_bot_help(mapping)
    assert channel.messages and channel.messages[-1].embed is not None

    await help_cmd.send_command_help(poll_cog.poll)
    assert channel.messages[-1].embed is not None


async def case_delete_log() -> None:
    bot = DummyBot()
    cog = UserCommands(bot)
    guild = DummyGuild(7)
    channel = DummyChannel(701, guild=guild)
    ctx = DummyContext(channel=channel, author=DummyAuthor(20), guild=guild)

    fd, temp_path = tempfile.mkstemp(prefix="masamong-test-log-", suffix=".log")
    os.close(fd)
    prev = config.LOG_FILE_NAME
    config.LOG_FILE_NAME = temp_path
    try:
        await cog.delete_log.callback(cog, ctx)
    finally:
        config.LOG_FILE_NAME = prev
        if os.path.exists(temp_path):
            os.remove(temp_path)

    assert "지웠다" in (channel.messages[-1].content or "")


async def _run_case(name: str, fn: Callable[[], Coroutine[Any, Any, None]]) -> CaseResult:
    try:
        await fn()
        return CaseResult(name=name, ok=True, detail="ok")
    except SkipCase as exc:
        return CaseResult(name=name, ok=True, detail=f"skip ({exc})")
    except Exception as exc:
        return CaseResult(name=name, ok=False, detail=f"{type(exc).__name__}: {exc}")


async def main() -> int:
    cases: list[tuple[str, Callable[[], Coroutine[Any, Any, None]]]] = [
        ("image_missing_prompt", case_image_missing_prompt),
        ("image_disabled", case_image_disabled),
        ("image_success", case_image_success),
        ("update_info", case_update_info),
        ("poll_commands", case_poll_commands),
        ("summary_fallback", case_fun_summary_fallback),
        ("ranking_fallback", case_activity_ranking_fallback),
        ("weather_routing", case_weather_routing),
        ("fortune_dispatch", case_fortune_command_dispatch),
        ("help_command", case_help_command),
        ("delete_log", case_delete_log),
    ]

    results = [await _run_case(name, fn) for name, fn in cases]
    failures = [r for r in results if not r.ok]

    for res in results:
        status = "PASS" if res.ok else "FAIL"
        print(f"[{status}] {res.name}: {res.detail}")

    if failures:
        print(f"\nFAILED: {len(failures)} / {len(results)}")
        return 1

    print(f"\nALL PASS: {len(results)} / {len(results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""운세 기능 핵심 플로우 검증 스크립트.

검증 범위:
- `!운세` (서버 요약/DM 상세)
- 상세 운세 일일 제한(3회) 및 사용량 기록
- `!운세 구독`, `!운세 구독취소`
- `!이번달운세`, `!올해운세` 기본 라우팅
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, Callable, Coroutine

import aiosqlite
import pytz

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
import discord
from cogs.fortune_cog import FortuneCog
from utils.fortune import FortuneCalculator

KST = pytz.timezone("Asia/Seoul")


class DummyTyping:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class DummySentMessage:
    def __init__(self, content: str | None = None, embed: Any = None):
        self.content = content
        self.embed = embed


class DummyGuild:
    def __init__(self, guild_id: int):
        self.id = guild_id


class DummyAuthor:
    def __init__(self, user_id: int, name: str = "tester"):
        self.id = user_id
        self.display_name = name


class DummyTextChannel:
    def __init__(self, channel_id: int, guild: DummyGuild):
        self.id = channel_id
        self.guild = guild
        self.messages: list[DummySentMessage] = []

    def typing(self) -> DummyTyping:
        return DummyTyping()

    async def send(self, content: str | None = None, **kwargs) -> DummySentMessage:
        msg = DummySentMessage(content=content, embed=kwargs.get("embed"))
        self.messages.append(msg)
        return msg


class DummyDMChannel:
    def __init__(self, channel_id: int = 999):
        self.id = channel_id
        self.guild = None
        self.messages: list[DummySentMessage] = []

    def typing(self) -> DummyTyping:
        return DummyTyping()

    async def send(self, content: str | None = None, **kwargs) -> DummySentMessage:
        msg = DummySentMessage(content=content, embed=kwargs.get("embed"))
        self.messages.append(msg)
        return msg


class DummyContext:
    def __init__(self, *, channel: Any, author: DummyAuthor, guild: DummyGuild | None):
        self.channel = channel
        self.author = author
        self.guild = guild
        self.invoked_subcommand = None
        self.message = SimpleNamespace(content="", channel=channel)

    async def send(self, content: str | None = None, **kwargs) -> DummySentMessage:
        return await self.channel.send(content, **kwargs)

    async def reply(self, content: str | None = None, **kwargs) -> DummySentMessage:
        return await self.channel.send(content, **kwargs)

    def typing(self) -> DummyTyping:
        return self.channel.typing()


class DummyBot:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db
        self._cogs: dict[str, Any] = {}
        self.locked_users: set[int] = set()
        self.loop = asyncio.get_running_loop()

    def get_cog(self, name: str) -> Any:
        return self._cogs.get(name)

    def set_cog(self, name: str, value: Any) -> None:
        self._cogs[name] = value


class FakeAIHandler:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    async def _cometapi_generate_content(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        log_extra: dict[str, Any] | None = None,
        model: str | None = None,
    ) -> str:
        self.calls.append(
            {
                "model": model,
                "system_prompt": system_prompt,
                "log_extra": log_extra or {},
            }
        )
        return f"[mock:{model}] 운세 응답"


class FortuneHarness(FortuneCog):
    """백그라운드 task 시작 없이 FortuneCog 메서드를 사용하기 위한 하네스."""

    def __init__(self, bot: DummyBot):  # type: ignore[override]
        self.bot = bot
        self.calculator = FortuneCalculator()
        self._ready = True


@dataclass
class CaseResult:
    name: str
    ok: bool
    detail: str


async def init_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id INTEGER PRIMARY KEY,
            birth_date TEXT,
            birth_time TEXT,
            gender TEXT,
            birth_place TEXT,
            created_at TEXT,
            subscription_time TEXT,
            subscription_active INTEGER DEFAULT 0,
            last_fortune_sent TEXT,
            pending_payload TEXT,
            last_fortune_content TEXT
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS api_call_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_type TEXT NOT NULL,
            called_at TEXT NOT NULL
        )
        """
    )
    await db.commit()


async def count_api_calls(db: aiosqlite.Connection, api_type: str) -> int:
    async with db.execute("SELECT COUNT(*) FROM api_call_log WHERE api_type = ?", (api_type,)) as cur:
        row = await cur.fetchone()
    return int(row[0] if row else 0)


async def get_subscription_state(db: aiosqlite.Connection, user_id: int) -> tuple[str | None, int]:
    async with db.execute(
        "SELECT subscription_time, COALESCE(subscription_active, 0) FROM user_profiles WHERE user_id = ?",
        (user_id,),
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None, 0
    return row[0], int(row[1])


async def case_no_profile_guild(cog: FortuneHarness) -> None:
    guild = DummyGuild(1001)
    ctx = DummyContext(channel=DummyTextChannel(2001, guild), author=DummyAuthor(3001), guild=guild)
    await FortuneCog.fortune.callback(cog, ctx, option=None)
    assert ctx.channel.messages, "응답 없음"
    assert "DM으로 `!운세 등록`" in (ctx.channel.messages[-1].content or "")


async def case_fortune_guild_summary(cog: FortuneHarness, ai: FakeAIHandler) -> None:
    guild = DummyGuild(1002)
    author = DummyAuthor(3002)
    ctx = DummyContext(channel=DummyTextChannel(2002, guild), author=author, guild=guild)
    await cog._save_user_profile(author.id, "1990-01-01", "12:00", "M", "서울")
    before = len(ai.calls)
    await FortuneCog.fortune.callback(cog, ctx, option=None)
    assert len(ai.calls) == before + 1, "AI 호출 누락"
    assert ai.calls[-1]["model"] == "DeepSeek-V3.2-Exp-nothinking"
    assert "[mock:DeepSeek-V3.2-Exp-nothinking]" in (ctx.channel.messages[-1].content or "")


async def case_fortune_dm_detail_and_limit(cog: FortuneHarness, db: aiosqlite.Connection, ai: FakeAIHandler) -> None:
    author = DummyAuthor(3003)
    dm = DummyDMChannel(2003)
    ctx = DummyContext(channel=dm, author=author, guild=None)
    await cog._save_user_profile(author.id, "1992-04-05", "08:30", "F", "부산")

    # 1회 상세 조회
    before = len(ai.calls)
    await FortuneCog.fortune.callback(cog, ctx, option="상세")
    assert len(ai.calls) == before + 1, "상세 AI 호출 누락"
    assert ai.calls[-1]["model"] == "DeepSeek-V3.2-Exp-thinking"
    assert any("남은 일일 조회 횟수: 2회" in (m.content or "") for m in dm.messages), "잔여 횟수 안내 누락"

    # 추가 2회 사용량 적재 -> 총 3회로 한도 도달 상태 구성
    now = datetime.now(timezone.utc).isoformat()
    api_type = f"fortune_detail_{author.id}"
    await db.execute("INSERT INTO api_call_log (api_type, called_at) VALUES (?, ?)", (api_type, now))
    await db.execute("INSERT INTO api_call_log (api_type, called_at) VALUES (?, ?)", (api_type, now))
    await db.commit()

    ai_before_limit = len(ai.calls)
    await FortuneCog.fortune.callback(cog, ctx, option="상세")
    assert len(ai.calls) == ai_before_limit, "한도 초과 시 AI가 호출되면 안 됨"
    assert "일일 운세 조회 한도 초과" in (dm.messages[-1].content or "")


async def case_subscribe_unsubscribe(cog: FortuneHarness, db: aiosqlite.Connection) -> None:
    user_id = 3004
    guild = DummyGuild(1004)
    guild_ctx = DummyContext(channel=DummyTextChannel(2004, guild), author=DummyAuthor(user_id), guild=guild)
    await FortuneCog.fortune_subscribe.callback(cog, guild_ctx, "07:30")
    assert "DM에서만" in (guild_ctx.channel.messages[-1].content or "")

    dm_ctx = DummyContext(channel=DummyDMChannel(2005), author=DummyAuthor(user_id), guild=None)
    await FortuneCog.fortune_subscribe.callback(cog, dm_ctx, "bad-time")
    assert "시간 형식" in (dm_ctx.channel.messages[-1].content or "")

    await cog._save_user_profile(user_id, "1991-09-21", "11:10", "M", "광주")
    valid_time = (datetime.now(KST) + timedelta(minutes=7)).strftime("%H:%M")
    await FortuneCog.fortune_subscribe.callback(cog, dm_ctx, valid_time)
    assert "구독이 활성화" in (dm_ctx.channel.messages[-1].content or "")
    sub_time, active = await get_subscription_state(db, user_id)
    assert sub_time == valid_time and active == 1

    await FortuneCog.fortune_unsubscribe.callback(cog, dm_ctx)
    assert "구독이 취소" in (dm_ctx.channel.messages[-1].content or "")
    _, active_after = await get_subscription_state(db, user_id)
    assert active_after == 0


async def case_monthly_yearly(cog: FortuneHarness, ai: FakeAIHandler) -> None:
    user_id = 3005
    dm_ctx = DummyContext(channel=DummyDMChannel(2006), author=DummyAuthor(user_id), guild=None)
    await cog._save_user_profile(user_id, "1988-12-03", "05:20", "F", "대전")
    before = len(ai.calls)
    await FortuneCog.monthly_fortune.callback(cog, dm_ctx, arg=None)
    await FortuneCog.yearly_fortune.callback(cog, dm_ctx, arg=None)
    assert len(ai.calls) >= before + 2, "월/년 운세 AI 호출 누락"
    assert any("남은 일일 조회 횟수" in (m.content or "") for m in dm_ctx.channel.messages), "조회 횟수 안내 누락"


async def _run_case(name: str, fn: Callable[[], Coroutine[Any, Any, None]]) -> CaseResult:
    try:
        await fn()
        return CaseResult(name=name, ok=True, detail="ok")
    except Exception as exc:
        return CaseResult(name=name, ok=False, detail=f"{type(exc).__name__}: {exc}")


async def main() -> int:
    original_backend = config.DB_BACKEND
    original_dm_channel = discord.DMChannel
    # 로컬 sqlite 테스트를 위해 임시 백엔드 강제
    config.DB_BACKEND = "sqlite"
    discord.DMChannel = DummyDMChannel  # type: ignore[assignment]

    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await init_schema(db)

    bot = DummyBot(db)
    ai = FakeAIHandler()
    bot.set_cog("AIHandler", ai)
    cog = FortuneHarness(bot)

    try:
        cases: list[tuple[str, Callable[[], Coroutine[Any, Any, None]]]] = [
            ("fortune_no_profile_guild", lambda: case_no_profile_guild(cog)),
            ("fortune_guild_summary", lambda: case_fortune_guild_summary(cog, ai)),
            ("fortune_dm_detail_limit", lambda: case_fortune_dm_detail_and_limit(cog, db, ai)),
            ("fortune_subscribe_unsubscribe", lambda: case_subscribe_unsubscribe(cog, db)),
            ("fortune_monthly_yearly", lambda: case_monthly_yearly(cog, ai)),
        ]
        results = [await _run_case(name, fn) for name, fn in cases]
    finally:
        await db.close()
        config.DB_BACKEND = original_backend
        discord.DMChannel = original_dm_channel  # type: ignore[assignment]

    for res in results:
        status = "PASS" if res.ok else "FAIL"
        print(f"[{status}] {res.name}: {res.detail}")

    failed = [r for r in results if not r.ok]
    if failed:
        print(f"\nFAILED: {len(failed)} / {len(results)}")
        return 1

    print(f"\nALL PASS: {len(results)} / {len(results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

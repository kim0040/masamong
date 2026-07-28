import asyncio
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

import config
from cogs.fortune_cog import FortuneCog, _ZODIAC_ATTEMPT_API_TYPE


ROOT = Path(__file__).resolve().parents[1]


class _Typing:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_args):
        return None


class _Calculator:
    def _get_astrology_chart(self, _now):
        return "shared chart"


class _AI:
    def __init__(self, result="shared zodiac", *, delay=0):
        self.result = result
        self.delay = delay
        self.calls = 0
        self.prompts = []

    async def _cometapi_generate_content(
        self,
        system_prompt,
        user_prompt,
        **_kwargs,
    ):
        self.calls += 1
        self.prompts.append((system_prompt, user_prompt))
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.result


class _Bot:
    def __init__(self, db, ai):
        self.db = db
        self.ai = ai

    def get_cog(self, name):
        return self.ai if name == "AIHandler" else None


async def _make_cog(ai):
    db = await aiosqlite.connect(":memory:")
    await db.executescript(
        (ROOT / "database" / "schema.sql").read_text(encoding="utf-8")
    )
    await db.commit()
    cog = FortuneCog.__new__(FortuneCog)
    cog.bot = _Bot(db, ai)
    cog.calculator = _Calculator()
    return cog, db


def _ctx(user_id, display_name):
    return SimpleNamespace(
        author=SimpleNamespace(id=user_id, display_name=display_name),
        channel=object(),
        guild=object(),
        typing=lambda: _Typing(),
        send=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_same_zodiac_key_is_singleflight_cached_and_not_user_personalized():
    ai = _AI(delay=0.02)
    cog, db = await _make_cog(ai)
    try:
        cog._has_fortune_consent = AsyncMock(
            side_effect=AssertionError(
                "explicit zodiac must not require fortune privacy consent"
            )
        )
        first = _ctx(1, "alpha")
        second = _ctx(2, "beta")

        await asyncio.gather(
            cog._show_zodiac_fortune(first, "물병자리"),
            cog._show_zodiac_fortune(second, "물병자리"),
        )

        assert ai.calls == 1
        assert "alpha" not in ai.prompts[0][1]
        assert "beta" not in ai.prompts[0][1]
        assert first.send.await_count == 1
        assert second.send.await_count == 1
        async with db.execute(
            "SELECT COUNT(*) FROM api_call_log WHERE api_type = ?",
            (_ZODIAC_ATTEMPT_API_TYPE,),
        ) as cursor:
            assert (await cursor.fetchone())[0] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_zodiac_failure_is_negative_cached_without_second_provider_call():
    ai = _AI(result=None)
    cog, db = await _make_cog(ai)
    try:
        first = _ctx(1, "alpha")
        second = _ctx(2, "beta")

        await cog._show_zodiac_ranking(first)
        await cog._show_zodiac_ranking(second)

        assert ai.calls == 1
        assert "오류" in str(first.send.await_args)
        assert "오류" in str(second.send.await_args)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_zodiac_physical_attempt_cap_is_reserved_before_provider(
    monkeypatch,
):
    monkeypatch.setattr(config, "FORTUNE_ZODIAC_DAILY_PHYSICAL_LIMIT", 1)
    ai = _AI()
    cog, db = await _make_cog(ai)
    try:
        first = _ctx(1, "alpha")
        second = _ctx(2, "beta")

        await cog._show_zodiac_fortune(first, "물병자리")
        await cog._show_zodiac_fortune(second, "사자자리")

        assert ai.calls == 1
        assert "상한" in str(second.send.await_args)
        async with db.execute(
            "SELECT COUNT(*) FROM api_call_log WHERE api_type = ?",
            (_ZODIAC_ATTEMPT_API_TYPE,),
        ) as cursor:
            assert (await cursor.fetchone())[0] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_zodiac_user_cooldown_blocks_distinct_cache_misses(monkeypatch):
    monkeypatch.setattr(config, "FORTUNE_ZODIAC_USER_COOLDOWN_SECONDS", 5)
    ai = _AI()
    cog, db = await _make_cog(ai)
    try:
        ctx = _ctx(1, "alpha")

        await cog._show_zodiac_fortune(ctx, "물병자리")
        await cog._show_zodiac_fortune(ctx, "사자자리")

        assert ai.calls == 1
        assert "뒤에 다시 요청" in str(ctx.send.await_args)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_one_user_cannot_overlap_distinct_zodiac_provider_calls():
    ai = _AI(delay=0.02)
    cog, db = await _make_cog(ai)
    try:
        ctx = _ctx(1, "alpha")
        first = asyncio.create_task(
            cog._bounded_zodiac_generation(
                ctx=ctx,
                key=("first",),
                system_prompt="system",
                user_prompt="first",
                log_extra={"mode": "first"},
            )
        )
        await asyncio.sleep(0)
        second = await cog._bounded_zodiac_generation(
            ctx=ctx,
            key=("second",),
            system_prompt="system",
            user_prompt="second",
            log_extra={"mode": "second"},
        )

        assert second == (None, "user_busy")
        assert await first == ("shared zodiac", "generated")
        assert ai.calls == 1
    finally:
        await db.close()


def test_zodiac_cache_is_bounded_lru(monkeypatch):
    monkeypatch.setattr(config, "FORTUNE_ZODIAC_CACHE_MAX_ENTRIES", 2)
    cog = FortuneCog.__new__(FortuneCog)
    cog._zodiac_cache = OrderedDict()

    cog._zodiac_cache_put(("one",), "1", negative=False, now=0)
    cog._zodiac_cache_put(("two",), "2", negative=False, now=0)
    assert cog._zodiac_cache_get(("one",), now=1) == (True, "1")
    cog._zodiac_cache_put(("three",), "3", negative=False, now=1)

    assert list(cog._zodiac_cache) == [("one",), ("three",)]
    assert cog._zodiac_cache_get(("two",), now=1) == (False, None)

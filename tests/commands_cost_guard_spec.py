"""비용이 큰 사용자 명령의 cache/cooldown/precheck 회귀 테스트."""

import asyncio
from types import SimpleNamespace

import pytest

import config
from cogs.commands import UserCommands


class _FakeBot:
    def __init__(self, ai_handler=None) -> None:
        self.ai_handler = ai_handler

    def get_cog(self, name: str):
        return self.ai_handler if name == "AIHandler" else None


class _FakeAI:
    def __init__(self, tools_cog=None) -> None:
        self.tools_cog = tools_cog
        self.summary_calls = 0
        self.image_prompt_calls = 0

    async def get_ai_completion(self, *_args, **_kwargs):
        self.summary_calls += 1
        await asyncio.sleep(0.01)
        return "업데이트 요약"

    async def _generate_image_prompt(self, *_args, **_kwargs):
        self.image_prompt_calls += 1
        return "optimized prompt"


class _QuotaBlockedTools:
    async def check_image_quota(self, _user_id):
        return {"allowed": False, "error": "테스트 한도 초과"}

    async def generate_image(self, **_kwargs):
        raise AssertionError("제한 초과 요청은 이미지 API까지 도달하면 안 됩니다.")


class _FakeContext:
    def __init__(self) -> None:
        self.guild = SimpleNamespace(id=10)
        self.author = SimpleNamespace(id=20)
        self.sent: list[dict] = []

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})
        return SimpleNamespace()


@pytest.mark.asyncio
async def test_update_summary_is_singleflight_cached_by_head(monkeypatch):
    ai = _FakeAI()
    cog = UserCommands(_FakeBot(ai))
    current_head = "head-a"
    git_log_calls = 0

    async def fake_git(*args, **_kwargs):
        nonlocal git_log_calls
        if args[:2] == ("rev-parse", "HEAD"):
            return current_head
        git_log_calls += 1
        return "- 변경 A\n- 변경 B"

    monkeypatch.setattr(cog, "_run_git", fake_git)

    first = await asyncio.gather(
        cog._get_update_summary(ai),
        cog._get_update_summary(ai),
        cog._get_update_summary(ai),
    )

    assert first == ["업데이트 요약"] * 3
    assert git_log_calls == 1
    assert ai.summary_calls == 1

    # HEAD가 바뀌면 TTL이 남아 있어도 즉시 새 요약을 만든다.
    current_head = "head-b"
    assert await cog._get_update_summary(ai) == "업데이트 요약"
    assert git_log_calls == 2
    assert ai.summary_calls == 2


def test_update_cache_is_bounded_lru():
    cog = UserCommands(_FakeBot())
    cog._update_cache_max_entries = 2
    cog._update_cache_ttl_seconds = 100

    cog._store_update_summary("head-a", "A", now=1)
    cog._store_update_summary("head-b", "B", now=2)
    assert cog._get_cached_update_summary("head-a", now=3) == (True, "A")
    cog._store_update_summary("head-c", "C", now=4)

    assert list(cog._update_cache) == ["head-a", "head-c"]
    assert cog._get_cached_update_summary("head-b", now=5) == (False, None)


def test_update_user_cooldown_is_per_user_and_bounded():
    cog = UserCommands(_FakeBot())
    cog._update_user_cooldown_seconds = 30
    cog._update_user_cooldown_max_entries = 32

    assert cog._consume_update_cooldown(1, now=100) == (True, 0)
    allowed, remaining = cog._consume_update_cooldown(1, now=101)
    assert allowed is False
    assert remaining == 29
    assert cog._consume_update_cooldown(2, now=101) == (True, 0)
    assert cog._consume_update_cooldown(1, now=131) == (True, 0)

    for user_id in range(3, 100):
        cog._consume_update_cooldown(user_id, now=200 + user_id)
    assert len(cog._update_user_cooldowns) <= 32


@pytest.mark.asyncio
async def test_image_quota_precheck_skips_prompt_optimization(monkeypatch):
    tools = _QuotaBlockedTools()
    ai = _FakeAI(tools)
    cog = UserCommands(_FakeBot(ai))
    ctx = _FakeContext()
    monkeypatch.setattr(config, "COMETAPI_IMAGE_ENABLED", True)

    await UserCommands.generate_image_command.callback(
        cog,
        ctx,
        prompt="고양이",
    )

    assert ai.image_prompt_calls == 0
    assert len(ctx.sent) == 1
    assert "테스트 한도 초과" in ctx.sent[0]["content"]

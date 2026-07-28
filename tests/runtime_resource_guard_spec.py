"""저사양 런타임의 대기열·캐시·executor 상한 회귀 테스트."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import config
from main import ReMasamongBot


@pytest.mark.asyncio
async def test_ai_queue_timeout_drops_request_without_running_handler(monkeypatch):
    bot = ReMasamongBot(command_prefix="!", intents=discord.Intents.none())
    fake_ai = SimpleNamespace(
        is_ready=True,
        ai_processing_semaphore=asyncio.Semaphore(0),
        add_message_to_history=AsyncMock(),
        process_agent_message=AsyncMock(),
        _message_has_valid_mention=lambda _message: True,
    )
    monkeypatch.setattr(
        bot,
        "get_cog",
        lambda name: fake_ai if name == "AIHandler" else None,
    )
    monkeypatch.setattr(config, "AI_QUEUE_WAIT_TIMEOUT_SECONDS", 0.01)
    channel = SimpleNamespace(id=456, send=AsyncMock())
    message = SimpleNamespace(
        author=SimpleNamespace(bot=False, id=123),
        guild=None,
        channel=channel,
        content="마사몽아 알려줘",
    )

    try:
        await bot.on_message(message)
    finally:
        await bot.close()

    fake_ai.add_message_to_history.assert_awaited_once()
    fake_ai.process_agent_message.assert_not_awaited()
    channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_ai_handler_timeout_is_not_misreported_as_queue_saturation(monkeypatch):
    bot = ReMasamongBot(command_prefix="!", intents=discord.Intents.none())
    semaphore = asyncio.Semaphore(1)
    fake_ai = SimpleNamespace(
        is_ready=True,
        ai_processing_semaphore=semaphore,
        add_message_to_history=AsyncMock(),
        process_agent_message=AsyncMock(side_effect=asyncio.TimeoutError()),
        _message_has_valid_mention=lambda _message: True,
    )
    monkeypatch.setattr(
        bot,
        "get_cog",
        lambda name: fake_ai if name == "AIHandler" else None,
    )
    channel = SimpleNamespace(id=456, send=AsyncMock())
    message = SimpleNamespace(
        author=SimpleNamespace(bot=False, id=123),
        guild=None,
        channel=channel,
        content="마사몽아 알려줘",
    )

    try:
        await bot.on_message(message)
    finally:
        await bot.close()

    fake_ai.process_agent_message.assert_awaited_once()
    channel.send.assert_not_awaited()
    assert not semaphore.locked()


def test_runtime_resource_limits_are_bounded():
    assert 1 <= config.EXECUTOR_WORKERS <= 16
    assert 1 <= config.AI_QUEUE_WAIT_TIMEOUT_SECONDS <= 30
    assert 50 <= config.DISCORD_MAX_MESSAGES <= 1_000

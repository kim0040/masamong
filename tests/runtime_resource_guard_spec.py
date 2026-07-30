"""저사양 런타임의 대기열·캐시·executor 상한 회귀 테스트."""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import config
from cogs.ai_handler import AIHandler, _QueuedAIRequest
from utils import db as db_utils
from main import ReMasamongBot


@pytest.mark.asyncio
async def test_on_message_delegates_ai_work_to_fifo_queue(monkeypatch):
    bot = ReMasamongBot(command_prefix="!", intents=discord.Intents.none())
    fake_ai = SimpleNamespace(
        is_ready=True,
        add_message_to_history=AsyncMock(),
        process_agent_message=AsyncMock(),
        enqueue_message=AsyncMock(return_value=True),
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

    fake_ai.add_message_to_history.assert_awaited_once()
    fake_ai.process_agent_message.assert_not_awaited()
    fake_ai.enqueue_message.assert_awaited_once_with(message)
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_message_queue_error_does_not_run_handler_directly(monkeypatch):
    bot = ReMasamongBot(command_prefix="!", intents=discord.Intents.none())
    fake_ai = SimpleNamespace(
        is_ready=True,
        add_message_to_history=AsyncMock(),
        process_agent_message=AsyncMock(),
        enqueue_message=AsyncMock(side_effect=RuntimeError("queue failed")),
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

    fake_ai.process_agent_message.assert_not_awaited()
    fake_ai.enqueue_message.assert_awaited_once_with(message)
    channel.send.assert_not_awaited()


def _queue_handler(*, workers: int = 2, capacity: int = 8) -> AIHandler:
    handler = AIHandler.__new__(AIHandler)
    handler._ai_worker_count = workers
    handler.ai_processing_semaphore = asyncio.Semaphore(workers)
    handler.ai_request_queue = asyncio.Queue(maxsize=capacity)
    handler._ai_queue_workers = []
    handler._ai_queue_start_lock = asyncio.Lock()
    handler._ai_queue_closing = False
    handler._ai_active_requests = 0
    return handler


def _queue_message(message_id: int, guild_id: int):
    notice = SimpleNamespace(
        edit=AsyncMock(),
        delete=AsyncMock(),
    )
    notice.edit.return_value = notice
    channel = SimpleNamespace(
        id=message_id + 10_000,
        send=AsyncMock(return_value=notice),
    )
    message = SimpleNamespace(
        id=message_id,
        author=SimpleNamespace(id=message_id + 100, bot=False),
        guild=SimpleNamespace(id=guild_id),
        channel=channel,
        content=f"질문 {message_id}",
    )
    return message, notice


@pytest.mark.asyncio
async def test_fifo_queue_processes_mixed_guilds_with_bounded_workers():
    handler = _queue_handler(workers=2, capacity=8)
    release_first_pair = asyncio.Event()
    started: list[int] = []
    active = 0
    max_active = 0

    async def _process(message, *, queue_request=None):
        nonlocal active, max_active
        queue_request.status_claimed = True
        started.append(message.id)
        active += 1
        max_active = max(max_active, active)
        try:
            if message.id in {1, 2}:
                await release_first_pair.wait()
        finally:
            active -= 1

    handler.process_agent_message = _process
    messages = [
        _queue_message(1, 100)[0],
        _queue_message(2, 200)[0],
        _queue_message(3, 100)[0],
        _queue_message(4, 200)[0],
    ]

    try:
        for message in messages:
            assert await handler.enqueue_message(message) is True

        for _ in range(100):
            if len(started) >= 2:
                break
            await asyncio.sleep(0)
        assert started[:2] == [1, 2]
        assert max_active == 2

        release_first_pair.set()
        await asyncio.wait_for(handler.ai_request_queue.join(), timeout=1)
        assert started == [1, 2, 3, 4]
        assert max_active == 2
    finally:
        await handler.close_ai_queue()


@pytest.mark.asyncio
async def test_fifo_queue_full_is_rejected_without_starting_extra_work():
    handler = _queue_handler(workers=1, capacity=1)
    blocker = asyncio.create_task(asyncio.Event().wait())
    handler._ai_queue_workers = [blocker]
    queued_message, queued_notice = _queue_message(1, 100)
    handler.ai_request_queue.put_nowait(
        _QueuedAIRequest(
            message=queued_message,
            enqueued_at=0.0,
            notice=queued_notice,
        )
    )
    rejected_message, _notice = _queue_message(2, 200)

    try:
        assert await handler.enqueue_message(rejected_message) is False
        rejected_message.channel.send.assert_awaited_once()
        assert handler.ai_request_queue.qsize() == 1
    finally:
        await handler.close_ai_queue()


@pytest.mark.asyncio
async def test_delayed_request_history_stops_before_original_message():
    captured = {}
    older_author = SimpleNamespace(id=55, display_name="과거 사용자")
    older = SimpleNamespace(
        id=9,
        author=older_author,
        content="원래 질문보다 앞선 대화",
    )

    class _Channel:
        async def history(self, **kwargs):
            captured.update(kwargs)
            yield older

    message = SimpleNamespace(
        id=10,
        author=SimpleNamespace(id=77),
        channel=_Channel(),
    )
    handler = AIHandler.__new__(AIHandler)
    handler.bot = SimpleNamespace(user=SimpleNamespace(id=999))

    history = await handler._get_recent_history(message, "")

    assert captured["before"] is message
    assert captured["limit"] >= 1
    assert history[0]["parts"] == ["원래 질문보다 앞선 대화"]


@pytest.mark.asyncio
async def test_queued_distinct_question_is_not_dropped_by_processing_time_cooldown(
    monkeypatch,
):
    """앞 요청 직후 dequeue된 항목도 접수됐으면 안전장치까지 진행해야 합니다."""
    handler = AIHandler.__new__(AIHandler)
    handler.use_cometapi = True
    handler.bot = SimpleNamespace(db=object())
    handler.tools_cog = object()
    handler.ai_user_cooldowns = {77: datetime.now()}
    handler._spam_cache = {}
    handler._prepare_user_query = lambda *_args, **_kwargs: None
    daily_counts = AsyncMock(return_value={})
    monkeypatch.setattr(db_utils, "get_daily_api_counts", daily_counts)

    message = SimpleNamespace(
        id=10,
        author=SimpleNamespace(id=77),
        guild=SimpleNamespace(id=100),
        channel=SimpleNamespace(id=200),
        content="두 번째로 접수한 서로 다른 질문",
    )
    request = _QueuedAIRequest(
        message=message,
        enqueued_at=0.0,
    )

    await handler.process_agent_message(message, queue_request=request)

    daily_counts.assert_awaited_once()


def test_runtime_resource_limits_are_bounded():
    assert 1 <= config.EXECUTOR_WORKERS <= 16
    assert config.AI_MAX_CONCURRENT_PROCESSING <= config.AI_QUEUE_MAX_SIZE <= 64
    assert 50 <= config.DISCORD_MAX_MESSAGES <= 1_000

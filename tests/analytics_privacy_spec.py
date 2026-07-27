import json
from datetime import datetime, timedelta

import aiosqlite
import pytest

import config
from cogs.ai_handler import AIHandler
from utils import db as db_utils


async def _analytics_db():
    db = await aiosqlite.connect(":memory:")
    await db.execute(
        """
        CREATE TABLE analytics_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            log_timestamp TEXT NOT NULL,
            event_type TEXT NOT NULL,
            guild_id,
            user_id,
            details TEXT
        )
        """
    )
    await db.commit()
    return db


@pytest.mark.asyncio
async def test_log_analytics_persists_only_allowlisted_metadata_by_default(
    monkeypatch,
):
    monkeypatch.setattr(config, "ANALYTICS_STORE_CONTENT", False)
    db = await _analytics_db()
    details = {
        "guild_id": 1,
        "user_id": 2,
        "channel_id": 3,
        "trace_id": "trace-1",
        "user_query_chars": 12,
        "final_response_chars": 34,
        "tools": ["weather"],
        "user_query": "민감한 질문",
        "final_response": "민감한 답변",
        "tool_plan": [{"params": {"prompt": "민감한 프롬프트"}}],
        "error_message": "사용자가 입력한 비밀값",
        "unexpected_content": "allowlist 밖 원문",
    }

    try:
        await db_utils.log_analytics(db, "AI_INTERACTION", details)
        async with db.execute(
            "SELECT guild_id, user_id, details FROM analytics_log"
        ) as cursor:
            row = await cursor.fetchone()
    finally:
        await db.close()

    stored = json.loads(row[2])
    assert stored == {
        "guild_id": 1,
        "user_id": 2,
        "channel_id": 3,
        "trace_id": "trace-1",
        "user_query_chars": 12,
        "final_response_chars": 34,
        "tools": ["weather"],
    }
    assert "민감한" not in row[2]
    assert details["user_query"] == "민감한 질문"


@pytest.mark.asyncio
async def test_log_analytics_preserves_content_only_when_explicitly_enabled(
    monkeypatch,
):
    monkeypatch.setattr(config, "ANALYTICS_STORE_CONTENT", True)
    db = await _analytics_db()
    details = {
        "guild_id": 1,
        "user_id": 2,
        "user_query": "opt-in content",
        "final_response": "opt-in response",
        "error_message": "opt-in error",
    }

    try:
        await db_utils.log_analytics(db, "AI_INTERACTION", details)
        async with db.execute(
            "SELECT details FROM analytics_log"
        ) as cursor:
            row = await cursor.fetchone()
    finally:
        await db.close()

    assert json.loads(row[0]) == details


def test_dm_interaction_analytics_uses_null_guild_id():
    class _Author:
        id = 2

    class _Channel:
        id = 3

    class _Message:
        guild = None
        author = _Author()
        channel = _Channel()

    details = AIHandler._build_interaction_analytics(
        message=_Message(),
        trace_id="trace-dm",
        user_query="hello",
        final_response="world",
        tool_plan=[],
    )

    assert details["guild_id"] is None


@pytest.mark.asyncio
async def test_log_analytics_always_writes_an_explicit_utc_timestamp(monkeypatch):
    monkeypatch.setattr(config, "ANALYTICS_STORE_CONTENT", False)
    db = await _analytics_db()
    try:
        await db_utils.log_analytics(
            db,
            "HEALTH_CHECK",
            {"guild_id": 1, "user_id": 2},
        )
        async with db.execute(
            "SELECT log_timestamp FROM analytics_log"
        ) as cursor:
            row = await cursor.fetchone()
    finally:
        await db.close()

    timestamp = datetime.fromisoformat(row[0])
    assert timestamp.utcoffset() == timedelta(0)

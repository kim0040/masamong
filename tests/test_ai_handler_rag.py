import pickle
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import aiosqlite

from cogs.ai_handler import AIHandler


async def _setup_in_memory_db():
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    schema_sql = Path("database/schema.sql").read_text(encoding="utf-8")
    await db.executescript(schema_sql)
    return db


@pytest.mark.asyncio
async def test_get_rag_context_returns_top_similar_message(monkeypatch):
    db = await _setup_in_memory_db()

    dummy_bot = SimpleNamespace(db=db, get_cog=lambda name: None)
    handler = AIHandler(dummy_bot)
    handler.gemini_configured = True
    handler.embedding_model_name = "gemini-embedding-001"

    # Seed two conversation rows with different embeddings
    vector_relevant = np.array([0.95, 0.05], dtype=float)
    vector_irrelevant = np.array([0.0, 1.0], dtype=float)

    await db.execute(
        "INSERT INTO conversation_history (message_id, guild_id, channel_id, user_id, user_name, content, is_bot, created_at, embedding) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            1,
            123,
            456,
            111,
            "tester",
            "첫 번째 메시지",
            0,
            "2025-01-01T00:00:00",
            pickle.dumps(vector_relevant),
        ),
    )

    await db.execute(
        "INSERT INTO conversation_history (message_id, guild_id, channel_id, user_id, user_name, content, is_bot, created_at, embedding) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            2,
            123,
            456,
            222,
            "tester2",
            "두 번째 메시지",
            0,
            "2025-01-01T00:01:00",
            pickle.dumps(vector_irrelevant),
        ),
    )
    await db.commit()

    async def fake_embed(model_name, content, task_type, log_extra):
        assert task_type == "retrieval_query"
        return {"embedding": [0.9, 0.1]}

    monkeypatch.setattr(handler, "_safe_embed_content", fake_embed)

    context_text, top_contents = await handler._get_rag_context(123, 456, 111, "테스트 질문")

    assert "첫 번째 메시지" in context_text
    assert "두 번째 메시지" not in context_text
    assert top_contents == ["첫 번째 메시지"]

    await db.close()

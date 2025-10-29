from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import aiosqlite

import config
from cogs.ai_handler import AIHandler


async def _setup_in_memory_db():
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    schema_sql = Path("database/schema.sql").read_text(encoding="utf-8")
    await db.executescript(schema_sql)
    return db


@pytest.mark.asyncio
async def test_get_rag_context_returns_top_similar_message(monkeypatch, tmp_path):
    db = await _setup_in_memory_db()

    temp_embed_db = tmp_path / "discord_embeddings.db"
    monkeypatch.setattr(config, "DISCORD_EMBEDDING_DB_PATH", str(temp_embed_db))
    monkeypatch.setattr(config, "BM25_DATABASE_PATH", str(tmp_path / "bm25.db"))
    monkeypatch.setattr(config, "RAG_QUERY_REWRITE_ENABLED", False)
    monkeypatch.setattr(config, "RAG_RERANKER_MODEL_NAME", "")

    dummy_bot = SimpleNamespace(db=db, get_cog=lambda name: None)
    handler = AIHandler(dummy_bot)
    handler.gemini_configured = True

    vector_relevant = np.array([0.95, 0.05], dtype=np.float32)
    vector_irrelevant = np.array([0.0, 1.0], dtype=np.float32)

    await handler.discord_embedding_store.upsert_message_embedding(
        message_id=1,
        server_id=123,
        channel_id=456,
        user_id=111,
        user_name="tester",
        message="첫 번째 메시지",
        timestamp_iso="2025-01-01T00:00:00",
        embedding=vector_relevant,
    )

    await handler.discord_embedding_store.upsert_message_embedding(
        message_id=2,
        server_id=123,
        channel_id=456,
        user_id=111,
        user_name="tester",
        message="두 번째 메시지",
        timestamp_iso="2025-01-01T00:01:00",
        embedding=vector_irrelevant,
    )

    async def fake_get_embedding(_content: str):
        return np.array([0.9, 0.1], dtype=np.float32)

    monkeypatch.setattr("utils.embeddings.get_embedding", fake_get_embedding)
    monkeypatch.setattr("utils.hybrid_search.get_embedding", fake_get_embedding)
    monkeypatch.setattr("cogs.ai_handler.get_embedding", fake_get_embedding)

    context_text, top_entries, top_similarity = await handler._get_rag_context(123, 456, 111, "테스트 질문")

    assert "첫 번째 메시지" in context_text
    assert "두 번째 메시지" not in context_text
    assert isinstance(top_entries, list)
    assert top_entries and top_entries[0]["message"] == "첫 번째 메시지"
    assert "context_window" in top_entries[0]
    assert top_entries[0]["origin"] == "Discord"
    assert top_similarity > 0.0

    await db.close()

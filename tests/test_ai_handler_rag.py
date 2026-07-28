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
    monkeypatch.setattr(config, "SEARCH_QUERY_EXPANSION_ENABLED", False)
    monkeypatch.setattr(config, "RAG_RERANKER_MODEL_NAME", "")
    monkeypatch.setattr(config, "AI_MEMORY_ENABLED", True)
    monkeypatch.setattr(config, "EMBEDDING_ENABLED", True)
    monkeypatch.setattr(config, "DB_BACKEND", "sqlite")
    monkeypatch.setattr(config, "REMOTE_DB_STRICT_MODE", False)
    monkeypatch.setattr(config, "DISCORD_EMBEDDING_BACKEND", "sqlite")
    monkeypatch.setattr(config, "KAKAO_STORE_BACKEND", "local")

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

    async def fake_get_embedding(_content: str, prefix: str = ""):
        return np.array([0.9, 0.1], dtype=np.float32)

    monkeypatch.setattr("utils.embeddings.get_embedding", fake_get_embedding)
    monkeypatch.setattr("utils.hybrid_search.get_embedding", fake_get_embedding)

    context_text, top_entries, top_score, rag_blocks = await handler._get_rag_context(123, 456, 111, "테스트 질문")

    assert "첫 번째 메시지" in context_text
    assert "두 번째 메시지" not in context_text
    assert isinstance(top_entries, list)
    assert top_entries and "dialogue_block" in top_entries[0]
    assert "첫 번째 메시지" in top_entries[0]["dialogue_block"]
    assert top_entries[0]["combined_score"] > 0.0
    assert top_score > 0.0
    assert rag_blocks and "첫 번째 메시지" in rag_blocks[0]

    await db.close()


@pytest.mark.asyncio
async def test_rag_info_logs_exclude_query_and_retrieved_content(monkeypatch):
    sensitive_query = "로그에 남으면 안 되는 질문"
    sensitive_dialogue = "로그에 남으면 안 되는 대화 원문"

    class _Engine:
        async def search(self, *_args, **_kwargs):
            return SimpleNamespace(
                entries=[
                    {
                        "combined_score": 0.91,
                        "dialogue_block": sensitive_dialogue,
                        "origin": "discord",
                        "message_id": 777,
                    }
                ],
                top_score=0.91,
            )

    class _RecordingLogger:
        def __init__(self):
            self.lines = []

        def info(self, message, *args, **_kwargs):
            self.lines.append(message % args if args else str(message))

        def warning(self, message, *args, **_kwargs):
            self.lines.append(message % args if args else str(message))

        def error(self, message, *args, **_kwargs):
            self.lines.append(message % args if args else str(message))

        def debug(self, message, *args, **_kwargs):
            self.lines.append(message % args if args else str(message))

    recorder = _RecordingLogger()
    monkeypatch.setattr("cogs.ai_handler.logger", recorder)
    monkeypatch.setattr(config, "AI_MEMORY_ENABLED", True)
    monkeypatch.setattr(config, "EMBEDDING_ENABLED", True)
    monkeypatch.setattr(config, "RAG_GUILD_SCOPE", "channel")
    monkeypatch.setattr(config, "RAG_HYBRID_TOP_K", 4)
    monkeypatch.setattr(config, "RAG_SIMILARITY_THRESHOLD", 0.6)
    handler = AIHandler.__new__(AIHandler)
    handler.hybrid_search_engine = _Engine()

    context, entries, score, _blocks = await handler._get_rag_context(
        123,
        456,
        789,
        sensitive_query,
    )

    rendered_logs = "\n".join(recorder.lines)
    assert sensitive_dialogue in context
    assert entries and score == pytest.approx(0.91)
    assert sensitive_query not in rendered_logs
    assert sensitive_dialogue not in rendered_logs
    assert "message_id=777" in rendered_logs


@pytest.mark.asyncio
async def test_structured_memory_uses_its_own_acceptance_threshold(monkeypatch):
    """구조화 검색을 통과한 0.50대 결과를 전역 0.60으로 다시 버리지 않는다."""

    class _Engine:
        async def search(self, *_args, **_kwargs):
            return SimpleNamespace(
                entries=[
                    {
                        "combined_score": 0.55,
                        "acceptance_threshold": 0.50,
                        "dialogue_block": "구조화 기억",
                        "origin": "structured_memory",
                        "message_id": "memory:1",
                    },
                    {
                        "combined_score": 0.59,
                        "dialogue_block": "레거시 기억",
                        "origin": "discord",
                        "message_id": 2,
                    },
                ],
                top_score=0.59,
            )

    monkeypatch.setattr(config, "AI_MEMORY_ENABLED", True)
    monkeypatch.setattr(config, "EMBEDDING_ENABLED", True)
    monkeypatch.setattr(config, "RAG_GUILD_SCOPE", "channel")
    monkeypatch.setattr(config, "RAG_HYBRID_TOP_K", 4)
    monkeypatch.setattr(config, "RAG_SIMILARITY_THRESHOLD", 0.60)
    handler = AIHandler.__new__(AIHandler)
    handler.hybrid_search_engine = _Engine()

    context, entries, _score, blocks = await handler._get_rag_context(
        1,
        2,
        3,
        "기억 테스트",
    )

    assert "구조화 기억" in context
    assert "레거시 기억" not in context
    assert [entry["message_id"] for entry in entries] == ["memory:1"]
    assert blocks == ["구조화 기억"]


def test_disabled_kakao_memory_does_not_construct_kakao_store(monkeypatch):
    class _UnexpectedKakaoStore:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("general profile must not construct Kakao storage")

    monkeypatch.setattr(config, "KAKAO_MEMORY_ENABLED", False)
    monkeypatch.setattr(config, "KAKAO_EMBEDDING_DB_PATH", "/unused/kakao.db")
    monkeypatch.setattr(
        config,
        "KAKAO_EMBEDDING_SERVER_MAP",
        [{"server_id": "123", "room_key": "must-not-load"}],
    )
    monkeypatch.setattr(
        "cogs.ai_handler.KakaoEmbeddingStore",
        _UnexpectedKakaoStore,
    )
    handler = AIHandler(
        SimpleNamespace(db=None, get_cog=lambda _name: None)
    )

    assert handler.kakao_embedding_store is None

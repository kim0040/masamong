import numpy as np
import pytest

import config
from utils.hybrid_search import HybridSearchEngine


class DummyDiscordStore:
    async def fetch_recent_embeddings(self, server_id, channel_id, user_id, limit):
        return [
            {
                "message_id": 1,
                "message": "하이브리드 검색 테스트 관련 메시지",
                "embedding": np.array([0.9, 0.1], dtype=np.float32),
                "user_name": "tester",
                "timestamp": "2025-01-01T00:00:00",
            },
            {
                "message_id": 2,
                "message": "무관한 대화",
                "embedding": np.array([0.0, 1.0], dtype=np.float32),
                "user_name": "tester",
                "timestamp": "2025-01-01T00:01:00",
            },
        ]


@pytest.mark.asyncio
async def test_hybrid_search_returns_embedding_match(monkeypatch):
    monkeypatch.setattr(config, "SEARCH_QUERY_EXPANSION_ENABLED", False)
    monkeypatch.setattr(config, "RAG_SIMILARITY_THRESHOLD", 0.1)
    monkeypatch.setattr(config, "RAG_EMBEDDING_TOP_N", 5)
    monkeypatch.setattr(config, "RAG_HYBRID_TOP_K", 3)

    async def fake_get_embedding(text: str):
        if "테스트" in text:
            return np.array([0.92, 0.08], dtype=np.float32)
        return np.array([0.0, 1.0], dtype=np.float32)

    monkeypatch.setattr("utils.embeddings.get_embedding", fake_get_embedding)
    monkeypatch.setattr("utils.hybrid_search.get_embedding", fake_get_embedding)

    engine = HybridSearchEngine(
        discord_store=DummyDiscordStore(),
        kakao_store=None,
        bm25_manager=None,
        reranker=None,
    )

    result = await engine.search(
        "하이브리드 검색 테스트",
        guild_id=123,
        channel_id=456,
        user_id=789,
    )

    assert result.entries, "최소 한 개의 결과가 반환되어야 합니다."
    top_entry = result.entries[0]
    assert "하이브리드 검색 테스트 관련 메시지" in (top_entry.get("dialogue_block") or "")
    assert top_entry["origin"] == "Discord"
    assert top_entry["combined_score"] > 0.0

import numpy as np
import pytest

import config
from utils.hybrid_search import HybridSearchEngine


class DummyDiscordStore:
    def __init__(self):
        self.structured_calls = 0
        self.legacy_calls = 0

    async def fetch_recent_embeddings(self, server_id, channel_id, user_id, limit):
        self.legacy_calls += 1
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

    async def fetch_recent_memory_entries(self, *, server_id, channel_id, user_id=None, limit=200, query_vector=None):
        self.structured_calls += 1
        return []


@pytest.mark.asyncio
async def test_hybrid_search_returns_embedding_match(monkeypatch):
    monkeypatch.setattr(config, "SEARCH_QUERY_EXPANSION_ENABLED", False)
    monkeypatch.setattr(config, "RAG_SIMILARITY_THRESHOLD", 0.1)
    monkeypatch.setattr(config, "RAG_EMBEDDING_TOP_N", 5)
    monkeypatch.setattr(config, "RAG_HYBRID_TOP_K", 3)

    async def fake_get_embedding(text: str, prefix: str = ""):
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


@pytest.mark.asyncio
async def test_query_variants_reuse_same_discord_database_rows(monkeypatch):
    monkeypatch.setattr(config, "SEARCH_QUERY_EXPANSION_ENABLED", False)
    monkeypatch.setattr(config, "RAG_QUERY_REWRITE_VARIANTS", 3)
    monkeypatch.setattr(config, "RAG_SIMILARITY_THRESHOLD", 0.1)

    async def fake_get_embedding(_text: str, prefix: str = ""):
        return np.array([0.9, 0.1], dtype=np.float32)

    monkeypatch.setattr("utils.hybrid_search.get_embedding", fake_get_embedding)
    store = DummyDiscordStore()
    engine = HybridSearchEngine(
        discord_store=store,
        kakao_store=None,
        bm25_manager=None,
    )

    result = await engine.search(
        "후속 질문",
        guild_id=123,
        channel_id=456,
        user_id=789,
        recent_messages=["직전 대화 주제"],
        deep_search=True,
    )

    assert len(result.query_variants) == 2
    assert store.structured_calls == 1
    assert store.legacy_calls == 1


@pytest.mark.asyncio
async def test_shallow_search_uses_one_variant_to_bound_database_work(monkeypatch):
    monkeypatch.setattr(config, "SEARCH_QUERY_EXPANSION_ENABLED", False)
    monkeypatch.setattr(config, "RAG_QUERY_REWRITE_VARIANTS", 3)
    monkeypatch.setattr(config, "RAG_SIMILARITY_THRESHOLD", 0.1)

    async def fake_get_embedding(_text: str, prefix: str = ""):
        return np.array([0.9, 0.1], dtype=np.float32)

    monkeypatch.setattr("utils.hybrid_search.get_embedding", fake_get_embedding)
    store = DummyDiscordStore()
    engine = HybridSearchEngine(store, None, None)

    result = await engine.search(
        "후속 질문",
        guild_id=123,
        channel_id=456,
        user_id=789,
        recent_messages=["직전 대화 주제"],
        deep_search=False,
    )

    assert result.query_variants == ["후속 질문"]
    assert store.structured_calls == 1
    assert store.legacy_calls == 1


def test_lexical_name_bonus_handles_korean_postposition():
    row = {
        "summary_text": "김재원은 부산 여행을 다녀왔다.",
        "raw_context": "서버 대화에서 김재원의 여행 이야기를 나눴다.",
    }

    score = HybridSearchEngine._lexical_relevance(
        "김재원이 누구야?",
        row,
    )

    assert score >= 0.04


def test_overlapping_structured_memories_do_not_monopolize_top_k():
    entries = [
        {
            "candidate_id": "shared-window",
            "dialogue_block": "김재원: 부산 여행은 KTX로 간다.",
            "source_message_ids": "[1, 2, 3, 4, 5, 6]",
        },
        {
            "candidate_id": "user-window",
            "dialogue_block": "김재원: 부산 여행은 KTX로 간다. 숙소를 찾는다.",
            "source_message_ids": [2, 3, 4],
        },
        {
            "candidate_id": "distinct-window",
            "dialogue_block": "민수: 숙소는 해운대 근처를 선호한다.",
            "source_message_ids": [20, 21],
        },
    ]

    selected = HybridSearchEngine._dedupe_overlapping_entries(entries)

    assert [entry["candidate_id"] for entry in selected] == [
        "shared-window",
        "distinct-window",
    ]


def test_identical_memory_blocks_are_deduplicated_without_source_ids():
    entries = [
        {"candidate_id": "first", "dialogue_block": "민수: 파전을 좋아한다."},
        {
            "candidate_id": "duplicate",
            "dialogue_block": "  민수:   파전을 좋아한다.  ",
        },
        {"candidate_id": "other", "dialogue_block": "민수: 비 오는 날을 좋아한다."},
    ]

    selected = HybridSearchEngine._dedupe_overlapping_entries(entries)

    assert [entry["candidate_id"] for entry in selected] == ["first", "other"]


@pytest.mark.asyncio
async def test_deep_search_reads_structured_and_raw_discord_embeddings(monkeypatch):
    monkeypatch.setattr(config, "SEARCH_QUERY_EXPANSION_ENABLED", False)
    monkeypatch.setattr(config, "RAG_SIMILARITY_THRESHOLD", 0.1)
    monkeypatch.setattr(config, "STRUCTURED_MEMORY_SIMILARITY_THRESHOLD", 0.1)

    class _Store(DummyDiscordStore):
        async def fetch_recent_memory_entries(
            self,
            *,
            server_id,
            channel_id,
            user_id=None,
            limit=200,
            query_vector=None,
        ):
            self.structured_calls += 1
            return [
                {
                    "memory_id": "memory-1",
                    "message_id": 10,
                    "summary_text": "김재원은 부산 여행을 다녀왔다.",
                    "raw_context": "김재원: 부산에 다녀왔어.",
                    "embedding": np.array([0.8, 0.2], dtype=np.float32),
                    "memory_scope": "guild",
                    "memory_type": "event",
                    "timestamp": "2026-07-01T00:00:00",
                }
            ]

    async def fake_get_embedding(_text: str, prefix: str = ""):
        return np.array([0.9, 0.1], dtype=np.float32)

    monkeypatch.setattr("utils.hybrid_search.get_embedding", fake_get_embedding)
    store = _Store()
    engine = HybridSearchEngine(store, None, None)

    result = await engine.search(
        "김재원이 누구야?",
        guild_id=123,
        channel_id=456,
        user_id=None,
        memory_user_id=789,
        deep_search=True,
    )

    assert result.entries
    assert store.structured_calls == 1
    assert store.legacy_calls == 1
    assert result.entries[0]["lexical_score"] >= 0.04

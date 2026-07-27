import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import numpy as np
import pytest

import config
from utils.embeddings import KakaoEmbeddingStore
from utils.rag_manager import RAGManager


def _message(channel_id: int, message_id: int):
    return SimpleNamespace(
        id=message_id,
        guild=SimpleNamespace(id=10),
        channel=SimpleNamespace(id=channel_id),
        author=SimpleNamespace(
            id=20,
            display_name="tester",
            name="tester",
            bot=False,
        ),
        content=f"channel {channel_id} message {message_id}",
        attachments=[],
        embeds=[],
        stickers=[],
        created_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_rag_window_buffers_evict_least_recently_used_channel(monkeypatch):
    monkeypatch.setattr(config, "RAG_MAX_TRACKED_WINDOWS", 2, raising=False)
    monkeypatch.setattr(config, "CONVERSATION_WINDOW_SIZE", 10)
    monkeypatch.setattr(config, "CONVERSATION_WINDOW_STRIDE", 5)
    monkeypatch.setattr(config, "CONVERSATION_WINDOW_MAX_TOKENS", 10_000)
    monkeypatch.setattr(config, "CONVERSATION_WINDOW_MAX_CHARS", 10_000)

    manager = RAGManager(
        db=object(),
        embedding_store=None,
        hybrid_search_engine=None,
        reranker=None,
        llm_client=None,
        bot=SimpleNamespace(),
    )

    await manager._update_conversation_windows(_message(1, 1))
    await manager._update_conversation_windows(_message(2, 2))
    await manager._update_conversation_windows(_message(1, 3))
    await manager._update_conversation_windows(_message(3, 4))

    assert list(manager._window_buffers) == [(10, 1), (10, 3)]
    assert set(manager._window_counts) == {(10, 1), (10, 3)}

    await manager.close()
    assert not manager._window_buffers
    assert not manager._window_counts


@pytest.mark.asyncio
async def test_kakao_message_window_helper_returns_bounded_ordered_rows():
    store = KakaoEmbeddingStore(None, {})
    async with aiosqlite.connect(":memory:") as db:
        await db.execute(
            "CREATE TABLE kakao_messages "
            "(id INTEGER PRIMARY KEY, user_name TEXT, message TEXT)"
        )
        await db.executemany(
            "INSERT INTO kakao_messages (id, user_name, message) VALUES (?, ?, ?)",
            [(idx, f"user-{idx}", f"message-{idx}") for idx in range(1, 9)],
        )
        await db.commit()

        rows = await store._fetch_message_window(db, 4)

    assert [row["id"] for row in rows] == list(range(1, 8))
    assert rows[3] == {
        "id": 4,
        "user_name": "user-4",
        "message": "message-4",
    }


@pytest.mark.asyncio
async def test_numpy_kakao_store_uses_memmap_and_returns_top_k(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "KAKAO_STORE_BACKEND", "local")
    store_path = tmp_path / "kakao-numpy"
    store_path.mkdir()
    np.save(
        store_path / "vectors.npy",
        np.asarray(
            [
                [1.0, 0.0],
                [0.8, 0.2],
                [0.0, 1.0],
                [0.0, 0.0],
            ],
            dtype=np.float32,
        ),
    )
    (store_path / "metadata.json").write_text(
        json.dumps(
            [
                {"id": 1, "text": "first"},
                {"id": 2, "text": "second"},
                {"id": 3, "text": "third"},
                {"id": 4, "text": "zero-vector"},
            ]
        ),
        encoding="utf-8",
    )
    store = KakaoEmbeddingStore(str(store_path), {})

    assert await store._ensure_numpy_backend(store_path) is True
    vectors, _ = store._numpy_cache[store_path]
    assert isinstance(vectors, np.memmap)

    rows = await store._vector_search(
        store_path,
        limit=2,
        query_vector=np.asarray([1.0, 0.0], dtype=np.float32),
    )
    assert [row["message_id"] for row in rows] == [1, 2]
    assert rows[0]["score"] > rows[1]["score"]

    all_rows = await store._vector_search(
        store_path,
        limit=4,
        query_vector=np.asarray([1.0, 0.0], dtype=np.float32),
    )
    assert [row["message_id"] for row in all_rows] == [1, 2, 3]

    zero_rows = await store._vector_search(
        store_path,
        limit=2,
        query_vector=np.zeros(2, dtype=np.float32),
    )
    assert zero_rows == []


def test_numpy_kakao_store_rejects_metadata_count_mismatch(tmp_path):
    vector_path = tmp_path / "vectors.npy"
    metadata_path = tmp_path / "metadata.json"
    np.save(vector_path, np.zeros((2, 3), dtype=np.float32))
    metadata_path.write_text(json.dumps([{"id": 1}]), encoding="utf-8")

    with pytest.raises(ValueError, match="개수가 다릅니다"):
        KakaoEmbeddingStore._load_numpy_files(vector_path, metadata_path)


def test_numpy_kakao_store_batched_top_k_matches_full_cosine():
    rng = np.random.default_rng(20260727)
    vectors = rng.normal(size=(4_205, 8)).astype(np.float32)
    metadata = [{"id": idx, "text": str(idx)} for idx in range(len(vectors))]
    query = rng.normal(size=8).astype(np.float32)
    store = KakaoEmbeddingStore(None, {})
    cache_key = Path("batched-test")
    store._numpy_cache[cache_key] = (vectors, metadata)

    rows = store._vector_search_numpy(cache_key, query, limit=7)

    expected_scores = np.dot(vectors, query) / (
        np.linalg.norm(vectors, axis=1) * np.linalg.norm(query)
    )
    expected_ids = np.argsort(expected_scores)[::-1][:7].tolist()
    assert [row["message_id"] for row in rows] == expected_ids

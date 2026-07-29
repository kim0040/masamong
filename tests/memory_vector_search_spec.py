"""서버측 벡터 검색 경로가 안전하게만 켜지는지 확인한다.

`embedding_vec` 열이 없거나 백필이 끝나지 않은 인스턴스에서 이 경로가 켜지면
기억이 통째로 사라진다. 운영 중인 Masamo는 `AUTO_MIGRATE=false`로 기동하므로
런타임이 열을 만들어주지도 않는다. 그래서 기본값은 꺼짐이고, 켜져 있어도 열이
없으면 조용히 기존 경로로 내려가야 한다.
"""

import numpy as np
import pytest

import config
from utils.embeddings import DiscordEmbeddingStore

GUILD = 659398210275770368
CHANNEL = 659398210980151307
USER = 284894334569152512


def _store(
    monkeypatch,
    *,
    enabled: bool,
    column_exists: bool,
    coverage_complete: bool = True,
    search_fails=False,
):
    monkeypatch.setattr(config, "STRUCTURED_MEMORY_VECTOR_SEARCH_ENABLED", enabled)
    store = DiscordEmbeddingStore(":memory:", read_only=True)
    store.backend = "tidb"
    store._initialized = True
    queries: list[str] = []

    def fake_exec(query, params, *, fetch=False):
        queries.append(query)
        if "information_schema.COLUMNS" in query:
            return [{"n": 1 if column_exists else 0}]
        if "embedding_vec IS NULL LIMIT 1" in query:
            return [{"missing": 0 if coverage_complete else 1}]
        if "VEC_COSINE_DISTANCE" in query:
            if search_fails:
                raise RuntimeError("Unknown column 'embedding_vec'")
            return [{"memory_id": "vec-1", "memory_scope": "channel"}]
        return [{"memory_id": "recent-1", "memory_scope": "channel"}]

    store._tidb_exec = fake_exec

    async def noop():
        return None

    store.initialize = noop
    return store, queries


async def _fetch(store, *, with_vector=True):
    return await store.fetch_recent_memory_entries(
        server_id=GUILD,
        channel_id=CHANNEL,
        user_id=USER,
        limit=32,
        query_vector=np.zeros(384, dtype=np.float32) if with_vector else None,
    )


def test_vector_search_is_disabled_by_default():
    assert config.STRUCTURED_MEMORY_VECTOR_SEARCH_ENABLED is False, (
        "열이 준비되지 않은 인스턴스에서 기본으로 켜지면 기억이 사라진다."
    )


@pytest.mark.asyncio
async def test_disabled_flag_uses_recency_path(monkeypatch):
    store, queries = _store(monkeypatch, enabled=False, column_exists=True)

    rows = await _fetch(store)

    assert rows[0]["memory_id"] == "recent-1"
    assert not any("VEC_COSINE_DISTANCE" in q for q in queries)


@pytest.mark.asyncio
async def test_missing_column_falls_back_silently(monkeypatch):
    store, queries = _store(monkeypatch, enabled=True, column_exists=False)

    rows = await _fetch(store)

    assert rows[0]["memory_id"] == "recent-1"
    assert not any("VEC_COSINE_DISTANCE" in q for q in queries)


@pytest.mark.asyncio
async def test_partial_backfill_falls_back_silently(monkeypatch):
    store, queries = _store(
        monkeypatch,
        enabled=True,
        column_exists=True,
        coverage_complete=False,
    )

    rows = await _fetch(store)

    assert rows[0]["memory_id"] == "recent-1"
    assert not any("VEC_COSINE_DISTANCE" in q for q in queries)


@pytest.mark.asyncio
async def test_enabled_with_column_uses_vector_order(monkeypatch):
    store, queries = _store(monkeypatch, enabled=True, column_exists=True)

    rows = await _fetch(store)

    assert rows[0]["memory_id"] == "vec-1"
    vector_query = next(q for q in queries if "VEC_COSINE_DISTANCE" in q)
    assert "embedding_vec IS NOT NULL" in vector_query
    # 스코프 경계는 벡터 경로에서도 그대로 유지돼야 한다.
    assert "memory_scope = 'guild'" in vector_query
    assert "owner_user_id" in vector_query


@pytest.mark.asyncio
async def test_vector_failure_falls_back_and_stops_retrying(monkeypatch):
    store, queries = _store(
        monkeypatch, enabled=True, column_exists=True, search_fails=True
    )

    rows = await _fetch(store)
    assert rows[0]["memory_id"] == "recent-1"

    queries.clear()
    rows = await _fetch(store)

    assert rows[0]["memory_id"] == "recent-1"
    assert not any("VEC_COSINE_DISTANCE" in q for q in queries), (
        "한 번 실패하면 매 검색마다 다시 시도하지 않는다."
    )


@pytest.mark.asyncio
async def test_no_query_vector_uses_recency_path(monkeypatch):
    store, queries = _store(monkeypatch, enabled=True, column_exists=True)

    rows = await _fetch(store, with_vector=False)

    assert rows[0]["memory_id"] == "recent-1"
    assert not any("VEC_COSINE_DISTANCE" in q for q in queries)


@pytest.mark.asyncio
async def test_upsert_dual_writes_when_vector_column_exists(monkeypatch):
    store, queries = _store(monkeypatch, enabled=False, column_exists=True)
    store.read_only = False
    captured_params: list[tuple] = []

    def capture_exec(query, params, *, fetch=False):
        queries.append(query)
        captured_params.append(tuple(params))
        return []

    store._vector_column_state = True
    store._tidb_exec = capture_exec

    await store.upsert_memory_entry(
        memory_id="memory-1",
        anchor_message_id=1,
        server_id=GUILD,
        channel_id=CHANNEL,
        owner_user_id=USER,
        owner_user_name="tester",
        memory_scope="guild_user",
        memory_type="preference",
        summary_text="요약",
        memory_text="기억",
        raw_context="원문",
        source_message_ids=[1],
        speaker_names=["tester"],
        keywords=["기억"],
        timestamp_iso="2026-07-29T00:00:00+09:00",
        embedding=np.zeros(384, dtype=np.float32),
    )

    write_query = queries[-1]
    params = captured_params[-1]
    assert "embedding_vec" in write_query
    assert len(params) == 17
    assert params[-1].startswith("[")


@pytest.mark.asyncio
async def test_upsert_keeps_legacy_blob_path_without_vector_column(monkeypatch):
    store, queries = _store(monkeypatch, enabled=False, column_exists=False)
    store.read_only = False
    captured_params: list[tuple] = []

    def capture_exec(query, params, *, fetch=False):
        queries.append(query)
        captured_params.append(tuple(params))
        return []

    # 부정 결과를 캐시해 이 테스트에서는 schema 확인 쿼리를 생략한다.
    store._vector_column_state = False
    store._vector_column_checked_at = __import__("time").monotonic()
    store._tidb_exec = capture_exec

    await store.upsert_memory_entry(
        memory_id="memory-legacy",
        anchor_message_id=2,
        server_id=GUILD,
        channel_id=CHANNEL,
        owner_user_id=USER,
        owner_user_name="tester",
        memory_scope="guild_user",
        memory_type="preference",
        summary_text="요약",
        memory_text="기억",
        raw_context="원문",
        source_message_ids=[2],
        speaker_names=["tester"],
        keywords=["기억"],
        timestamp_iso="2026-07-29T00:00:00+09:00",
        embedding=np.zeros(384, dtype=np.float32),
    )

    write_query = queries[-1]
    assert "embedding_vec" not in write_query
    assert len(captured_params[-1]) == 16

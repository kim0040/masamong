"""기억이 필요 없는 대화에 기억을 주입하지 않는지 확인한다.

운영 기억으로 실측했을 때 "ㅇㅇ", "안녕", "고마워" 같은 잡담에도 기억이 6~14개씩
프롬프트에 들어갔다. 후보 생성 임계값(0.5)이 E5 코사인의 절대 척도 문제 때문에
사실상 아무것도 거르지 못했기 때문이다. 최종 관문을 그 위에 둔다.
"""

import numpy as np
import pytest

import config
from utils.hybrid_search import HybridSearchEngine


class _Store:
    """지정한 유사도가 나오도록 만든 기억 한 벌."""

    def __init__(self, vectors: list[tuple[str, np.ndarray]]):
        self.vectors = vectors

    async def fetch_recent_memory_entries(self, *, server_id, channel_id, user_id=None, limit=200, query_vector=None):
        return [
            {
                "memory_id": f"m-{i}",
                "message_id": 100 + i,
                "summary_text": text,
                "raw_context": text,
                "embedding": vector,
                "memory_scope": "channel",
                "memory_type": "shared_context",
                "timestamp": "2026-07-01T00:00:00",
            }
            for i, (text, vector) in enumerate(self.vectors)
        ]

    async def fetch_recent_embeddings(self, *, server_id, channel_id, user_id=None, limit=200):
        return []


def _unit(angle: float) -> np.ndarray:
    return np.array([np.cos(angle), np.sin(angle)], dtype=np.float32)


def _engine(monkeypatch, similarities: list[float]):
    monkeypatch.setattr(config, "SEARCH_QUERY_EXPANSION_ENABLED", False)
    monkeypatch.setattr(config, "STRUCTURED_MEMORY_SIMILARITY_THRESHOLD", 0.5)
    monkeypatch.setattr(config, "RAG_SIMILARITY_THRESHOLD", 0.5)
    monkeypatch.setattr(config, "RAG_EMBEDDING_TOP_N", 14)
    monkeypatch.setattr(config, "RAG_HYBRID_TOP_K", 8)
    monkeypatch.setattr(config, "RAG_MEMORY_MAX_BLOCKS", 3)
    monkeypatch.setattr(config, "RAG_MEMORY_GATE_SCORE", 0.61)
    monkeypatch.setattr(config, "RAG_EXPLICIT_MEMORY_GATE_SCORE", 0.58)
    monkeypatch.setattr(config, "RAG_MEMORY_RELATIVE_FLOOR", 0.94)

    query_vector = _unit(0.0)
    rows = [
        (f"기억 {i} (유사도 {s})", _unit(float(np.arccos(min(1.0, s)))))
        for i, s in enumerate(similarities)
    ]

    async def fake_get_embedding(_text, prefix=""):
        return query_vector

    monkeypatch.setattr("utils.hybrid_search.get_embedding", fake_get_embedding)
    return HybridSearchEngine(_Store(rows), None, None, reranker=None)


@pytest.mark.asyncio
async def test_chitchat_injects_nothing(monkeypatch):
    # 실측 잡담 최고 유사도 분포: 0.45~0.61
    engine = _engine(monkeypatch, [0.575, 0.56, 0.54, 0.53, 0.51])

    result = await engine.search("ㅇㅇ", guild_id=1, channel_id=2, user_id=3)

    assert result.entries == [], (
        "관련 없는 기억은 한 건도 프롬프트에 들어가면 안 된다."
    )
    assert result.top_score == 0.0


@pytest.mark.asyncio
async def test_real_question_still_recalls(monkeypatch):
    # 실측 기억 질문 최고 유사도 분포: 0.60~0.74
    engine = _engine(monkeypatch, [0.70, 0.55, 0.52])

    result = await engine.search(
        "우리 저번에 숙소 얘기했던 거 뭐였지", guild_id=1, channel_id=2, user_id=3
    )

    assert result.entries, "관련 있는 기억은 회수돼야 한다."
    assert result.top_score >= 0.61


@pytest.mark.asyncio
async def test_relative_floor_drops_weaker_neighbours(monkeypatch):
    # 최고점 0.70이면 0.658 미만은 버린다(0.94 비율).
    engine = _engine(monkeypatch, [0.70, 0.68, 0.62, 0.55])

    result = await engine.search("질문", guild_id=1, channel_id=2, user_id=3)

    scores = [entry["combined_score"] for entry in result.entries]
    assert scores, "최고점이 게이트를 넘으면 최소 한 건은 남아야 한다."
    assert min(scores) >= 0.70 * 0.94 - 1e-6
    assert all(score <= 0.70 + 1e-6 for score in scores)


@pytest.mark.asyncio
async def test_injection_count_is_capped(monkeypatch):
    # 사람은 과거 기억 여덟 개를 동시에 떠올리지 않는다. config 파일의 top_k가
    # 8이어도 코드 상한이 우선한다 — 이미 배포된 서버 설정을 고치지 않아도
    # 새 동작이 적용돼야 하기 때문이다.
    engine = _engine(monkeypatch, [0.72, 0.715, 0.71, 0.705, 0.70, 0.699])

    result = await engine.search("질문", guild_id=1, channel_id=2, user_id=3)

    assert len(result.entries) <= 3


@pytest.mark.asyncio
async def test_gate_is_configurable(monkeypatch):
    engine = _engine(monkeypatch, [0.575, 0.56])
    monkeypatch.setattr(config, "RAG_MEMORY_GATE_SCORE", 0.50)

    result = await engine.search("ㅇㅇ", guild_id=1, channel_id=2, user_id=3)

    assert result.entries, "게이트를 낮추면 예전 동작으로 돌아갈 수 있어야 한다."


def test_ranking_bonus_cannot_bypass_passive_semantic_gate(monkeypatch):
    """개인/키워드 보너스는 순위만 바꾸고 관련성 판정은 속이지 못해야 한다."""
    engine = _engine(monkeypatch, [])
    entries = [
        {
            "combined_score": 0.6309,
            "semantic_similarity": 0.5909,
            "text": "관련 없는 과거 기억",
        }
    ]

    assert engine._gate_by_relevance(entries, deep_search=False) == []
    assert engine._gate_by_relevance(entries, deep_search=True) == entries, (
        "명시적으로 과거 기억을 묻는 경우에는 별도의 완화된 관문을 쓴다."
    )

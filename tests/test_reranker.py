import pytest

from utils.reranker import Reranker, RerankerConfig


@pytest.mark.asyncio
async def test_reranker_returns_original_when_model_missing(monkeypatch):
    reranker = Reranker(RerankerConfig(model_name="dummy"))

    async def fake_ensure_model():
        raise RuntimeError("missing dependencies")

    monkeypatch.setattr(reranker, "_ensure_model", fake_ensure_model)

    documents = [
        {"text": "첫 번째 문장", "hybrid_score": 0.4},
        {"text": "두 번째 문장", "hybrid_score": 0.3},
    ]
    ranked = await reranker.rerank("테스트 쿼리", documents)

    assert ranked == documents

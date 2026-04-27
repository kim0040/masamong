import pytest

from utils import embeddings


class _DummyTokenizer:
    def __call__(self, text, **kwargs):
        tokens = [token for token in str(text).split() if token]
        return {"input_ids": list(range(len(tokens)))}

    def decode(self, token_ids, skip_special_tokens=True):
        _ = skip_special_tokens
        return " ".join(f"t{i}" for i in token_ids)


class _DummyModel:
    max_seq_length = 512
    tokenizer = _DummyTokenizer()


@pytest.mark.asyncio
async def test_get_embedding_token_limit_uses_model_max(monkeypatch):
    async def _fake_load_model():
        return _DummyModel()

    monkeypatch.setattr(embeddings, "_load_model", _fake_load_model)
    limit = await embeddings.get_embedding_token_limit(reserve_tokens=32)
    assert limit == 480


@pytest.mark.asyncio
async def test_count_embedding_tokens_uses_tokenizer(monkeypatch):
    async def _fake_load_model():
        return _DummyModel()

    monkeypatch.setattr(embeddings, "_load_model", _fake_load_model)
    count = await embeddings.count_embedding_tokens("a b c d")
    assert count == 4


@pytest.mark.asyncio
async def test_trim_text_to_embedding_token_limit(monkeypatch):
    async def _fake_load_model():
        return _DummyModel()

    monkeypatch.setattr(embeddings, "_load_model", _fake_load_model)
    trimmed = await embeddings.trim_text_to_embedding_token_limit("a b c d e f g h i j", 8)
    assert trimmed == "t0 t1 t2 t3 t4 t5 t6 t7"

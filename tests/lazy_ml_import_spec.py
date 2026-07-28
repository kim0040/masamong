import os
from pathlib import Path
import subprocess
import sys
import threading

import numpy as np
import pytest

from utils import embeddings
from utils import query_rewriter
from utils import reranker as reranker_module


ROOT = Path(__file__).resolve().parents[1]


def test_ai_handler_import_does_not_load_optional_ml_packages():
    """비활성 general 인스턴스의 Cog import만으로 ML 런타임을 올리지 않는다."""
    script = """
import importlib.abc
import sys

class OptionalMlImportBlocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if (
            fullname == "torch"
            or fullname.startswith("torch.")
            or fullname == "transformers"
            or fullname.startswith("transformers.")
            or fullname == "sentence_transformers"
            or fullname.startswith("sentence_transformers.")
        ):
            raise RuntimeError("eager optional ML import attempted: " + fullname)
        return None

sys.meta_path.insert(0, OptionalMlImportBlocker())
import cogs.ai_handler  # noqa: F401

heavy_modules = sorted(
    name
    for name in sys.modules
    if name == "torch"
    or name.startswith("torch.")
    or name == "numpy"
    or name.startswith("numpy.")
    or name == "transformers"
    or name.startswith("transformers.")
    or name == "sentence_transformers"
    or name.startswith("sentence_transformers.")
)
if heavy_modules:
    raise SystemExit("unexpected eager ML imports: " + ", ".join(heavy_modules))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_disabled_rag_does_not_construct_storage_or_search_objects(monkeypatch):
    """RAG/embedding을 끈 프로필은 사용하지 않을 객체 그래프도 만들지 않는다."""
    import cogs.ai_handler as ai_handler_module

    class _UnexpectedConstruction:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("disabled RAG object was constructed")

    monkeypatch.setattr(ai_handler_module.config, "AI_MEMORY_ENABLED", False)
    monkeypatch.setattr(ai_handler_module.config, "EMBEDDING_ENABLED", False)
    monkeypatch.setattr(
        ai_handler_module,
        "DiscordEmbeddingStore",
        _UnexpectedConstruction,
    )
    monkeypatch.setattr(
        ai_handler_module,
        "KakaoEmbeddingStore",
        _UnexpectedConstruction,
    )
    monkeypatch.setattr(ai_handler_module, "BM25IndexManager", _UnexpectedConstruction)
    monkeypatch.setattr(ai_handler_module, "Reranker", _UnexpectedConstruction)
    monkeypatch.setattr(
        ai_handler_module,
        "HybridSearchEngine",
        _UnexpectedConstruction,
    )

    class _Bot:
        db = None

        @staticmethod
        def get_cog(_name):
            return None

    handler = ai_handler_module.AIHandler(_Bot())

    assert handler.rag_enabled is False
    assert handler.discord_embedding_store is None
    assert handler.kakao_embedding_store is None
    assert handler.bm25_manager is None
    assert handler.reranker is None
    assert handler.hybrid_search_engine is None
    assert handler.rag_manager.embedding_store is None
    assert handler.rag_manager.hybrid_search_engine is None


@pytest.mark.asyncio
async def test_disabled_embedding_helpers_never_attempt_optional_imports(monkeypatch):
    """비활성 플래그는 우발적인 직접 호출에서도 무거운 import를 차단한다."""
    def _unexpected_import():
        raise AssertionError("optional ML import attempted while disabled")

    monkeypatch.setattr(embeddings.config, "EMBEDDING_ENABLED", False)
    monkeypatch.setattr(embeddings, "_get_numpy", _unexpected_import)
    monkeypatch.setattr(
        embeddings,
        "_get_sentence_transformer_class",
        _unexpected_import,
    )

    assert await embeddings.get_embedding("본문", prefix="query: ") is None
    assert await embeddings.get_embedding_token_limit(reserve_tokens=32) > 0
    assert await embeddings.count_embedding_tokens("한 두 세") == 3
    assert await embeddings.trim_text_to_embedding_token_limit("짧은 본문", 8) == "짧은 본문"


@pytest.mark.asyncio
async def test_disabled_rag_query_rewriter_does_not_load_model(monkeypatch):
    """재작성 옵션이 남아 있어도 상위 RAG 플래그가 꺼지면 모델을 올리지 않는다."""
    monkeypatch.setattr(query_rewriter.config, "AI_MEMORY_ENABLED", False)
    monkeypatch.setattr(query_rewriter.config, "EMBEDDING_ENABLED", False)
    monkeypatch.setattr(query_rewriter.config, "RAG_QUERY_REWRITE_ENABLED", True)
    monkeypatch.setattr(
        query_rewriter,
        "_get_sentence_transformer_class",
        lambda: (_ for _ in ()).throw(
            AssertionError("query rewrite model import attempted while disabled")
        ),
    )

    assert await query_rewriter.expand_query("테스트 질문", max_variants=3) == [
        "테스트 질문"
    ]


@pytest.mark.asyncio
async def test_embedding_model_still_loads_on_first_real_request(monkeypatch):
    """lazy import 이후에도 기존 float32 임베딩 API를 유지한다."""
    calls = []

    class FakeSentenceTransformer:
        def __init__(self, model_name, **kwargs):
            calls.append((model_name, kwargs))

        def encode(self, text, *, normalize_embeddings):
            calls.append((text, normalize_embeddings))
            return [1.0, 2.0, 3.0]

    monkeypatch.setattr(embeddings, "_MODEL", None)
    monkeypatch.setattr(embeddings, "SentenceTransformer", FakeSentenceTransformer)
    monkeypatch.setattr(embeddings.config, "EMBEDDING_ENABLED", True)
    monkeypatch.setattr(embeddings.config, "CPU_THREAD_LIMIT", 0)
    monkeypatch.setattr(embeddings.config, "LOCAL_EMBEDDING_LOCAL_FILES_ONLY", False)

    vector = await embeddings.get_embedding("본문", prefix="query: ")

    assert isinstance(vector, np.ndarray)
    assert vector.dtype == np.float32
    assert vector.tolist() == [1.0, 2.0, 3.0]
    assert calls[1] == ("query: 본문", True)


@pytest.mark.asyncio
async def test_embedding_dependency_imports_run_outside_event_loop(monkeypatch):
    """저사양 서버의 무거운 import도 Discord heartbeat thread를 막지 않는다."""
    event_loop_thread = threading.get_ident()

    class FakeSentenceTransformer:
        def __init__(self, _model_name, **_kwargs):
            assert threading.get_ident() != event_loop_thread

    def _fake_numpy():
        assert threading.get_ident() != event_loop_thread
        return np

    def _fake_transformer():
        assert threading.get_ident() != event_loop_thread
        return FakeSentenceTransformer

    monkeypatch.setattr(embeddings, "_MODEL", None)
    monkeypatch.setattr(embeddings, "_MODEL_FAILURE_RETRY_AT", 0.0)
    monkeypatch.setattr(embeddings, "_get_numpy", _fake_numpy)
    monkeypatch.setattr(
        embeddings,
        "_get_sentence_transformer_class",
        _fake_transformer,
    )
    monkeypatch.setattr(embeddings.config, "EMBEDDING_ENABLED", True)
    monkeypatch.setattr(embeddings.config, "CPU_THREAD_LIMIT", 0)
    monkeypatch.setattr(
        embeddings.config,
        "LOCAL_EMBEDDING_LOCAL_FILES_ONLY",
        False,
    )

    assert isinstance(await embeddings._load_model(), FakeSentenceTransformer)


@pytest.mark.asyncio
async def test_embedding_load_failure_has_finite_retry_cooldown(monkeypatch):
    calls = 0

    def _broken_numpy():
        nonlocal calls
        calls += 1
        raise OSError("broken optional runtime")

    monkeypatch.setattr(embeddings, "_MODEL", None)
    monkeypatch.setattr(embeddings, "_MODEL_FAILURE_RETRY_AT", 0.0)
    monkeypatch.setattr(embeddings, "_get_numpy", _broken_numpy)
    monkeypatch.setattr(embeddings.config, "EMBEDDING_ENABLED", True)

    with pytest.raises(RuntimeError):
        await embeddings._load_model()
    with pytest.raises(RuntimeError):
        await embeddings._load_model()

    assert calls == 1


@pytest.mark.asyncio
async def test_query_rewriter_dependency_import_runs_outside_event_loop(
    monkeypatch,
):
    event_loop_thread = threading.get_ident()

    class FakeSentenceTransformer:
        def __init__(self, _model_name, **_kwargs):
            assert threading.get_ident() != event_loop_thread

    def _fake_transformer():
        assert threading.get_ident() != event_loop_thread
        return FakeSentenceTransformer

    monkeypatch.setattr(query_rewriter, "_MODEL_INSTANCE", None)
    monkeypatch.setattr(query_rewriter, "_MODEL_RETRY_AT", 0.0)
    monkeypatch.setattr(
        query_rewriter,
        "_get_sentence_transformer_class",
        _fake_transformer,
    )
    monkeypatch.setattr(query_rewriter.config, "AI_MEMORY_ENABLED", True)
    monkeypatch.setattr(query_rewriter.config, "EMBEDDING_ENABLED", True)
    monkeypatch.setattr(
        query_rewriter.config,
        "RAG_QUERY_REWRITE_MODEL_NAME",
        "test/model",
    )
    monkeypatch.setattr(
        query_rewriter.config,
        "RAG_QUERY_REWRITE_BACKEND",
        None,
    )

    assert isinstance(
        await query_rewriter._get_model(),
        FakeSentenceTransformer,
    )


@pytest.mark.asyncio
async def test_reranker_dependency_import_runs_outside_event_loop(monkeypatch):
    event_loop_thread = threading.get_ident()

    class FakeTokenizer:
        @classmethod
        def from_pretrained(cls, _model_name):
            return cls()

    class FakeModel:
        @classmethod
        def from_pretrained(cls, _model_name):
            return cls()

        def to(self, _device):
            return self

        def eval(self):
            return self

    class FakeCuda:
        @staticmethod
        def is_available():
            return False

    class FakeTorch:
        cuda = FakeCuda()

    def _fake_dependencies():
        assert threading.get_ident() != event_loop_thread
        return FakeTokenizer, FakeModel, FakeTorch

    monkeypatch.setattr(
        reranker_module,
        "_load_reranker_dependencies",
        _fake_dependencies,
    )
    reranker = reranker_module.Reranker()

    await reranker._ensure_model()

    assert isinstance(reranker._model, FakeModel)
    assert isinstance(reranker._tokenizer, FakeTokenizer)


@pytest.mark.asyncio
async def test_reranker_still_loads_and_scores_on_first_request(monkeypatch):
    """선택적 의존성을 주입했을 때 기존 재순위화 결과 형식을 유지한다."""
    class FakeInput:
        def to(self, device):
            return self

    class FakeLogits:
        ndim = 2

        def squeeze(self, _axis):
            return self

        def detach(self):
            return self

        def cpu(self):
            return self

        def tolist(self):
            return [0.2, 0.8]

    class FakeTokenizer:
        @classmethod
        def from_pretrained(cls, _model_name):
            return cls()

        def __call__(self, *_args, **_kwargs):
            return {"input_ids": FakeInput()}

    class FakeModel:
        @classmethod
        def from_pretrained(cls, _model_name):
            return cls()

        def to(self, _device):
            return self

        def eval(self):
            return self

        def __call__(self, **_kwargs):
            return type("Output", (), {"logits": FakeLogits()})()

    class FakeNoGrad:
        def __enter__(self):
            return None

        def __exit__(self, *_args):
            return False

    class FakeTorch:
        class cuda:
            @staticmethod
            def is_available():
                return False

        @staticmethod
        def no_grad():
            return FakeNoGrad()

    monkeypatch.setattr(reranker_module, "AutoTokenizer", FakeTokenizer)
    monkeypatch.setattr(
        reranker_module,
        "AutoModelForSequenceClassification",
        FakeModel,
    )
    monkeypatch.setattr(reranker_module, "torch", FakeTorch)

    reranker = reranker_module.Reranker(
        reranker_module.RerankerConfig(model_name="local-test-model")
    )
    ranked = await reranker.rerank(
        "질문",
        [{"text": "낮은 점수"}, {"text": "높은 점수"}],
    )

    assert [item["text"] for item in ranked] == ["높은 점수", "낮은 점수"]
    assert [item["rerank_score"] for item in ranked] == [0.8, 0.2]

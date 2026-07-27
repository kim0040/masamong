import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import pytest

from utils import embeddings
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
    monkeypatch.setattr(embeddings.config, "CPU_THREAD_LIMIT", 0)
    monkeypatch.setattr(embeddings.config, "LOCAL_EMBEDDING_LOCAL_FILES_ONLY", False)

    vector = await embeddings.get_embedding("본문", prefix="query: ")

    assert isinstance(vector, np.ndarray)
    assert vector.dtype == np.float32
    assert vector.tolist() == [1.0, 2.0, 3.0]
    assert calls[1] == ("query: 본문", True)


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

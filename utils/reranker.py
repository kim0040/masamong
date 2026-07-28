# -*- coding: utf-8 -*-
"""문서 재순위화를 담당하는 유틸리티."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time
from typing import Any, Iterable, List

from logger_config import logger

# transformers/torch는 import만으로도 상당한 메모리를 점유한다. Reranker 타입은
# 비활성 프로필에서도 생성 경로가 import될 수 있으므로 실제 rerank 요청 전에는
# 패키지를 로드하지 않는다. 모듈 변수는 기존 테스트/호출자의 monkeypatch 호환을
# 위해 유지한다.
AutoModelForSequenceClassification: Any | None = None
AutoTokenizer: Any | None = None
torch: Any | None = None
_DEPENDENCIES_IMPORT_ATTEMPTED = False


def _load_reranker_dependencies() -> tuple[Any, Any, Any] | None:
    """선택적 재순위화 의존성을 최초 모델 로드 시 한 번만 import합니다."""
    global AutoModelForSequenceClassification, AutoTokenizer, torch
    global _DEPENDENCIES_IMPORT_ATTEMPTED

    if (
        AutoTokenizer is not None
        and AutoModelForSequenceClassification is not None
        and torch is not None
    ):
        return AutoTokenizer, AutoModelForSequenceClassification, torch
    if _DEPENDENCIES_IMPORT_ATTEMPTED:
        return None

    _DEPENDENCIES_IMPORT_ATTEMPTED = True
    try:
        from transformers import (
            AutoModelForSequenceClassification as model_class,
            AutoTokenizer as tokenizer_class,
        )
        import torch as torch_module
    except ImportError:  # pragma: no cover - 선택적 의존성이 없는 경량 환경
        return None

    AutoTokenizer = tokenizer_class
    AutoModelForSequenceClassification = model_class
    torch = torch_module
    return AutoTokenizer, AutoModelForSequenceClassification, torch


@dataclass
class RerankerConfig:
    """재순위화 모델 설정."""

    model_name: str = "BAAI/bge-reranker-v2-m3"
    device: str | None = None
    batch_size: int = 8
    max_length: int = 512
    score_threshold: float | None = None


class Reranker:
    """Cross-Encoder 기반 재순위화 래퍼."""

    def __init__(self, config: RerankerConfig | None = None):
        """RerankerConfig를 받아 모델명·디바이스·배치 크기 등을 초기화하고 지연 로딩 준비를 한다."""
        self.config = config or RerankerConfig()
        self._tokenizer = None
        self._model = None
        self._device = None
        self._lock = asyncio.Lock()
        self._dependency_warning_logged = False
        self._model_retry_at = 0.0

    async def _ensure_model(self):
        """모델이 로드되지 않았다면 지연 로딩을 수행합니다."""
        if self._model is not None and self._tokenizer is not None:
            return
        if time.monotonic() < self._model_retry_at:
            raise RuntimeError("재순위화 모델 재시도 대기 중")

        async with self._lock:
            if self._model is not None and self._tokenizer is not None:
                return
            if time.monotonic() < self._model_retry_at:
                raise RuntimeError("재순위화 모델 재시도 대기 중")
            loop = asyncio.get_running_loop()

            def _load():
                # transformers/torch import도 저사양 서버에서 heartbeat를 막을 수
                # 있으므로 모델과 같은 worker thread에서 한 번만 실행한다.
                dependencies = _load_reranker_dependencies()
                if dependencies is None:
                    raise RuntimeError("transformers 또는 torch 패키지가 필요합니다.")
                tokenizer_class, model_class, torch_module = dependencies
                tokenizer = tokenizer_class.from_pretrained(self.config.model_name)
                model = model_class.from_pretrained(self.config.model_name)
                device = self.config.device or ("cuda" if torch_module.cuda.is_available() else "cpu")
                model.to(device)
                model.eval()
                return tokenizer, model, device

            logger.info("재순위화 모델 로드 시작: %s", self.config.model_name)
            try:
                tokenizer, model, device = await loop.run_in_executor(None, _load)
            except Exception as exc:
                self._model_retry_at = time.monotonic() + 300
                if not self._dependency_warning_logged:
                    logger.warning("재순위화 모델을 사용할 수 없습니다: %s", exc)
                    self._dependency_warning_logged = True
                raise RuntimeError("재순위화 모델 로드 실패") from exc
            logger.info("재순위화 모델 로드 완료: %s (device=%s)", self.config.model_name, device)
            self._tokenizer = tokenizer
            self._model = model
            self._device = device
            self._model_retry_at = 0.0

    async def rerank(
        self,
        query: str,
        documents: Iterable[dict[str, Any]],
        *,
        top_k: int | None = None,
    ) -> List[dict[str, Any]]:
        """재순위화 점수에 따라 문서 리스트를 재정렬합니다."""
        docs = list(documents)
        if not docs:
            return []

        try:
            await self._ensure_model()
        except RuntimeError:
            return docs

        tokenizer = self._tokenizer
        model = self._model
        device = self._device
        assert tokenizer is not None and model is not None and device is not None

        batch_size = max(1, self.config.batch_size)
        max_length = max(32, self.config.max_length)

        def _batch_scores() -> List[float]:
            scores: List[float] = []
            with torch.no_grad():
                for start in range(0, len(docs), batch_size):
                    batch = docs[start : start + batch_size]
                    # Cross-Encoder는 (query, document) 쌍을 한 번에 인퍼런스한다.
                    paired = tokenizer(
                        [query] * len(batch),
                        [doc.get("text", "") for doc in batch],
                        truncation=True,
                        padding=True,
                        max_length=max_length,
                        return_tensors="pt",
                    )
                    paired = {k: v.to(device) for k, v in paired.items()}
                    logits = model(**paired).logits
                    if logits.ndim == 1:
                        logits = logits.unsqueeze(-1)
                    batch_scores = logits.squeeze(-1).detach().cpu().tolist()
                    if isinstance(batch_scores, float):
                        batch_scores = [float(batch_scores)]
                    scores.extend(float(score) for score in batch_scores)
            return scores

        loop = asyncio.get_running_loop()
        scores = await loop.run_in_executor(None, _batch_scores)
        enriched: List[dict[str, Any]] = []
        for doc, score in zip(docs, scores):
            item = dict(doc)
            item["rerank_score"] = score
            enriched.append(item)

        threshold = self.config.score_threshold
        if threshold is not None:
            enriched = [item for item in enriched if item.get("rerank_score", float("-inf")) >= threshold]

        enriched.sort(key=lambda item: item.get("rerank_score", float("-inf")), reverse=True)
        if top_k is not None and top_k > 0:
            enriched = enriched[:top_k]
        return enriched

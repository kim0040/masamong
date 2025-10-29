# -*- coding: utf-8 -*-
"""문서 재순위화를 담당하는 유틸리티."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Iterable, List

from logger_config import logger

try:  # pragma: no cover - 선택적 의존성
    from transformers import AutoModelForSequenceClassification, AutoTokenizer  # type: ignore
    import torch  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - 환경에 따라 미설치 가능
    AutoModelForSequenceClassification = None  # type: ignore
    AutoTokenizer = None  # type: ignore
    torch = None  # type: ignore


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
        self.config = config or RerankerConfig()
        self._tokenizer = None
        self._model = None
        self._device = None
        self._lock = asyncio.Lock()
        self._dependency_warning_logged = False

    async def _ensure_model(self):
        if self._model is not None and self._tokenizer is not None:
            return
        if AutoTokenizer is None or AutoModelForSequenceClassification is None or torch is None:
            if not self._dependency_warning_logged:
                logger.warning("transformers/torch 패키지가 없어 재순위화를 비활성화합니다.")
                self._dependency_warning_logged = True
            raise RuntimeError("transformers 또는 torch 패키지가 필요합니다.")

        async with self._lock:
            if self._model is not None and self._tokenizer is not None:
                return
            loop = asyncio.get_running_loop()

            def _load():
                # 모델과 토크나이저는 CPU/GPU 여부에 따라 한 번만 로딩한다.
                tokenizer = AutoTokenizer.from_pretrained(self.config.model_name)
                model = AutoModelForSequenceClassification.from_pretrained(self.config.model_name)
                device = self.config.device or ("cuda" if torch.cuda.is_available() else "cpu")
                model.to(device)
                model.eval()
                return tokenizer, model, device

            logger.info("재순위화 모델 로드 시작: %s", self.config.model_name)
            tokenizer, model, device = await loop.run_in_executor(None, _load)
            logger.info("재순위화 모델 로드 완료: %s (device=%s)", self.config.model_name, device)
            self._tokenizer = tokenizer
            self._model = model
            self._device = device

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

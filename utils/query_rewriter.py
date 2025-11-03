# -*- coding: utf-8 -*-
"""쿼리 다양화 및 확장을 담당하는 모듈."""

from __future__ import annotations

import asyncio
from typing import List
import inspect

import config
from logger_config import logger

try:  # pragma: no cover - 경량 환경에서는 sentence-transformers가 없을 수 있다.
    from sentence_transformers import SentenceTransformer
except ModuleNotFoundError:  # pragma: no cover
    SentenceTransformer = None  # type: ignore

import numpy as np

_MODEL_LOCK = asyncio.Lock()
_MODEL_INSTANCE: SentenceTransformer | None = None

_SYNONYM_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("알려줘", ("말해줘", "설명해줘", "얘기해줘", "알려줄래", "알려줄 수 있어?")),
    ("알려줄래", ("말해줄래", "설명해줄래")),
    ("알려줄 수 있어", ("말해줄 수 있어", "설명해줄 수 있어")),
    ("찾아줘", ("검색해줘", "찾아볼 수 있어?", "찾아줄래")),
    ("추천해줘", ("추천해줄래", "추천해줄 수 있어?", "추천 좀 해줘")),
    ("확인해줘", ("확인해줄래", "체크해줘", "봐줄래")),
    ("어때", ("어떤지 알려줘", "상황이 어때", "어떤지 말해줘")),
    ("가격", ("비용", "가격대")),
    ("날씨", ("기상", "날씨 상황")),
    ("주가", ("주식 가격", "주식 시세")),
    ("환율", ("환 시세", "환율 정보")),
)

_TAIL_VARIANTS: tuple[str, ...] = (
    "{query}?",
    "{query} 알려줘",
    "{query}에 대해 알려줘",
    "{query} 정보 알려줘",
    "{query} 자세히 말해줘",
    "{query} 정리해줘",
    "{query} 요약해줘",
)


def _normalize_query(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    return stripped


async def _async_encode(model: SentenceTransformer, sentences: List[str]) -> np.ndarray:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: model.encode(sentences, normalize_embeddings=True),
    )


async def _get_model() -> SentenceTransformer | None:
    global _MODEL_INSTANCE
    if SentenceTransformer is None:
        logger.warning("sentence-transformers 패키지를 찾을 수 없어 쿼리 재작성을 비활성화합니다.")
        return None
    if _MODEL_INSTANCE is not None:
        return _MODEL_INSTANCE

    model_name = config.RAG_QUERY_REWRITE_MODEL_NAME or "upskyy/e5-small-korean"
    backend = getattr(config, "RAG_QUERY_REWRITE_BACKEND", None)

    async with _MODEL_LOCK:
        if _MODEL_INSTANCE is not None:
            return _MODEL_INSTANCE
        try:
            if backend:
                ctor_params = set(inspect.signature(SentenceTransformer.__init__).parameters)
                if "backend" in ctor_params:
                    _MODEL_INSTANCE = SentenceTransformer(model_name, backend=backend)
                else:
                    logger.warning("SentenceTransformer 버전이 backend 인자를 지원하지 않아 기본 설정으로 로드합니다.")
                    _MODEL_INSTANCE = SentenceTransformer(model_name)
            else:
                _MODEL_INSTANCE = SentenceTransformer(model_name)
            logger.info("쿼리 재작성용 SentenceTransformer 로드 완료: %s", model_name)
        except Exception as exc:  # pragma: no cover - 외부 모델 로드 실패 대비
            logger.warning("쿼리 재작성 모델 로드 실패(%s): %s", model_name, exc)
            _MODEL_INSTANCE = None
        return _MODEL_INSTANCE


def _build_candidate_variants(query: str) -> list[str]:
    base = _normalize_query(query)
    if not base:
        return []

    variants: set[str] = {base}
    normalized_base = base.rstrip(".!?")
    variants.add(normalized_base)

    for template in _TAIL_VARIANTS:
        candidate = template.format(query=normalized_base)
        variants.add(candidate.strip())

    for needle, replacements in _SYNONYM_GROUPS:
        if needle in base:
            for replacement in replacements:
                variants.add(base.replace(needle, replacement))

    if "?" in base:
        variants.add(base.replace("?", ""))
    else:
        variants.add(f"{normalized_base}?")

    if " 알려줘" not in base and " 말해줘" not in base:
        variants.add(f"{normalized_base} 알려줘")

    deduped = [variant.strip() for variant in variants if variant.strip()]
    # 길이가 너무 긴 변형은 제거 (모델 입력 제한 보호)
    deduped = [v for v in deduped if len(v) <= 200]

    # 원본 문장은 항상 첫 번째에 위치하도록 정렬
    deduped.sort(key=lambda v: (0 if v == base else 1, len(v)))
    return deduped


async def expand_query(
    query: str,
    *,
    max_variants: int | None = None,
) -> List[str]:
    """주어진 질문에 대한 패러프레이즈 후보를 생성합니다."""
    trimmed = query.strip()
    if not trimmed:
        return []

    variants_target = max_variants or config.RAG_QUERY_REWRITE_VARIANTS
    variants_target = max(1, variants_target)
    candidates = _build_candidate_variants(trimmed)
    if not candidates:
        return [trimmed][:variants_target]

    results: List[str] = [trimmed]

    if not config.RAG_QUERY_REWRITE_ENABLED:
        return results[:variants_target]

    try:
        model = await _get_model()
    except Exception as exc:  # pragma: no cover
        logger.warning("쿼리 재작성 모델 로딩 중 오류: %s", exc)
        model = None

    if model is None:
        return results[:variants_target]

    try:
        encoded = await _async_encode(model, candidates)
    except Exception as exc:  # pragma: no cover - 모델 추론 실패 대비
        logger.warning("쿼리 재작성 임베딩 계산 실패: %s", exc)
        return results[:variants_target]

    query_embedding = encoded[0]
    candidate_embeddings = encoded[1:]
    candidate_sentences = candidates[1:]

    if candidate_embeddings.size == 0:
        return results[:variants_target]

    scores = candidate_embeddings @ query_embedding
    scored_candidates = sorted(
        zip(candidate_sentences, scores.tolist()),
        key=lambda item: item[1],
        reverse=True,
    )

    for sentence, _score in scored_candidates:
        if sentence not in results:
            results.append(sentence)
        if len(results) >= variants_target:
            break

    return results[:variants_target]

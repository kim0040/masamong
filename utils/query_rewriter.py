# -*- coding: utf-8 -*-
"""쿼리 다양화 및 확장을 담당하는 모듈."""

from __future__ import annotations

import asyncio
from typing import Any, List
import inspect

import config
from logger_config import logger

_MODEL_LOCK = asyncio.Lock()
_MODEL_INSTANCE: Any | None = None
SentenceTransformer: Any | None = None
_SENTENCE_TRANSFORMER_IMPORT_ATTEMPTED = False

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
    """질의 문자열의 앞뒤 공백을 제거한 정규화된 텍스트를 반환한다."""
    stripped = text.strip()
    if not stripped:
        return ""
    return stripped


def _get_sentence_transformer_class() -> Any | None:
    """실제 쿼리 재작성 요청이 들어올 때만 무거운 ML 패키지를 import합니다."""
    global SentenceTransformer, _SENTENCE_TRANSFORMER_IMPORT_ATTEMPTED

    if SentenceTransformer is not None:
        return SentenceTransformer
    if _SENTENCE_TRANSFORMER_IMPORT_ATTEMPTED:
        return None

    _SENTENCE_TRANSFORMER_IMPORT_ATTEMPTED = True
    try:
        from sentence_transformers import SentenceTransformer as transformer_class
    except ImportError:  # pragma: no cover - 선택적 의존성이 없는 경량 환경
        return None

    SentenceTransformer = transformer_class
    return SentenceTransformer


async def _async_encode(model: Any, sentences: List[str]) -> Any:
    """SentenceTransformer 인코딩을 별도 스레드에서 실행합니다."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: model.encode(sentences, normalize_embeddings=True),
    )


async def _get_model() -> Any | None:
    """쿼리 재작성용 SentenceTransformer 모델을 지연 로딩합니다."""
    global _MODEL_INSTANCE
    if not (
        getattr(config, "AI_MEMORY_ENABLED", True)
        and getattr(config, "EMBEDDING_ENABLED", True)
    ):
        return None
    transformer_class = _get_sentence_transformer_class()
    if transformer_class is None:
        logger.warning("sentence-transformers 패키지를 찾을 수 없어 쿼리 재작성을 비활성화합니다.")
        return None
    if _MODEL_INSTANCE is not None:
        return _MODEL_INSTANCE

    model_name = config.RAG_QUERY_REWRITE_MODEL_NAME or "upskyy/e5-small-korean"
    backend = getattr(config, "RAG_QUERY_REWRITE_BACKEND", None)

    loop = asyncio.get_running_loop()

    def _build_model() -> Any:
        """SentenceTransformer를 동기적으로 생성한다(수 초 소요될 수 있는 블로킹 작업)."""
        if backend:
            ctor_params = set(inspect.signature(transformer_class.__init__).parameters)
            if "backend" in ctor_params:
                return transformer_class(model_name, backend=backend)
            logger.warning("SentenceTransformer 버전이 backend 인자를 지원하지 않아 기본 설정으로 로드합니다.")
            return transformer_class(model_name)
        return transformer_class(model_name)

    async with _MODEL_LOCK:
        if _MODEL_INSTANCE is not None:
            return _MODEL_INSTANCE
        try:
            # 모델 로드는 가중치 역직렬화로 수 초간 블로킹된다. 이벤트 루프를 멈추지
            # 않도록 executor에서 로드한다(embeddings._load_model과 동일한 패턴).
            _MODEL_INSTANCE = await loop.run_in_executor(None, _build_model)
            logger.info("쿼리 재작성용 SentenceTransformer 로드 완료: %s", model_name)
        except Exception as exc:  # pragma: no cover - 외부 모델 로드 실패 대비
            logger.warning("쿼리 재작성 모델 로드 실패(%s): %s", model_name, exc)
            _MODEL_INSTANCE = None
        return _MODEL_INSTANCE


def _build_candidate_variants(query: str) -> list[str]:
    """동의어 치환과 꼬리말 확장을 통해 후보 질의 변형을 생성합니다."""
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

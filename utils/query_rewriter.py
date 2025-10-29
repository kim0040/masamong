# -*- coding: utf-8 -*-
"""쿼리 다양화 및 확장을 담당하는 모듈."""

from __future__ import annotations

import asyncio
from typing import List

import config
from logger_config import logger

try:  # pragma: no cover - 환경에 따라 설치되지 않을 수 있음
    import google.generativeai as genai  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    genai = None  # type: ignore

_MODEL_CACHE: dict[str, "genai.GenerativeModel"] = {}
_MODEL_LOCK = asyncio.Lock()
_GENAI_READY = False


async def _ensure_model(model_name: str) -> "genai.GenerativeModel":
    if genai is None:
        raise RuntimeError("google-generativeai 패키지가 필요합니다.")
    global _GENAI_READY
    if not _GENAI_READY:
        if not config.GEMINI_API_KEY:
            raise RuntimeError("Gemini API 키가 설정되어 있지 않습니다.")
        genai.configure(api_key=config.GEMINI_API_KEY)
        _GENAI_READY = True

    cached = _MODEL_CACHE.get(model_name)
    if cached is not None:
        return cached

    async with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(model_name)
        if cached is not None:
            return cached
        # 동일 모델을 여러 번 로드하지 않도록 캐시에 저장한다.
        model = genai.GenerativeModel(model_name)
        _MODEL_CACHE[model_name] = model
        return model


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
    results: List[str] = [trimmed]

    if not config.RAG_QUERY_REWRITE_ENABLED:
        return results

    model_name = config.RAG_QUERY_REWRITE_MODEL_NAME
    try:
        model = await _ensure_model(model_name)
    except Exception as exc:  # pragma: no cover - 네트워크/환경 이슈 대비
        logger.warning("쿼리 재작성 모델 초기화 실패: %s", exc)
        return results

    prompt = (
        "다음 한국어 질문을 의미는 유지하되 표현을 바꾸어 3~5개의 버전으로 만들어 주세요.\n"
        "출력 형식: 각 줄마다 하나의 패러프레이즈만 작성하고, 불필요한 서두나 번호는 쓰지 마세요.\n"
        f"질문: {trimmed}"
    )

    try:
        response = await model.generate_content_async(
            prompt,
            generation_config=genai.types.GenerationConfig(
                candidate_count=1,
                temperature=0.7,
            ),
        )
    except Exception as exc:  # pragma: no cover - API 오류 대비
        logger.warning("쿼리 재작성 호출 실패: %s", exc)
        return results

    candidates: List[str] = []
    text = (response.text or "").strip() if hasattr(response, "text") else ""
    for line in text.splitlines():
        candidate = line.strip("-•· ").strip()
        if not candidate:
            continue
        if candidate in results or candidate in candidates:
            continue
        candidates.append(candidate)
        if len(candidates) >= variants_target - 1:
            break

    if not candidates:
        return results

    results.extend(candidates)
    return results[:variants_target]

# -*- coding: utf-8 -*-
"""
Linkup 기반 웹 검색 파이프라인.

ToolsCog.web_search_rag()에서 호출하는 비동기 진입점 `run_linkup_search_pipeline()`을 제공합니다.
반환 형식은 기존 news_search 파이프라인과 동일한 dict 계약을 유지합니다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

import config
from logger_config import logger
from utils import db as db_utils


KST = timezone(timedelta(hours=9))
_URL_RE = re.compile(r"https?://[^\s<>\"']+")

_REALTIME_HINTS = (
    "오늘",
    "지금",
    "현재",
    "실시간",
    "최신",
    "최근",
    "속보",
    "업데이트",
    "발표",
    "릴리즈",
    "release",
    "breaking",
)

_DEEP_HINTS = (
    "비교",
    "분석",
    "시장조사",
    "리서치",
    "여러",
    "각각",
    "목록",
    "리스트",
    "정리해줘",
    "자세히",
    "심층",
    "trend",
    "research",
    "first",
    "then",
)

_FAST_HINTS = (
    "언제",
    "누가",
    "어디",
    "몇",
    "얼마",
    "무엇",
    "what",
    "when",
    "who",
    "price",
)

_FAST_BLOCK_HINTS = (
    "비교",
    "분석",
    "정리",
    "라인업",
    "동향",
    "리서치",
    "trend",
    "research",
)

_FETCH_HINTS = (
    "링크",
    "url",
    "페이지",
    "본문",
    "요약",
    "정리",
    "분석",
    "스크랩",
    "fetch",
    "scrape",
)

_pipeline_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_pipeline_cache_lock = asyncio.Lock()
_linkup_budget_lock = asyncio.Lock()


def _cache_key(query: str) -> str:
    base = (query or "").strip().lower()
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


async def _load_cache(query: str) -> dict[str, Any] | None:
    ttl = max(0, int(getattr(config, "WEB_RAG_CACHE_TTL_SECONDS", 300)))
    if ttl <= 0:
        return None

    key = _cache_key(query)
    now = time.time()
    async with _pipeline_cache_lock:
        item = _pipeline_cache.get(key)
        if not item:
            return None
        expire_at, payload = item
        if expire_at <= now:
            _pipeline_cache.pop(key, None)
            return None
        return dict(payload)


async def _save_cache(query: str, payload: dict[str, Any]) -> None:
    ttl = max(0, int(getattr(config, "WEB_RAG_CACHE_TTL_SECONDS", 300)))
    if ttl <= 0:
        return

    max_entries = max(1, int(getattr(config, "WEB_RAG_CACHE_MAX_ENTRIES", 128)))
    key = _cache_key(query)
    now = time.time()
    expire_at = now + ttl

    async with _pipeline_cache_lock:
        if len(_pipeline_cache) >= max_entries:
            oldest_key = min(_pipeline_cache.items(), key=lambda item: item[1][0])[0]
            _pipeline_cache.pop(oldest_key, None)
        _pipeline_cache[key] = (expire_at, dict(payload))


def _contains_realtime_hint(query: str) -> bool:
    query_lower = (query or "").lower()
    return any(token in query_lower for token in _REALTIME_HINTS)


def _looks_complex_query(query: str) -> bool:
    query_lower = (query or "").lower()
    if len(query_lower) >= 90:
        return True
    return any(token in query_lower for token in _DEEP_HINTS)


def infer_linkup_depth(query: str) -> str:
    """질의 성격에 따라 Linkup depth를 결정합니다."""
    query_norm = (query or "").strip().lower()
    if not query_norm:
        return "standard"
    if _looks_complex_query(query_norm):
        return "deep"
    if (
        len(query_norm) <= 36
        and any(token in query_norm for token in _FAST_HINTS)
        and not any(token in query_norm for token in _FAST_BLOCK_HINTS)
    ):
        return "fast"
    return "standard"


def _extract_first_url(query: str) -> str | None:
    match = _URL_RE.search(query or "")
    if not match:
        return None
    return match.group(0).strip()


def _should_fetch_first(query: str, url: str) -> bool:
    """
    공식 가이드에 맞춰 URL이 명시되면 /fetch를 우선 사용합니다.
    """
    _ = query
    _ = url
    return True


def _clip(text: str, limit: int) -> str:
    if limit <= 0:
        return text
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "...(생략)"


def _build_search_prompt(user_query: str, depth: str) -> str:
    cleaned = re.sub(r"\s+", " ", (user_query or "").strip())
    if not cleaned:
        return ""

    today_kst = datetime.now(KST).strftime("%Y-%m-%d")
    if depth == "deep":
        return (
            f"{cleaned}\n\n"
            "Run several searches with adjacent keywords. "
            "If needed, perform sequential retrieval (find URL then scrape). "
            "Prefer authoritative and recent sources. "
            f"Today's date is {today_kst} (KST)."
        )
    if depth == "standard":
        return (
            f"{cleaned}\n\n"
            "Retrieve precise sources for this question and keep concrete dates/numbers when available."
        )
    return cleaned


def _build_search_payload(user_query: str, depth: str) -> dict[str, Any]:
    max_results_default = {
        "fast": max(1, int(getattr(config, "LINKUP_FAST_MAX_RESULTS", 5))),
        "standard": max(1, int(getattr(config, "LINKUP_STANDARD_MAX_RESULTS", 8))),
        "deep": max(1, int(getattr(config, "LINKUP_DEEP_MAX_RESULTS", 10))),
    }
    output_type = str(getattr(config, "LINKUP_OUTPUT_TYPE", "searchResults") or "searchResults")
    if output_type not in {"searchResults", "sourcedAnswer", "structured"}:
        output_type = "searchResults"

    payload: dict[str, Any] = {
        "q": _build_search_prompt(user_query, depth),
        "depth": depth,
        "outputType": output_type,
        "includeInlineCitations": True,
        "maxResults": max_results_default.get(depth, 8),
    }

    if _contains_realtime_hint(user_query):
        lookback_days = max(1, int(getattr(config, "LINKUP_REALTIME_LOOKBACK_DAYS", 30)))
        now_kst = datetime.now(KST).date()
        payload["fromDate"] = (now_kst - timedelta(days=lookback_days)).isoformat()
        payload["toDate"] = now_kst.isoformat()

    return payload


def _normalize_sources(data: dict[str, Any]) -> list[dict[str, str]]:
    sources = data.get("sources")
    if isinstance(sources, list):
        normalized = []
        for item in sources:
            if not isinstance(item, dict):
                continue
            normalized.append(
                {
                    "name": str(item.get("name") or "").strip(),
                    "url": str(item.get("url") or "").strip(),
                    "snippet": str(item.get("snippet") or item.get("content") or "").strip(),
                }
            )
        return [src for src in normalized if src.get("url")]

    results = data.get("results")
    if isinstance(results, list):
        normalized = []
        for item in results:
            if not isinstance(item, dict):
                continue
            normalized.append(
                {
                    "name": str(item.get("name") or "").strip(),
                    "url": str(item.get("url") or "").strip(),
                    "snippet": str(item.get("snippet") or item.get("content") or "").strip(),
                }
            )
        return [src for src in normalized if src.get("url")]
    return []


def _collect_source_urls(sources: list[dict[str, str]]) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for source in sources:
        url = (source.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def _build_context(answer: str, sources: list[dict[str, str]]) -> str:
    blocks: list[str] = []
    answer_clean = (answer or "").strip()
    if answer_clean:
        blocks.append(f"[검색 요약]\n{answer_clean}")

    max_source_blocks = max(1, int(getattr(config, "LINKUP_CONTEXT_SOURCE_BLOCKS", 4)))
    snippet_limit = max(120, int(getattr(config, "LINKUP_CONTEXT_SNIPPET_MAX_CHARS", 300)))
    for idx, source in enumerate(sources[:max_source_blocks], start=1):
        url = source.get("url") or ""
        if not url:
            continue
        title = source.get("name") or "제목 없음"
        snippet = _clip(source.get("snippet") or "", snippet_limit)
        block = f"[출처 {idx}] {title}\n- URL: {url}"
        if snippet:
            block += f"\n- 발췌: {snippet}"
        blocks.append(block)

    context = "\n\n".join(blocks).strip()
    limit = max(800, int(getattr(config, "LINKUP_CONTEXT_MAX_CHARS", 3200)))
    return _clip(context, limit)


def _is_low_quality(answer: str, source_urls: list[str]) -> bool:
    min_sources = max(1, int(getattr(config, "LINKUP_DEEP_RETRY_MIN_SOURCES", 2)))
    min_answer_chars = max(40, int(getattr(config, "LINKUP_MIN_ANSWER_CHARS", 120)))
    if len(source_urls) < min_sources:
        return True
    return len((answer or "").strip()) < min_answer_chars


def _is_low_quality_for_output(
    query: str,
    answer: str,
    sources: list[dict[str, str]],
    source_urls: list[str],
) -> bool:
    output_type = str(getattr(config, "LINKUP_OUTPUT_TYPE", "searchResults") or "searchResults")
    if output_type == "sourcedAnswer":
        return _is_low_quality(answer, source_urls)

    min_sources = max(1, int(getattr(config, "LINKUP_DEEP_RETRY_MIN_SOURCES", 2)))
    if len(source_urls) < min_sources:
        return True

    # searchResults 모드에서는 발췌 텍스트가 너무 빈약하면 deep 재시도
    snippet_len = 0
    for item in sources[: max(1, min(4, len(sources)))]:
        snippet_len += len((item.get("snippet") or "").strip())
    if snippet_len < 120 and (_contains_realtime_hint(query) or _looks_complex_query(query)):
        return True
    return False


def _should_retry_with_deep(query: str, depth: str, answer: str, source_urls: list[str]) -> bool:
    if depth == "deep":
        return False
    if not bool(getattr(config, "LINKUP_QUALITY_RETRY_ENABLED", True)):
        return False
    if not (_contains_realtime_hint(query) or _looks_complex_query(query)):
        return False
    return _is_low_quality(answer, source_urls)


def _format_linkup_error(status: int, body: str) -> str:
    default = f"Linkup API 오류(status={status})"
    try:
        data = json.loads(body)
    except Exception:
        return default

    err = data.get("error")
    if isinstance(err, dict):
        code = err.get("code")
        message = err.get("message")
        if code and message:
            return f"Linkup API 오류({code}): {message}"
        if message:
            return f"Linkup API 오류: {message}"
    return default


def _estimate_linkup_cost(endpoint: str, *, depth: str | None = None, render_js: bool | None = None) -> float:
    ep = str(endpoint or "").strip().lower()
    if ep == "search":
        depth_key = str(depth or "standard").strip().lower()
        if depth_key == "deep":
            return 0.05
        return 0.005  # fast / standard
    if ep == "fetch":
        return 0.005 if bool(render_js) else 0.001
    return 0.0


def _build_budget_exceeded_message(used: float, limit: float, cost: float) -> str:
    month_label = datetime.now(KST).strftime("%Y-%m")
    return (
        "Linkup 월 예산 한도에 도달해 외부 검색을 중단했어요. "
        f"(기준월: {month_label}, 사용: €{used:.3f}, 한도: €{limit:.3f}, 요청비용: €{cost:.3f})"
    )


async def _linkup_post_json(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    api_key = str(getattr(config, "LINKUP_API_KEY", "") or "").strip()
    base_url = str(getattr(config, "LINKUP_BASE_URL", "https://api.linkup.so/v1") or "").strip().rstrip("/")
    if not api_key:
        raise RuntimeError("LINKUP_API_KEY가 설정되지 않았습니다.")

    timeout_seconds = max(5, int(getattr(config, "LINKUP_TIMEOUT_SECONDS", 40)))
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    url = f"{base_url}/{endpoint.lstrip('/')}"
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers, json=payload) as response:
            body_text = await response.text()
            if response.status >= 400:
                raise RuntimeError(_format_linkup_error(response.status, body_text))
            try:
                return json.loads(body_text) if body_text else {}
            except Exception:
                return {}


async def _execute_billed_linkup_call(
    *,
    endpoint: str,
    payload: dict[str, Any],
    db_conn=None,
    depth: str | None = None,
    render_js: bool | None = None,
) -> dict[str, Any]:
    """
    Linkup API 호출 전 월 예산을 확인하고, 성공 호출은 비용을 기록합니다.
    """
    estimated_cost = _estimate_linkup_cost(endpoint, depth=depth, render_js=render_js)
    enforce_budget = bool(getattr(config, "LINKUP_MONTHLY_BUDGET_ENFORCED", True))
    budget_limit = float(getattr(config, "LINKUP_MONTHLY_BUDGET_EUR", 4.5))

    if db_conn is None or not enforce_budget:
        data = await _linkup_post_json(endpoint, payload)
        if db_conn is not None and estimated_cost > 0:
            await db_utils.log_linkup_usage(
                db_conn,
                endpoint=endpoint,
                depth=depth,
                render_js=render_js,
                cost_eur=estimated_cost,
            )
        return data

    async with _linkup_budget_lock:
        allowed, used, limit = await db_utils.can_spend_linkup_budget(
            db_conn,
            estimated_cost,
            budget_limit_eur=budget_limit,
        )
        if not allowed:
            raise RuntimeError(_build_budget_exceeded_message(used, limit, estimated_cost))

        data = await _linkup_post_json(endpoint, payload)
        if estimated_cost > 0:
            await db_utils.log_linkup_usage(
                db_conn,
                endpoint=endpoint,
                depth=depth,
                render_js=render_js,
                cost_eur=estimated_cost,
            )
        return data


async def _run_fetch_pipeline(url: str, db_conn=None) -> dict[str, Any]:
    payload = {
        "url": url,
        "renderJs": bool(getattr(config, "LINKUP_FETCH_RENDER_JS", True)),
        "includeRawHtml": False,
        "extractImages": False,
    }
    data = await _execute_billed_linkup_call(
        endpoint="fetch",
        payload=payload,
        db_conn=db_conn,
        render_js=bool(payload.get("renderJs")),
    )
    markdown = str(data.get("markdown") or "").strip()
    if not markdown:
        return {"status": "error", "message": "Linkup /fetch 응답에 markdown이 없습니다."}

    context_limit = max(800, int(getattr(config, "LINKUP_CONTEXT_MAX_CHARS", 3200)))
    context = _clip(f"[직접 링크 분석]\n{markdown}", context_limit)
    return {
        "status": "success",
        "context": context,
        "source_urls": [url],
        "search_kind": "DIRECT_URL",
        "provider": "linkup",
    }


async def _run_search_pipeline(user_query: str, depth: str, db_conn=None) -> dict[str, Any]:
    payload = _build_search_payload(user_query, depth)
    data = await _execute_billed_linkup_call(
        endpoint="search",
        payload=payload,
        db_conn=db_conn,
        depth=depth,
    )

    answer = str(data.get("answer") or "").strip()
    sources = _normalize_sources(data)
    source_urls = _collect_source_urls(sources)

    should_retry = _should_retry_with_deep(user_query, depth, answer, source_urls)
    if not should_retry:
        should_retry = (
            depth != "deep"
            and bool(getattr(config, "LINKUP_QUALITY_RETRY_ENABLED", True))
            and (_contains_realtime_hint(user_query) or _looks_complex_query(user_query))
            and _is_low_quality_for_output(user_query, answer, sources, source_urls)
        )
    if should_retry:
        retry_payload = _build_search_payload(user_query, "deep")
        retry_data = await _execute_billed_linkup_call(
            endpoint="search",
            payload=retry_payload,
            db_conn=db_conn,
            depth="deep",
        )
        retry_answer = str(retry_data.get("answer") or "").strip()
        retry_sources = _normalize_sources(retry_data)
        retry_urls = _collect_source_urls(retry_sources)
        if retry_answer or retry_urls:
            answer, sources, source_urls, depth = retry_answer, retry_sources, retry_urls, "deep"

    if not answer and not source_urls:
        return {"status": "error", "message": "Linkup 검색 결과가 비어 있습니다."}

    context = _build_context(answer, sources)
    if not context:
        return {"status": "error", "message": "Linkup 검색 컨텍스트 생성에 실패했습니다."}

    return {
        "status": "success",
        "context": context,
        "source_urls": source_urls,
        "search_kind": depth.upper(),
        "provider": "linkup",
        "quality": {
            "depth": depth,
            "source_count": len(source_urls),
            "answer_chars": len(answer),
            "has_inline_citations": bool(re.search(r"\[\d+\]", answer)),
        },
    }


async def run_linkup_search_pipeline(user_query: str, db_conn=None) -> dict[str, Any]:
    """
    Linkup 기반 범용 웹 검색 파이프라인 진입점.
    반환 형식은 tools_cog.web_search_rag() 계약을 따릅니다.
    """
    if not bool(getattr(config, "LINKUP_ENABLED", True)):
        return {"status": "error", "message": "LINKUP_ENABLED=false 로 비활성화되어 있습니다."}

    if not str(getattr(config, "LINKUP_API_KEY", "") or "").strip():
        return {"status": "error", "message": "LINKUP_API_KEY가 설정되지 않았습니다."}

    query = (user_query or "").strip()
    if not query:
        return {"status": "error", "message": "검색어가 비어 있습니다."}

    cached = await _load_cache(query)
    if cached:
        cached["cached"] = True
        return cached

    try:
        url = _extract_first_url(query)
        if url and _should_fetch_first(query, url):
            logger.info("[web_search] Linkup /fetch 경로 사용: %s", url)
            result = await _run_fetch_pipeline(url, db_conn=db_conn)
        else:
            depth = infer_linkup_depth(query)
            logger.info("[web_search] Linkup /search 실행 (depth=%s): %s", depth, query)
            result = await _run_search_pipeline(query, depth, db_conn=db_conn)

        if result.get("status") == "success":
            await _save_cache(query, result)
        return result
    except Exception as exc:
        logger.warning("[web_search] Linkup 파이프라인 실패: %s", exc)
        return {"status": "error", "message": f"Linkup 검색 실패: {exc}"}

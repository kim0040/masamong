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
import math
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

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
_pipeline_inflight: dict[str, asyncio.Task] = {}
_pipeline_cache_lock = asyncio.Lock()
_linkup_budget_lock = asyncio.Lock()


class LinkupRequestError(RuntimeError):
    """Linkup 물리 호출 실패와 폴백 안전 여부를 함께 전달합니다."""

    def __init__(
        self,
        message: str,
        *,
        fallback_safe: bool,
        failure_kind: str,
        status: int | None = None,
    ):
        super().__init__(message)
        self.fallback_safe = bool(fallback_safe)
        self.failure_kind = str(failure_kind)
        self.status = status


class LinkupReservationError(RuntimeError):
    """provider 호출 전 비용 예약을 안전하게 완료하지 못했습니다."""


class LinkupBudgetExceededError(RuntimeError):
    """월 예산 검사에서 호출이 명확히 차단되었습니다."""


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    return min(maximum, max(minimum, parsed))


def _bounded_float(
    value: Any,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    if not math.isfinite(parsed):
        parsed = default
    return min(maximum, max(minimum, parsed))


def _error_result(
    message: str,
    *,
    fallback_safe: bool,
    failure_kind: str,
) -> dict[str, Any]:
    return {
        "status": "error",
        "message": str(message),
        "provider": "linkup",
        "fallback_safe": bool(fallback_safe),
        "failure_kind": str(failure_kind),
    }


def _cache_key(query: str, depth_hint: str | None = None) -> str:
    base = re.sub(r"\s+", " ", (query or "").strip().lower())
    depth_scope = str(depth_hint or "auto").strip().lower()
    return hashlib.sha256(
        f"{depth_scope}\n{base}".encode("utf-8")
    ).hexdigest()


async def _load_cache(
    query: str,
    depth_hint: str | None = None,
) -> dict[str, Any] | None:
    ttl = _bounded_int(
        getattr(config, "WEB_RAG_CACHE_TTL_SECONDS", 300),
        300,
        0,
        3600,
    )
    if ttl <= 0:
        return None

    key = _cache_key(query, depth_hint)
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


async def _save_cache(
    query: str,
    payload: dict[str, Any],
    depth_hint: str | None = None,
) -> None:
    ttl = _bounded_int(
        getattr(config, "WEB_RAG_CACHE_TTL_SECONDS", 300),
        300,
        0,
        3600,
    )
    if ttl <= 0:
        return

    max_entries = _bounded_int(
        getattr(config, "WEB_RAG_CACHE_MAX_ENTRIES", 128),
        128,
        1,
        1024,
    )
    key = _cache_key(query, depth_hint)
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


def normalize_linkup_depth_hint(query: str, depth_hint: str | None) -> str:
    """의미 라우터의 depth 힌트를 실행 가능한 범위로 제한합니다.

    정상 경로에서는 이미 호출한 의미 라우터가 질의의 범위와 순서를 판단합니다.
    여기서는 추가 LLM 호출이나 이름별 하드코딩 없이, URL·다단계 질의를 fast로
    잘못 내리는 구조적 오류만 막습니다. 힌트가 없으면 기존 보수적 추론을 씁니다.
    """
    normalized = str(depth_hint or "").strip().lower()
    if normalized not in {"fast", "standard", "deep"}:
        return infer_linkup_depth(query)
    if normalized == "fast" and (
        _extract_first_url(query)
        or _looks_complex_query(query)
        or "\n" in str(query or "")
    ):
        return "standard"
    return normalized


def _extract_first_url(query: str) -> str | None:
    match = _URL_RE.search(query or "")
    if not match:
        return None
    candidate = match.group(0).strip().rstrip(".,!?;:)]}>\"'")
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _should_fetch_first(query: str, url: str) -> bool:
    """단일 페이지 읽기 요청만 /fetch로 보내고 비교·검증은 /search로 보냅니다."""
    if not url:
        return False
    query_lower = (query or "").lower()
    cross_source_hints = (
        "비교",
        "경쟁",
        "다른",
        "여러",
        "사실 확인",
        "팩트체크",
        "검증",
        "최신",
        "현재",
        "compare",
        "versus",
        " vs ",
        "verify",
    )
    if any(token in query_lower for token in cross_source_hints):
        return False
    return any(token in query_lower for token in _FETCH_HINTS)


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
            "Retrieve precise, authoritative sources for this question. Prefer official "
            "records and reputable primary reporting over social posts or community summaries. "
            "Keep concrete dates/numbers only when a retrieved source supports them, and "
            f"distinguish event date from publication date. Today's date is {today_kst} (KST)."
        )
    return cleaned


def _build_search_payload(user_query: str, depth: str) -> dict[str, Any]:
    max_results_default = {
        "fast": _bounded_int(
            getattr(config, "LINKUP_FAST_MAX_RESULTS", 5), 5, 1, 20
        ),
        "standard": _bounded_int(
            getattr(config, "LINKUP_STANDARD_MAX_RESULTS", 8), 8, 1, 20
        ),
        "deep": _bounded_int(
            getattr(config, "LINKUP_DEEP_MAX_RESULTS", 10), 10, 1, 20
        ),
    }
    output_type = str(getattr(config, "LINKUP_OUTPUT_TYPE", "searchResults") or "searchResults")
    if output_type not in {"searchResults", "sourcedAnswer", "structured"}:
        output_type = "searchResults"

    payload: dict[str, Any] = {
        "q": _build_search_prompt(user_query, depth),
        "depth": depth,
        "outputType": output_type,
        "maxResults": max_results_default.get(depth, 8),
    }
    if output_type == "sourcedAnswer":
        payload["includeInlineCitations"] = True

    if _contains_realtime_hint(user_query):
        lookback_days = _bounded_int(
            getattr(config, "LINKUP_REALTIME_LOOKBACK_DAYS", 30),
            30,
            1,
            365,
        )
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
        return _dedupe_valid_sources(normalized)

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
        return _dedupe_valid_sources(normalized)
    return []


def _dedupe_valid_sources(sources: list[dict[str, str]]) -> list[dict[str, str]]:
    """HTTP(S) 출처만 정규화하고 동일 URL 중복을 제거합니다."""
    seen: set[str] = set()
    normalized: list[dict[str, str]] = []
    for source in sources:
        raw_url = str(source.get("url") or "").strip()
        try:
            parsed = urlsplit(raw_url)
        except ValueError:
            continue
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        clean_url = urlunsplit(
            (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, "")
        )
        key = clean_url.rstrip("/")
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append(
            {
                "name": str(source.get("name") or "").strip(),
                "url": clean_url,
                "snippet": str(source.get("snippet") or "").strip(),
            }
        )
    return normalized


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

    max_source_blocks = _bounded_int(
        getattr(config, "LINKUP_CONTEXT_SOURCE_BLOCKS", 4), 4, 1, 10
    )
    snippet_limit = _bounded_int(
        getattr(config, "LINKUP_CONTEXT_SNIPPET_MAX_CHARS", 300),
        300,
        120,
        2000,
    )
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
    limit = _bounded_int(
        getattr(config, "LINKUP_CONTEXT_MAX_CHARS", 3200),
        3200,
        800,
        16000,
    )
    return _clip(context, limit)


def _is_low_quality(answer: str, source_urls: list[str]) -> bool:
    min_sources = _bounded_int(
        getattr(config, "LINKUP_DEEP_RETRY_MIN_SOURCES", 2), 2, 1, 10
    )
    min_answer_chars = _bounded_int(
        getattr(config, "LINKUP_MIN_ANSWER_CHARS", 120), 120, 40, 2000
    )
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

    min_sources = _bounded_int(
        getattr(config, "LINKUP_DEEP_RETRY_MIN_SOURCES", 2), 2, 1, 10
    )
    if len(source_urls) < min_sources:
        return True

    # searchResults 모드에서는 발췌 텍스트가 너무 빈약하면 deep 재시도
    snippet_len = 0
    for item in sources[: max(1, min(4, len(sources)))]:
        snippet_len += len((item.get("snippet") or "").strip())
    if snippet_len < 120 and (_contains_realtime_hint(query) or _looks_complex_query(query)):
        return True
    return False


def _should_retry_for_quality(
    query: str,
    depth: str,
    answer: str,
    source_urls: list[str],
) -> bool:
    if depth == "deep":
        return False
    if not bool(getattr(config, "LINKUP_QUALITY_RETRY_ENABLED", True)):
        return False
    if not (_contains_realtime_hint(query) or _looks_complex_query(query)):
        return False
    output_type = str(
        getattr(config, "LINKUP_OUTPUT_TYPE", "searchResults")
        or "searchResults"
    )
    if output_type != "sourcedAnswer":
        min_sources = _bounded_int(
            getattr(config, "LINKUP_DEEP_RETRY_MIN_SOURCES", 2),
            2,
            1,
            10,
        )
        return len(source_urls) < min_sources
    return _is_low_quality(answer, source_urls)


def _quality_retry_depth(depth: str) -> str | None:
    """품질 재시도는 한 단계만 올려 불필요한 deep 비용을 피합니다."""
    return {
        "fast": "standard",
        "standard": "deep",
    }.get(str(depth or "").strip().lower())


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


def _estimate_linkup_cost_usd(
    endpoint: str,
    *,
    depth: str | None = None,
    render_js: bool | None = None,
) -> float:
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
        f"(기준월: {month_label}, 사용: ${used:.3f}, 한도: ${limit:.3f}, 요청비용: ${cost:.3f})"
    )


async def _linkup_post_json(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    api_key = str(getattr(config, "LINKUP_API_KEY", "") or "").strip()
    base_url = str(getattr(config, "LINKUP_BASE_URL", "https://api.linkup.so/v1") or "").strip().rstrip("/")
    if not api_key:
        raise RuntimeError("LINKUP_API_KEY가 설정되지 않았습니다.")

    timeout_seconds = _bounded_int(
        getattr(config, "LINKUP_TIMEOUT_SECONDS", 40),
        40,
        5,
        120,
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    url = f"{base_url}/{endpoint.lstrip('/')}"
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as response:
                body_text = await response.text()
                if response.status >= 400:
                    # 명시적인 4xx 거절은 provider가 검색을 수행하지 않은 것으로
                    # 보아 레거시 폴백을 허용한다. 5xx는 검색 처리가 일부 진행됐을
                    # 가능성이 있어 같은 요청의 추가 외부 검색을 막는다. 비용 상태는
                    # 공식 계약에 따라 HTTP 오류 모두 not_billed로 별도 확정한다.
                    fallback_safe = 400 <= response.status < 500
                    raise LinkupRequestError(
                        _format_linkup_error(response.status, body_text),
                        fallback_safe=fallback_safe,
                        failure_kind=(
                            "provider_rejected"
                            if fallback_safe
                            else "provider_outcome_unknown"
                        ),
                        status=response.status,
                    )
                try:
                    return json.loads(body_text) if body_text else {}
                except (TypeError, ValueError, json.JSONDecodeError):
                    return {}
    except LinkupRequestError:
        raise
    except (asyncio.TimeoutError, aiohttp.ClientError, ConnectionError, OSError) as exc:
        raise LinkupRequestError(
            f"Linkup provider 응답을 확인하지 못했습니다: {exc}",
            fallback_safe=False,
            failure_kind="provider_outcome_unknown",
        ) from exc


async def _execute_billed_linkup_call(
    *,
    endpoint: str,
    payload: dict[str, Any],
    db_conn=None,
    depth: str | None = None,
    render_js: bool | None = None,
) -> dict[str, Any]:
    """비용 check→reservation→provider call을 프로세스 lock 안에서 수행합니다."""
    estimated_cost = _estimate_linkup_cost_usd(
        endpoint,
        depth=depth,
        render_js=render_js,
    )
    enforce_budget = bool(getattr(config, "LINKUP_MONTHLY_BUDGET_ENFORCED", True))
    budget_limit = _bounded_float(
        getattr(
            config,
            "LINKUP_MONTHLY_BUDGET_USD",
            getattr(config, "LINKUP_MONTHLY_BUDGET_EUR", 4.5),
        ),
        4.5,
        0.0,
        1000.0,
    )

    if estimated_cost <= 0:
        raise LinkupReservationError("알 수 없는 Linkup 요청 비용이라 호출을 중단했습니다.")

    async with _linkup_budget_lock:
        if db_conn is None:
            raise LinkupReservationError(
                "Linkup 사용량 저장소가 없어 비용을 예약할 수 없습니다."
            )

        if enforce_budget:
            allowed, used, limit = await db_utils.can_spend_linkup_budget_usd(
                db_conn,
                estimated_cost,
                budget_limit_usd=budget_limit,
            )
            if used == float("inf"):
                raise LinkupReservationError(
                    "Linkup 월 사용량을 확인하지 못해 호출을 중단했습니다."
                )
            if not allowed:
                raise LinkupBudgetExceededError(
                    _build_budget_exceeded_message(used, limit, estimated_cost)
                )

        # 물리 호출 전에 예상 비용을 예약한다. DB 기록이 실패하면 provider를
        # 호출하지 않는다. 성공/명시 실패/결과 불명을 호출 뒤에 확정해 실제
        # 과금과 가깝게 집계하되, 확정 실패 행은 reserved로 남아 과소 계측되지 않는다.
        request_id = uuid.uuid4().hex
        reserved = await db_utils.reserve_linkup_usage(
            db_conn,
            request_id=request_id,
            endpoint=endpoint,
            depth=depth,
            render_js=render_js,
            cost_usd=estimated_cost,
        )
        if not reserved:
            raise LinkupReservationError(
                "Linkup 예상 비용을 저장하지 못해 호출을 중단했습니다."
            )

        try:
            result = await _linkup_post_json(endpoint, payload)
        except LinkupRequestError as exc:
            # HTTP 오류 응답은 Linkup 문서상 과금되지 않는다. timeout/연결 유실은
            # provider가 처리했는지 확인할 수 없으므로 보수적으로 과금 추정한다.
            billing_status = (
                "not_billed"
                if exc.status is not None
                else "billed_assumed"
            )
            finalized = await db_utils.finalize_linkup_usage(
                db_conn,
                request_id=request_id,
                billing_status=billing_status,
            )
            if not finalized:
                logger.error(
                    "[web_search] Linkup 실패 호출 비용 상태 확정 실패. "
                    "reserved 상태로 보수 집계합니다."
                )
            raise
        except Exception:
            finalized = await db_utils.finalize_linkup_usage(
                db_conn,
                request_id=request_id,
                billing_status="billed_assumed",
            )
            if not finalized:
                logger.error(
                    "[web_search] Linkup 결과 불명 호출 비용 상태 확정 실패. "
                    "reserved 상태로 보수 집계합니다."
                )
            raise

        finalized = await db_utils.finalize_linkup_usage(
            db_conn,
            request_id=request_id,
            billing_status="billed",
        )
        if not finalized:
            logger.error(
                "[web_search] Linkup 성공 호출 비용 확정 실패. "
                "reserved 상태로 보수 집계합니다."
            )
        return result


async def _run_fetch_pipeline(url: str, db_conn=None) -> dict[str, Any]:
    render_js = bool(getattr(config, "LINKUP_FETCH_RENDER_JS", False))
    payload = {
        "url": url,
        "renderJs": render_js,
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
    if (
        not markdown
        and not render_js
        and bool(getattr(config, "LINKUP_FETCH_JS_RETRY_ENABLED", True))
    ):
        retry_payload = dict(payload)
        retry_payload["renderJs"] = True
        data = await _execute_billed_linkup_call(
            endpoint="fetch",
            payload=retry_payload,
            db_conn=db_conn,
            render_js=True,
        )
        markdown = str(data.get("markdown") or "").strip()
    if not markdown:
        return _error_result(
            "Linkup /fetch 응답에 markdown이 없습니다.",
            fallback_safe=True,
            failure_kind="empty_result",
        )

    context_limit = _bounded_int(
        getattr(config, "LINKUP_CONTEXT_MAX_CHARS", 3200),
        3200,
        800,
        16000,
    )
    context = _clip(f"[직접 링크 분석]\n{markdown}", context_limit)
    return {
        "status": "success",
        "context": context,
        "source_urls": [url],
        "sources": [{"name": "직접 링크", "url": url, "snippet": ""}],
        "search_kind": "DIRECT_URL",
        "provider": "linkup",
    }


async def _run_search_pipeline(
    user_query: str,
    depth: str,
    db_conn=None,
) -> dict[str, Any]:
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

    should_retry = _should_retry_for_quality(
        user_query,
        depth,
        answer,
        source_urls,
    )
    if not should_retry:
        should_retry = (
            depth != "deep"
            and bool(getattr(config, "LINKUP_QUALITY_RETRY_ENABLED", True))
            and (_contains_realtime_hint(user_query) or _looks_complex_query(user_query))
            and _is_low_quality_for_output(user_query, answer, sources, source_urls)
        )
    if should_retry:
        retry_depth = _quality_retry_depth(depth)
        if retry_depth is None:
            retry_depth = depth
        retry_payload = _build_search_payload(user_query, retry_depth)
        retry_data = await _execute_billed_linkup_call(
            endpoint="search",
            payload=retry_payload,
            db_conn=db_conn,
            depth=retry_depth,
        )
        retry_answer = str(retry_data.get("answer") or "").strip()
        retry_sources = _normalize_sources(retry_data)
        retry_urls = _collect_source_urls(retry_sources)
        if retry_answer or retry_urls:
            answer = retry_answer
            sources = retry_sources
            source_urls = retry_urls
            depth = retry_depth

    if not answer and not source_urls:
        return _error_result(
            "Linkup 검색 결과가 비어 있습니다.",
            fallback_safe=True,
            failure_kind="empty_result",
        )

    context = _build_context(answer, sources)
    if not context:
        return _error_result(
            "Linkup 검색 컨텍스트 생성에 실패했습니다.",
            fallback_safe=True,
            failure_kind="empty_result",
        )

    return {
        "status": "success",
        "context": context,
        "source_urls": source_urls,
        "sources": sources,
        "search_kind": depth.upper(),
        "provider": "linkup",
        "quality": {
            "depth": depth,
            "source_count": len(source_urls),
            "answer_chars": len(answer),
            "has_inline_citations": bool(re.search(r"\[\d+\]", answer)),
        },
    }


async def _run_uncached_pipeline(
    user_query: str,
    db_conn=None,
    depth_hint: str | None = None,
) -> dict[str, Any]:
    """
    Linkup 기반 범용 웹 검색 파이프라인 진입점.
    반환 형식은 tools_cog.web_search_rag() 계약을 따릅니다.
    """
    if not bool(getattr(config, "LINKUP_ENABLED", True)):
        return _error_result(
            "LINKUP_ENABLED=false 로 비활성화되어 있습니다.",
            fallback_safe=True,
            failure_kind="disabled",
        )

    if not str(getattr(config, "LINKUP_API_KEY", "") or "").strip():
        return _error_result(
            "LINKUP_API_KEY가 설정되지 않았습니다.",
            fallback_safe=True,
            failure_kind="configuration",
        )

    query = re.sub(r"\s+", " ", (user_query or "").strip())
    if not query:
        return _error_result(
            "검색어가 비어 있습니다.",
            fallback_safe=True,
            failure_kind="invalid_input",
        )

    effective_depth = normalize_linkup_depth_hint(query, depth_hint)
    cached = await _load_cache(query, effective_depth)
    if cached:
        cached["cached"] = True
        return cached

    try:
        url = _extract_first_url(query)
        if url and _should_fetch_first(query, url):
            logger.info(
                "[web_search] Linkup /fetch 경로 사용. url_chars=%d",
                len(url),
            )
            result = await _run_fetch_pipeline(url, db_conn=db_conn)
        else:
            depth = effective_depth
            logger.info(
                "[web_search] Linkup /search 실행. depth=%s query_chars=%d",
                depth,
                len(query),
            )
            result = await _run_search_pipeline(query, depth, db_conn=db_conn)

        if result.get("status") == "success":
            await _save_cache(query, result, effective_depth)
        return result
    except LinkupRequestError as exc:
        logger.warning("[web_search] Linkup 파이프라인 실패: %s", exc)
        return _error_result(
            f"Linkup 검색 실패: {exc}",
            fallback_safe=exc.fallback_safe,
            failure_kind=exc.failure_kind,
        )
    except LinkupBudgetExceededError as exc:
        logger.info("[web_search] Linkup 예산으로 호출 차단: %s", exc)
        return _error_result(
            str(exc),
            fallback_safe=True,
            failure_kind="budget_exceeded",
        )
    except LinkupReservationError as exc:
        logger.error("[web_search] Linkup 비용 예약 실패: %s", exc)
        return _error_result(
            str(exc),
            fallback_safe=False,
            failure_kind="reservation_failed",
        )
    except (asyncio.TimeoutError, aiohttp.ClientError, ConnectionError, OSError) as exc:
        logger.warning("[web_search] Linkup 결과 불명 실패: %s", exc)
        return _error_result(
            f"Linkup 검색 결과를 확인하지 못했습니다: {exc}",
            fallback_safe=False,
            failure_kind="provider_outcome_unknown",
        )
    except Exception as exc:
        logger.exception("[web_search] Linkup 파이프라인 예기치 않은 실패")
        return _error_result(
            f"Linkup 검색 실패: {exc}",
            fallback_safe=False,
            failure_kind="unexpected_error",
        )


async def _run_singleflight(
    query: str,
    key: str,
    db_conn=None,
    depth_hint: str | None = None,
) -> dict[str, Any]:
    """동일 검색어의 provider 호출을 합치고 작업 종료 시 슬롯을 회수합니다."""
    try:
        pipeline_timeout = _bounded_int(
            getattr(config, "LINKUP_PIPELINE_TIMEOUT_SECONDS", 55),
            55,
            10,
            180,
        )
        try:
            return await asyncio.wait_for(
                _run_uncached_pipeline(
                    query,
                    db_conn=db_conn,
                    depth_hint=depth_hint,
                ),
                timeout=pipeline_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[web_search] Linkup 전체 파이프라인 시간 상한(%ss) 초과",
                pipeline_timeout,
            )
            return _error_result(
                "외부 검색이 제한 시간 안에 완료되지 않았습니다.",
                fallback_safe=False,
                failure_kind="pipeline_timeout",
            )
    finally:
        current = asyncio.current_task()
        async with _pipeline_cache_lock:
            if _pipeline_inflight.get(key) is current:
                _pipeline_inflight.pop(key, None)


async def run_linkup_search_pipeline(
    user_query: str,
    db_conn=None,
    depth_hint: str | None = None,
) -> dict[str, Any]:
    """Linkup 검색 진입점. 캐시 미스인 동일 동시 질의는 한 번만 과금합니다."""
    query = re.sub(r"\s+", " ", (user_query or "").strip())
    if not query:
        return _error_result(
            "검색어가 비어 있습니다.",
            fallback_safe=True,
            failure_kind="invalid_input",
        )

    effective_depth = normalize_linkup_depth_hint(query, depth_hint)
    cached = await _load_cache(query, effective_depth)
    if cached:
        cached["cached"] = True
        return cached

    key = _cache_key(query, effective_depth)
    async with _pipeline_cache_lock:
        task = _pipeline_inflight.get(key)
        shared = task is not None
        if task is None:
            task = asyncio.create_task(
                _run_singleflight(
                    query,
                    key,
                    db_conn=db_conn,
                    depth_hint=effective_depth,
                ),
                name=f"linkup:{key[:10]}",
            )
            _pipeline_inflight[key] = task

    result = await asyncio.shield(task)
    if shared:
        result = dict(result)
        result["shared_inflight"] = True
    return result

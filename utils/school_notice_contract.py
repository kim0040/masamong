# -*- coding: utf-8 -*-
"""학교 공지 digest JSON을 검증해 타입이 있는 객체로 변환합니다.

digest는 마사몽 봇이 아니라 별도 batch 프로세스가 만든 파일이므로, 이 모듈은
파일 내용을 신뢰하지 않고 계약(docs/SCHOOL_NOTICE_INTEGRATION_PLAN.ko.md 12장)을
전부 확인한 뒤에만 통과시킵니다. 계약이 깨진 digest를 조용히 부분 렌더링하면
사용자에게 잘못된 마감이나 자격 판정을 보여줄 수 있어 전달 자체를 중단합니다.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SUPPORTED_SCHEMA_VERSION = 1

# digest는 별도 프로세스가 만든 외부 입력이다. 파일과 컬렉션 상한을 함께 두어
# 잘못된 산출물 하나가 저사양 봇의 메모리를 소진하지 못하게 한다. 코어 프로필의
# 밴드별 최대치(각 100)를 모두 허용하면서도 그 이상은 계약 오류로 처리한다.
MAX_DIGEST_FILE_BYTES = 8 * 1024 * 1024
MAX_DIGEST_ITEMS = 300
MAX_HEALTH_SOURCES = 128
MAX_COLLECTION_ENTRIES = 100
MAX_RAW_STRING_CHARS = 3_000_000
MAX_TOTAL_STRING_CHARS = MAX_DIGEST_FILE_BYTES
MAX_JSON_NODES = 100_000
MAX_JSON_DEPTH = 32

_MAX_USER_KEY = 128
_MAX_SOURCE_ID = 64
_MAX_EXTERNAL_ID = 128
_MAX_DEDUP_KEY = 256
_MAX_URL = 4096
_MAX_TITLE = 2048
_MAX_ITEM_TEXT = 20_000
_MAX_SHORT_TEXT = 1024
_MAX_LIST_TEXT = 2048
_MAX_EVIDENCE = 8192
_MAX_COUNTER = 1_000_000
_MISSING = object()

BANDS = frozenset({"action", "opportunity", "reference", "hidden"})
ELIGIBILITIES = frozenset(
    {"ELIGIBLE", "LIKELY_ELIGIBLE", "INELIGIBLE", "UNKNOWN"}
)
URGENCIES = frozenset({"low", "normal", "high", "critical"})
CHANGES = frozenset({"new", "updated", "unchanged"})
HEALTH_STATUSES = frozenset({"healthy", "degraded", "failed"})
RUN_STATUSES = frozenset({"succeeded", "partial", "failed"})

# 밴드는 사용자에게 보여줄 때 항상 이 순서를 지킨다.
BAND_ORDER = ("action", "opportunity", "reference")

FEEDBACK_TYPES = frozenset(
    {
        "useful",
        "saved",
        "applied",
        "not_interested",
        "already_knew",
        "completed",
        "dismiss_once",
        "not_eligible",
        "mute_topic",
    }
)


class DigestContractError(ValueError):
    """digest JSON이 계약을 만족하지 않을 때 발생합니다."""


def _require(payload: Any, key: str, types: tuple[type, ...], where: str) -> Any:
    if not isinstance(payload, dict) or key not in payload:
        raise DigestContractError(f"{where}: 필수 필드 누락 {key!r}")
    value = payload[key]
    if not isinstance(value, types) or isinstance(value, bool) and bool not in types:
        raise DigestContractError(
            f"{where}: {key!r} 타입이 올바르지 않습니다 ({type(value).__name__})"
        )
    return value


def _bounded_string(
    value: Any,
    *,
    where: str,
    max_length: int,
    allow_empty: bool = True,
) -> str:
    if not isinstance(value, str):
        raise DigestContractError(
            f"{where}: 문자열이어야 합니다 ({type(value).__name__})"
        )
    rendered = value.strip() if not allow_empty else value
    if not allow_empty and not rendered:
        raise DigestContractError(f"{where}: 비어 있지 않은 문자열이어야 합니다.")
    if len(value) > max_length:
        raise DigestContractError(
            f"{where}: 문자열이 너무 깁니다 ({len(value)}>{max_length})"
        )
    return rendered


def _optional_str(
    payload: dict[str, Any],
    key: str,
    *,
    where: str,
    max_length: int = _MAX_SHORT_TEXT,
) -> str | None:
    value = payload.get(key, _MISSING)
    if value is _MISSING or value is None:
        return None
    return _bounded_string(
        value,
        where=f"{where}.{key}",
        max_length=max_length,
    )


def _optional_bool(
    payload: dict[str, Any],
    key: str,
    *,
    where: str,
    default: bool,
) -> bool:
    value = payload.get(key, _MISSING)
    if value is _MISSING:
        return default
    if type(value) is not bool:
        raise DigestContractError(
            f"{where}.{key}: boolean이어야 합니다 ({type(value).__name__})"
        )
    return value


def _int_value(
    value: Any,
    *,
    where: str,
    minimum: int = 0,
    maximum: int = _MAX_COUNTER,
) -> int:
    if type(value) is not int:
        raise DigestContractError(
            f"{where}: 정수여야 합니다 ({type(value).__name__})"
        )
    if not minimum <= value <= maximum:
        raise DigestContractError(
            f"{where}: {minimum}~{maximum} 범위여야 합니다 ({value!r})"
        )
    return value


def _optional_int(
    payload: dict[str, Any],
    key: str,
    *,
    where: str,
    default: int,
    minimum: int = 0,
    maximum: int = _MAX_COUNTER,
) -> int:
    value = payload.get(key, _MISSING)
    if value is _MISSING:
        return default
    return _int_value(
        value,
        where=f"{where}.{key}",
        minimum=minimum,
        maximum=maximum,
    )


def _number_value(
    value: Any,
    *,
    where: str,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DigestContractError(
            f"{where}: 숫자여야 합니다 ({type(value).__name__})"
        )
    rendered = float(value)
    if not math.isfinite(rendered) or not minimum <= rendered <= maximum:
        raise DigestContractError(
            f"{where}: {minimum:g}~{maximum:g}의 유한한 숫자여야 합니다 "
            f"({value!r})"
        )
    return rendered


def _string_list(
    payload: dict[str, Any],
    key: str,
    *,
    where: str,
    max_items: int = MAX_COLLECTION_ENTRIES,
    max_length: int = _MAX_LIST_TEXT,
) -> tuple[str, ...]:
    value = payload.get(key, _MISSING)
    if value is _MISSING:
        return ()
    if not isinstance(value, list):
        raise DigestContractError(f"{where}.{key}: 배열이어야 합니다.")
    if len(value) > max_items:
        raise DigestContractError(
            f"{where}.{key}: 항목이 너무 많습니다 ({len(value)}>{max_items})"
        )
    return tuple(
        _bounded_string(
            item,
            where=f"{where}.{key}[{index}]",
            max_length=max_length,
        )
        for index, item in enumerate(value)
    )


def _http_url(value: Any, *, where: str, allow_empty: bool = False) -> str:
    rendered = _bounded_string(
        value,
        where=where,
        max_length=_MAX_URL,
        allow_empty=allow_empty,
    )
    if not rendered and allow_empty:
        return rendered
    parsed = urlsplit(rendered)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise DigestContractError(
            f"{where}: http/https 절대 URL이어야 합니다 ({rendered!r})"
        )
    return rendered


def _validate_payload_budget(payload: Any) -> None:
    """직접 `parse_digest`를 부른 경우에도 파일 입력과 같은 자원 상한을 적용."""
    total_chars = 0
    nodes = 0
    stack: list[tuple[Any, int]] = [(payload, 0)]
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise DigestContractError(
                f"digest JSON 노드가 너무 많습니다 ({nodes}>{MAX_JSON_NODES})"
            )
        if depth > MAX_JSON_DEPTH:
            raise DigestContractError(
                f"digest JSON 중첩이 너무 깊습니다 ({depth}>{MAX_JSON_DEPTH})"
            )
        if isinstance(value, str):
            if len(value) > MAX_RAW_STRING_CHARS:
                raise DigestContractError(
                    "digest JSON 문자열 하나가 너무 깁니다 "
                    f"({len(value)}>{MAX_RAW_STRING_CHARS})"
                )
            total_chars += len(value)
            if total_chars > MAX_TOTAL_STRING_CHARS:
                raise DigestContractError(
                    "digest JSON 문자열 총량이 너무 큽니다 "
                    f"({total_chars}>{MAX_TOTAL_STRING_CHARS})"
                )
        elif isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise DigestContractError(
                        "digest JSON 객체 키는 문자열이어야 합니다."
                    )
                if len(key) > _MAX_SHORT_TEXT:
                    raise DigestContractError("digest JSON 객체 키가 너무 깁니다.")
                total_chars += len(key)
                if total_chars > MAX_TOTAL_STRING_CHARS:
                    raise DigestContractError(
                        "digest JSON 문자열 총량이 너무 큽니다 "
                        f"({total_chars}>{MAX_TOTAL_STRING_CHARS})"
                    )
                stack.append((child, depth + 1))
        elif isinstance(value, (list, tuple)):
            stack.extend((child, depth + 1) for child in value)
        elif isinstance(value, float) and not math.isfinite(value):
            raise DigestContractError(
                f"digest JSON에 유한하지 않은 숫자가 있습니다: {value!r}"
            )


def _parse_iso_date(value: Any, where: str) -> date | None:
    """`YYYY-MM-DD`만 허용합니다. 형식이 다르면 날짜를 추측하지 않습니다."""
    if value is None:
        return None
    rendered = _bounded_string(
        value,
        where=where,
        max_length=10,
        allow_empty=False,
    )
    try:
        parsed = date.fromisoformat(rendered)
    except (TypeError, ValueError) as exc:
        raise DigestContractError(
            f"{where}: 날짜 형식이 올바르지 않습니다 {value!r}"
        ) from exc
    if parsed.isoformat() != rendered:
        raise DigestContractError(f"{where}: YYYY-MM-DD 형식이어야 합니다 {value!r}")
    return parsed


@dataclass(frozen=True)
class NoticeDate:
    """분석이 추출한 날짜 하나."""

    value: date
    kind: str
    evidence: str = ""
    # 원문에 연도가 없어 코어가 추론한 값. 마감 표시에 주의 문구가 필요하다.
    inferred_year: bool = False


@dataclass(frozen=True)
class SourceHealth:
    """source 하나의 수집 상태."""

    source_id: str
    status: str
    list_candidates: int = 0
    details_succeeded: int = 0
    details_failed: int = 0
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class CollectionHealth:
    """이번 실행의 수집 상태 요약."""

    status: str
    healthy: int = 0
    degraded: int = 0
    failed: int = 0
    may_include_stale_notices: bool = False
    sources: tuple[SourceHealth, ...] = ()

    @property
    def has_problem(self) -> bool:
        return self.status != "healthy" or self.may_include_stale_notices

    def failed_sources(self) -> tuple[SourceHealth, ...]:
        return tuple(item for item in self.sources if item.status == "failed")

    def degraded_sources(self) -> tuple[SourceHealth, ...]:
        return tuple(item for item in self.sources if item.status == "degraded")


@dataclass(frozen=True)
class DigestItem:
    """digest 항목 하나. 공지 원문·분석·점수를 함께 담습니다."""

    notice_id: int
    dedup_key: str
    change: str
    revision_count: int
    source_id: str
    external_id: str
    url: str
    title: str
    university: str | None
    board: str | None
    author: str | None
    published_text: str | None
    attachments: tuple[dict[str, Any], ...]
    summary: str
    topics: tuple[str, ...]
    actions: tuple[str, ...]
    audiences: tuple[str, ...]
    required: bool
    urgency: str
    dates: tuple[NoticeDate, ...]
    analysis_source: str
    score: float
    band: str
    eligibility: str
    reasons: tuple[str, ...]
    deadline: date | None
    next_event: date | None
    mandatory_protected: bool
    duplicate_sources: tuple[dict[str, str], ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def needs_manual_check(self) -> bool:
        """자격을 확정할 수 없어 사용자가 원문을 확인해야 하는가."""
        return self.eligibility == "UNKNOWN"

    @property
    def has_inferred_deadline(self) -> bool:
        """마감 날짜의 연도가 추론된 값인가."""
        return any(item.inferred_year and item.kind == "deadline" for item in self.dates)

    def feedback_key(self) -> tuple[str, str]:
        """피드백 기록에 쓰는 안정적 식별자."""
        return (self.source_id, self.external_id)


@dataclass(frozen=True)
class Digest:
    """검증을 통과한 하루치 digest."""

    schema_version: int
    user_key: str
    digest_date: date
    summary: dict[str, int]
    items: tuple[DigestItem, ...]
    collection_health: CollectionHealth | None = None
    warnings: tuple[str, ...] = field(default=())

    def visible_items(self) -> tuple[DigestItem, ...]:
        """밴드 우선순위와 점수 순으로 정렬한 표시 대상.

        최소 점수·밴드 필터는 코어가 이미 적용했으므로 여기서 다시 적용하지
        않습니다. `hidden`만 방어적으로 제외합니다.
        """
        ordered = [item for item in self.items if item.band in BAND_ORDER]
        ordered.sort(
            key=lambda item: (BAND_ORDER.index(item.band), -item.score, item.notice_id)
        )
        return tuple(ordered)

    def items_by_band(self) -> dict[str, tuple[DigestItem, ...]]:
        grouped: dict[str, list[DigestItem]] = {band: [] for band in BAND_ORDER}
        for item in self.visible_items():
            grouped[item.band].append(item)
        return {band: tuple(values) for band, values in grouped.items()}

    @property
    def is_empty(self) -> bool:
        return not self.visible_items()


def _parse_dates(raw: Any, where: str) -> tuple[NoticeDate, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise DigestContractError(f"{where}: dates는 배열이어야 합니다.")
    if len(raw) > MAX_COLLECTION_ENTRIES:
        raise DigestContractError(
            f"{where}: dates 항목이 너무 많습니다 "
            f"({len(raw)}>{MAX_COLLECTION_ENTRIES})"
        )
    parsed: list[NoticeDate] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise DigestContractError(f"{where}.dates[{index}]: 객체가 아닙니다.")
        entry_where = f"{where}.dates[{index}]"
        value = _parse_iso_date(entry.get("date"), f"{entry_where}.date")
        if value is None:
            continue
        parsed.append(
            NoticeDate(
                value=value,
                kind=_bounded_string(
                    entry.get("kind", "unknown"),
                    where=f"{entry_where}.kind",
                    max_length=64,
                    allow_empty=False,
                ),
                evidence=_bounded_string(
                    entry.get("evidence", ""),
                    where=f"{entry_where}.evidence",
                    max_length=_MAX_EVIDENCE,
                ),
                inferred_year=_optional_bool(
                    entry,
                    "inferred_year",
                    where=entry_where,
                    default=False,
                ),
            )
        )
    return tuple(parsed)


def _parse_health(raw: Any) -> CollectionHealth | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise DigestContractError("collection_health는 객체이거나 null이어야 합니다.")
    status = _bounded_string(
        raw.get("status", "healthy"),
        where="collection_health.status",
        max_length=16,
        allow_empty=False,
    )
    if status not in HEALTH_STATUSES:
        raise DigestContractError(
            f"collection_health.status 값이 올바르지 않습니다: {status!r}"
        )
    sources_raw = raw.get("sources", {})
    if not isinstance(sources_raw, dict):
        raise DigestContractError("collection_health.sources는 객체여야 합니다.")
    if len(sources_raw) > MAX_HEALTH_SOURCES:
        raise DigestContractError(
            "collection_health.sources 항목이 너무 많습니다 "
            f"({len(sources_raw)}>{MAX_HEALTH_SOURCES})"
        )
    sources: list[SourceHealth] = []
    for source_id, entry in sources_raw.items():
        source_id = _bounded_string(
            source_id,
            where="collection_health.sources source_id",
            max_length=_MAX_SOURCE_ID,
            allow_empty=False,
        )
        if not isinstance(entry, dict):
            raise DigestContractError(
                f"collection_health.sources[{source_id}]: 객체가 아닙니다."
            )
        entry_where = f"collection_health.sources[{source_id}]"
        source_status = _bounded_string(
            entry.get("status", "healthy"),
            where=f"{entry_where}.status",
            max_length=16,
            allow_empty=False,
        )
        if source_status not in HEALTH_STATUSES:
            raise DigestContractError(
                f"collection_health.sources[{source_id}].status 값이 올바르지 않습니다."
            )
        errors = _string_list(
            entry,
            "errors",
            where=entry_where,
            max_length=_MAX_LIST_TEXT,
        )
        sources.append(
            SourceHealth(
                source_id=source_id,
                status=source_status,
                list_candidates=_optional_int(
                    entry,
                    "list_candidates",
                    where=entry_where,
                    default=0,
                ),
                details_succeeded=_optional_int(
                    entry,
                    "details_succeeded",
                    where=entry_where,
                    default=0,
                ),
                details_failed=_optional_int(
                    entry,
                    "details_failed",
                    where=entry_where,
                    default=0,
                ),
                errors=errors,
            )
        )
    healthy = _optional_int(raw, "healthy", where="collection_health", default=0)
    degraded = _optional_int(raw, "degraded", where="collection_health", default=0)
    failed = _optional_int(raw, "failed", where="collection_health", default=0)
    expected_counts = {
        "healthy": sum(item.status == "healthy" for item in sources),
        "degraded": sum(item.status == "degraded" for item in sources),
        "failed": sum(item.status == "failed" for item in sources),
    }
    actual_counts = {
        "healthy": healthy,
        "degraded": degraded,
        "failed": failed,
    }
    if actual_counts != expected_counts:
        raise DigestContractError(
            "collection_health 요약 수치가 sources 상태와 일치하지 않습니다: "
            f"got={actual_counts!r}, expected={expected_counts!r}"
        )
    return CollectionHealth(
        status=status,
        healthy=healthy,
        degraded=degraded,
        failed=failed,
        may_include_stale_notices=_optional_bool(
            raw,
            "may_include_stale_notices",
            where="collection_health",
            default=False,
        ),
        sources=tuple(sorted(sources, key=lambda item: item.source_id)),
    )


def _parse_attachments(raw: Any, where: str) -> tuple[dict[str, Any], ...]:
    if raw is _MISSING:
        return ()
    if not isinstance(raw, list):
        raise DigestContractError(f"{where}: 배열이어야 합니다.")
    if len(raw) > MAX_COLLECTION_ENTRIES:
        raise DigestContractError(
            f"{where}: 항목이 너무 많습니다 ({len(raw)}>{MAX_COLLECTION_ENTRIES})"
        )
    parsed: list[dict[str, Any]] = []
    for index, entry in enumerate(raw):
        entry_where = f"{where}[{index}]"
        if not isinstance(entry, dict):
            raise DigestContractError(f"{entry_where}: 객체여야 합니다.")
        name = _bounded_string(
            entry.get("name", "첨부"),
            where=f"{entry_where}.name",
            max_length=_MAX_SHORT_TEXT,
            allow_empty=False,
        )
        url = _http_url(entry.get("url"), where=f"{entry_where}.url")
        normalized: dict[str, Any] = {"name": name, "url": url}
        if "kind" in entry:
            normalized["kind"] = _bounded_string(
                entry["kind"],
                where=f"{entry_where}.kind",
                max_length=64,
                allow_empty=False,
            )
        parsed.append(normalized)
    return tuple(parsed)


def _parse_duplicate_sources(raw: Any, where: str) -> tuple[dict[str, str], ...]:
    if raw is _MISSING:
        return ()
    if not isinstance(raw, list):
        raise DigestContractError(f"{where}: 배열이어야 합니다.")
    if len(raw) > MAX_COLLECTION_ENTRIES:
        raise DigestContractError(
            f"{where}: 항목이 너무 많습니다 ({len(raw)}>{MAX_COLLECTION_ENTRIES})"
        )
    parsed: list[dict[str, str]] = []
    for index, entry in enumerate(raw):
        entry_where = f"{where}[{index}]"
        if not isinstance(entry, dict):
            raise DigestContractError(f"{entry_where}: 객체여야 합니다.")
        parsed.append(
            {
                "source_id": _bounded_string(
                    entry.get("source_id"),
                    where=f"{entry_where}.source_id",
                    max_length=_MAX_SOURCE_ID,
                    allow_empty=False,
                ),
                "url": _http_url(entry.get("url"), where=f"{entry_where}.url"),
            }
        )
    return tuple(parsed)


def _parse_item(raw: Any, index: int) -> DigestItem:
    where = f"items[{index}]"
    if not isinstance(raw, dict):
        raise DigestContractError(f"{where}: 객체가 아닙니다.")

    notice = _require(raw, "notice", (dict,), where)
    candidate = _require(notice, "candidate", (dict,), f"{where}.notice")
    analysis = _require(raw, "analysis", (dict,), where)
    score = _require(raw, "score", (dict,), where)

    change = _bounded_string(
        raw.get("change", "unchanged"),
        where=f"{where}.change",
        max_length=16,
        allow_empty=False,
    )
    if change not in CHANGES:
        raise DigestContractError(f"{where}.change 값이 올바르지 않습니다: {change!r}")

    band = _bounded_string(
        _require(score, "band", (str,), f"{where}.score"),
        where=f"{where}.score.band",
        max_length=16,
        allow_empty=False,
    )
    if band not in BANDS:
        raise DigestContractError(f"{where}.score.band 값이 올바르지 않습니다: {band!r}")

    eligibility = _bounded_string(
        _require(score, "eligibility", (str,), f"{where}.score"),
        where=f"{where}.score.eligibility",
        max_length=32,
        allow_empty=False,
    )
    if eligibility not in ELIGIBILITIES:
        raise DigestContractError(
            f"{where}.score.eligibility 값이 올바르지 않습니다: {eligibility!r}"
        )

    urgency = _bounded_string(
        analysis.get("urgency", "normal"),
        where=f"{where}.analysis.urgency",
        max_length=16,
        allow_empty=False,
    )
    if urgency not in URGENCIES:
        raise DigestContractError(
            f"{where}.analysis.urgency 값이 올바르지 않습니다: {urgency!r}"
        )

    score_value = _number_value(
        _require(score, "score", (int, float), f"{where}.score"),
        where=f"{where}.score.score",
        minimum=0,
        maximum=100,
    )
    notice_id = _int_value(
        _require(raw, "notice_id", (int,), where),
        where=f"{where}.notice_id",
        minimum=1,
        maximum=2**63 - 1,
    )
    dedup_key = _bounded_string(
        raw.get("dedup_key"),
        where=f"{where}.dedup_key",
        max_length=_MAX_DEDUP_KEY,
        allow_empty=False,
    )
    revision_count = _optional_int(
        raw,
        "revision_count",
        where=where,
        default=1,
        minimum=1,
    )
    source_id = _bounded_string(
        _require(candidate, "source_id", (str,), f"{where}.notice.candidate"),
        where=f"{where}.notice.candidate.source_id",
        max_length=_MAX_SOURCE_ID,
        allow_empty=False,
    )
    external_id = _bounded_string(
        _require(candidate, "external_id", (str,), f"{where}.notice.candidate"),
        where=f"{where}.notice.candidate.external_id",
        max_length=_MAX_EXTERNAL_ID,
        allow_empty=False,
    )
    url = _http_url(
        _require(candidate, "url", (str,), f"{where}.notice.candidate"),
        where=f"{where}.notice.candidate.url",
    )
    title_value = notice.get("title", _MISSING)
    if title_value is _MISSING or title_value is None or title_value == "":
        title_value = candidate.get("title")
    title = _bounded_string(
        title_value,
        where=f"{where}.notice.title",
        max_length=_MAX_TITLE,
        allow_empty=False,
    )

    return DigestItem(
        notice_id=notice_id,
        dedup_key=dedup_key,
        change=change,
        revision_count=revision_count,
        source_id=source_id,
        external_id=external_id,
        url=url,
        title=title,
        university=_optional_str(
            candidate,
            "source_university",
            where=f"{where}.notice.candidate",
        ),
        board=_optional_str(
            candidate,
            "source_board",
            where=f"{where}.notice.candidate",
        ),
        author=_optional_str(notice, "author", where=f"{where}.notice"),
        published_text=_optional_str(
            notice,
            "published_text",
            where=f"{where}.notice",
        ),
        attachments=_parse_attachments(
            notice.get("attachments", _MISSING),
            f"{where}.notice.attachments",
        ),
        summary=_bounded_string(
            analysis.get("summary", ""),
            where=f"{where}.analysis.summary",
            max_length=_MAX_ITEM_TEXT,
        ),
        topics=_string_list(analysis, "topics", where=f"{where}.analysis"),
        actions=_string_list(analysis, "actions", where=f"{where}.analysis"),
        audiences=_string_list(analysis, "audiences", where=f"{where}.analysis"),
        required=_optional_bool(
            analysis,
            "required",
            where=f"{where}.analysis",
            default=False,
        ),
        urgency=urgency,
        dates=_parse_dates(analysis.get("dates"), f"{where}.analysis"),
        analysis_source=_bounded_string(
            analysis.get("analysis_source", "rules"),
            where=f"{where}.analysis.analysis_source",
            max_length=128,
            allow_empty=False,
        ),
        score=score_value,
        band=band,
        eligibility=eligibility,
        reasons=_string_list(score, "reasons", where=f"{where}.score"),
        deadline=_parse_iso_date(score.get("deadline"), f"{where}.score.deadline"),
        next_event=_parse_iso_date(score.get("next_event"), f"{where}.score.next_event"),
        mandatory_protected=_optional_bool(
            score,
            "mandatory_protected",
            where=f"{where}.score",
            default=False,
        ),
        duplicate_sources=_parse_duplicate_sources(
            raw.get("duplicate_sources", _MISSING),
            f"{where}.duplicate_sources",
        ),
        warnings=_string_list(notice, "warnings", where=f"{where}.notice"),
    )


def parse_digest(
    payload: Any,
    *,
    expected_schema_version: int = SUPPORTED_SCHEMA_VERSION,
    expected_user_key: str | None = None,
    expected_digest_date: date | None = None,
) -> Digest:
    """digest JSON 객체를 검증해 `Digest`로 변환합니다.

    Raises:
        DigestContractError: 스키마 버전이 다르거나 계약을 만족하지 않을 때.
    """
    try:
        if not isinstance(payload, dict):
            raise DigestContractError("digest 최상위 값은 JSON 객체여야 합니다.")
        _validate_payload_budget(payload)

        expected_version = _int_value(
            expected_schema_version,
            where="expected_schema_version",
            minimum=1,
            maximum=2**31 - 1,
        )
        schema_version = _int_value(
            _require(payload, "schema_version", (int,), "digest"),
            where="digest.schema_version",
            minimum=1,
            maximum=2**31 - 1,
        )
        if schema_version != expected_version:
            # 알 수 없는 스키마를 부분 해석하면 잘못된 마감/자격을 보여줄 수 있다.
            raise DigestContractError(
                "지원하지 않는 digest schema_version입니다: "
                f"got={schema_version!r}, expected={expected_version!r}"
            )

        user_key = _bounded_string(
            _require(payload, "user_key", (str,), "digest"),
            where="digest.user_key",
            max_length=_MAX_USER_KEY,
            allow_empty=False,
        )
        digest_date = _parse_iso_date(
            _require(payload, "date", (str,), "digest"),
            "digest.date",
        )
        if digest_date is None:
            raise DigestContractError("digest.date가 필요합니다.")

        items_raw = payload.get("items", [])
        if items_raw is None:
            items_raw = []
        if not isinstance(items_raw, list):
            raise DigestContractError("digest.items는 배열이어야 합니다.")
        if len(items_raw) > MAX_DIGEST_ITEMS:
            raise DigestContractError(
                f"digest.items 항목이 너무 많습니다 "
                f"({len(items_raw)}>{MAX_DIGEST_ITEMS})"
            )
        items = tuple(_parse_item(item, index) for index, item in enumerate(items_raw))
        notice_ids = [item.notice_id for item in items]
        if len(notice_ids) != len(set(notice_ids)):
            raise DigestContractError("digest.items에 중복 notice_id가 있습니다.")

        summary_raw = payload.get("summary", {})
        if not isinstance(summary_raw, dict):
            raise DigestContractError("digest.summary는 객체여야 합니다.")
        summary = {
            band: _optional_int(
                summary_raw,
                band,
                where="digest.summary",
                default=0,
                maximum=MAX_DIGEST_ITEMS,
            )
            for band in BAND_ORDER
        }

        digest = Digest(
            schema_version=schema_version,
            user_key=user_key,
            digest_date=digest_date,
            summary=summary,
            items=items,
            collection_health=_parse_health(payload.get("collection_health")),
        )
        return ensure_digest_identity(
            digest,
            expected_user_key=expected_user_key,
            expected_digest_date=expected_digest_date,
        )
    except DigestContractError:
        raise
    except (TypeError, ValueError, OverflowError) as exc:
        # 외부 입력의 숫자 변환 등은 호출자가 한 종류의 계약 오류로 처리할 수
        # 있어야 한다. 원래 예외는 체인에 남겨 운영 로그에서 원인을 확인한다.
        raise DigestContractError(
            f"digest 값을 해석할 수 없습니다: {type(exc).__name__}: {exc}"
        ) from exc


def ensure_digest_identity(
    digest: Digest,
    *,
    expected_user_key: str | None = None,
    expected_digest_date: date | None = None,
) -> Digest:
    """파일 경로가 아니라 digest 내부 소유자·날짜를 호출자 기대값에 결합."""
    if expected_user_key is not None:
        expected_key = _bounded_string(
            expected_user_key,
            where="expected_user_key",
            max_length=_MAX_USER_KEY,
            allow_empty=False,
        )
        if digest.user_key != expected_key:
            raise DigestContractError(
                "digest.user_key가 요청 사용자와 다릅니다: "
                f"got={digest.user_key!r}, expected={expected_key!r}"
            )
    if expected_digest_date is not None:
        if not isinstance(expected_digest_date, date):
            raise DigestContractError("expected_digest_date는 date여야 합니다.")
        if digest.digest_date != expected_digest_date:
            raise DigestContractError(
                "digest.date가 요청 날짜와 다릅니다: "
                f"got={digest.digest_date.isoformat()!r}, "
                f"expected={expected_digest_date.isoformat()!r}"
            )
    return digest


def load_digest(
    path: str | Path,
    *,
    expected_schema_version: int = SUPPORTED_SCHEMA_VERSION,
    expected_user_key: str | None = None,
    expected_digest_date: date | None = None,
) -> Digest:
    """digest JSON 파일을 읽어 검증합니다."""
    file_path = Path(path)
    try:
        declared_size = file_path.stat().st_size
        if declared_size > MAX_DIGEST_FILE_BYTES:
            raise DigestContractError(
                f"digest 파일이 너무 큽니다: {declared_size}>{MAX_DIGEST_FILE_BYTES}"
            )
        encoded = file_path.read_bytes()
    except OSError as exc:
        raise DigestContractError(f"digest 파일을 읽을 수 없습니다: {file_path}") from exc
    if len(encoded) > MAX_DIGEST_FILE_BYTES:
        raise DigestContractError(
            f"digest 파일이 너무 큽니다: {len(encoded)}>{MAX_DIGEST_FILE_BYTES}"
        )
    try:
        raw = encoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DigestContractError(
            f"digest 파일이 UTF-8이 아닙니다: {file_path}"
        ) from exc
    try:
        payload = json.loads(
            raw,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise DigestContractError(
            f"digest 파일이 유효한 JSON이 아닙니다: {file_path}"
        ) from exc
    return parse_digest(
        payload,
        expected_schema_version=expected_schema_version,
        expected_user_key=expected_user_key,
        expected_digest_date=expected_digest_date,
    )


def _reject_json_constant(value: str) -> None:
    raise DigestContractError(
        f"digest JSON에 허용되지 않는 숫자 상수가 있습니다: {value}"
    )


def digest_path_for(directory: str | Path, digest_date: date) -> Path:
    """코어가 저장하는 digest 파일 경로 규칙."""
    return Path(directory) / f"daily-digest-{digest_date.isoformat()}.json"

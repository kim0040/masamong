# -*- coding: utf-8 -*-
"""학교 공지 digest JSON을 검증해 타입이 있는 객체로 변환합니다.

digest는 마사몽 봇이 아니라 별도 batch 프로세스가 만든 파일이므로, 이 모듈은
파일 내용을 신뢰하지 않고 계약(docs/SCHOOL_NOTICE_INTEGRATION_PLAN.ko.md 12장)을
전부 확인한 뒤에만 통과시킵니다. 계약이 깨진 digest를 조용히 부분 렌더링하면
사용자에게 잘못된 마감이나 자격 판정을 보여줄 수 있어 전달 자체를 중단합니다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

SUPPORTED_SCHEMA_VERSION = 1

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


def _optional_str(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    return str(value)


def _parse_iso_date(value: Any, where: str) -> date | None:
    """`YYYY-MM-DD`만 허용합니다. 형식이 다르면 날짜를 추측하지 않습니다."""
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise DigestContractError(f"{where}: 날짜 형식이 올바르지 않습니다 {value!r}") from exc


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
    parsed: list[NoticeDate] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise DigestContractError(f"{where}.dates[{index}]: 객체가 아닙니다.")
        value = _parse_iso_date(entry.get("date"), f"{where}.dates[{index}]")
        if value is None:
            continue
        parsed.append(
            NoticeDate(
                value=value,
                kind=str(entry.get("kind") or "unknown"),
                evidence=str(entry.get("evidence") or ""),
                inferred_year=bool(entry.get("inferred_year", False)),
            )
        )
    return tuple(parsed)


def _parse_health(raw: Any) -> CollectionHealth | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise DigestContractError("collection_health는 객체이거나 null이어야 합니다.")
    status = str(raw.get("status") or "healthy")
    if status not in HEALTH_STATUSES:
        raise DigestContractError(f"collection_health.status 값이 올바르지 않습니다: {status!r}")
    sources_raw = raw.get("sources") or {}
    if not isinstance(sources_raw, dict):
        raise DigestContractError("collection_health.sources는 객체여야 합니다.")
    sources: list[SourceHealth] = []
    for source_id, entry in sources_raw.items():
        if not isinstance(entry, dict):
            raise DigestContractError(f"collection_health.sources[{source_id}]: 객체가 아닙니다.")
        source_status = str(entry.get("status") or "healthy")
        if source_status not in HEALTH_STATUSES:
            raise DigestContractError(
                f"collection_health.sources[{source_id}].status 값이 올바르지 않습니다."
            )
        errors = entry.get("errors") or []
        if not isinstance(errors, list):
            raise DigestContractError(
                f"collection_health.sources[{source_id}].errors는 배열이어야 합니다."
            )
        sources.append(
            SourceHealth(
                source_id=str(source_id),
                status=source_status,
                list_candidates=int(entry.get("list_candidates", 0) or 0),
                details_succeeded=int(entry.get("details_succeeded", 0) or 0),
                details_failed=int(entry.get("details_failed", 0) or 0),
                errors=tuple(str(item) for item in errors),
            )
        )
    return CollectionHealth(
        status=status,
        healthy=int(raw.get("healthy", 0) or 0),
        degraded=int(raw.get("degraded", 0) or 0),
        failed=int(raw.get("failed", 0) or 0),
        may_include_stale_notices=bool(raw.get("may_include_stale_notices", False)),
        sources=tuple(sorted(sources, key=lambda item: item.source_id)),
    )


def _parse_item(raw: Any, index: int) -> DigestItem:
    where = f"items[{index}]"
    if not isinstance(raw, dict):
        raise DigestContractError(f"{where}: 객체가 아닙니다.")

    notice = _require(raw, "notice", (dict,), where)
    candidate = _require(notice, "candidate", (dict,), f"{where}.notice")
    analysis = _require(raw, "analysis", (dict,), where)
    score = _require(raw, "score", (dict,), where)

    change = str(raw.get("change") or "unchanged")
    if change not in CHANGES:
        raise DigestContractError(f"{where}.change 값이 올바르지 않습니다: {change!r}")

    band = str(_require(score, "band", (str,), f"{where}.score"))
    if band not in BANDS:
        raise DigestContractError(f"{where}.score.band 값이 올바르지 않습니다: {band!r}")

    eligibility = str(_require(score, "eligibility", (str,), f"{where}.score"))
    if eligibility not in ELIGIBILITIES:
        raise DigestContractError(
            f"{where}.score.eligibility 값이 올바르지 않습니다: {eligibility!r}"
        )

    urgency = str(analysis.get("urgency") or "normal")
    if urgency not in URGENCIES:
        raise DigestContractError(f"{where}.analysis.urgency 값이 올바르지 않습니다: {urgency!r}")

    score_value = _require(score, "score", (int, float), f"{where}.score")
    if not 0 <= float(score_value) <= 100:
        raise DigestContractError(f"{where}.score.score는 0~100이어야 합니다: {score_value!r}")

    attachments_raw = notice.get("attachments") or []
    if not isinstance(attachments_raw, list):
        raise DigestContractError(f"{where}.notice.attachments는 배열이어야 합니다.")

    duplicates_raw = raw.get("duplicate_sources") or []
    if not isinstance(duplicates_raw, list):
        raise DigestContractError(f"{where}.duplicate_sources는 배열이어야 합니다.")

    def _string_tuple(container: dict[str, Any], key: str) -> tuple[str, ...]:
        values = container.get(key) or []
        if not isinstance(values, list):
            raise DigestContractError(f"{where}: {key}는 배열이어야 합니다.")
        return tuple(str(item) for item in values)

    return DigestItem(
        notice_id=int(_require(raw, "notice_id", (int,), where)),
        dedup_key=str(raw.get("dedup_key") or ""),
        change=change,
        revision_count=int(raw.get("revision_count", 1) or 1),
        source_id=str(_require(candidate, "source_id", (str,), f"{where}.notice.candidate")),
        external_id=str(
            _require(candidate, "external_id", (str,), f"{where}.notice.candidate")
        ),
        url=str(_require(candidate, "url", (str,), f"{where}.notice.candidate")),
        title=str(notice.get("title") or candidate.get("title") or ""),
        university=_optional_str(candidate, "source_university"),
        board=_optional_str(candidate, "source_board"),
        author=_optional_str(notice, "author"),
        published_text=_optional_str(notice, "published_text"),
        attachments=tuple(
            item for item in attachments_raw if isinstance(item, dict)
        ),
        summary=str(analysis.get("summary") or ""),
        topics=_string_tuple(analysis, "topics"),
        actions=_string_tuple(analysis, "actions"),
        audiences=_string_tuple(analysis, "audiences"),
        required=bool(analysis.get("required", False)),
        urgency=urgency,
        dates=_parse_dates(analysis.get("dates"), f"{where}.analysis"),
        analysis_source=str(analysis.get("analysis_source") or "rules"),
        score=float(score_value),
        band=band,
        eligibility=eligibility,
        reasons=_string_tuple(score, "reasons"),
        deadline=_parse_iso_date(score.get("deadline"), f"{where}.score.deadline"),
        next_event=_parse_iso_date(score.get("next_event"), f"{where}.score.next_event"),
        mandatory_protected=bool(score.get("mandatory_protected", False)),
        duplicate_sources=tuple(
            {"source_id": str(item.get("source_id") or ""), "url": str(item.get("url") or "")}
            for item in duplicates_raw
            if isinstance(item, dict)
        ),
        warnings=_string_tuple(notice, "warnings"),
    )


def parse_digest(
    payload: Any,
    *,
    expected_schema_version: int = SUPPORTED_SCHEMA_VERSION,
) -> Digest:
    """digest JSON 객체를 검증해 `Digest`로 변환합니다.

    Raises:
        DigestContractError: 스키마 버전이 다르거나 계약을 만족하지 않을 때.
    """
    if not isinstance(payload, dict):
        raise DigestContractError("digest 최상위 값은 JSON 객체여야 합니다.")

    schema_version = _require(payload, "schema_version", (int,), "digest")
    if int(schema_version) != int(expected_schema_version):
        # 알 수 없는 스키마를 부분 해석하면 잘못된 마감/자격을 보여줄 수 있다.
        raise DigestContractError(
            "지원하지 않는 digest schema_version입니다: "
            f"got={schema_version!r}, expected={expected_schema_version!r}"
        )

    user_key = str(_require(payload, "user_key", (str,), "digest")).strip()
    if not user_key:
        raise DigestContractError("digest.user_key가 비어 있습니다.")

    digest_date = _parse_iso_date(_require(payload, "date", (str,), "digest"), "digest.date")
    if digest_date is None:
        raise DigestContractError("digest.date가 필요합니다.")

    items_raw = payload.get("items")
    if items_raw is None:
        items_raw = []
    if not isinstance(items_raw, list):
        raise DigestContractError("digest.items는 배열이어야 합니다.")

    summary_raw = payload.get("summary") or {}
    if not isinstance(summary_raw, dict):
        raise DigestContractError("digest.summary는 객체여야 합니다.")
    summary = {band: int(summary_raw.get(band, 0) or 0) for band in BAND_ORDER}

    return Digest(
        schema_version=int(schema_version),
        user_key=user_key,
        digest_date=digest_date,
        summary=summary,
        items=tuple(_parse_item(item, index) for index, item in enumerate(items_raw)),
        collection_health=_parse_health(payload.get("collection_health")),
    )


def load_digest(
    path: str | Path,
    *,
    expected_schema_version: int = SUPPORTED_SCHEMA_VERSION,
) -> Digest:
    """digest JSON 파일을 읽어 검증합니다."""
    file_path = Path(path)
    try:
        raw = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DigestContractError(f"digest 파일을 읽을 수 없습니다: {file_path}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DigestContractError(f"digest 파일이 유효한 JSON이 아닙니다: {file_path}") from exc
    return parse_digest(payload, expected_schema_version=expected_schema_version)


def digest_path_for(directory: str | Path, digest_date: date) -> Path:
    """코어가 저장하는 digest 파일 경로 규칙."""
    return Path(directory) / f"daily-digest-{digest_date.isoformat()}.json"

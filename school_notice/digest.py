from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True)
class DigestResult:
    markdown: str
    payload: dict[str, Any]


def _collection_health(
    source_stats: dict[str, dict[str, Any]] | None,
) -> dict[str, Any] | None:
    if source_stats is None:
        return None
    sources: dict[str, dict[str, Any]] = {}
    for source_id, stats in source_stats.items():
        errors = [str(item) for item in stats.get("errors", [])]
        details_succeeded = int(stats.get("details_succeeded", 0))
        status = (
            "failed"
            if errors and details_succeeded == 0
            else "degraded"
            if errors
            else "healthy"
        )
        sources[source_id] = {
            "status": status,
            "list_candidates": int(stats.get("list_candidates", 0)),
            "details_succeeded": details_succeeded,
            "details_failed": int(stats.get("details_failed", 0)),
            "errors": errors,
        }
    failed = sum(item["status"] == "failed" for item in sources.values())
    degraded = sum(item["status"] == "degraded" for item in sources.values())
    status = "failed" if failed == len(sources) and sources else (
        "degraded" if failed or degraded else "healthy"
    )
    return {
        "status": status,
        "healthy": sum(item["status"] == "healthy" for item in sources.values()),
        "degraded": degraded,
        "failed": failed,
        "sources": sources,
        "may_include_stale_notices": bool(failed),
    }


def _item_title(item: dict[str, Any]) -> str:
    notice = item["notice"]
    return str(notice.get("title") or notice["candidate"]["title"])


def _deduplicate(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        groups.setdefault(str(item["dedup_key"]), []).append(item)
    results = []
    for grouped in groups.values():
        grouped.sort(
            key=lambda item: (
                item["score"]["score"],
                item["change"] in {"new", "updated"},
            ),
            reverse=True,
        )
        chosen = dict(grouped[0])
        chosen["duplicate_sources"] = [
            {
                "source_id": sibling["notice"]["candidate"]["source_id"],
                "url": sibling["notice"]["candidate"]["url"],
            }
            for sibling in grouped[1:]
        ]
        results.append(chosen)
    return results


def build_digest(
    *,
    user_key: str,
    digest_date: date,
    items: list[dict[str, Any]],
    profile: dict[str, Any] | None = None,
    source_stats: dict[str, dict[str, Any]] | None = None,
) -> DigestResult:
    preferences = (profile or {}).get("notification_preferences", {})
    minimum_score = float(preferences.get("minimum_score", 40))
    include_bands = set(
        preferences.get(
            "include_bands",
            ["action", "opportunity", "reference"],
        )
    )
    visible = [
        item
        for item in _deduplicate(items)
        if item["score"]["band"] != "hidden"
        and item["score"]["band"] in include_bands
        and float(item["score"]["score"]) >= minimum_score
    ]
    visible.sort(
        key=lambda item: (
            item["score"]["score"],
            item["change"] in {"new", "updated"},
        ),
        reverse=True,
    )
    buckets = {
        "action": [
            item for item in visible if item["score"]["band"] == "action"
        ][: int(preferences.get("max_action", 100))],
        "opportunity": [
            item for item in visible if item["score"]["band"] == "opportunity"
        ][: int(preferences.get("max_opportunity", 5))],
        "reference": [
            item for item in visible if item["score"]["band"] == "reference"
        ][: int(preferences.get("max_reference", 5))],
    }
    selected = [*buckets["action"], *buckets["opportunity"], *buckets["reference"]]
    collection_health = _collection_health(source_stats)
    payload = {
        "schema_version": 1,
        "user_key": user_key,
        "date": digest_date.isoformat(),
        "notification_preferences": {
            "minimum_score": minimum_score,
            "include_bands": sorted(include_bands),
            "max_action": int(preferences.get("max_action", 100)),
            "max_opportunity": int(preferences.get("max_opportunity", 5)),
            "max_reference": int(preferences.get("max_reference", 5)),
        },
        "summary": {
            "action": len(buckets["action"]),
            "opportunity": len(buckets["opportunity"]),
            "reference": len(buckets["reference"]),
        },
        "items": selected,
    }
    if collection_health is not None:
        payload["collection_health"] = collection_health

    lines = [
        f"# {digest_date.isoformat()} 맞춤 학교 공지",
        "",
        (
            f"해야 할 일 {len(buckets['action'])}건 · "
            f"도움 되는 기회 {len(buckets['opportunity'])}건 · "
            f"알아둘 공지 {len(buckets['reference'])}건"
        ),
    ]
    if collection_health and collection_health["status"] != "healthy":
        lines.extend(
            [
                "",
                (
                    "> ⚠️ 오늘 수집 상태가 완전하지 않습니다. "
                    f"저하 {collection_health['degraded']}개 · "
                    f"실패 {collection_health['failed']}개 source"
                ),
            ]
        )
        if collection_health["may_include_stale_notices"]:
            lines.append(
                "> 실패한 source의 이전 저장 공지가 포함될 수 있으므로 "
                "중요 일정은 원문에서 다시 확인하세요."
            )
        else:
            lines.append(
                "> 일부 상세 수집이 누락되었을 수 있으므로 중요한 일정은 "
                "원문에서 다시 확인하세요."
            )
    labels = {
        "action": "해야 할 일",
        "opportunity": "도움 되는 기회",
        "reference": "알아둘 공지",
    }
    for band in ("action", "opportunity", "reference"):
        lines.extend(["", f"## {labels[band]}", ""])
        if not buckets[band]:
            lines.append("- 해당 공지가 없습니다.")
            continue
        for item in buckets[band]:
            notice = item["notice"]
            candidate = notice["candidate"]
            analysis = item["analysis"]
            score = item["score"]
            change_label = {
                "new": "새 공지",
                "updated": "수정 공지",
                "unchanged": "기존 공지",
            }.get(item["change"], item["change"])
            lines.extend(
                [
                    f"### [{_item_title(item)}]({candidate['url']})",
                    "",
                    (
                        f"- 우선순위: {score['score']:.0f}점 · "
                        f"{change_label} · 자격 {score['eligibility']}"
                    ),
                    f"- 요약: {analysis['summary']}",
                    (
                        f"- 마감: {score['deadline']}"
                        if score["deadline"]
                        else (
                            f"- 다음 일정: {score.get('next_event')} "
                            "(마감 여부는 원문 확인)"
                            if score.get("next_event")
                            else "- 마감: 명시적 마감 미추출"
                        )
                    ),
                    (
                        "- 이유: "
                        + ("; ".join(score["reasons"]) or "기본 관련성")
                    ),
                    (
                        "- 해야 할 것: "
                        + (", ".join(analysis.get("actions", [])) or "별도 행동 없음")
                    ),
                ]
            )
            if item.get("duplicate_sources"):
                lines.append(
                    f"- 중복 게시판 {len(item['duplicate_sources'])}곳을 한 건으로 묶음"
                )
    lines.extend(
        [
            "",
            "## 주의",
            "",
            "자동 추출 결과이므로 신청 전 원문과 첨부파일을 최종 확인하세요. "
            "마감이 미기재되거나 추출 실패한 공지는 날짜를 임의로 만들지 않았습니다.",
            "",
        ]
    )
    return DigestResult(markdown="\n".join(lines), payload=payload)

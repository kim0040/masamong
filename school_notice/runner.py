from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .http import AsyncFetcher, FetchError
from .models import SourceConfig, SourceRun
from .parsing import parse_detail, parse_list


@dataclass(frozen=True)
class ValidationReport:
    generated_at: str
    request_count: int
    request_limit: int
    details_per_source: int
    source_runs: tuple[SourceRun, ...]
    robots_notes: dict[str, str]

    @property
    def summary(self) -> dict[str, int]:
        return {
            "healthy": sum(item.status == "healthy" for item in self.source_runs),
            "degraded": sum(item.status == "degraded" for item in self.source_runs),
            "failed": sum(item.status == "failed" for item in self.source_runs),
            "sources": len(self.source_runs),
            "notices": sum(len(item.notices) for item in self.source_runs),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "request_count": self.request_count,
            "request_limit": self.request_limit,
            "details_per_source": self.details_per_source,
            "summary": self.summary,
            "robots_notes": self.robots_notes,
            "sources": [item.as_dict() for item in self.source_runs],
        }


class UniversityNoticeValidator:
    def __init__(
        self,
        *,
        details_per_source: int = 2,
        max_requests: int = 30,
        respect_robots: bool = True,
    ) -> None:
        if details_per_source < 0 or details_per_source > 5:
            raise ValueError("details_per_source는 0~5 범위여야 합니다.")
        if not 1 <= max_requests <= 200:
            raise ValueError("max_requests는 1~200 범위여야 합니다.")
        self.details_per_source = details_per_source
        self.max_requests = max_requests
        self.respect_robots = respect_robots

    async def run(self, sources: list[SourceConfig]) -> ValidationReport:
        runs: list[SourceRun] = []
        async with AsyncFetcher(
            max_requests=self.max_requests,
            respect_robots=self.respect_robots,
        ) as fetcher:
            for source in sources:
                source_run = SourceRun(
                    source_id=source.source_id,
                    university=source.university,
                    board_name=source.board_name,
                    list_url=source.list_url,
                )
                runs.append(source_run)
                try:
                    listing = await fetcher.fetch_text(
                        source.list_url,
                        allowed_hosts=source.allowed_hosts,
                    )
                    source_run.list_elapsed_ms = listing.elapsed_ms
                    candidates, parser_warnings = parse_list(listing.text, source)
                    source_run.warnings.extend(parser_warnings)
                    source_run.discovered_count = len(candidates)
                    source_run.unique_count = len(
                        {candidate.external_id for candidate in candidates}
                    )
                except Exception as exc:
                    source_run.errors.append(
                        str(exc)
                        if isinstance(exc, FetchError)
                        else f"{type(exc).__name__}:{exc}"
                    )
                    source_run.health_checks = {
                        "list_fetch": False,
                        "minimum_items": False,
                        "stable_unique_ids": False,
                        "detail_success": False,
                        "body_quality": False,
                    }
                    source_run.status = "failed"
                    continue

                selected = candidates[: self.details_per_source]
                source_run.details_attempted = len(selected)
                for candidate in selected:
                    try:
                        detail = await fetcher.fetch_text(
                            candidate.url,
                            allowed_hosts=source.allowed_hosts,
                        )
                        notice = parse_detail(detail.text, source, candidate)
                        source_run.notices.append(notice)
                        source_run.details_succeeded += 1
                    except Exception as exc:
                        source_run.errors.append(
                            f"detail:{candidate.external_id}:"
                            + (
                                str(exc)
                                if isinstance(exc, FetchError)
                                else f"{type(exc).__name__}:{exc}"
                            )
                        )

                enough_items = (
                    source_run.discovered_count
                    >= source.validation.min_list_items
                )
                unique_ids = (
                    source_run.discovered_count > 0
                    and source_run.discovered_count == source_run.unique_count
                )
                detail_success = (
                    source_run.details_attempted == 0
                    or source_run.details_succeeded == source_run.details_attempted
                )
                body_quality = all(
                    len(notice.body_text)
                    >= source.validation.min_body_characters
                    for notice in source_run.notices
                )
                if source_run.details_attempted and not source_run.notices:
                    body_quality = False

                source_run.health_checks = {
                    "list_fetch": True,
                    "minimum_items": enough_items,
                    "stable_unique_ids": unique_ids,
                    "detail_success": detail_success,
                    "body_quality": body_quality,
                }
                source_run.status = (
                    "healthy"
                    if all(source_run.health_checks.values())
                    else "degraded"
                )

            return ValidationReport(
                generated_at=datetime.now(ZoneInfo("Asia/Seoul")).isoformat(
                    timespec="seconds"
                ),
                request_count=fetcher.request_count,
                request_limit=fetcher.max_requests,
                details_per_source=self.details_per_source,
                source_runs=tuple(runs),
                robots_notes=dict(fetcher.robots_notes),
            )


def write_report(report: ValidationReport, output_dir: str | Path) -> tuple[Path, Path]:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    json_path = destination / "live-extraction-report.json"
    markdown_path = destination / "live-extraction-report.md"
    json_path.write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def _sample(text: str, limit: int = 280) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def render_markdown(report: ValidationReport) -> str:
    lines = [
        "# 여러 대학 공개 공지 실수집 검증 보고서",
        "",
        f"- 생성 시각: {report.generated_at}",
        f"- HTTP 요청: {report.request_count}/{report.request_limit}",
        f"- 소스당 상세 확인: 최대 {report.details_per_source}건",
        (
            f"- 결과: 정상 {report.summary['healthy']}, "
            f"저하 {report.summary['degraded']}, 실패 {report.summary['failed']}"
        ),
        "",
        "## 요약",
        "",
        "| 대학 | 게시판 | 상태 | 목록 추출 | 상세 성공 |",
        "|---|---|---:|---:|---:|",
    ]
    for run in report.source_runs:
        lines.append(
            f"| {run.university} | {run.board_name} | {run.status} | "
            f"{run.discovered_count} | "
            f"{run.details_succeeded}/{run.details_attempted} |"
        )

    for run in report.source_runs:
        lines.extend(
            [
                "",
                f"## {run.university} · {run.board_name}",
                "",
                f"- 소스 ID: `{run.source_id}`",
                f"- 목록: {run.list_url}",
                f"- 상태: `{run.status}`",
                f"- 목록 응답: {run.list_elapsed_ms}ms"
                if run.list_elapsed_ms is not None
                else "- 목록 응답: 실패",
                "- 건강 검사: "
                + ", ".join(
                    f"`{key}={'PASS' if value else 'FAIL'}`"
                    for key, value in run.health_checks.items()
                ),
            ]
        )
        if run.warnings:
            lines.append("- 경고: " + ", ".join(f"`{item}`" for item in run.warnings))
        if run.errors:
            lines.append("- 오류: " + ", ".join(f"`{item}`" for item in run.errors))
        for notice in run.notices:
            signals = notice.signals
            lines.extend(
                [
                    "",
                    f"### [{notice.title}]({notice.candidate.url})",
                    "",
                    f"- 외부 ID: `{notice.candidate.external_id}`",
                    f"- 게시일: {notice.published_text or '미추출'}",
                    f"- 작성자: {notice.author or '미추출'}",
                    f"- 본문: {len(notice.body_text)}자",
                    (
                        f"- 파일: 첨부 {len(notice.attachments)}개, "
                        f"본문 이미지 {len(notice.inline_images)}개"
                    ),
                    f"- 내용 해시: `{notice.content_hash[:16]}`",
                    f"- 날짜 후보: {', '.join(signals.dates) or '없음'}",
                    f"- 행동 신호: {', '.join(signals.actions) or '없음'}",
                    f"- 대상 신호: {', '.join(signals.audiences) or '없음'}",
                    f"- 주제 신호: {', '.join(signals.topics) or '없음'}",
                    f"- 본문 미리보기: {_sample(notice.body_text)}",
                ]
            )
            if notice.warnings:
                lines.append(
                    "- 상세 경고: "
                    + ", ".join(f"`{item}`" for item in notice.warnings)
                )

    lines.extend(
        [
            "",
            "## 판정 범위",
            "",
            "이 보고서는 목록·상세 HTML, 본문 텍스트, 첨부·이미지 링크와 "
            "명시적 날짜·행동 키워드가 안정적으로 추출되는지 확인한다. "
            "첨부 전문과 개인화 판정은 `daily` 전체 작업에서 별도로 검증하며, "
            "이미지와 스캔 PDF OCR은 현재 `ocr_required` 상태로 보존한다.",
            "",
        ]
    )
    return "\n".join(lines)

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

from .analysis import AnalyzerService
from .attachments import enrich_notice_attachments
from .digest import DigestResult, build_digest
from .http import AsyncFetcher, FetchError
from .llm import DeepSeekClient
from .models import SourceConfig, notice_from_dict
from .parsing import parse_detail, parse_list
from .personalization import score_notice, validate_profile
from .storage import NoticeRepository


def select_detail_candidates(
    candidates,
    limit: int,
):
    """저사양 한도 안에서 최신 일반 공지를 놓치지 않게 고릅니다.

    대학 게시판은 오래된 고정 공지를 목록 맨 위에 여러 건 두는 경우가 많다.
    단순 앞 N건은 매일 같은 고정 공지만 읽고 실제 신규 공지를 누락할 수 있다.
    고정 공지는 최대 한 건만 포함하고 나머지 예산을 목록 순서상 최신 일반
    공지에 배정한다.
    """
    if limit <= 0:
        return []
    regular = [candidate for candidate in candidates if not candidate.pinned]
    pinned = [candidate for candidate in candidates if candidate.pinned]
    if not pinned:
        return list(candidates[:limit])
    if not regular:
        return pinned[:limit]
    if limit == 1:
        return regular[:1]
    return [pinned[0], *regular[: limit - 1]]


@dataclass(frozen=True)
class DailyRunResult:
    run_id: str
    run_date: str
    status: str
    profile_version: int
    source_stats: dict[str, dict[str, Any]]
    changes: dict[str, int]
    llm_calls: int
    http_requests: int
    digest_markdown_path: str
    digest_json_path: str
    digest: DigestResult

    def summary_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_date": self.run_date,
            "status": self.status,
            "profile_version": self.profile_version,
            "source_stats": self.source_stats,
            "changes": self.changes,
            "llm_calls": self.llm_calls,
            "http_requests": self.http_requests,
            "digest_markdown_path": self.digest_markdown_path,
            "digest_json_path": self.digest_json_path,
        }


class DailyNoticeJob:
    def __init__(
        self,
        *,
        repository: NoticeRepository,
        profile: dict[str, Any],
        sources: list[SourceConfig],
        run_date: date,
        output_dir: str | Path,
        use_llm: bool = True,
        max_details_per_source: int = 12,
        max_attachments_per_notice: int = 0,
        max_http_requests: int = 80,
        max_binary_bytes: int = 20_000_000,
        refresh_attachments: bool = False,
        respect_robots: bool = True,
        collect_sources: bool = True,
    ) -> None:
        if not 1 <= max_details_per_source <= 30:
            raise ValueError("max_details_per_source는 1~30이어야 합니다.")
        if not 0 <= max_attachments_per_notice <= 10:
            raise ValueError("max_attachments_per_notice는 0~10이어야 합니다.")
        if not 1 <= max_http_requests <= 500:
            raise ValueError("max_http_requests는 1~500이어야 합니다.")
        if not 1_000_000 <= max_binary_bytes <= 20_000_000:
            raise ValueError("max_binary_bytes는 1MB~20MB여야 합니다.")
        if not sources:
            raise ValueError("최소 한 개 source가 필요합니다.")
        validated_profile = validate_profile(profile)
        school_id = str(validated_profile["school_id"])
        mismatched = [source.source_id for source in sources if source.school_id != school_id]
        if mismatched:
            raise ValueError(
                "프로필 학교와 다른 source: " + ", ".join(mismatched)
        )
        self.repository = repository
        self.profile = validated_profile
        self.sources = sources
        self.run_date = run_date
        self.output_dir = Path(output_dir)
        self.use_llm = use_llm
        self.max_details_per_source = max_details_per_source
        self.max_attachments_per_notice = max_attachments_per_notice
        self.max_http_requests = max_http_requests
        self.max_binary_bytes = max_binary_bytes
        self.refresh_attachments = refresh_attachments
        self.respect_robots = respect_robots
        self.collect_sources = bool(collect_sources)

    async def run(self) -> DailyRunResult:
        run_id = self.repository.start_job("school_notice_daily", self.run_date)
        profile_version = self.repository.upsert_profile(self.profile)
        llm_client = DeepSeekClient(self.repository) if self.use_llm else None
        analyzer = AnalyzerService(self.repository, llm_client)
        changes_by_id: dict[int, str] = {}
        change_counts = {"new": 0, "updated": 0, "unchanged": 0}
        source_stats: dict[str, dict[str, Any]] = {}
        fatal_error: str | None = None
        http_requests = 0

        try:
            if self.collect_sources:
                async with AsyncFetcher(
                    max_requests=self.max_http_requests,
                    max_binary_bytes=self.max_binary_bytes,
                    min_request_interval_seconds=0.25,
                    respect_robots=self.respect_robots,
                ) as fetcher:
                    for source in self.sources:
                        stats: dict[str, Any] = {
                            "list_candidates": 0,
                            "selected": 0,
                            "details_succeeded": 0,
                            "details_failed": 0,
                            "parser_warnings": [],
                            "errors": [],
                        }
                        source_stats[source.source_id] = stats
                        try:
                            listing = await fetcher.fetch_text(
                                source.list_url,
                                allowed_hosts=source.allowed_hosts,
                            )
                            candidates, warnings = parse_list(listing.text, source)
                            stats["list_candidates"] = len(candidates)
                            stats["parser_warnings"] = warnings
                            if len(candidates) < source.validation.min_list_items:
                                stats["errors"].append(
                                    "minimum_list_contract_failed:"
                                    f"{len(candidates)}<{source.validation.min_list_items}"
                                )
                            selected = select_detail_candidates(
                                candidates,
                                self.max_details_per_source,
                            )
                            stats["selected"] = len(selected)
                        except Exception as exc:
                            stats["errors"].append(
                                str(exc)
                                if isinstance(exc, FetchError)
                                else f"{type(exc).__name__}:{exc}"
                            )
                            continue

                        for candidate in selected:
                            try:
                                detail = await fetcher.fetch_text(
                                    candidate.url,
                                    allowed_hosts=source.allowed_hosts,
                                )
                                notice = parse_detail(detail.text, source, candidate)
                                existing_payload = self.repository.existing_notice_payload(
                                    source.source_id,
                                    candidate.external_id,
                                )
                                if (
                                    existing_payload
                                    and not self.refresh_attachments
                                    and existing_payload.get("base_content_hash")
                                    == notice.base_content_hash
                                ):
                                    previous = notice_from_dict(existing_payload)
                                    notice = replace(
                                        notice,
                                        attachment_extractions=(
                                            previous.attachment_extractions
                                        ),
                                        content_hash=previous.content_hash,
                                    )
                                elif self.max_attachments_per_notice:
                                    notice = await enrich_notice_attachments(
                                        notice,
                                        source,
                                        fetcher,
                                        max_attachments=self.max_attachments_per_notice,
                                    )
                                persisted = self.repository.upsert_notice(
                                    source.school_id,
                                    notice,
                                )
                                changes_by_id[persisted.notice_id] = persisted.change
                                change_counts[persisted.change] += 1
                                stats["details_succeeded"] += 1
                            except Exception as exc:
                                stats["details_failed"] += 1
                                stats["errors"].append(
                                    f"{candidate.external_id}:"
                                    f"{type(exc).__name__}:{exc}"
                                )
                    http_requests = fetcher.request_count
            else:
                # 같은 학교의 첫 프로필이 이미 이 실행에서 상세 페이지를
                # 수집·저장했다. 공개 snapshot과 분석 캐시를 재사용하고 사용자별
                # 점수/digest만 계산해 동일 학교를 사람 수만큼 다시 크롤링하지 않는다.
                for source in self.sources:
                    stats: dict[str, Any] = {
                        "list_candidates": 0,
                        "selected": 0,
                        "details_succeeded": 0,
                        "details_failed": 0,
                        "parser_warnings": [],
                        "errors": [],
                        "reused_snapshot": True,
                    }
                    source_stats[source.source_id] = stats

            feedback_events = self.repository.feedback_events(
                str(self.profile["user_key"])
            )
            scored_items: list[dict[str, Any]] = []
            for stored in self.repository.current_notices(
                str(self.profile["school_id"])
            ):
                notice = notice_from_dict(stored.notice_payload)
                analysis = await analyzer.analyze(
                    notice_id=stored.notice_id,
                    notice=notice,
                    run_date=self.run_date,
                )
                score = score_notice(
                    profile=self.profile,
                    profile_version=profile_version,
                    notice_id=stored.notice_id,
                    notice_payload=stored.notice_payload,
                    analysis=analysis,
                    feedback_events=feedback_events,
                    today=self.run_date,
                )
                self.repository.save_score(
                    user_key=str(self.profile["user_key"]),
                    notice_id=stored.notice_id,
                    profile_version=profile_version,
                    score_date=self.run_date,
                    score=score,
                )
                scored_items.append(
                    {
                        "notice_id": stored.notice_id,
                        "dedup_key": stored.dedup_key,
                        "revision_count": stored.revision_count,
                        "change": changes_by_id.get(stored.notice_id, "unchanged"),
                        "notice": stored.notice_payload,
                        "analysis": analysis,
                        "score": score,
                    }
                )

            digest = build_digest(
                user_key=str(self.profile["user_key"]),
                digest_date=self.run_date,
                items=scored_items,
                profile=self.profile,
                source_stats=source_stats,
            )
            self.repository.save_digest(
                user_key=str(self.profile["user_key"]),
                digest_date=self.run_date,
                markdown=digest.markdown,
                payload=digest.payload,
            )
            self.output_dir.mkdir(parents=True, exist_ok=True)
            markdown_path = (
                self.output_dir / f"daily-digest-{self.run_date.isoformat()}.md"
            )
            json_path = (
                self.output_dir / f"daily-digest-{self.run_date.isoformat()}.json"
            )
            markdown_path.write_text(digest.markdown, encoding="utf-8")
            json_path.write_text(
                json.dumps(digest.payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            failed_sources = sum(
                bool(stats["errors"]) and not stats["details_succeeded"]
                for stats in source_stats.values()
            )
            status = (
                "failed"
                if failed_sources == len(source_stats)
                else "partial"
                if any(stats["errors"] for stats in source_stats.values())
                else "succeeded"
            )
            result = DailyRunResult(
                run_id=run_id,
                run_date=self.run_date.isoformat(),
                status=status,
                profile_version=profile_version,
                source_stats=source_stats,
                changes=change_counts,
                llm_calls=llm_client.run_calls if llm_client else 0,
                http_requests=http_requests,
                digest_markdown_path=str(markdown_path.resolve()),
                digest_json_path=str(json_path.resolve()),
                digest=digest,
            )
            if llm_client is not None:
                await llm_client.close()
            self.repository.finish_job(
                run_id,
                status=status,
                stats=result.summary_dict(),
            )
            run_report = self.output_dir / f"daily-run-{self.run_date.isoformat()}.json"
            run_report.write_text(
                json.dumps(result.summary_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return result
        except Exception as exc:
            if llm_client is not None:
                await llm_client.close()
            fatal_error = f"{type(exc).__name__}:{exc}"
            self.repository.finish_job(
                run_id,
                status="failed",
                stats={
                    "source_stats": source_stats,
                    "changes": change_counts,
                    "http_requests": http_requests,
                },
                error=fatal_error,
            )
            raise

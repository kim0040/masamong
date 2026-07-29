from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .daily import DailyNoticeJob
from .llm_check import run_deepseek_check
from .personalization import ALLOWED_FEEDBACK, FeedbackService, load_profile
from .runner import UniversityNoticeValidator, write_report
from .sources import load_sources
from .storage import NoticeRepository


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="school-notice",
        description=(
            "공개 대학 공지를 유한 수집하고 분석·개인화한 일일 digest를 만듭니다."
        ),
    )
    parser.add_argument(
        "--source-config",
        type=Path,
        help="기본 내장 설정 대신 사용할 sources.json",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list-sources", help="사용 가능한 소스 목록")

    check = subparsers.add_parser("live-check", help="공개 사이트 실수집 검증")
    check.add_argument(
        "--source",
        action="append",
        default=[],
        help="검증할 source id. 여러 번 지정 가능하며 생략하면 전체",
    )
    check.add_argument(
        "--details-per-source",
        type=int,
        default=2,
        choices=range(0, 6),
        metavar="0..5",
    )
    check.add_argument("--max-requests", type=int, default=30)
    check.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/live"),
    )
    check.add_argument(
        "--ignore-robots",
        action="store_true",
        help="기본 robots.txt 확인을 끕니다. 공개 검증에서는 권장하지 않습니다.",
    )

    daily = subparsers.add_parser(
        "daily",
        help="증분 수집·분석·개인화·digest 전체 작업",
    )
    daily.add_argument(
        "--profile",
        type=Path,
        default=Path("profiles/jbnu_software_transfer_grade3.json"),
    )
    daily.add_argument("--db", type=Path, default=Path("data/school_notice.db"))
    daily.add_argument(
        "--source",
        action="append",
        default=[],
        help="생략하면 프로필 학교의 전체 source",
    )
    daily.add_argument("--date", help="YYYY-MM-DD, 기본 Asia/Seoul 오늘")
    daily.add_argument("--no-llm", action="store_true")
    daily.add_argument("--max-details-per-source", type=int, default=12)
    daily.add_argument("--max-attachments-per-notice", type=int, default=0)
    daily.add_argument("--max-requests", type=int, default=80)
    daily.add_argument(
        "--low-resource",
        action="store_true",
        help=(
            "저사양 모드: source당 상세 4건, 첨부 다운로드 없음, "
            "전체 HTTP 30회로 상한을 낮춥니다."
        ),
    )
    daily.add_argument("--refresh-attachments", action="store_true")
    daily.add_argument(
        "--reuse-current-snapshot",
        action="store_true",
        help=(
            "같은 batch에서 이미 수집한 학교 snapshot을 재사용하고 "
            "사용자별 분석·점수·digest만 계산합니다."
        ),
    )
    daily.add_argument("--ignore-robots", action="store_true")
    daily.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/daily"),
    )

    feedback = subparsers.add_parser(
        "feedback",
        help="관심·자격·완료 피드백 기록",
    )
    feedback.add_argument(
        "--profile",
        type=Path,
        default=Path("profiles/jbnu_software_transfer_grade3.json"),
    )
    feedback.add_argument("--db", type=Path, default=Path("data/school_notice.db"))
    feedback.add_argument("--type", required=True, choices=sorted(ALLOWED_FEEDBACK))
    feedback.add_argument("--notice-id", type=int)
    feedback.add_argument("--source-id")
    feedback.add_argument("--external-id")
    feedback.add_argument("--topic")
    feedback.add_argument("--reason")

    init_db = subparsers.add_parser("init-db", help="SQLite 스키마 초기화")
    init_db.add_argument("--db", type=Path, default=Path("data/school_notice.db"))

    llm_check = subparsers.add_parser(
        "llm-check",
        help="DeepSeek 실제 1회 분석·캐시·개인화 계약 검증",
    )
    llm_check.add_argument(
        "--db",
        type=Path,
        default=Path("artifacts/llm-check/llm-check.db"),
    )
    llm_check.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/llm-check"),
    )
    return parser


async def _live_check(args: argparse.Namespace) -> int:
    sources_by_id = load_sources(args.source_config)
    unknown = sorted(set(args.source) - set(sources_by_id))
    if unknown:
        raise SystemExit(f"알 수 없는 source id: {', '.join(unknown)}")
    selected = (
        [sources_by_id[source_id] for source_id in args.source]
        if args.source
        else list(sources_by_id.values())
    )
    validator = UniversityNoticeValidator(
        details_per_source=args.details_per_source,
        max_requests=args.max_requests,
        respect_robots=not args.ignore_robots,
    )
    report = await validator.run(selected)
    json_path, markdown_path = write_report(report, args.output_dir)
    print(
        f"sources={report.summary['sources']} "
        f"healthy={report.summary['healthy']} "
        f"degraded={report.summary['degraded']} "
        f"failed={report.summary['failed']} "
        f"requests={report.request_count}/{report.request_limit}"
    )
    print(json_path.resolve())
    print(markdown_path.resolve())
    return 2 if report.summary["failed"] else 0


def _run_date(value: str | None) -> date:
    if value:
        return date.fromisoformat(value)
    return datetime.now(ZoneInfo("Asia/Seoul")).date()


def _daily_resource_limits(
    args: argparse.Namespace,
) -> tuple[int, int, int, int]:
    if args.low_resource:
        return (
            min(args.max_details_per_source, 4),
            0,
            min(args.max_requests, 30),
            8_000_000,
        )
    return (
        args.max_details_per_source,
        args.max_attachments_per_notice,
        args.max_requests,
        20_000_000,
    )


async def _daily(args: argparse.Namespace) -> int:
    profile = load_profile(args.profile)
    sources_by_id = load_sources(args.source_config)
    unknown = sorted(set(args.source) - set(sources_by_id))
    if unknown:
        raise SystemExit(f"알 수 없는 source id: {', '.join(unknown)}")
    if args.source:
        sources = [sources_by_id[item] for item in args.source]
    else:
        sources = [
            source
            for source in sources_by_id.values()
            if source.school_id == profile["school_id"]
        ]
    with NoticeRepository(args.db) as repository:
        (
            max_details,
            max_attachments,
            max_requests,
            max_binary_bytes,
        ) = _daily_resource_limits(args)
        job = DailyNoticeJob(
            repository=repository,
            profile=profile,
            sources=sources,
            run_date=_run_date(args.date),
            output_dir=args.output_dir,
            use_llm=not args.no_llm,
            max_details_per_source=max_details,
            max_attachments_per_notice=max_attachments,
            max_http_requests=max_requests,
            max_binary_bytes=max_binary_bytes,
            refresh_attachments=args.refresh_attachments,
            respect_robots=not args.ignore_robots,
            collect_sources=not args.reuse_current_snapshot,
        )
        result = await job.run()
    print(json.dumps(result.summary_dict(), ensure_ascii=False, indent=2))
    return 2 if result.status == "failed" else 0


def _feedback(args: argparse.Namespace) -> int:
    profile = load_profile(args.profile)
    with NoticeRepository(args.db) as repository:
        repository.upsert_profile(profile)
        notice_id = args.notice_id
        if notice_id is None and (args.source_id or args.external_id):
            if not (args.source_id and args.external_id):
                raise SystemExit(
                    "--source-id와 --external-id는 함께 지정해야 합니다."
                )
            notice_id = repository.resolve_notice_id(
                args.source_id,
                args.external_id,
            )
            if notice_id is None:
                raise SystemExit("해당 공지를 DB에서 찾을 수 없습니다.")
        service = FeedbackService(repository)
        try:
            event_ids = service.record(
                user_key=str(profile["user_key"]),
                feedback_type=args.type,
                notice_id=notice_id,
                topic=args.topic,
                reason=args.reason,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            {
                "recorded": len(event_ids),
                "event_ids": event_ids,
                "feedback_type": args.type,
                "notice_id": notice_id,
                "topic": args.topic,
            },
            ensure_ascii=False,
        )
    )
    return 0


async def _llm_check(args: argparse.Namespace) -> int:
    try:
        report, report_path = await run_deepseek_check(
            database=args.db,
            output_dir=args.output_dir,
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    print(
        json.dumps(
            {
                "status": report["status"],
                "model": report["model"],
                "checks": report["checks"],
                "api_usage": report["api_usage"],
                "report": str(report_path.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["status"] == "passed" else 2


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    sources = load_sources(args.source_config)
    if args.command == "list-sources":
        for source in sources.values():
            print(
                f"{source.source_id}\t{source.university}\t"
                f"{source.board_name}\t{source.adapter}"
            )
        return 0
    if args.command == "live-check":
        return asyncio.run(_live_check(args))
    if args.command == "daily":
        return asyncio.run(_daily(args))
    if args.command == "feedback":
        return _feedback(args)
    if args.command == "llm-check":
        return asyncio.run(_llm_check(args))
    if args.command == "init-db":
        with NoticeRepository(args.db):
            pass
        print(args.db.resolve())
        return 0
    parser.error("unknown command")
    return 2

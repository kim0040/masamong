#!/usr/bin/env python3
"""20개교 편입 공지 일일 수집기를 중복 실행 없이 한 번 수행한다."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from transfer_notice.collector import TransferNoticeCollector


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-config",
        default=str(PROJECT_ROOT / "transfer_notice" / "sources.json"),
    )
    parser.add_argument("--database", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--lock-file", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument("--request-timeout-seconds", type=float, default=20.0)
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument("--max-details-per-source", type=int, default=3)
    parser.add_argument("--min-request-interval-seconds", type=float, default=0.35)
    return parser


async def _run(args: argparse.Namespace) -> dict:
    collector = TransferNoticeCollector(
        source_config=args.source_config,
        database_path=args.database,
        output_dir=args.output_dir,
        request_timeout_seconds=args.request_timeout_seconds,
        max_retries=args.max_retries,
        max_details_per_source=args.max_details_per_source,
        min_request_interval_seconds=args.min_request_interval_seconds,
    )
    return await asyncio.wait_for(
        collector.run(),
        timeout=max(60.0, min(1800.0, float(args.timeout_seconds))),
    )


def main() -> int:
    args = _parser().parse_args()
    lock_path = Path(args.lock_file)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("transfer notice batch is already running", file=sys.stderr)
            return 75
        try:
            payload = asyncio.run(_run(args))
        except TimeoutError:
            print("transfer notice batch timed out", file=sys.stderr)
            return 124
        print(
            json.dumps(
                {
                    "run_id": payload["run_id"],
                    "status": payload["status"],
                    "healthy": payload["healthy_count"],
                    "sources": payload["source_count"],
                    "changes": len(payload["changes"]),
                    "requests": payload["http_requests"],
                },
                ensure_ascii=False,
            )
        )
        return 0 if payload["healthy_count"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

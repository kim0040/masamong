#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""학교 공지 batch를 마사몽 DB와 연결해 실행합니다.

봇 프로세스가 아니라 systemd timer/cron이 이 스크립트를 호출합니다. 크롤링과
분석은 코어(`school_notice`)가 별도 프로세스로 수행하므로 봇의 상주 CPU/RSS
예산에 영향을 주지 않습니다.

흐름:
    마사몽 DB에서 활성 프로필 export
      → 사용자별로 코어 CLI 실행 (사용자별 output 디렉터리로 분리)
      → 실행 상태를 school_notice_batch_runs에 기록
      → 미반영 피드백을 코어로 전달

`--core-python`과 `--core-cwd`로 코어 위치를 지정합니다. 코어를 이 저장소에
vendoring하지 않으므로 배포 환경에 맞게 넘겨야 합니다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from database.compat_db import TiDBSettings, connect_main_db  # noqa: E402
from utils.school_notice_contract import (  # noqa: E402
    DigestContractError,
    digest_path_for,
    load_digest,
)

# 코어 CLI가 실패를 알리는 방식. partial은 exit 0이므로 종료 코드만 믿으면 안 된다.
_CORE_FAILED_EXIT = 2


def parse_args() -> argparse.Namespace:
    """CLI 인자를 파싱합니다."""
    parser = argparse.ArgumentParser(description="학교 공지 batch 실행")
    parser.add_argument("--core-python", required=True, help="코어 venv의 python 경로")
    parser.add_argument("--core-cwd", required=True, help="코어 저장소 루트")
    parser.add_argument("--date", help="실행 기준일 (YYYY-MM-DD). 생략 시 오늘")
    parser.add_argument("--no-llm", action="store_true", default=True)
    parser.add_argument("--use-llm", dest="no_llm", action="store_false")
    parser.add_argument("--low-resource", action="store_true", default=True)
    parser.add_argument("--max-details-per-source", type=int)
    parser.add_argument("--max-requests", type=int)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="코어를 실행하지 않고 대상 프로필만 출력",
    )
    return parser.parse_args()


async def open_db():
    """마사몽 운영 DB에 연결합니다."""
    tidb_settings = None
    if config.DB_BACKEND == "tidb":
        tidb_settings = TiDBSettings(
            host=config.TIDB_HOST or "",
            port=config.TIDB_PORT,
            user=config.TIDB_USER or "",
            password=config.TIDB_PASSWORD or "",
            database=config.TIDB_NAME,
            ssl_ca=config.TIDB_SSL_CA,
            ssl_verify_identity=config.TIDB_SSL_VERIFY_IDENTITY,
            require_tls=config.REQUIRE_DB_TLS,
        )
    return await connect_main_db(
        config.DB_BACKEND,
        sqlite_path=config.DATABASE_FILE,
        tidb_settings=tidb_settings,
    )


async def load_profiles(db) -> list[dict]:
    """전달 대상 프로필을 읽어옵니다."""
    async with db.execute(
        "SELECT user_key, school_id, profile_json FROM school_notice_profiles WHERE enabled = 1"
    ) as cursor:
        rows = await cursor.fetchall()
    profiles = []
    for row in rows:
        try:
            payload = json.loads(row[2])
        except (TypeError, json.JSONDecodeError):
            print(f"경고: 프로필 JSON을 읽을 수 없습니다: {row[0]}", file=sys.stderr)
            continue
        payload["user_key"] = str(row[0])
        profiles.append(payload)
    return profiles


async def pending_feedback(db) -> list[dict]:
    """아직 코어에 반영하지 않은 피드백."""
    async with db.execute(
        """
        SELECT id, user_key, source_id, external_id, feedback_type, topic
        FROM school_notice_feedback
        WHERE consumed_at IS NULL
        ORDER BY id
        """
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        {
            "id": int(row[0]),
            "user_key": str(row[1]),
            "source_id": str(row[2] or ""),
            "external_id": str(row[3] or ""),
            "feedback_type": str(row[4]),
            "topic": row[5],
        }
        for row in rows
    ]


async def mark_feedback_consumed(db, ids: list[int]) -> None:
    if not ids:
        return
    now = datetime.now().isoformat(timespec="seconds")
    for feedback_id in ids:
        await db.execute(
            "UPDATE school_notice_feedback SET consumed_at = ? WHERE id = ?",
            (now, feedback_id),
        )
    await db.commit()


async def record_run(db, *, user_key: str, run_date: date, summary: dict) -> None:
    """실행 결과를 기록합니다. 같은 날 재실행이면 갱신합니다."""
    if config.DB_BACKEND == "tidb":
        query = """
            INSERT INTO school_notice_batch_runs
                (user_key, run_date, status, collection_status, may_include_stale,
                 item_count, http_requests, llm_calls, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON DUPLICATE KEY UPDATE
                status = VALUES(status),
                collection_status = VALUES(collection_status),
                may_include_stale = VALUES(may_include_stale),
                item_count = VALUES(item_count),
                http_requests = VALUES(http_requests),
                llm_calls = VALUES(llm_calls),
                finished_at = VALUES(finished_at)
        """
    else:
        query = """
            INSERT INTO school_notice_batch_runs
                (user_key, run_date, status, collection_status, may_include_stale,
                 item_count, http_requests, llm_calls, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_key, run_date) DO UPDATE SET
                status = excluded.status,
                collection_status = excluded.collection_status,
                may_include_stale = excluded.may_include_stale,
                item_count = excluded.item_count,
                http_requests = excluded.http_requests,
                llm_calls = excluded.llm_calls,
                finished_at = excluded.finished_at
        """
    await db.execute(
        query,
        (
            user_key,
            run_date.isoformat(),
            summary["status"],
            summary.get("collection_status"),
            1 if summary.get("may_include_stale") else 0,
            int(summary.get("item_count", 0)),
            summary.get("http_requests"),
            summary.get("llm_calls"),
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    await db.commit()


def build_core_command(args: argparse.Namespace, profile_path: Path, output_dir: Path) -> list[str]:
    """코어 CLI 명령을 만듭니다."""
    command = [
        args.core_python,
        "-m",
        "school_notice",
        "daily",
        "--profile",
        str(profile_path),
        "--db",
        str(Path(config.SCHOOL_NOTICE_CORE_DB).expanduser()),
        "--output-dir",
        str(output_dir),
    ]
    if args.no_llm:
        command.append("--no-llm")
    if args.low_resource:
        command.append("--low-resource")
    if args.date:
        command.extend(["--date", args.date])
    if args.max_details_per_source:
        command.extend(["--max-details-per-source", str(args.max_details_per_source)])
    if args.max_requests:
        command.extend(["--max-requests", str(args.max_requests)])
    return command


def summarize_run(output_dir: Path, run_date: date, returncode: int) -> dict:
    """digest와 실행 보고서에서 실제 상태를 읽습니다.

    코어는 `partial`을 exit 0으로 반환하므로 종료 코드만으로 성공을 판정하지
    않고 보고서와 collection_health를 함께 확인합니다.
    """
    summary: dict = {
        "status": "failed" if returncode == _CORE_FAILED_EXIT else "succeeded",
        "collection_status": None,
        "may_include_stale": False,
        "item_count": 0,
        "http_requests": None,
        "llm_calls": None,
    }

    run_report = output_dir / f"daily-run-{run_date.isoformat()}.json"
    if run_report.is_file():
        try:
            payload = json.loads(run_report.read_text(encoding="utf-8"))
            summary["status"] = str(payload.get("status") or summary["status"])
            summary["http_requests"] = payload.get("http_requests")
            summary["llm_calls"] = payload.get("llm_calls")
        except (OSError, json.JSONDecodeError):
            summary["status"] = "failed"

    try:
        digest = load_digest(
            digest_path_for(output_dir, run_date),
            expected_schema_version=config.SCHOOL_NOTICE_SCHEMA_VERSION,
        )
    except DigestContractError:
        # digest를 읽지 못하면 봇이 전달할 수 없으므로 성공으로 볼 수 없다.
        summary["status"] = "failed"
        return summary

    summary["item_count"] = len(digest.visible_items())
    if digest.collection_health is not None:
        summary["collection_status"] = digest.collection_health.status
        summary["may_include_stale"] = digest.collection_health.may_include_stale_notices
    return summary


async def main() -> int:
    """batch를 실행하고 종료 코드를 반환합니다."""
    args = parse_args()
    if not config.SCHOOL_NOTICE_ENABLED:
        print("SCHOOL_NOTICE_ENABLED=false 이므로 실행하지 않습니다.", file=sys.stderr)
        return 1

    run_date = date.fromisoformat(args.date) if args.date else date.today()
    digest_root = Path(config.SCHOOL_NOTICE_DIGEST_DIR).expanduser()
    work_root = digest_root / ".profiles"
    work_root.mkdir(parents=True, exist_ok=True)

    db = await open_db()
    try:
        profiles = await load_profiles(db)
        if not profiles:
            print("활성 프로필이 없습니다.")
            return 0

        feedback = await pending_feedback(db)
        print(f"대상 프로필 {len(profiles)}명, 미반영 피드백 {len(feedback)}건")
        if args.dry_run:
            for profile in profiles:
                print(f"  - {profile['user_key']} ({profile.get('school_id')})")
            return 0

        exit_code = 0
        for profile in profiles:
            user_key = profile["user_key"]
            # 코어는 파일명에 user_key를 넣지 않으므로 디렉터리로 분리해야
            # 사용자끼리 digest를 덮어쓰지 않는다.
            output_dir = digest_root / user_key
            output_dir.mkdir(parents=True, exist_ok=True)
            profile_path = work_root / f"{user_key}.json"
            profile_path.write_text(
                json.dumps(profile, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            command = build_core_command(args, profile_path, output_dir)
            completed = subprocess.run(  # noqa: S603 - 인자를 직접 구성한 고정 명령
                command,
                cwd=args.core_cwd,
                capture_output=True,
                text=True,
                timeout=1800,
            )
            summary = summarize_run(output_dir, run_date, completed.returncode)
            await record_run(db, user_key=user_key, run_date=run_date, summary=summary)

            print(
                f"  {user_key}: status={summary['status']} "
                f"collection={summary['collection_status']} "
                f"items={summary['item_count']} "
                f"stale={summary['may_include_stale']}"
            )
            if summary["status"] == "failed":
                exit_code = 2
                if completed.stderr:
                    print(completed.stderr[-2000:], file=sys.stderr)

        consumed = [entry["id"] for entry in feedback]
        await mark_feedback_consumed(db, consumed)
        return exit_code
    finally:
        await db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

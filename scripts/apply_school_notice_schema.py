#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""학교 공지용 테이블 5개만 additive 방식으로 생성하는 one-shot 도구."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import re
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from database.compat_db import get_table_columns  # noqa: E402
from scripts.apply_privacy_consent_schema import (  # noqa: E402
    configured_target_identity,
    open_configured_db,
    validate_target,
    verify_connected_target,
)


SCHOOL_NOTICE_TABLE_COLUMNS = {
    "school_notice_profiles": {
        "user_id", "user_key", "school_id", "profile_json", "profile_version",
        "enabled", "delivery_time", "created_at", "updated_at",
    },
    "school_notice_feedback": {
        "id", "user_key", "source_id", "external_id", "feedback_type", "topic",
        "interaction_id", "created_at", "consumed_at",
    },
    "school_notice_deliveries": {
        "id", "user_key", "digest_date", "notice_id", "revision_count", "status",
        "failure_reason", "attempt_count", "delivered_at",
    },
    "school_notice_batch_runs": {
        "id", "user_key", "run_date", "profile_version", "profile_hash", "status",
        "collection_status", "may_include_stale", "item_count", "http_requests",
        "llm_calls", "finished_at",
    },
    "school_notice_delivery_runs": {
        "user_key", "digest_date", "status", "attempt_count", "next_attempt_at",
        "last_error", "finished_at", "updated_at",
    },
}

_SQLITE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS school_notice_profiles (
        user_id INTEGER PRIMARY KEY,
        user_key TEXT NOT NULL UNIQUE,
        school_id TEXT NOT NULL,
        profile_json TEXT NOT NULL,
        profile_version INTEGER NOT NULL DEFAULT 1,
        enabled INTEGER NOT NULL DEFAULT 1,
        delivery_time TEXT NOT NULL DEFAULT '09:00',
        created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc')),
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS school_notice_feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_key TEXT NOT NULL,
        source_id TEXT NOT NULL,
        external_id TEXT NOT NULL,
        feedback_type TEXT NOT NULL,
        topic TEXT,
        interaction_id TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        consumed_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS school_notice_deliveries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_key TEXT NOT NULL,
        digest_date TEXT NOT NULL,
        notice_id INTEGER NOT NULL,
        revision_count INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL,
        failure_reason TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 1,
        delivered_at TEXT NOT NULL,
        UNIQUE(user_key, notice_id, revision_count)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS school_notice_batch_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_key TEXT NOT NULL,
        run_date TEXT NOT NULL,
        profile_version INTEGER NOT NULL,
        profile_hash TEXT NOT NULL,
        status TEXT NOT NULL,
        collection_status TEXT,
        may_include_stale INTEGER NOT NULL DEFAULT 0,
        item_count INTEGER NOT NULL DEFAULT 0,
        http_requests INTEGER,
        llm_calls INTEGER,
        finished_at TEXT NOT NULL,
        UNIQUE(user_key, run_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS school_notice_delivery_runs (
        user_key TEXT NOT NULL,
        digest_date TEXT NOT NULL,
        status TEXT NOT NULL,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT,
        last_error TEXT,
        finished_at TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(user_key, digest_date)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_school_notice_feedback_pending
    ON school_notice_feedback (user_key, consumed_at, created_at)
    """,
)

_TIDB_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS school_notice_profiles (
        user_id BIGINT PRIMARY KEY,
        user_key VARCHAR(128) NOT NULL UNIQUE,
        school_id VARCHAR(64) NOT NULL,
        profile_json TEXT NOT NULL,
        profile_version INT NOT NULL DEFAULT 1,
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        delivery_time VARCHAR(5) NOT NULL DEFAULT '09:00',
        created_at VARCHAR(64),
        updated_at VARCHAR(64)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS school_notice_feedback (
        id BIGINT PRIMARY KEY AUTO_RANDOM,
        user_key VARCHAR(128) NOT NULL,
        source_id VARCHAR(128) NOT NULL,
        external_id VARCHAR(128) NOT NULL,
        feedback_type VARCHAR(32) NOT NULL,
        topic VARCHAR(256),
        interaction_id VARCHAR(128) NOT NULL UNIQUE,
        created_at VARCHAR(64) NOT NULL,
        consumed_at VARCHAR(64),
        KEY idx_school_notice_feedback_pending (user_key, consumed_at, created_at)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS school_notice_deliveries (
        id BIGINT PRIMARY KEY AUTO_RANDOM,
        user_key VARCHAR(128) NOT NULL,
        digest_date VARCHAR(10) NOT NULL,
        notice_id BIGINT NOT NULL,
        revision_count INT NOT NULL DEFAULT 1,
        status VARCHAR(32) NOT NULL,
        failure_reason VARCHAR(64),
        attempt_count INT NOT NULL DEFAULT 1,
        delivered_at VARCHAR(64) NOT NULL,
        UNIQUE KEY uq_school_notice_delivery_revision
            (user_key, notice_id, revision_count),
        KEY idx_school_notice_deliveries_user_date (user_key, digest_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS school_notice_batch_runs (
        id BIGINT PRIMARY KEY AUTO_RANDOM,
        user_key VARCHAR(128) NOT NULL,
        run_date VARCHAR(10) NOT NULL,
        profile_version INT NOT NULL,
        profile_hash CHAR(64) NOT NULL,
        status VARCHAR(32) NOT NULL,
        collection_status VARCHAR(32),
        may_include_stale BOOLEAN NOT NULL DEFAULT FALSE,
        item_count INT NOT NULL DEFAULT 0,
        http_requests INT,
        llm_calls INT,
        finished_at VARCHAR(64) NOT NULL,
        UNIQUE KEY uq_school_notice_batch_run (user_key, run_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS school_notice_delivery_runs (
        user_key VARCHAR(128) NOT NULL,
        digest_date VARCHAR(10) NOT NULL,
        status VARCHAR(32) NOT NULL,
        attempt_count INT NOT NULL DEFAULT 0,
        next_attempt_at VARCHAR(64),
        last_error VARCHAR(64),
        finished_at VARCHAR(64),
        updated_at VARCHAR(64) NOT NULL,
        PRIMARY KEY (user_key, digest_date),
        KEY idx_school_notice_delivery_due (status, next_attempt_at, updated_at)
    )
    """,
)

_CREATE_RE = re.compile(
    r"\ACREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+"
    r"(school_notice_(?:profiles|feedback|deliveries|batch_runs|delivery_runs))\s*\(",
    re.IGNORECASE,
)
_CREATE_INDEX_RE = re.compile(
    r"\ACREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+"
    r"idx_school_notice_feedback_pending\s+ON\s+"
    r"school_notice_feedback\s*\(\s*user_key\s*,\s*consumed_at\s*,\s*"
    r"created_at\s*\)\Z",
    re.IGNORECASE,
)
_FORBIDDEN_RE = re.compile(
    r"\b(?:ALTER|DELETE|DROP|INSERT|REPLACE|TRUNCATE|UPDATE)\b",
    re.IGNORECASE,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="학교 공지 테이블 5개만 additive 방식으로 생성"
    )
    parser.add_argument("--expected-profile", required=True, choices=("masamo", "general"))
    parser.add_argument("--expected-db", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    return parser.parse_args(argv)


def schema_statements(backend: str) -> tuple[str, ...]:
    if backend == "sqlite":
        statements = tuple(item.strip() for item in _SQLITE_STATEMENTS)
    elif backend == "tidb":
        statements = tuple(item.strip() for item in _TIDB_STATEMENTS)
    else:
        raise ValueError(f"지원하지 않는 DB backend입니다: {backend!r}")
    found = set()
    for statement in statements:
        if ";" in statement or _FORBIDDEN_RE.search(statement):
            raise ValueError("additive CREATE TABLE 단일 문장만 허용합니다.")
        match = _CREATE_RE.match(statement)
        if match is not None:
            found.add(match.group(1).lower())
        elif backend == "sqlite" and _CREATE_INDEX_RE.match(statement):
            continue
        else:
            raise ValueError("허용되지 않은 학교 공지 CREATE TABLE 문장입니다.")
    if found != set(SCHOOL_NOTICE_TABLE_COLUMNS):
        raise ValueError("학교 공지 테이블 5개만 정확히 생성해야 합니다.")
    return statements


def confirmation_phrase() -> str:
    return (
        "APPLY SCHOOL NOTICE SCHEMA TO "
        f"profile={config.PROFILE} backend={config.DB_BACKEND} "
        f"database={configured_target_identity()}"
    )


async def verify_schema(db) -> None:
    errors = []
    for table, expected in SCHOOL_NOTICE_TABLE_COLUMNS.items():
        missing = sorted(set(expected) - set(await get_table_columns(db, table)))
        if missing:
            errors.append(f"{table}: {', '.join(missing)}")
    if errors:
        raise RuntimeError("학교 공지 schema 사후 검증 실패: " + "; ".join(errors))


async def apply_schema(db, *, backend: str) -> None:
    """고정된 5개 CREATE 문만 적용하고 컬럼 계약을 사후 검증합니다."""
    try:
        for statement in schema_statements(backend):
            await db.execute(statement)
        await db.commit()
        await verify_schema(db)
    except Exception:
        await db.rollback()
        raise


async def run(args: argparse.Namespace) -> int:
    # 기존 one-shot 도구의 프로필·DB·TLS·AUTO_MIGRATE 검증을 그대로 재사용하되,
    # 실제 적용 확인 문구는 이 도구 전용으로 별도 검사한다.
    validation_args = argparse.Namespace(
        expected_profile=args.expected_profile,
        expected_db=args.expected_db,
        apply=False,
        confirm=None,
    )
    validate_target(validation_args)
    statements = schema_statements(config.DB_BACKEND)
    phrase = confirmation_phrase()
    print(
        f"target profile={config.PROFILE} backend={config.DB_BACKEND} "
        f"database={configured_target_identity()}"
    )
    print(
        "실행 허용 SQL: CREATE TABLE IF NOT EXISTS 5문장"
        + (" + CREATE INDEX IF NOT EXISTS 1문장" if config.DB_BACKEND == "sqlite" else "")
    )
    if not args.apply:
        print("[DRY-RUN] DB에 연결하거나 변경하지 않았습니다.")
        print("실제 적용 확인 문구:")
        print(phrase)
        return 0
    if args.confirm != phrase:
        raise SystemExit("--apply에는 --confirm으로 다음 문구를 정확히 입력해야 합니다:\n" + phrase)

    db = await open_configured_db()
    try:
        await verify_connected_target(
            db,
            backend=config.DB_BACKEND,
            expected_db=configured_target_identity(),
        )
        await apply_schema(db, backend=config.DB_BACKEND)
    finally:
        await db.close()
    print("[APPLIED] 학교 공지 테이블 5개의 컬럼 검증까지 완료했습니다.")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())

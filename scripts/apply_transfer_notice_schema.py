#!/usr/bin/env python3
"""편입 구독 2개와 기존 구독자 동의 안내 1개 테이블을 additive 생성한다."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import re
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config
from database.compat_db import TiDBSettings, connect_main_db, get_table_columns


TABLE_COLUMNS = {
    "privacy_consent_prompts": {
        "user_id", "scope", "policy_version", "notice_hash", "status",
        "attempt_count", "next_attempt_at", "sent_at", "last_error", "updated_at",
    },
    "transfer_notice_subscriptions": {
        "user_id", "schools_json", "enabled", "created_at", "updated_at",
    },
    "transfer_notice_deliveries": {
        "user_id", "run_id", "source_id", "external_id", "revision",
        "payload_json", "status", "attempt_count", "next_attempt_at",
        "delivered_at", "last_error", "updated_at",
    },
}

SQLITE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS privacy_consent_prompts (
        user_id INTEGER NOT NULL, scope TEXT NOT NULL,
        policy_version TEXT NOT NULL, notice_hash TEXT NOT NULL,
        status TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT, sent_at TEXT, last_error TEXT, updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, scope, policy_version, notice_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS transfer_notice_subscriptions (
        user_id INTEGER PRIMARY KEY, schools_json TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS transfer_notice_deliveries (
        user_id INTEGER NOT NULL, run_id TEXT NOT NULL,
        source_id TEXT NOT NULL, external_id TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1, payload_json TEXT NOT NULL,
        status TEXT NOT NULL,
        attempt_count INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT,
        delivered_at TEXT, last_error TEXT, updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, source_id, external_id, revision)
    )
    """,
)

TIDB_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS privacy_consent_prompts (
        user_id BIGINT NOT NULL, scope VARCHAR(64) NOT NULL,
        policy_version VARCHAR(64) NOT NULL, notice_hash CHAR(64) NOT NULL,
        status VARCHAR(16) NOT NULL, attempt_count INT NOT NULL DEFAULT 0,
        next_attempt_at VARCHAR(64), sent_at VARCHAR(64),
        last_error VARCHAR(64), updated_at VARCHAR(64) NOT NULL,
        PRIMARY KEY (user_id, scope, policy_version, notice_hash),
        KEY idx_privacy_consent_prompts_due (status, next_attempt_at, updated_at)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS transfer_notice_subscriptions (
        user_id BIGINT PRIMARY KEY, schools_json TEXT NOT NULL,
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        created_at VARCHAR(64) NOT NULL, updated_at VARCHAR(64) NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS transfer_notice_deliveries (
        user_id BIGINT NOT NULL, run_id VARCHAR(64) NOT NULL,
        source_id VARCHAR(64) NOT NULL, external_id VARCHAR(64) NOT NULL,
        revision INT NOT NULL DEFAULT 1, payload_json TEXT NOT NULL,
        status VARCHAR(16) NOT NULL,
        attempt_count INT NOT NULL DEFAULT 0, next_attempt_at VARCHAR(64),
        delivered_at VARCHAR(64), last_error VARCHAR(64),
        updated_at VARCHAR(64) NOT NULL,
        PRIMARY KEY (user_id, source_id, external_id, revision),
        KEY idx_transfer_notice_deliveries_due
            (status, next_attempt_at, updated_at)
    )
    """,
)

_CREATE_RE = re.compile(
    r"\ACREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+"
    r"(privacy_consent_prompts|transfer_notice_subscriptions|"
    r"transfer_notice_deliveries)\s*\(",
    re.IGNORECASE,
)
_FORBIDDEN_RE = re.compile(
    r"\b(?:ALTER|DELETE|DROP|INSERT|REPLACE|TRUNCATE|UPDATE)\b",
    re.IGNORECASE,
)


def schema_statements(backend: str) -> tuple[str, ...]:
    if backend == "sqlite":
        statements = tuple(item.strip() for item in SQLITE_STATEMENTS)
    elif backend == "tidb":
        statements = tuple(item.strip() for item in TIDB_STATEMENTS)
    else:
        raise ValueError(f"지원하지 않는 DB backend: {backend}")
    found: set[str] = set()
    for statement in statements:
        if ";" in statement or _FORBIDDEN_RE.search(statement):
            raise ValueError("additive CREATE TABLE 문장만 허용합니다.")
        match = _CREATE_RE.match(statement)
        if not match:
            raise ValueError("허용되지 않은 schema 문장입니다.")
        found.add(match.group(1).lower())
    if found != set(TABLE_COLUMNS):
        raise ValueError("정확히 세 개의 허용된 테이블만 생성해야 합니다.")
    return statements


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-profile", choices=("masamo", "general"), required=True)
    parser.add_argument("--expected-db", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    return parser.parse_args()


def target_identity() -> str:
    if config.DB_BACKEND == "tidb":
        return str(config.TIDB_NAME)
    return str(Path(config.DATABASE_FILE).expanduser().resolve())


def confirmation_phrase() -> str:
    return (
        "APPLY TRANSFER NOTICE SCHEMA TO "
        f"profile={config.PROFILE} backend={config.DB_BACKEND} "
        f"database={target_identity()}"
    )


def validate(args: argparse.Namespace) -> str:
    if config.ENV_FILE_PATH is None or not config.REQUIRE_EXPLICIT_PROFILE:
        raise SystemExit("MASAMONG_ENV_FILE로 선택한 명시적 운영 프로필이 필요합니다.")
    if config.PROFILE != args.expected_profile or config.PROFILE != config.INSTANCE_NAME:
        raise SystemExit("profile/instance/--expected-profile이 일치해야 합니다.")
    if config.AUTO_MIGRATE:
        raise SystemExit("적용 중 MASAMONG_AUTO_MIGRATE=false여야 합니다.")
    target = target_identity()
    if config.DB_BACKEND == "tidb":
        if args.expected_db != config.TIDB_NAME:
            raise SystemExit("--expected-db가 현재 TiDB 이름과 일치해야 합니다.")
        if (
            not config.REMOTE_DB_STRICT_MODE
            or not config.REQUIRE_DB_TLS
            or not config.TIDB_SSL_VERIFY_IDENTITY
            or not config.TIDB_SSL_CA
        ):
            raise SystemExit("TiDB는 CA와 hostname 검증을 포함한 strict TLS가 필요합니다.")
    elif config.DB_BACKEND == "sqlite":
        expected = Path(args.expected_db).expanduser()
        if not expected.is_absolute() or expected.resolve() != Path(target):
            raise SystemExit("--expected-db가 현재 SQLite 절대 경로와 일치해야 합니다.")
    else:
        raise SystemExit("지원하지 않는 DB backend입니다.")
    phrase = confirmation_phrase()
    if args.apply and args.confirm != phrase:
        raise SystemExit("--confirm 문구가 일치하지 않습니다:\n" + phrase)
    return phrase


def _tidb_settings() -> TiDBSettings:
    return TiDBSettings(
        host=config.TIDB_HOST or "",
        port=config.TIDB_PORT,
        user=config.TIDB_USER or "",
        password=config.TIDB_PASSWORD or "",
        database=config.TIDB_NAME,
        ssl_ca=config.TIDB_SSL_CA,
        ssl_verify_identity=config.TIDB_SSL_VERIFY_IDENTITY,
        require_tls=config.REQUIRE_DB_TLS,
        connect_timeout=config.TIDB_CONNECT_TIMEOUT,
        read_timeout=config.TIDB_READ_TIMEOUT,
        write_timeout=config.TIDB_WRITE_TIMEOUT,
        conn_max_lifetime_seconds=config.TIDB_CONN_MAX_LIFETIME_SECONDS,
    )


async def apply_schema() -> None:
    db = await connect_main_db(
        config.DB_BACKEND,
        sqlite_path=config.DATABASE_FILE,
        tidb_settings=_tidb_settings() if config.DB_BACKEND == "tidb" else None,
    )
    try:
        if config.DB_BACKEND == "tidb":
            async with db.execute("SELECT DATABASE()") as cursor:
                row = await cursor.fetchone()
            if not row or str(row[0]) != config.TIDB_NAME:
                raise RuntimeError("연결된 TiDB 대상이 설정과 다릅니다.")
        for statement in schema_statements(config.DB_BACKEND):
            await db.execute(statement)
        await db.commit()
        for table, required in TABLE_COLUMNS.items():
            actual = set(await get_table_columns(db, table))
            missing = sorted(required - actual)
            if missing:
                raise RuntimeError(f"{table} 필수 컬럼 누락: {missing}")
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


def main() -> int:
    args = parse_args()
    phrase = validate(args)
    schema_statements(config.DB_BACKEND)
    if not args.apply:
        print("DRY-RUN: 기존 행을 읽거나 수정하지 않고 CREATE TABLE 3개만 준비합니다.")
        print(phrase)
        return 0
    asyncio.run(apply_schema())
    print("편입 구독/동의 안내 schema 적용과 컬럼 검증이 완료되었습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

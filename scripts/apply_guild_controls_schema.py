#!/usr/bin/env python3
"""인스턴스별 최고 관리자 서버 제어 테이블을 additive 생성합니다."""

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


TABLE = "bot_guild_controls"
REQUIRED_COLUMNS = {
    "instance_name",
    "guild_id",
    "ai_enabled",
    "enabled_channels_json",
    "disabled_channels_json",
    "changed_by",
    "created_at",
    "updated_at",
}
SQLITE_STATEMENT = """
CREATE TABLE IF NOT EXISTS bot_guild_controls (
    instance_name TEXT NOT NULL,
    guild_id INTEGER NOT NULL,
    ai_enabled INTEGER NOT NULL DEFAULT 1,
    enabled_channels_json TEXT NOT NULL DEFAULT '[]',
    disabled_channels_json TEXT NOT NULL DEFAULT '[]',
    changed_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (instance_name, guild_id)
)
""".strip()
TIDB_STATEMENT = """
CREATE TABLE IF NOT EXISTS bot_guild_controls (
    instance_name VARCHAR(32) NOT NULL,
    guild_id BIGINT NOT NULL,
    ai_enabled TINYINT(1) NOT NULL DEFAULT 1,
    enabled_channels_json LONGTEXT NOT NULL,
    disabled_channels_json LONGTEXT NOT NULL,
    changed_by BIGINT NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    updated_at VARCHAR(64) NOT NULL,
    PRIMARY KEY (instance_name, guild_id)
)
""".strip()
_CREATE_RE = re.compile(
    r"\ACREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+bot_guild_controls\s*\(",
    re.IGNORECASE,
)
_FORBIDDEN_RE = re.compile(
    r"\b(?:ALTER|DELETE|DROP|INSERT|REPLACE|TRUNCATE|UPDATE)\b",
    re.IGNORECASE,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--expected-profile",
        choices=("masamo", "general"),
        required=True,
    )
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
        "APPLY GUILD CONTROLS SCHEMA TO "
        f"profile={config.PROFILE} backend={config.DB_BACKEND} "
        f"database={target_identity()}"
    )


def schema_statement(backend: str) -> str:
    statement = {
        "sqlite": SQLITE_STATEMENT,
        "tidb": TIDB_STATEMENT,
    }.get(backend)
    if statement is None:
        raise ValueError(f"지원하지 않는 DB backend: {backend}")
    if ";" in statement or _FORBIDDEN_RE.search(statement):
        raise ValueError("additive CREATE TABLE 단일 문장만 허용합니다.")
    if not _CREATE_RE.match(statement):
        raise ValueError("허용되지 않은 schema 문장입니다.")
    return statement


def validate(args: argparse.Namespace) -> str:
    if config.ENV_FILE_PATH is None or not config.REQUIRE_EXPLICIT_PROFILE:
        raise SystemExit("MASAMONG_ENV_FILE로 선택한 명시적 프로필이 필요합니다.")
    if config.PROFILE != args.expected_profile or config.PROFILE != config.INSTANCE_NAME:
        raise SystemExit("profile/instance/--expected-profile이 일치해야 합니다.")
    if config.AUTO_MIGRATE:
        raise SystemExit("적용 중 MASAMONG_AUTO_MIGRATE=false여야 합니다.")
    target = target_identity()
    if config.DB_BACKEND == "tidb":
        if args.expected_db != config.TIDB_NAME:
            raise SystemExit("--expected-db가 현재 TiDB 이름과 일치해야 합니다.")
    elif args.expected_db != target:
        raise SystemExit("--expected-db가 현재 SQLite 절대 경로와 일치해야 합니다.")
    return target


async def run(args: argparse.Namespace) -> None:
    target = validate(args)
    phrase = confirmation_phrase()
    if not args.apply:
        print(f"dry-run: profile={config.PROFILE} backend={config.DB_BACKEND} database={target}")
        print(f"confirmation={phrase}")
        return
    if args.confirm != phrase:
        raise SystemExit("--confirm 값이 현재 대상과 정확히 일치해야 합니다.")
    settings = None
    if config.DB_BACKEND == "tidb":
        settings = TiDBSettings(
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
    db = await connect_main_db(
        config.DB_BACKEND,
        sqlite_path=config.DATABASE_FILE,
        tidb_settings=settings,
    )
    try:
        await db.execute(schema_statement(config.DB_BACKEND))
        await db.commit()
        columns = set(await get_table_columns(db, TABLE))
        missing = sorted(REQUIRED_COLUMNS - columns)
        if missing:
            raise RuntimeError("생성 후 필수 컬럼 누락: " + ", ".join(missing))
    finally:
        await db.close()
    print(f"applied: table={TABLE} profile={config.PROFILE} database={target}")


def main() -> None:
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()

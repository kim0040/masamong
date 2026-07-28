#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""개인정보 동의 테이블 두 개만 안전하게 추가하는 one-shot 도구.

기본 동작은 DB에 연결하지 않는 dry-run이다. 실제 적용은 명시적 운영
프로필에서 ``--apply``와 대상 프로필/DB 재입력, 출력된 확인 문구가 모두
정확히 일치할 때만 수행한다. 기존 테이블의 행을 읽어 옮기거나
UPDATE/DELETE/ALTER/backfill/seed를 수행하지 않는다.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from database.compat_db import (  # noqa: E402
    TiDBSettings,
    connect_main_db,
    get_table_columns,
)


PRIVACY_TABLE_COLUMNS: dict[str, frozenset[str]] = {
    "privacy_consents": frozenset(
        {
            "user_id",
            "scope",
            "policy_version",
            "notice_hash",
            "status",
            "granted_at",
            "withdrawn_at",
            "updated_at",
        }
    ),
    "privacy_consent_events": frozenset(
        {
            "id",
            "user_id",
            "scope",
            "policy_version",
            "notice_hash",
            "status",
            "granted_at",
            "withdrawn_at",
            "created_at",
        }
    ),
}

_SQLITE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS privacy_consents (
        user_id INTEGER NOT NULL,
        scope TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        notice_hash TEXT NOT NULL,
        status TEXT NOT NULL,
        granted_at TEXT,
        withdrawn_at TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, scope)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS privacy_consent_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        scope TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        notice_hash TEXT NOT NULL,
        status TEXT NOT NULL,
        granted_at TEXT,
        withdrawn_at TEXT,
        created_at TEXT NOT NULL
    )
    """,
)

_TIDB_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS privacy_consents (
        user_id BIGINT NOT NULL,
        scope VARCHAR(64) NOT NULL,
        policy_version VARCHAR(64) NOT NULL,
        notice_hash CHAR(64) NOT NULL,
        status VARCHAR(16) NOT NULL,
        granted_at VARCHAR(64),
        withdrawn_at VARCHAR(64),
        updated_at VARCHAR(64) NOT NULL,
        PRIMARY KEY (user_id, scope)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS privacy_consent_events (
        id BIGINT PRIMARY KEY AUTO_RANDOM,
        user_id BIGINT NOT NULL,
        scope VARCHAR(64) NOT NULL,
        policy_version VARCHAR(64) NOT NULL,
        notice_hash CHAR(64) NOT NULL,
        status VARCHAR(16) NOT NULL,
        granted_at VARCHAR(64),
        withdrawn_at VARCHAR(64),
        created_at VARCHAR(64) NOT NULL,
        KEY idx_privacy_consent_events_user_scope (user_id, scope, created_at)
    )
    """,
)

_CREATE_TABLE_PATTERN = re.compile(
    r"\ACREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+"
    r"`?(privacy_consents|privacy_consent_events)`?\s*\(",
    re.IGNORECASE,
)
_FORBIDDEN_SQL = re.compile(
    r"\b(?:ALTER|DELETE|DROP|INSERT|REPLACE|TRUNCATE|UPDATE)\b",
    re.IGNORECASE,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """명시 대상과 적용 여부를 파싱한다."""
    parser = argparse.ArgumentParser(
        description="개인정보 동의 테이블 두 개만 additive 방식으로 생성"
    )
    parser.add_argument(
        "--expected-profile",
        required=True,
        choices=("masamo", "general"),
        help="현재 선택한 MASAMONG_PROFILE을 정확히 재입력",
    )
    parser.add_argument(
        "--expected-db",
        required=True,
        help="TiDB DB 이름 또는 SQLite DB 절대 경로를 정확히 재입력",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="기본 dry-run을 끝내고 실제 CREATE TABLE을 실행",
    )
    parser.add_argument(
        "--confirm",
        help="--apply 시 dry-run에 표시된 전체 확인 문구를 정확히 입력",
    )
    return parser.parse_args(argv)


def schema_statements(backend: str) -> tuple[str, str]:
    """백엔드별 고정 CREATE TABLE 문장만 반환한다."""
    normalized = str(backend or "").strip().lower()
    if normalized == "sqlite":
        statements = tuple(statement.strip() for statement in _SQLITE_STATEMENTS)
    elif normalized == "tidb":
        statements = tuple(statement.strip() for statement in _TIDB_STATEMENTS)
    else:
        raise ValueError(f"지원하지 않는 DB backend입니다: {backend!r}")
    assert_additive_only(statements)
    return statements  # type: ignore[return-value]


def assert_additive_only(statements: tuple[str, ...]) -> None:
    """실행 목록이 정확히 두 consent CREATE TABLE 문장인지 검증한다."""
    if len(statements) != 2:
        raise ValueError("개인정보 동의 테이블 CREATE 문장은 정확히 2개여야 합니다.")

    found_tables: set[str] = set()
    for raw_statement in statements:
        statement = str(raw_statement).strip()
        # 드라이버의 다중 문장 설정 여부와 관계없이 한 호출에 한 문장만 허용한다.
        if ";" in statement:
            raise ValueError("세미콜론을 포함한 다중 SQL 문장은 허용하지 않습니다.")
        match = _CREATE_TABLE_PATTERN.match(statement)
        if match is None or _FORBIDDEN_SQL.search(statement):
            raise ValueError(
                "CREATE TABLE IF NOT EXISTS privacy consent 문장만 허용합니다."
            )
        found_tables.add(match.group(1).lower())

    if found_tables != set(PRIVACY_TABLE_COLUMNS):
        raise ValueError(
            "허용된 두 개인정보 동의 테이블만 정확히 생성해야 합니다."
        )


def configured_target_identity() -> str:
    """로그와 재확인에 쓸 현재 설정의 DB 식별자를 반환한다."""
    if config.DB_BACKEND == "tidb":
        return str(config.TIDB_NAME)
    return str(Path(config.DATABASE_FILE).expanduser().resolve())


def confirmation_phrase(*, profile: str, backend: str, database: str) -> str:
    """복사해 입력해야 하는 typed confirmation 문구."""
    return (
        "APPLY PRIVACY CONSENT SCHEMA TO "
        f"profile={profile} backend={backend} database={database}"
    )


def validate_target(args: argparse.Namespace) -> str:
    """연결 전에 명시 프로필·DB·확인 문구를 fail-closed로 검증한다."""
    if (
        config.ENV_FILE_PATH is None
        or not Path(config.ENV_FILE_PATH).expanduser().is_file()
    ):
        raise SystemExit(
            "MASAMONG_ENV_FILE로 선택한 실제 명시적 env 파일이 필요합니다."
        )
    if not config.REQUIRE_EXPLICIT_PROFILE:
        raise SystemExit(
            "MASAMONG_REQUIRE_EXPLICIT_PROFILE=true인 운영 프로필에서만 실행할 수 있습니다."
        )
    if config.PROFILE not in {"masamo", "general"}:
        raise SystemExit("legacy 또는 알 수 없는 프로필에는 적용할 수 없습니다.")
    if config.PROFILE != config.INSTANCE_NAME:
        raise SystemExit("현재 profile과 instance 이름이 일치하지 않습니다.")
    if args.expected_profile != config.PROFILE:
        raise SystemExit(
            "--expected-profile이 현재 MASAMONG_PROFILE과 정확히 일치해야 합니다."
        )
    if config.AUTO_MIGRATE:
        raise SystemExit(
            "one-shot 적용 중에는 MASAMONG_AUTO_MIGRATE=false여야 합니다."
        )

    target = configured_target_identity()
    if config.DB_BACKEND == "tidb":
        if (
            not config.REMOTE_DB_STRICT_MODE
            or not config.REQUIRE_DB_TLS
            or not config.TIDB_SSL_CA
            or not Path(str(config.TIDB_SSL_CA)).expanduser().is_file()
            or not config.TIDB_SSL_VERIFY_IDENTITY
        ):
            raise SystemExit(
                "TiDB 적용은 CA·hostname 검증을 포함한 strict remote TLS가 필요합니다."
            )
        if (
            not config.EXPECTED_DB_NAME
            or config.EXPECTED_DB_NAME != config.TIDB_NAME
        ):
            raise SystemExit(
                "MASAMONG_EXPECTED_DB_NAME이 현재 TiDB 이름과 일치해야 합니다."
            )
        if args.expected_db != config.TIDB_NAME:
            raise SystemExit(
                "--expected-db에 현재 TiDB DB 이름을 정확히 입력해야 합니다."
            )
    elif config.DB_BACKEND == "sqlite":
        configured_path = Path(config.DATABASE_FILE).expanduser().resolve()
        expected_path = Path(str(args.expected_db)).expanduser()
        if not expected_path.is_absolute():
            raise SystemExit("--expected-db의 SQLite 경로는 절대 경로여야 합니다.")
        if expected_path.resolve() != configured_path:
            raise SystemExit(
                "--expected-db가 현재 SQLite DB 절대 경로와 정확히 일치해야 합니다."
            )
        if not configured_path.is_file():
            raise SystemExit(
                "기존 SQLite DB 파일이 없습니다. 새 빈 DB를 암묵적으로 만들지 않습니다."
            )
    else:  # config가 먼저 차단하지만 독립 안전장치로 유지한다.
        raise SystemExit(f"지원하지 않는 DB backend입니다: {config.DB_BACKEND}")

    phrase = confirmation_phrase(
        profile=config.PROFILE,
        backend=config.DB_BACKEND,
        database=target,
    )
    if args.apply and args.confirm != phrase:
        raise SystemExit(
            "--apply에는 --confirm으로 다음 문구를 정확히 입력해야 합니다:\n"
            + phrase
        )
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


async def open_configured_db():
    """검증된 현재 profile의 DB에 연결한다."""
    return await connect_main_db(
        config.DB_BACKEND,
        sqlite_path=config.DATABASE_FILE,
        tidb_settings=_tidb_settings() if config.DB_BACKEND == "tidb" else None,
    )


async def verify_connected_target(db, *, backend: str, expected_db: str) -> None:
    """연결 후 서버/파일이 CLI에서 재확인한 대상과 같은지 읽기 전용 확인한다."""
    if backend == "tidb":
        async with db.execute("SELECT DATABASE()") as cursor:
            row = await cursor.fetchone()
        actual = str(row[0]) if row and row[0] is not None else ""
        if actual != expected_db:
            raise RuntimeError(
                f"연결된 TiDB가 기대 대상과 다릅니다: actual={actual!r}"
            )
        return

    async with db.execute("PRAGMA database_list") as cursor:
        rows = await cursor.fetchall()
    main_path = next(
        (str(row[2]) for row in rows if str(row[1]) == "main"),
        "",
    )
    if not main_path or Path(main_path).resolve() != Path(expected_db).resolve():
        raise RuntimeError("연결된 SQLite 파일이 기대 대상과 다릅니다.")


async def verify_schema(db) -> None:
    """두 테이블이 필요한 컬럼을 모두 가진 상태인지 확인한다."""
    errors: list[str] = []
    for table_name, required_columns in PRIVACY_TABLE_COLUMNS.items():
        actual = set(await get_table_columns(db, table_name))
        missing = sorted(required_columns - actual)
        if missing:
            errors.append(f"{table_name}: {', '.join(missing)}")
    if errors:
        raise RuntimeError(
            "개인정보 동의 schema 사후 검증에 실패했습니다: "
            + "; ".join(errors)
        )


async def apply_schema(db, *, backend: str) -> None:
    """고정된 두 CREATE TABLE만 실행하고 컬럼을 read-back 검증한다."""
    statements = schema_statements(backend)
    try:
        for statement in statements:
            await db.execute(statement)
        await db.commit()
        await verify_schema(db)
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass
        raise


async def run(args: argparse.Namespace) -> int:
    """dry-run 또는 명시 적용을 수행한다."""
    phrase = validate_target(args)
    statements = schema_statements(config.DB_BACKEND)
    target = configured_target_identity()

    print(
        f"target profile={config.PROFILE} backend={config.DB_BACKEND} "
        f"database={target}"
    )
    print("실행 허용 SQL: CREATE TABLE IF NOT EXISTS 2문장만")
    for statement in statements:
        print(statement + ";")

    if not args.apply:
        print("[DRY-RUN] DB에 연결하거나 변경하지 않았습니다.")
        print("실제 적용 확인 문구:")
        print(phrase)
        return 0

    db = await open_configured_db()
    try:
        await verify_connected_target(
            db,
            backend=config.DB_BACKEND,
            expected_db=target,
        )
        await apply_schema(db, backend=config.DB_BACKEND)
    finally:
        await db.close()
    print("[APPLIED] 개인정보 동의 테이블 2개의 컬럼 검증까지 완료했습니다.")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())

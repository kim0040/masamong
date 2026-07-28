#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""최신 운영 데이터셋을 TiDB `masamong` DB로 적재한다."""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
import sys

import numpy as np
import pymysql

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config
from database.compat_db import TiDBSettings, split_sql_script


SOURCE_MAIN_TABLES = [
    "guild_settings",
    "user_activity",
    "user_activity_log",
    "linkup_usage_log",
    "channel_summary_state",
    "conversation_history",
    "conversation_windows",
    "system_counters",
    "api_call_log",
    "analytics_log",
    "conversation_history_archive",
    "user_preferences",
    "locations",
    "user_profiles",
    "dm_usage_logs",
]
KAKAO_VECTOR_DIMENSION = 384
DISCORD_CHAT_COLUMNS = {
    "id",
    "message_id",
    "server_id",
    "channel_id",
    "user_id",
    "user_name",
    "message",
    "timestamp",
    "embedding",
}
DISCORD_MEMORY_COLUMNS = {
    "id",
    "memory_id",
    "anchor_message_id",
    "server_id",
    "channel_id",
    "owner_user_id",
    "owner_user_name",
    "memory_scope",
    "memory_type",
    "summary_text",
    "memory_text",
    "raw_context",
    "source_message_ids",
    "speaker_names",
    "keyword_json",
    "timestamp",
    "embedding",
}
_TARGET_CONFIRMATION_CAPABILITY = object()
_CONNECTED_TARGET_CAPABILITY = object()
_INVALID_BACKUP_REFERENCES = frozenset(
    {
        "backup",
        "latest",
        "n/a",
        "none",
        "snapshot",
        "test",
        "todo",
        "unknown",
    }
)


@dataclass(frozen=True)
class TargetIdentityConfirmation:
    """CLI/config에서 독립적으로 확인한 원격 쓰기 대상이다."""

    profile: str
    host: str
    port: int
    database: str
    destructive: bool
    backup_reference: str | None = field(default=None, repr=False)
    _capability: object | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class ConnectedTargetAuthorization:
    """연결 후 DATABASE()까지 대조한 파괴 작업 capability다."""

    target: TargetIdentityConfirmation
    _capability: object = field(repr=False, compare=False)


def parse_args() -> argparse.Namespace:
    """CLI 인자를 파싱하여 소스 루트, 스킵 플래그, truncate 옵션을 반환합니다."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", default="임시", help="최신 운영 데이터셋 루트")
    parser.add_argument("--skip-main", action="store_true")
    parser.add_argument("--skip-discord", action="store_true")
    parser.add_argument("--skip-kakao", action="store_true")
    execution_mode = parser.add_mutually_exclusive_group()
    execution_mode.add_argument(
        "--apply",
        action="store_true",
        help="사전검사 후 실제 원격 DB 쓰기를 실행 (기본값은 dry-run)",
    )
    execution_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="원격 DB에 연결하거나 쓰지 않고 사전검사만 실행 (기본 동작)",
    )
    parser.add_argument("--truncate", action="store_true", help="적재 전 대상 테이블 비우기")
    parser.add_argument(
        "--confirm-database",
        help="쓰기 대상 DB 이름을 다시 입력",
    )
    parser.add_argument(
        "--confirm-profile",
        help="쓰기 대상 MASAMONG_PROFILE 값을 다시 입력",
    )
    parser.add_argument(
        "--confirm-destructive",
        help="--truncate 사용 시 안내된 전체 확인 문구를 정확히 입력",
    )
    parser.add_argument(
        "--backup-reference",
        help="--truncate 직전 실제 생성 및 복원 검증한 snapshot/backup 식별자",
    )
    parser.add_argument(
        "--confirm-backup-reference",
        help="--backup-reference 식별자를 공백까지 정확히 다시 입력",
    )
    return parser.parse_args()


def connect_tidb(settings: TiDBSettings) -> pymysql.connections.Connection:
    """검증된 환경 변수로 TiDB에 연결한다. 이 함수 자체는 SQL을 실행하지 않는다."""
    return pymysql.connect(**settings.to_connect_kwargs())


def configure_tidb_session(conn: pymysql.connections.Connection) -> None:
    """연결된 DB identity를 확인한 뒤 필요한 session 옵션만 활성화한다."""
    with conn.cursor() as cursor:
        cursor.execute("SET @@allow_auto_random_explicit_insert = true")
    conn.commit()


def apply_schema(conn: pymysql.connections.Connection) -> None:
    """schema_tidb.sql 스크립트를 읽어 TiDB에 스키마를 적용합니다."""
    schema_path = Path(__file__).resolve().parents[1] / "database" / "schema_tidb.sql"
    statements = split_sql_script(schema_path.read_text(encoding="utf-8"))
    with conn.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)
    conn.commit()


def recreate_tables(
    conn: pymysql.connections.Connection,
    *,
    authorization: ConnectedTargetAuthorization,
) -> None:
    """의존성 순서대로 모든 테이블을 DROP 후 재생성합니다."""
    if (
        not isinstance(authorization, ConnectedTargetAuthorization)
        or authorization._capability is not _CONNECTED_TARGET_CAPABILITY
        or (
            authorization.target._capability
            is not _TARGET_CONFIRMATION_CAPABILITY
        )
        or not authorization.target.destructive
        or not authorization.target.backup_reference
    ):
        raise RuntimeError(
            "연결 대상과 backup을 모두 검증한 파괴 작업 authorization이 필요합니다."
        )
    ordered = [
        "kakao_chunks",
        "discord_memory_entries",
        "discord_chat_embeddings",
        "analytics_log",
        "api_call_log",
        "conversation_windows",
        "conversation_history_archive",
        "conversation_history",
        "linkup_usage_log",
        "channel_summary_state",
        "user_activity_log",
        "user_activity",
        "guild_settings",
        "system_counters",
        "user_preferences",
        "locations",
        "user_profiles",
        "dm_usage_logs",
    ]
    with conn.cursor() as cursor:
        for table in ordered:
            cursor.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()


def _connect_sqlite_read_only(path: Path) -> sqlite3.Connection:
    """원본 SQLite를 생성/수정하지 않는 URI read-only 모드로 연다."""
    uri = path.resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _sqlite_table_names(path: Path) -> set[str]:
    """네트워크 연결 전에 SQLite 원본의 테이블 목록을 읽습니다."""
    conn = _connect_sqlite_read_only(path)
    try:
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        conn.close()


def _sqlite_table_columns(path: Path, table_name: str) -> set[str]:
    """SQLite 원본 테이블의 열 이름을 read-only로 반환한다."""
    conn = _connect_sqlite_read_only(path)
    try:
        return {
            str(row[1])
            for row in conn.execute(
                f'PRAGMA table_info("{table_name}")'
            ).fetchall()
        }
    finally:
        conn.close()


def _sqlite_row_count(path: Path, table_name: str) -> int:
    """SQLite 원본 테이블의 행 수를 read-only로 확인한다."""
    conn = _connect_sqlite_read_only(path)
    try:
        row = conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def _sqlite_quick_check(path: Path) -> str:
    """SQLite 원본의 빠른 무결성 검사 결과를 반환한다."""
    conn = _connect_sqlite_read_only(path)
    try:
        rows = conn.execute("PRAGMA quick_check").fetchall()
        return "; ".join(str(row[0]) for row in rows)
    finally:
        conn.close()


def _expected_main_columns() -> dict[str, set[str]]:
    """현재 repository schema와 일치하는 메인 SQLite 열 집합을 만든다."""
    schema_path = Path(__file__).resolve().parents[1] / "database" / "schema.sql"
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(schema_path.read_text(encoding="utf-8"))
        return {
            table_name: {
                str(row[1])
                for row in conn.execute(
                    f'PRAGMA table_info("{table_name}")'
                ).fetchall()
            }
            for table_name in SOURCE_MAIN_TABLES
        }
    finally:
        conn.close()


def preflight_sources(
    *,
    source_root: Path,
    main_db: Path,
    discord_db: Path,
    skip_main: bool,
    skip_discord: bool,
    skip_kakao: bool,
    destructive: bool,
) -> None:
    """대상 DB에 연결하기 전에 모든 선택 원본이 완전한지 검증합니다."""
    errors: list[str] = []

    if destructive and (skip_main or skip_discord or skip_kakao):
        errors.append(
            "--truncate는 일부 저장소를 의도치 않게 비울 수 있어 --skip-* 옵션과 함께 사용할 수 없습니다."
        )

    if not skip_main:
        if not main_db.is_file():
            errors.append(f"메인 SQLite 원본을 찾을 수 없습니다: {main_db}")
        else:
            try:
                quick_check = _sqlite_quick_check(main_db)
                if quick_check.lower() != "ok":
                    errors.append(f"메인 SQLite 원본 무결성 검사 실패: {quick_check}")
                missing = set(SOURCE_MAIN_TABLES) - _sqlite_table_names(main_db)
                if missing:
                    errors.append(
                        "메인 SQLite 원본에 보존 대상 테이블이 없습니다: "
                        + ", ".join(sorted(missing))
                    )
                else:
                    for table_name, expected_columns in (
                        _expected_main_columns().items()
                    ):
                        actual_columns = _sqlite_table_columns(
                            main_db,
                            table_name,
                        )
                        missing_columns = sorted(
                            expected_columns - actual_columns
                        )
                        unexpected_columns = sorted(
                            actual_columns - expected_columns
                        )
                        if missing_columns:
                            errors.append(
                                f"메인 SQLite {table_name} 열이 누락되었습니다: "
                                + ", ".join(missing_columns)
                            )
                        if unexpected_columns:
                            errors.append(
                                f"메인 SQLite {table_name}에 대상 schema에 없는 "
                                "열이 있습니다: "
                                + ", ".join(unexpected_columns)
                            )
                    if destructive and _sqlite_row_count(
                        main_db, "conversation_history"
                    ) <= 0:
                        errors.append(
                            "--truncate 원본의 conversation_history가 비어 있습니다."
                        )
            except sqlite3.Error as exc:
                errors.append(
                    f"메인 SQLite 원본을 검사할 수 없습니다: {type(exc).__name__}"
                )

    if not skip_discord:
        if not discord_db.is_file():
            errors.append(f"Discord 임베딩 원본을 찾을 수 없습니다: {discord_db}")
        else:
            try:
                quick_check = _sqlite_quick_check(discord_db)
                if quick_check.lower() != "ok":
                    errors.append(
                        f"Discord 임베딩 원본 무결성 검사 실패: {quick_check}"
                    )
                required_discord_tables = {"discord_chat_embeddings"}
                if destructive:
                    required_discord_tables.add("discord_memory_entries")
                missing = required_discord_tables - _sqlite_table_names(discord_db)
                if missing:
                    errors.append(
                        "Discord 임베딩 원본에 보존 대상 테이블이 없습니다: "
                        + ", ".join(sorted(missing))
                    )
                else:
                    missing_chat_columns = (
                        DISCORD_CHAT_COLUMNS
                        - _sqlite_table_columns(
                            discord_db, "discord_chat_embeddings"
                        )
                    )
                    if missing_chat_columns:
                        errors.append(
                            "Discord chat 원본 열이 누락되었습니다: "
                            + ", ".join(sorted(missing_chat_columns))
                        )
                    if destructive:
                        missing_memory_columns = (
                            DISCORD_MEMORY_COLUMNS
                            - _sqlite_table_columns(
                                discord_db, "discord_memory_entries"
                            )
                        )
                        if missing_memory_columns:
                            errors.append(
                                "Discord memory 원본 열이 누락되었습니다: "
                                + ", ".join(sorted(missing_memory_columns))
                            )
                        discord_rows = _sqlite_row_count(
                            discord_db, "discord_chat_embeddings"
                        ) + _sqlite_row_count(
                            discord_db, "discord_memory_entries"
                        )
                        if discord_rows <= 0:
                            errors.append(
                                "--truncate Discord 임베딩 원본이 모두 비어 있습니다."
                            )
            except sqlite3.Error as exc:
                errors.append(
                    f"Discord 임베딩 원본을 검사할 수 없습니다: {type(exc).__name__}"
                )

    if not skip_kakao:
        rooms_root = source_root / "kakao_store"
        if not rooms_root.is_dir():
            errors.append(
                f"Kakao 원본 디렉터리를 찾을 수 없습니다: {rooms_root} "
                "(의도한 비파괴 적재라면 --skip-kakao 사용)"
            )
        else:
            room_dirs = sorted(path for path in rooms_root.iterdir() if path.is_dir())
            if not room_dirs:
                errors.append(f"Kakao room 원본이 비어 있습니다: {rooms_root}")
            total_kakao_rows = 0
            for room_dir in room_dirs:
                metadata_path = room_dir / "metadata.json"
                vectors_path = room_dir / "vectors.npy"
                if not metadata_path.is_file() or not vectors_path.is_file():
                    errors.append(
                        f"Kakao room 원본 파일이 누락되었습니다: {room_dir.name}"
                    )
                    continue
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    vectors = np.load(vectors_path, mmap_mode="r")
                    if not isinstance(metadata, list) or len(metadata) != len(vectors):
                        errors.append(
                            f"Kakao room metadata/vector 개수가 다릅니다: {room_dir.name}"
                        )
                        continue
                    if any(not isinstance(item, dict) for item in metadata):
                        errors.append(
                            f"Kakao room metadata 행 형식이 잘못되었습니다: {room_dir.name}"
                        )
                    if (
                        vectors.ndim != 2
                        or vectors.shape[1] != KAKAO_VECTOR_DIMENSION
                    ):
                        errors.append(
                            "Kakao room vector 차원이 TiDB schema와 다릅니다: "
                            f"{room_dir.name}"
                        )
                    total_kakao_rows += len(metadata)
                except Exception as exc:
                    errors.append(
                        f"Kakao room 원본을 읽을 수 없습니다: {room_dir.name} ({type(exc).__name__})"
                    )
            if destructive and total_kakao_rows <= 0:
                errors.append("--truncate Kakao 원본이 비어 있습니다.")

    if errors:
        raise SystemExit("원본 사전검사 실패:\n- " + "\n- ".join(errors))


def _settings_from_config() -> TiDBSettings:
    """config가 검증한 값과 동일한 TiDB 연결 설정을 만든다."""
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


def validate_target_identity(
    args: argparse.Namespace,
    settings: TiDBSettings,
    *,
    require_write_confirmation: bool = True,
) -> TargetIdentityConfirmation:
    """모든 원격 쓰기 전에 프로필과 DB 대상을 독립적으로 재확인한다."""
    if config.ENV_FILE_PATH is None:
        raise SystemExit(
            "TiDB 적재는 MASAMONG_ENV_FILE로 명시한 환경 파일에서만 실행할 수 있습니다."
        )
    profile_databases = {
        "masamo": "masamong",
        "general": "masamong_general",
    }
    if (
        not config.REQUIRE_EXPLICIT_PROFILE
        or config.PROFILE not in profile_databases
    ):
        raise SystemExit(
            "TiDB 적재는 명시적 masamo/general 프로필과 "
            "MASAMONG_REQUIRE_EXPLICIT_PROFILE=true가 필요합니다."
        )
    if config.DB_BACKEND != "tidb" or not config.REMOTE_DB_STRICT_MODE:
        raise SystemExit(
            "TiDB 적재는 strict remote TiDB 프로필에서만 실행할 수 있습니다."
        )
    if (
        not settings.require_tls
        or not settings.ssl_ca
        or not settings.ssl_verify_identity
    ):
        raise SystemExit(
            "TiDB 적재는 CA와 hostname 검증을 포함한 strict TLS가 필요합니다."
        )
    if (
        not settings.host
        or not settings.user
        or not settings.password
        or not 1 <= settings.port <= 65535
    ):
        raise SystemExit(
            "TiDB 적재 대상의 host/port 및 접속 credential 설정이 완전해야 합니다."
        )
    if (
        not config.EXPECTED_DB_NAME
        or config.EXPECTED_DB_NAME != settings.database
        or config.TIDB_NAME != settings.database
        or profile_databases[config.PROFILE] != settings.database
    ):
        raise SystemExit(
            "프로필 고정 DB, MASAMONG_DB_NAME, MASAMONG_EXPECTED_DB_NAME과 "
            "실제 대상 DB가 모두 일치해야 합니다."
        )

    destructive = bool(getattr(args, "truncate", False))
    if not require_write_confirmation:
        return TargetIdentityConfirmation(
            profile=config.PROFILE,
            host=settings.host,
            port=settings.port,
            database=settings.database,
            destructive=destructive,
            _capability=_TARGET_CONFIRMATION_CAPABILITY,
        )

    if getattr(args, "confirm_database", None) != settings.database:
        raise SystemExit(
            "--confirm-database에 실제 대상 DB 이름을 정확히 입력해야 합니다."
        )
    if getattr(args, "confirm_profile", None) != config.PROFILE:
        raise SystemExit(
            "--confirm-profile에 현재 MASAMONG_PROFILE을 정확히 입력해야 합니다."
        )

    if not destructive:
        return TargetIdentityConfirmation(
            profile=config.PROFILE,
            host=settings.host,
            port=settings.port,
            database=settings.database,
            destructive=False,
            _capability=_TARGET_CONFIRMATION_CAPABILITY,
        )

    backup_reference = str(getattr(args, "backup_reference", "") or "")
    normalized_reference = backup_reference.casefold()
    if (
        backup_reference != backup_reference.strip()
        or not 8 <= len(backup_reference) <= 160
        or any(
            character.isspace() or not character.isprintable()
            for character in backup_reference
        )
        or normalized_reference in _INVALID_BACKUP_REFERENCES
    ):
        raise SystemExit(
            "--truncate는 실제 생성 및 복원 검증한 snapshot/backup의 "
            "고유 --backup-reference(8~160자, 공백/제어문자 없음)가 필요합니다."
        )
    if (
        getattr(args, "confirm_backup_reference", None)
        != backup_reference
    ):
        raise SystemExit(
            "--confirm-backup-reference에 --backup-reference를 정확히 다시 입력해야 합니다."
        )
    expected_phrase = (
        f"DROP ALL TABLES ON {settings.host}:{settings.port}/"
        f"{settings.database} FOR {config.PROFILE} "
        f"USING VERIFIED BACKUP {backup_reference}"
    )
    if getattr(args, "confirm_destructive", None) != expected_phrase:
        raise SystemExit(
            "--truncate는 --confirm-destructive에 다음 문구를 정확히 입력해야 합니다: "
            + expected_phrase
        )
    return TargetIdentityConfirmation(
        profile=config.PROFILE,
        host=settings.host,
        port=settings.port,
        database=settings.database,
        destructive=True,
        backup_reference=backup_reference,
        _capability=_TARGET_CONFIRMATION_CAPABILITY,
    )


def verify_connected_target(
    conn: pymysql.connections.Connection,
    confirmation: TargetIdentityConfirmation,
) -> ConnectedTargetAuthorization:
    """서버가 실제 선택한 DB를 read-back한 뒤에만 파괴 capability를 발급한다."""
    if (
        not isinstance(confirmation, TargetIdentityConfirmation)
        or confirmation._capability is not _TARGET_CONFIRMATION_CAPABILITY
    ):
        raise SystemExit(
            "정확한 CLI/config 확인을 통과한 target confirmation이 필요합니다."
        )
    with conn.cursor() as cursor:
        cursor.execute("SELECT DATABASE() AS current_database")
        row = cursor.fetchone()
    if isinstance(row, dict):
        actual_database = row.get("current_database")
    elif isinstance(row, (tuple, list)) and len(row) == 1:
        actual_database = row[0]
    else:
        actual_database = None
    if isinstance(actual_database, bytes):
        actual_database = actual_database.decode("utf-8", errors="strict")
    if actual_database != confirmation.database:
        raise SystemExit(
            "연결 후 DATABASE()가 확인한 대상 DB와 일치하지 않아 쓰기를 중단합니다."
        )
    return ConnectedTargetAuthorization(
        target=confirmation,
        _capability=_CONNECTED_TARGET_CAPABILITY,
    )


def migrate_sqlite_tables(source_db: Path, conn: pymysql.connections.Connection) -> None:
    """SQLite의 SOURCE_MAIN_TABLES 데이터를 TiDB로 이전합니다."""
    src = _connect_sqlite_read_only(source_db)
    src.row_factory = sqlite3.Row
    try:
        for table in SOURCE_MAIN_TABLES:
            source_cursor = src.execute(f'SELECT * FROM "{table}"')
            columns = [
                str(description[0])
                for description in (source_cursor.description or ())
            ]
            placeholder = ", ".join(["%s"] * len(columns))
            insert_sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholder})"
            total = 0
            while True:
                rows = source_cursor.fetchmany(250)
                if not rows:
                    break
                payload = [tuple(row[col] for col in columns) for row in rows]
                with conn.cursor() as cursor:
                    cursor.executemany(insert_sql, payload)
                conn.commit()
                total += len(rows)
            print(f"[main] {table}: {total} rows")
    finally:
        src.close()


def migrate_discord_embeddings(source_db: Path, conn: pymysql.connections.Connection) -> None:
    """discord_chat_embeddings 테이블을 SQLite에서 TiDB로 이전합니다."""
    src = _connect_sqlite_read_only(source_db)
    src.row_factory = sqlite3.Row
    try:
        source_cursor = src.execute(
            """
            SELECT message_id, server_id, channel_id, user_id, user_name, message, timestamp, embedding
            FROM discord_chat_embeddings
            ORDER BY id ASC
            """
        )
        sql = """
            INSERT INTO discord_chat_embeddings (
                message_id, server_id, channel_id, user_id, user_name, message, timestamp, embedding
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                user_name = VALUES(user_name),
                message = VALUES(message),
                timestamp = VALUES(timestamp),
                embedding = VALUES(embedding)
        """
        total = 0
        while True:
            rows = source_cursor.fetchmany(250)
            if not rows:
                break
            payload = [
                (
                    row["message_id"],
                    row["server_id"],
                    row["channel_id"],
                    row["user_id"],
                    row["user_name"],
                    row["message"],
                    row["timestamp"],
                    row["embedding"],
                )
                for row in rows
            ]
            with conn.cursor() as cursor:
                cursor.executemany(sql, payload)
            conn.commit()
            total += len(rows)
        print(f"[discord] {total} rows")
    finally:
        src.close()


def migrate_discord_memory_entries(source_db: Path, conn: pymysql.connections.Connection) -> None:
    """discord_memory_entries 테이블을 SQLite에서 TiDB로 이전합니다."""
    src = _connect_sqlite_read_only(source_db)
    src.row_factory = sqlite3.Row
    try:
        table_row = src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='discord_memory_entries'"
        ).fetchone()
        if not table_row:
            print("[discord_memory] table missing")
            return
        source_cursor = src.execute(
            """
            SELECT memory_id, anchor_message_id, server_id, channel_id, owner_user_id, owner_user_name,
                   memory_scope, memory_type, summary_text, memory_text, raw_context, source_message_ids,
                   speaker_names, keyword_json, timestamp, embedding
            FROM discord_memory_entries
            ORDER BY id ASC
            """
        )
        sql = """
            INSERT INTO discord_memory_entries (
                memory_id, anchor_message_id, server_id, channel_id, owner_user_id, owner_user_name,
                memory_scope, memory_type, summary_text, memory_text, raw_context, source_message_ids,
                speaker_names, keyword_json, timestamp, embedding
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                owner_user_id = VALUES(owner_user_id),
                owner_user_name = VALUES(owner_user_name),
                memory_scope = VALUES(memory_scope),
                memory_type = VALUES(memory_type),
                summary_text = VALUES(summary_text),
                memory_text = VALUES(memory_text),
                raw_context = VALUES(raw_context),
                source_message_ids = VALUES(source_message_ids),
                speaker_names = VALUES(speaker_names),
                keyword_json = VALUES(keyword_json),
                timestamp = VALUES(timestamp),
                embedding = VALUES(embedding)
        """
        total = 0
        while True:
            rows = source_cursor.fetchmany(250)
            if not rows:
                break
            payload = [
                (
                    row["memory_id"],
                    row["anchor_message_id"],
                    row["server_id"],
                    row["channel_id"],
                    row["owner_user_id"],
                    row["owner_user_name"],
                    row["memory_scope"],
                    row["memory_type"],
                    row["summary_text"],
                    row["memory_text"],
                    row["raw_context"],
                    row["source_message_ids"],
                    row["speaker_names"],
                    row["keyword_json"],
                    row["timestamp"],
                    row["embedding"],
                )
                for row in rows
            ]
            with conn.cursor() as cursor:
                cursor.executemany(sql, payload)
            conn.commit()
            total += len(rows)
        print(f"[discord_memory] {total} rows")
    finally:
        src.close()


def _room_label(room_key: str) -> str:
    """KAKAO_EMBEDDING_SERVER_MAP에서 room_key에 대응하는 레이블을 반환합니다."""
    for meta in config.KAKAO_EMBEDDING_SERVER_MAP.values():
        candidate_room_key = str(meta.get("room_key") or "").strip()
        db_path = meta.get("db_path") or ""
        if candidate_room_key == room_key or Path(db_path).name == room_key:
            return meta.get("label") or room_key
    return room_key


def migrate_kakao_store(source_root: Path, conn: pymysql.connections.Connection) -> None:
    """kakao_store 디렉토리의 모든 room 데이터를 TiDB로 이전합니다."""
    rooms_root = source_root / "kakao_store"
    room_dirs = sorted(path for path in rooms_root.iterdir() if path.is_dir())
    total = 0
    for room_dir in room_dirs:
        room_key = room_dir.name
        metadata = json.loads((room_dir / "metadata.json").read_text(encoding="utf-8"))
        vectors = np.load(room_dir / "vectors.npy", mmap_mode="r")
        label = _room_label(room_key)

        insert_sql = """
            INSERT INTO kakao_chunks (
                room_key, source_room_label, chunk_id, session_id,
                start_date, message_count, summary, text_long, embedding
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                session_id = VALUES(session_id),
                start_date = VALUES(start_date),
                message_count = VALUES(message_count),
                summary = VALUES(summary),
                text_long = VALUES(text_long),
                embedding = VALUES(embedding)
        """
        batch: list[tuple[object, ...]] = []
        with conn.cursor() as cursor:
            for idx, item in enumerate(metadata):
                vector_literal = "[" + ",".join(f"{float(v):.8f}" for v in vectors[idx].tolist()) + "]"
                batch.append(
                    (
                        room_key,
                        label,
                        int(item.get("id", idx)),
                        item.get("session_id"),
                        item.get("start_date"),
                        item.get("message_count"),
                        item.get("summary"),
                        item.get("text"),
                        vector_literal,
                    )
                )
                if len(batch) >= 250:
                    cursor.executemany(insert_sql, batch)
                    conn.commit()
                    batch.clear()
            if batch:
                cursor.executemany(insert_sql, batch)
                conn.commit()
        total += len(metadata)
        print(f"[kakao] {room_key}: {len(metadata)} rows")
    print(f"[kakao] total: {total} rows")


def main() -> None:
    """전체 마이그레이션 파이프라인을 실행합니다."""
    args = parse_args()
    source_root = Path(args.source_root).resolve()
    main_db = source_root / "remasamong.db"
    discord_db = source_root / "discord_embeddings.db"

    preflight_sources(
        source_root=source_root,
        main_db=main_db,
        discord_db=discord_db,
        skip_main=args.skip_main,
        skip_discord=args.skip_discord,
        skip_kakao=args.skip_kakao,
        destructive=args.truncate,
    )

    settings = _settings_from_config()
    apply_changes = bool(getattr(args, "apply", False))
    confirmation = validate_target_identity(
        args,
        settings,
        require_write_confirmation=apply_changes,
    )

    if not apply_changes:
        print(
            "[dry-run] 원격 DB 연결/쓰기 없음: "
            f"profile={confirmation.profile} "
            f"database={confirmation.database} "
            f"truncate={str(confirmation.destructive).lower()}"
        )
        return

    conn = connect_tidb(settings)
    try:
        authorization = verify_connected_target(conn, confirmation)
        configure_tidb_session(conn)
        if args.truncate:
            recreate_tables(conn, authorization=authorization)
        apply_schema(conn)
        if not args.skip_main:
            migrate_sqlite_tables(main_db, conn)
        if not args.skip_discord and discord_db.exists():
            migrate_discord_embeddings(discord_db, conn)
            migrate_discord_memory_entries(discord_db, conn)
        if not args.skip_kakao and (source_root / "kakao_store").exists():
            migrate_kakao_store(source_root, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()

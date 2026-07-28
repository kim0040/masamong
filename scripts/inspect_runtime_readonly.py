#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""현재 Masamong 런타임 DB의 비식별 read-only fingerprint를 JSON으로 출력한다.

이 도구는 운영 DB를 변경하지 않는다.

* SQLite는 URI ``mode=ro``로만 연다.
* TiDB는 쓰기가 실제로 거부되는 stale-read transaction을 시작한다.
* 실행 가능한 SQL은 코드에 고정된 SELECT/읽기 PRAGMA로 제한한다.
* commit, DDL, 사용자별 값, 대화 원문, 자격증명은 다루거나 출력하지 않는다.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Iterator, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import pymysql  # noqa: E402
from database.compat_db import TiDBSettings  # noqa: E402
from utils.privacy_consent import (  # noqa: E402
    FORTUNE_SCOPE,
    SCHOOL_NOTICE_SCOPE,
    TRANSFER_NOTICE_SCOPE,
    get_policy,
)


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}"
    r"(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?"
    r"(?:Z|[+-]\d{2}:\d{2})?)?$"
)
_TIDB_READ_ONLY_TRANSACTION_SQL = (
    "START TRANSACTION READ ONLY AS OF TIMESTAMP "
    "NOW() - INTERVAL 5 SECOND"
)

_BASE_REQUIRED_TABLES = frozenset(
    {
        "conversation_history",
        "guild_settings",
        "locations",
        "privacy_consents",
        "privacy_consent_events",
        "user_profiles",
        "user_activity_log",
        "linkup_usage_log",
    }
)
_EXPLICIT_PROFILE_TABLES = frozenset(
    {
        "user_activity",
        "conversation_windows",
        "system_counters",
        "api_call_log",
        "analytics_log",
        "conversation_history_archive",
        "user_preferences",
        "dm_usage_logs",
        "channel_summary_state",
    }
)
_SCHOOL_NOTICE_TABLES = frozenset(
    {
        "school_notice_profiles",
        "school_notice_feedback",
        "school_notice_deliveries",
        "school_notice_batch_runs",
        "school_notice_delivery_runs",
    }
)
_CONSENT_PROMPT_TABLES = frozenset({"privacy_consent_prompts"})
_TRANSFER_NOTICE_TABLES = frozenset(
    {
        "transfer_notice_subscriptions",
        "transfer_notice_deliveries",
    }
)

# COUNT와 MAX를 한 문장으로 실행해 큰 테이블당 DB 왕복/스캔을 한 번으로 제한한다.
_TABLE_TIMESTAMP_COLUMNS = {
    "guild_settings": "updated_at",
    "user_activity": "last_active_at",
    "user_activity_log": "created_at",
    "linkup_usage_log": "used_at",
    "conversation_history": "created_at",
    "conversation_windows": "anchor_timestamp",
    "system_counters": "last_reset_at",
    "api_call_log": "called_at",
    "analytics_log": "log_timestamp",
    "conversation_history_archive": "created_at",
    "user_preferences": "updated_at",
    "locations": None,
    "user_profiles": "created_at",
    "privacy_consents": "updated_at",
    "privacy_consent_events": "created_at",
    "dm_usage_logs": "reset_at",
    "channel_summary_state": "updated_at",
    "discord_chat_embeddings": "timestamp",
    "discord_memory_entries": "timestamp",
    "kakao_chunks": "start_date",
    "school_notice_profiles": "created_at",
    "school_notice_feedback": "created_at",
    "school_notice_deliveries": "delivered_at",
    "school_notice_batch_runs": "finished_at",
    "school_notice_delivery_runs": "updated_at",
    "privacy_consent_prompts": "updated_at",
    "transfer_notice_subscriptions": "updated_at",
    "transfer_notice_deliveries": "updated_at",
}

_CORE_EXPECTED_COLUMNS = {
    "guild_settings": frozenset(
        {
            "guild_id",
            "ai_enabled",
            "ai_allowed_channels",
            "persona_text",
            "language",
        }
    ),
    "user_profiles": frozenset(
        {
            "user_id",
            "birth_date",
            "birth_time",
            "gender",
            "birth_place",
            "subscription_active",
            "subscription_time",
            "pending_payload",
            "last_fortune_sent",
            "last_fortune_content",
            "created_at",
        }
    ),
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
    "channel_summary_state": frozenset(
        {
            "guild_id",
            "channel_id",
            "anchor_message_id",
            "summary_text",
            "updated_at",
        }
    ),
}
_TIDB_EXPECTED_COLUMNS = {
    "discord_chat_embeddings": frozenset(
        {
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
    ),
    "discord_memory_entries": frozenset(
        {
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
    ),
    "kakao_chunks": frozenset(
        {
            "id",
            "room_key",
            "source_room_label",
            "chunk_id",
            "session_id",
            "start_date",
            "message_count",
            "summary",
            "text_long",
            "embedding",
        }
    ),
}
_SCHOOL_NOTICE_EXPECTED_COLUMNS = {
    "school_notice_profiles": frozenset(
        {
            "user_id",
            "user_key",
            "school_id",
            "profile_json",
            "profile_version",
            "enabled",
            "delivery_time",
            "created_at",
            "updated_at",
        }
    ),
    "school_notice_deliveries": frozenset(
        {
            "id",
            "user_key",
            "digest_date",
            "notice_id",
            "revision_count",
            "status",
            "failure_reason",
            "attempt_count",
            "delivered_at",
        }
    ),
    "school_notice_delivery_runs": frozenset(
        {
            "user_key",
            "digest_date",
            "status",
            "attempt_count",
            "next_attempt_at",
            "last_error",
            "finished_at",
            "updated_at",
        }
    ),
}
_CONSENT_PROMPT_EXPECTED_COLUMNS = {
    "privacy_consent_prompts": frozenset(
        {
            "user_id",
            "scope",
            "policy_version",
            "notice_hash",
            "status",
            "attempt_count",
            "next_attempt_at",
            "sent_at",
            "last_error",
            "updated_at",
        }
    ),
}
_TRANSFER_NOTICE_EXPECTED_COLUMNS = {
    "transfer_notice_subscriptions": frozenset(
        {
            "user_id",
            "schools_json",
            "enabled",
            "created_at",
            "updated_at",
        }
    ),
    "transfer_notice_deliveries": frozenset(
        {
            "user_id",
            "run_id",
            "source_id",
            "external_id",
            "revision",
            "payload_json",
            "status",
            "attempt_count",
            "next_attempt_at",
            "delivered_at",
            "last_error",
            "updated_at",
        }
    ),
}


class InspectionError(RuntimeError):
    """비밀값을 포함하지 않는 운영자용 fail-closed 오류."""

    def __init__(self, code: str, message: str, *, exit_code: int = 2):
        super().__init__(message)
        self.code = code
        self.public_message = message
        self.exit_code = exit_code


@dataclass
class _ReadOnlySession:
    backend: str
    connection: Any

    @property
    def placeholder(self) -> str:
        return "%s" if self.backend == "tidb" else "?"

    def read(self, sql: str, params: Sequence[Any] = ()) -> list[Any]:
        _assert_read_statement(sql, backend=self.backend)
        if self.backend == "tidb":
            with self.connection.cursor() as cursor:
                cursor.execute(sql, tuple(params))
                return list(cursor.fetchall())
        cursor = self.connection.execute(sql, tuple(params))
        try:
            return list(cursor.fetchall())
        finally:
            cursor.close()


def _assert_read_statement(sql: str, *, backend: str) -> None:
    """향후 유지보수 중 실수로 쓰기 SQL이 추가되어도 실행 전에 차단한다."""
    normalized = str(sql or "").strip()
    if ";" in normalized:
        raise InspectionError("unsafe_sql", "여러 SQL 문장은 실행할 수 없습니다.")
    keyword = normalized.split(None, 1)[0].upper() if normalized else ""
    allowed = {"SELECT"}
    if backend == "sqlite":
        allowed.add("PRAGMA")
    if keyword not in allowed:
        raise InspectionError("unsafe_sql", "읽기 전용 SQL만 실행할 수 있습니다.")
    if keyword == "PRAGMA" and "=" in normalized:
        raise InspectionError("unsafe_sql", "값을 변경하는 PRAGMA는 실행할 수 없습니다.")
    if re.search(
        r"\b(?:INTO\s+(?:OUTFILE|DUMPFILE)|FOR\s+UPDATE|"
        r"LOCK\s+IN\s+SHARE\s+MODE)\b",
        normalized,
        flags=re.IGNORECASE,
    ):
        raise InspectionError("unsafe_sql", "잠금 또는 파일 출력을 동반한 SELECT는 금지됩니다.")


def _quote_identifier(value: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(value):
        raise InspectionError("unsafe_identifier", "허용되지 않은 DB 식별자입니다.")
    return f"`{value}`"


def _row_value(row: Any, key: str, index: int = 0) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return row[index]


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


@contextmanager
def _open_sqlite_readonly(path: Path) -> Iterator[_ReadOnlySession]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise InspectionError("database_unavailable", "SQLite DB 파일을 찾을 수 없습니다.")
    uri = resolved.as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, isolation_level=None)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise InspectionError(
            "database_connection_failed",
            "SQLite read-only 연결에 실패했습니다.",
        ) from exc
    try:
        yield _ReadOnlySession("sqlite", connection)
    finally:
        connection.close()


@contextmanager
def _open_tidb_readonly() -> Iterator[_ReadOnlySession]:
    try:
        connection = pymysql.connect(**_tidb_settings().to_connect_kwargs())
    except Exception as exc:
        raise InspectionError(
            "database_connection_failed",
            "TiDB 연결에 실패했습니다.",
        ) from exc

    try:
        try:
            with connection.cursor() as cursor:
                # 일반 TiDB `READ ONLY`는 버전에 따라 문법만 허용하고 쓰기를
                # 막지 않는다. AS OF TIMESTAMP stale-read transaction은 쓰기를
                # 실제로 거부하므로, 이 구문이 실패하면 일반 연결로 강등하지 않는다.
                cursor.execute(_TIDB_READ_ONLY_TRANSACTION_SQL)
        except Exception as exc:
            raise InspectionError(
                "tidb_read_only_unsupported",
                "TiDB가 강제 read-only transaction을 시작하지 못해 검사를 중단했습니다.",
            ) from exc
        yield _ReadOnlySession("tidb", connection)
    finally:
        # commit/DDL은 호출하지 않는다. 연결 종료 시 열린 읽기 트랜잭션이 폐기된다.
        connection.close()


def _configured_sqlite_path() -> Path:
    raw = str(config.DATABASE_FILE or "").strip()
    if not raw or raw == ":memory:" or raw.startswith("file:"):
        raise InspectionError(
            "unsupported_sqlite_target",
            "파일 기반 SQLite DB만 read-only 검사할 수 있습니다.",
        )
    return Path(raw).expanduser().resolve()


def _validate_expectations(expected_profile: str, expected_db: str) -> str:
    profile = str(config.PROFILE)
    instance = str(config.INSTANCE_NAME)
    if expected_profile != profile:
        raise InspectionError(
            "expected_profile_mismatch",
            "확인한 프로필과 현재 설정 프로필이 일치하지 않습니다.",
        )
    if expected_profile != instance:
        raise InspectionError(
            "expected_instance_mismatch",
            "확인한 프로필과 현재 인스턴스가 일치하지 않습니다.",
        )

    backend = str(config.DB_BACKEND).strip().lower()
    if backend == "tidb":
        configured_db = str(config.TIDB_NAME)
        if expected_db != configured_db:
            raise InspectionError(
                "expected_database_mismatch",
                "확인한 DB와 현재 설정 DB가 일치하지 않습니다.",
            )
    elif backend == "sqlite":
        configured_path = _configured_sqlite_path()
        expected_raw = str(expected_db or "").strip()
        if (
            not expected_raw
            or expected_raw == ":memory:"
            or Path(expected_raw).expanduser().resolve() != configured_path
        ):
            raise InspectionError(
                "expected_database_mismatch",
                "확인한 DB와 현재 설정 DB가 일치하지 않습니다.",
            )
    else:
        raise InspectionError("unsupported_backend", "지원하지 않는 DB backend입니다.")
    return backend


def _required_tables(backend: str) -> list[str]:
    tables = set(_BASE_REQUIRED_TABLES)
    if bool(config.REQUIRE_EXPLICIT_PROFILE):
        tables.update(_EXPLICIT_PROFILE_TABLES)
    if backend == "tidb":
        tables.update({"discord_chat_embeddings", "discord_memory_entries"})
        if bool(config.KAKAO_MEMORY_ENABLED):
            tables.add("kakao_chunks")
    if bool(config.SCHOOL_NOTICE_ENABLED):
        tables.update(_SCHOOL_NOTICE_TABLES)
    if (
        (
            bool(config.FORTUNE_MORNING_BRIEFING_ENABLED)
            and "fortune_cog" not in config.DISABLED_COGS
        )
        or bool(config.SCHOOL_NOTICE_ENABLED)
        or bool(config.TRANSFER_NOTICE_ENABLED)
    ):
        tables.update(_CONSENT_PROMPT_TABLES)
    if bool(config.TRANSFER_NOTICE_ENABLED):
        tables.update(_TRANSFER_NOTICE_TABLES)
    return sorted(tables)


def _expected_columns(backend: str) -> dict[str, frozenset[str]]:
    expected = {"guild_settings": _CORE_EXPECTED_COLUMNS["guild_settings"]}
    if bool(config.REQUIRE_EXPLICIT_PROFILE):
        expected.update(_CORE_EXPECTED_COLUMNS)
        if backend == "tidb":
            expected["discord_chat_embeddings"] = _TIDB_EXPECTED_COLUMNS[
                "discord_chat_embeddings"
            ]
            expected["discord_memory_entries"] = _TIDB_EXPECTED_COLUMNS[
                "discord_memory_entries"
            ]
            if bool(config.KAKAO_MEMORY_ENABLED):
                expected["kakao_chunks"] = _TIDB_EXPECTED_COLUMNS["kakao_chunks"]
    if bool(config.SCHOOL_NOTICE_ENABLED):
        expected.update(_SCHOOL_NOTICE_EXPECTED_COLUMNS)
    if (
        (
            bool(config.FORTUNE_MORNING_BRIEFING_ENABLED)
            and "fortune_cog" not in config.DISABLED_COGS
        )
        or bool(config.SCHOOL_NOTICE_ENABLED)
        or bool(config.TRANSFER_NOTICE_ENABLED)
    ):
        expected.update(_CONSENT_PROMPT_EXPECTED_COLUMNS)
    if bool(config.TRANSFER_NOTICE_ENABLED):
        expected.update(_TRANSFER_NOTICE_EXPECTED_COLUMNS)
    return expected


def _actual_database(session: _ReadOnlySession, configured_sqlite: Path | None) -> str:
    if session.backend == "tidb":
        rows = session.read("SELECT DATABASE() AS database_name")
        actual = str(_row_value(rows[0], "database_name") or "") if rows else ""
        if not actual:
            raise InspectionError("database_identity_missing", "TiDB DATABASE()가 비어 있습니다.")
        return actual

    rows = session.read("PRAGMA database_list")
    main_row = next(
        (row for row in rows if str(_row_value(row, "name", 1)) == "main"),
        None,
    )
    actual_path = (
        Path(str(_row_value(main_row, "file", 2))).expanduser().resolve()
        if main_row is not None and _row_value(main_row, "file", 2)
        else None
    )
    if configured_sqlite is None or actual_path != configured_sqlite:
        raise InspectionError(
            "database_identity_mismatch",
            "열린 SQLite DB가 현재 설정 대상과 일치하지 않습니다.",
        )
    # 서버 파일 경로는 출력하지 않고 파일명만 fingerprint에 포함한다.
    return actual_path.name


def _existing_tables(
    session: _ReadOnlySession,
    required_tables: Sequence[str],
) -> set[str]:
    placeholders = ", ".join(session.placeholder for _ in required_tables)
    if session.backend == "tidb":
        sql = (
            "SELECT TABLE_NAME AS table_name "
            "FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() "
            f"AND TABLE_NAME IN ({placeholders})"
        )
    else:
        sql = (
            "SELECT name AS table_name FROM sqlite_master "
            f"WHERE type = 'table' AND name IN ({placeholders})"
        )
    rows = session.read(sql, required_tables)
    return {str(_row_value(row, "table_name")) for row in rows}


def _schema_columns(
    session: _ReadOnlySession,
    existing_tables: Sequence[str],
) -> dict[str, list[str]]:
    if not existing_tables:
        return {}
    if session.backend == "tidb":
        placeholders = ", ".join(session.placeholder for _ in existing_tables)
        rows = session.read(
            "SELECT TABLE_NAME AS table_name, COLUMN_NAME AS column_name "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() "
            f"AND TABLE_NAME IN ({placeholders}) "
            "ORDER BY TABLE_NAME, ORDINAL_POSITION",
            existing_tables,
        )
        result = {table: [] for table in existing_tables}
        for row in rows:
            result[str(_row_value(row, "table_name"))].append(
                str(_row_value(row, "column_name", 1))
            )
        return result

    result: dict[str, list[str]] = {}
    for table in existing_tables:
        rows = session.read(f"PRAGMA table_info({_quote_identifier(table)})")
        result[table] = [str(_row_value(row, "name", 1)) for row in rows]
    return result


def _safe_timestamp(value: Any) -> tuple[str | None, bool]:
    if value is None or str(value).strip() == "":
        return None, True
    text = str(value).strip()
    if len(text) > 64 or not _SAFE_TIMESTAMP_RE.fullmatch(text):
        return None, False
    try:
        if len(text) == 10:
            return date.fromisoformat(text).isoformat(), True
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None, False
    if parsed.tzinfo is not None:
        return (
            parsed.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
            True,
        )
    return parsed.isoformat(timespec="microseconds"), True


def _table_statistics(
    session: _ReadOnlySession,
    existing_tables: Sequence[str],
    columns: dict[str, list[str]],
) -> dict[str, dict[str, Any]]:
    statistics: dict[str, dict[str, Any]] = {}
    for table in sorted(existing_tables):
        timestamp_column = _TABLE_TIMESTAMP_COLUMNS.get(table)
        table_sql = _quote_identifier(table)
        if timestamp_column and timestamp_column in set(columns.get(table, [])):
            column_sql = _quote_identifier(timestamp_column)
            rows = session.read(
                f"SELECT COUNT(*) AS row_count, "
                f"MAX({column_sql}) AS max_timestamp FROM {table_sql}"
            )
            raw_timestamp = _row_value(rows[0], "max_timestamp", 1) if rows else None
            max_timestamp, valid_timestamp = _safe_timestamp(raw_timestamp)
        else:
            rows = session.read(f"SELECT COUNT(*) AS row_count FROM {table_sql}")
            max_timestamp = None
            valid_timestamp = timestamp_column is None
        row_count = int(_row_value(rows[0], "row_count") or 0) if rows else 0
        statistics[table] = {
            "row_count": row_count,
            "max_timestamp_column": timestamp_column,
            "max_timestamp": max_timestamp,
            "max_timestamp_valid": valid_timestamp,
        }
    return statistics


def _fortune_subscription_counts(
    session: _ReadOnlySession,
    existing_tables: set[str],
    columns: dict[str, list[str]],
) -> dict[str, int | None]:
    profile_columns = set(columns.get("user_profiles", []))
    if (
        "user_profiles" not in existing_tables
        or "subscription_active" not in profile_columns
    ):
        return {
            "active": None,
            "active_with_current_consent": None,
            "active_without_current_consent": None,
        }

    rows = session.read(
        "SELECT COUNT(*) AS active_count "
        "FROM `user_profiles` WHERE `subscription_active` = 1"
    )
    active = int(_row_value(rows[0], "active_count") or 0) if rows else 0

    consent_columns = set(columns.get("privacy_consents", []))
    required_consent_columns = {
        "user_id",
        "scope",
        "policy_version",
        "notice_hash",
        "status",
        "granted_at",
        "withdrawn_at",
    }
    if (
        "privacy_consents" not in existing_tables
        or not required_consent_columns.issubset(consent_columns)
    ):
        return {
            "active": active,
            "active_with_current_consent": None,
            "active_without_current_consent": None,
        }

    policy = get_policy(FORTUNE_SCOPE)
    placeholder = session.placeholder
    rows = session.read(
        "SELECT COUNT(*) AS eligible_count "
        "FROM `user_profiles` AS up "
        "JOIN `privacy_consents` AS pc ON pc.`user_id` = up.`user_id` "
        "WHERE up.`subscription_active` = 1 "
        f"AND pc.`scope` = {placeholder} "
        f"AND pc.`policy_version` = {placeholder} "
        f"AND pc.`notice_hash` = {placeholder} "
        f"AND pc.`status` = {placeholder} "
        "AND pc.`granted_at` IS NOT NULL "
        "AND pc.`withdrawn_at` IS NULL",
        (policy.scope, policy.version, policy.notice_hash, "granted"),
    )
    eligible = int(_row_value(rows[0], "eligible_count") or 0) if rows else 0
    return {
        "active": active,
        "active_with_current_consent": eligible,
        "active_without_current_consent": max(0, active - eligible),
    }


def _notice_subscription_counts(
    session: _ReadOnlySession,
    existing_tables: set[str],
    columns: dict[str, list[str]],
) -> dict[str, dict[str, int | None]]:
    """학교·편입 구독은 사용자 식별자 없이 활성/동의 수만 집계한다."""
    consent_columns = set(columns.get("privacy_consents", []))
    consent_ready = (
        "privacy_consents" in existing_tables
        and {
            "user_id",
            "scope",
            "policy_version",
            "notice_hash",
            "status",
            "granted_at",
            "withdrawn_at",
        }.issubset(consent_columns)
    )
    result: dict[str, dict[str, int | None]] = {}
    specs = (
        ("school_notice", "school_notice_profiles", SCHOOL_NOTICE_SCOPE),
        (
            "transfer_notice",
            "transfer_notice_subscriptions",
            TRANSFER_NOTICE_SCOPE,
        ),
    )
    for label, table, scope in specs:
        table_columns = set(columns.get(table, []))
        if (
            table not in existing_tables
            or not {"user_id", "enabled"}.issubset(table_columns)
        ):
            result[label] = {
                "active": None,
                "active_with_current_consent": None,
                "active_without_current_consent": None,
            }
            continue
        table_sql = _quote_identifier(table)
        rows = session.read(
            f"SELECT COUNT(*) AS active_count FROM {table_sql} "
            "WHERE `enabled` = 1"
        )
        active = int(_row_value(rows[0], "active_count") or 0) if rows else 0
        if not consent_ready:
            result[label] = {
                "active": active,
                "active_with_current_consent": None,
                "active_without_current_consent": None,
            }
            continue
        policy = get_policy(scope)
        placeholder = session.placeholder
        rows = session.read(
            "SELECT COUNT(*) AS eligible_count "
            f"FROM {table_sql} AS source "
            "JOIN `privacy_consents` AS pc "
            "ON pc.`user_id` = source.`user_id` "
            "WHERE source.`enabled` = 1 "
            f"AND pc.`scope` = {placeholder} "
            f"AND pc.`policy_version` = {placeholder} "
            f"AND pc.`notice_hash` = {placeholder} "
            f"AND pc.`status` = {placeholder} "
            "AND pc.`granted_at` IS NOT NULL "
            "AND pc.`withdrawn_at` IS NULL",
            (policy.scope, policy.version, policy.notice_hash, "granted"),
        )
        eligible = (
            int(_row_value(rows[0], "eligible_count") or 0) if rows else 0
        )
        result[label] = {
            "active": active,
            "active_with_current_consent": eligible,
            "active_without_current_consent": max(0, active - eligible),
        }
    return result


def inspect_runtime(*, expected_profile: str, expected_db: str) -> dict[str, Any]:
    """설정 정체성을 먼저 확인한 뒤 aggregate/schema fingerprint를 수집한다."""
    backend = _validate_expectations(expected_profile, expected_db)
    configured_sqlite = _configured_sqlite_path() if backend == "sqlite" else None
    opener = (
        _open_sqlite_readonly(configured_sqlite)
        if configured_sqlite is not None
        else _open_tidb_readonly()
    )

    with opener as session:
        actual_database = _actual_database(session, configured_sqlite)
        if backend == "tidb" and actual_database != expected_db:
            raise InspectionError(
                "database_identity_mismatch",
                "TiDB DATABASE()와 확인한 DB가 일치하지 않습니다.",
            )

        required_tables = _required_tables(backend)
        existing = _existing_tables(session, required_tables)
        columns = _schema_columns(session, sorted(existing))
        missing_tables = sorted(set(required_tables) - existing)

        missing_columns: dict[str, list[str]] = {}
        for table, expected in _expected_columns(backend).items():
            if table not in existing:
                continue
            missing = sorted(expected - set(columns.get(table, [])))
            if missing:
                missing_columns[table] = missing

        table_stats = _table_statistics(session, sorted(existing), columns)
        fortune_counts = _fortune_subscription_counts(session, existing, columns)
        notice_counts = _notice_subscription_counts(session, existing, columns)

    healthy = not missing_tables and not missing_columns
    return {
        "ok": healthy,
        "runtime": {
            "profile": str(config.PROFILE),
            "instance": str(config.INSTANCE_NAME),
            "backend": backend,
            "expected_bot_user_id": int(config.EXPECTED_DISCORD_BOT_USER_ID or 0),
            "require_explicit_profile": bool(config.REQUIRE_EXPLICIT_PROFILE),
            "auto_migrate": bool(config.AUTO_MIGRATE),
            "remote_db_strict_mode": bool(config.REMOTE_DB_STRICT_MODE),
            "database_tls_required": bool(config.REQUIRE_DB_TLS)
            if backend == "tidb"
            else False,
            "kakao_memory_enabled": bool(config.KAKAO_MEMORY_ENABLED),
            "school_notice_enabled": bool(config.SCHOOL_NOTICE_ENABLED),
            "transfer_notice_enabled": bool(config.TRANSFER_NOTICE_ENABLED),
            "resource_limits": {
                "cpu_threads": int(config.CPU_THREAD_LIMIT),
                "executor_workers": int(config.EXECUTOR_WORKERS),
                "ai_max_concurrent_processing": int(
                    config.AI_MAX_CONCURRENT_PROCESSING
                ),
                "ai_queue_wait_timeout_seconds": int(
                    config.AI_QUEUE_WAIT_TIMEOUT_SECONDS
                ),
                "llm_max_concurrent_calls": int(config.LLM_MAX_CONCURRENT_CALLS),
                "llm_acquire_timeout_seconds": int(
                    config.LLM_ACQUIRE_TIMEOUT_SECONDS
                ),
                "llm_call_timeout_seconds": int(
                    config.LLM_CALL_TIMEOUT_SECONDS
                ),
                "embedding_max_concurrency": int(config.EMBEDDING_MAX_CONCURRENCY),
                "rag_max_background_tasks": int(config.RAG_MAX_BACKGROUND_TASKS),
                "rag_max_tracked_windows": int(config.RAG_MAX_TRACKED_WINDOWS),
                "kakao_api_max_concurrency": int(
                    config.KAKAO_API_MAX_CONCURRENCY
                ),
                "discord_max_messages": int(config.DISCORD_MAX_MESSAGES),
            },
        },
        "database": {
            "name": actual_database,
            "expected_match": True,
            "read_only": {
                "enforced": True,
                "mechanism": (
                    "tidb-stale-read-transaction"
                    if backend == "tidb"
                    else "sqlite-uri-mode-ro"
                ),
            },
        },
        "schema": {
            "required_table_count": len(required_tables),
            "present_table_count": len(existing),
            "tables": {table: table in existing for table in required_tables},
            "missing_tables": missing_tables,
            "columns": columns,
            "missing_columns": missing_columns,
        },
        "table_stats": table_stats,
        "fortune_subscriptions": fortune_counts,
        "notice_subscriptions": notice_counts,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Masamong 운영 DB의 비식별 read-only fingerprint를 JSON으로 출력"
    )
    parser.add_argument(
        "--expected-profile",
        required=True,
        help="현재 실행 프로필을 운영자가 직접 재확인",
    )
    parser.add_argument(
        "--expected-db",
        required=True,
        help="TiDB DB 이름 또는 SQLite DB 파일 경로를 운영자가 직접 재확인",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = inspect_runtime(
            expected_profile=str(args.expected_profile),
            expected_db=str(args.expected_db),
        )
    except InspectionError as exc:
        result = {
            "ok": False,
            "error": {
                "code": exc.code,
                "message": exc.public_message,
            },
        }
        exit_code = exc.exit_code
    except Exception:
        # 드라이버 예외에는 접속 대상 등 운영 정보가 포함될 수 있으므로 원문을
        # JSON/traceback으로 노출하지 않는다.
        result = {
            "ok": False,
            "error": {
                "code": "inspection_failed",
                "message": "read-only fingerprint 검사에 실패했습니다.",
            },
        }
        exit_code = 2
    else:
        exit_code = 0 if result["ok"] else 3

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

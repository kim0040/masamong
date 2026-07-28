from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

import config
from scripts import inspect_runtime_readonly as inspector
from utils.privacy_consent import FORTUNE_SCOPE, get_policy


ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prepare_runtime_db(path: Path) -> tuple[int, str, str]:
    schema = (ROOT / "database" / "schema.sql").read_text(encoding="utf-8")
    private_user_id = 987654321012345678
    private_content = "READONLY-INSPECTOR-PRIVATE-CONTENT"
    policy = get_policy(FORTUNE_SCOPE)

    connection = sqlite3.connect(path)
    try:
        connection.executescript(schema)
        connection.execute(
            """
            INSERT INTO conversation_history (
                message_id, guild_id, channel_id, user_id, user_name,
                content, is_bot, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                111,
                222,
                333,
                private_user_id,
                "PRIVATE-NAME",
                private_content,
                0,
                "2026-07-28T01:02:03+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO user_profiles (
                user_id, subscription_active, created_at
            ) VALUES (?, 1, ?)
            """,
            (private_user_id, "2026-07-28T01:02:04+00:00"),
        )
        connection.execute(
            """
            INSERT INTO privacy_consents (
                user_id, scope, policy_version, notice_hash, status,
                granted_at, withdrawn_at, updated_at
            ) VALUES (?, ?, ?, ?, 'granted', ?, NULL, ?)
            """,
            (
                private_user_id,
                policy.scope,
                policy.version,
                policy.notice_hash,
                "2026-07-28T01:02:05+00:00",
                "2026-07-28T01:02:05+00:00",
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return private_user_id, private_content, policy.notice_hash


def _row_count(path: Path, table: str) -> int:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
        return int(row[0])
    finally:
        connection.close()


def test_cli_sqlite_is_content_preserving_and_emits_only_safe_aggregates(
    monkeypatch,
    tmp_path,
    capsys,
):
    database = tmp_path / "general-runtime.db"
    private_user_id, private_content, policy_hash = _prepare_runtime_db(database)
    before_hash = _sha256(database)
    before_rows = _row_count(database, "conversation_history")

    monkeypatch.setattr(config, "DATABASE_FILE", str(database))
    monkeypatch.setattr(config, "DB_BACKEND", "sqlite")
    monkeypatch.setattr(config, "PROFILE", "general")
    monkeypatch.setattr(config, "INSTANCE_NAME", "general")
    monkeypatch.setattr(config, "REQUIRE_EXPLICIT_PROFILE", True)
    monkeypatch.setattr(config, "SCHOOL_NOTICE_ENABLED", False)
    monkeypatch.setattr(config, "TOKEN", "NEVER-PRINT-THIS-TOKEN", raising=False)

    exit_code = inspector.main(
        [
            "--expected-profile",
            "general",
            "--expected-db",
            str(database),
        ]
    )
    output = capsys.readouterr().out
    result = json.loads(output)

    assert exit_code == 0
    assert result["ok"] is True
    assert result["database"]["read_only"] == {
        "enforced": True,
        "mechanism": "sqlite-uri-mode-ro",
    }
    assert result["runtime"]["resource_limits"]["llm_acquire_timeout_seconds"] == (
        config.LLM_ACQUIRE_TIMEOUT_SECONDS
    )
    assert result["runtime"]["resource_limits"]["llm_call_timeout_seconds"] == (
        config.LLM_CALL_TIMEOUT_SECONDS
    )
    assert result["table_stats"]["conversation_history"]["row_count"] == 1
    assert result["table_stats"]["conversation_history"]["max_timestamp"] == (
        "2026-07-28T01:02:03.000000Z"
    )
    assert result["fortune_subscriptions"] == {
        "active": 1,
        "active_with_current_consent": 1,
        "active_without_current_consent": 0,
    }
    assert result["schema"]["missing_tables"] == []
    assert result["schema"]["missing_columns"] == {}

    assert str(private_user_id) not in output
    assert private_content not in output
    assert "PRIVATE-NAME" not in output
    assert "NEVER-PRINT-THIS-TOKEN" not in output
    assert policy_hash not in output
    assert _sha256(database) == before_hash
    assert _row_count(database, "conversation_history") == before_rows


@pytest.mark.parametrize(
    ("expected_profile", "expected_db", "error_code"),
    [
        ("masamo", "unused", "expected_profile_mismatch"),
        ("general", "wrong-runtime.db", "expected_database_mismatch"),
    ],
)
def test_expectation_mismatch_fails_before_opening_database(
    monkeypatch,
    tmp_path,
    capsys,
    expected_profile,
    expected_db,
    error_code,
):
    database = tmp_path / "general-runtime.db"
    _prepare_runtime_db(database)
    monkeypatch.setattr(config, "DATABASE_FILE", str(database))
    monkeypatch.setattr(config, "DB_BACKEND", "sqlite")
    monkeypatch.setattr(config, "PROFILE", "general")
    monkeypatch.setattr(config, "INSTANCE_NAME", "general")

    def unexpected_open(_path):
        raise AssertionError("identity mismatch must stop before DB open")

    monkeypatch.setattr(inspector, "_open_sqlite_readonly", unexpected_open)

    exit_code = inspector.main(
        [
            "--expected-profile",
            expected_profile,
            "--expected-db",
            expected_db,
        ]
    )
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert result == {
        "ok": False,
        "error": {
            "code": error_code,
            "message": result["error"]["message"],
        },
    }


def test_sql_guard_rejects_write_ddl_and_mutating_pragma():
    for sql, backend in (
        ("INSERT INTO user_profiles VALUES (1)", "sqlite"),
        ("CREATE TABLE accidental_write (id INTEGER)", "tidb"),
        ("PRAGMA user_version = 2", "sqlite"),
        ("SELECT * FROM user_profiles FOR UPDATE", "tidb"),
    ):
        with pytest.raises(inspector.InspectionError, match="읽기|PRAGMA|잠금"):
            inspector._assert_read_statement(sql, backend=backend)


def test_tidb_readonly_request_failure_has_no_fallback(monkeypatch):
    events: list[str] = []

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql):
            events.append(sql)
            raise RuntimeError("unsupported")

    class _Connection:
        def cursor(self):
            return _Cursor()

        def close(self):
            events.append("close")

        def commit(self):
            raise AssertionError("read-only inspector must never commit")

    class _Settings:
        def to_connect_kwargs(self):
            return {}

    monkeypatch.setattr(inspector, "_tidb_settings", lambda: _Settings())
    monkeypatch.setattr(inspector.pymysql, "connect", lambda **_kwargs: _Connection())

    with pytest.raises(
        inspector.InspectionError,
        match="read-only transaction",
    ) as exc_info:
        with inspector._open_tidb_readonly():
            raise AssertionError("unsupported read-only mode must not yield")

    assert exc_info.value.code == "tidb_read_only_unsupported"
    assert events == [inspector._TIDB_READ_ONLY_TRANSACTION_SQL, "close"]

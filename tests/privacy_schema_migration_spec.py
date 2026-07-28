import argparse
import sqlite3
from pathlib import Path

import aiosqlite
import pytest

from scripts import apply_privacy_consent_schema as migration


def _args(
    *,
    profile: str,
    database: str,
    apply: bool = False,
    confirm: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        expected_profile=profile,
        expected_db=database,
        apply=apply,
        confirm=confirm,
    )


def _configure_sqlite(monkeypatch, tmp_path) -> Path:
    env_file = tmp_path / "masamo.env"
    env_file.write_text("MASAMONG_PROFILE=masamo\n", encoding="utf-8")
    database = tmp_path / "masamo.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE existing_data (id INTEGER PRIMARY KEY, value TEXT)"
        )
        connection.execute(
            "INSERT INTO existing_data (id, value) VALUES (1, 'preserve-me')"
        )
        connection.commit()
    finally:
        connection.close()

    monkeypatch.setattr(migration.config, "ENV_FILE_PATH", env_file)
    monkeypatch.setattr(migration.config, "REQUIRE_EXPLICIT_PROFILE", True)
    monkeypatch.setattr(migration.config, "PROFILE", "masamo")
    monkeypatch.setattr(migration.config, "INSTANCE_NAME", "masamo")
    monkeypatch.setattr(migration.config, "AUTO_MIGRATE", False)
    monkeypatch.setattr(migration.config, "DB_BACKEND", "sqlite")
    monkeypatch.setattr(migration.config, "DATABASE_FILE", str(database))
    return database


def _configure_tidb(monkeypatch, tmp_path) -> None:
    env_file = tmp_path / "masamo.env"
    env_file.write_text("MASAMONG_PROFILE=masamo\n", encoding="utf-8")
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("test-ca", encoding="utf-8")

    monkeypatch.setattr(migration.config, "ENV_FILE_PATH", env_file)
    monkeypatch.setattr(migration.config, "REQUIRE_EXPLICIT_PROFILE", True)
    monkeypatch.setattr(migration.config, "PROFILE", "masamo")
    monkeypatch.setattr(migration.config, "INSTANCE_NAME", "masamo")
    monkeypatch.setattr(migration.config, "AUTO_MIGRATE", False)
    monkeypatch.setattr(migration.config, "DB_BACKEND", "tidb")
    monkeypatch.setattr(migration.config, "REMOTE_DB_STRICT_MODE", True)
    monkeypatch.setattr(migration.config, "REQUIRE_DB_TLS", True)
    monkeypatch.setattr(migration.config, "TIDB_SSL_CA", str(ca_file))
    monkeypatch.setattr(migration.config, "TIDB_SSL_VERIFY_IDENTITY", True)
    monkeypatch.setattr(migration.config, "TIDB_NAME", "masamong")
    monkeypatch.setattr(migration.config, "EXPECTED_DB_NAME", "masamong")


@pytest.mark.parametrize("backend", ["sqlite", "tidb"])
def test_schema_generator_only_contains_two_additive_create_tables(backend):
    statements = migration.schema_statements(backend)

    assert len(statements) == 2
    assert {
        "privacy_consents",
        "privacy_consent_events",
    } == {
        migration._CREATE_TABLE_PATTERN.match(statement).group(1)
        for statement in statements
    }
    assert all("CREATE TABLE IF NOT EXISTS" in statement for statement in statements)
    assert all(
        forbidden not in f" {statement.upper()} "
        for statement in statements
        for forbidden in (
            " ALTER ",
            " DELETE ",
            " DROP ",
            " INSERT ",
            " REPLACE ",
            " TRUNCATE ",
            " UPDATE ",
        )
    )


def test_additive_guard_rejects_mutation_and_multiple_statements():
    with pytest.raises(ValueError):
        migration.assert_additive_only(
            (
                migration.schema_statements("sqlite")[0],
                "CREATE TABLE IF NOT EXISTS privacy_consent_events (id INTEGER); "
                "DELETE FROM existing_data",
            )
        )
    with pytest.raises(ValueError):
        migration.assert_additive_only(
            ("DROP TABLE privacy_consents", "DROP TABLE privacy_consent_events")
        )


def test_target_validation_requires_exact_profile_db_and_confirmation(
    monkeypatch,
    tmp_path,
):
    database = _configure_sqlite(monkeypatch, tmp_path)
    expected = str(database.resolve())

    with pytest.raises(SystemExit, match="expected-profile"):
        migration.validate_target(
            _args(profile="general", database=expected)
        )
    with pytest.raises(SystemExit, match="expected-db"):
        migration.validate_target(
            _args(profile="masamo", database=str(tmp_path / "other.db"))
        )
    with pytest.raises(SystemExit, match="confirm"):
        migration.validate_target(
            _args(
                profile="masamo",
                database=expected,
                apply=True,
                confirm="yes",
            )
        )

    phrase = migration.confirmation_phrase(
        profile="masamo",
        backend="sqlite",
        database=expected,
    )
    assert migration.validate_target(
        _args(
            profile="masamo",
            database=expected,
            apply=True,
            confirm=phrase,
        )
    ) == phrase


def test_tidb_target_requires_strict_tls_and_exact_database(monkeypatch, tmp_path):
    _configure_tidb(monkeypatch, tmp_path)

    phrase = migration.validate_target(
        _args(profile="masamo", database="masamong")
    )
    assert "database=masamong" in phrase

    monkeypatch.setattr(migration.config, "TIDB_SSL_VERIFY_IDENTITY", False)
    with pytest.raises(SystemExit, match="strict remote TLS"):
        migration.validate_target(
            _args(profile="masamo", database="masamong")
        )


@pytest.mark.asyncio
async def test_default_dry_run_never_connects_or_changes_existing_db(
    monkeypatch,
    tmp_path,
):
    database = _configure_sqlite(monkeypatch, tmp_path)

    async def _unexpected_connection():
        raise AssertionError("dry-run must not connect")

    monkeypatch.setattr(migration, "open_configured_db", _unexpected_connection)
    result = await migration.run(
        _args(profile="masamo", database=str(database.resolve()))
    )

    assert result == 0
    connection = sqlite3.connect(database)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert "privacy_consents" not in tables
        assert connection.execute(
            "SELECT value FROM existing_data WHERE id = 1"
        ).fetchone() == ("preserve-me",)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_sqlite_apply_adds_only_consent_schema_and_preserves_existing_rows(
    tmp_path,
):
    database = tmp_path / "existing.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE existing_data (id INTEGER PRIMARY KEY, value TEXT)"
        )
        connection.execute(
            "INSERT INTO existing_data (id, value) VALUES (1, 'preserve-me')"
        )
        connection.commit()
    finally:
        connection.close()

    db = await aiosqlite.connect(database)
    try:
        await migration.apply_schema(db, backend="sqlite")
        await migration.apply_schema(db, backend="sqlite")
    finally:
        await db.close()

    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT value FROM existing_data WHERE id = 1"
        ).fetchone() == ("preserve-me",)
        assert {
            row[0]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name LIKE 'privacy_%'
                """
            ).fetchall()
        } == {"privacy_consents", "privacy_consent_events"}
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_tidb_apply_executes_only_two_fixed_create_statements(monkeypatch):
    class _FakeDB:
        def __init__(self):
            self.statements = []
            self.commits = 0

        async def execute(self, statement):
            self.statements.append(statement)

        async def commit(self):
            self.commits += 1

        async def rollback(self):
            raise AssertionError("successful additive apply must not rollback")

    async def _columns(_db, table_name):
        return list(migration.PRIVACY_TABLE_COLUMNS[table_name])

    monkeypatch.setattr(migration, "get_table_columns", _columns)
    db = _FakeDB()

    await migration.apply_schema(db, backend="tidb")

    assert db.statements == list(migration.schema_statements("tidb"))
    assert db.commits == 1
    assert all(
        statement.startswith("CREATE TABLE IF NOT EXISTS")
        for statement in db.statements
    )

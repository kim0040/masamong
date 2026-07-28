import argparse
import sqlite3
from pathlib import Path

import aiosqlite
import pytest

from scripts import apply_school_notice_schema as migration


def _args(
    *,
    database: str,
    apply: bool = False,
    confirm: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        expected_profile="masamo",
        expected_db=database,
        apply=apply,
        confirm=confirm,
    )


def _configure_sqlite(monkeypatch, tmp_path: Path) -> Path:
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


@pytest.mark.parametrize(
    ("backend", "expected_count"),
    [("sqlite", 6), ("tidb", 5)],
)
def test_schema_generator_is_additive_and_fixed(backend, expected_count):
    statements = migration.schema_statements(backend)

    assert len(statements) == expected_count
    assert sum(
        statement.upper().startswith("CREATE TABLE IF NOT EXISTS")
        for statement in statements
    ) == 5
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


@pytest.mark.asyncio
async def test_default_dry_run_never_connects_or_changes_existing_db(
    monkeypatch,
    tmp_path,
):
    database = _configure_sqlite(monkeypatch, tmp_path)

    async def unexpected_connection():
        raise AssertionError("dry-run must not connect")

    monkeypatch.setattr(
        migration,
        "open_configured_db",
        unexpected_connection,
    )
    assert await migration.run(
        _args(database=str(database.resolve()))
    ) == 0

    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT value FROM existing_data WHERE id = 1"
        ).fetchone() == ("preserve-me",)
        assert connection.execute(
            """
            SELECT COUNT(*) FROM sqlite_master
            WHERE type = 'table' AND name LIKE 'school_notice_%'
            """
        ).fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_sqlite_apply_is_idempotent_preserves_rows_and_matches_dedupe_key(
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
        await db.execute(
            """
            INSERT INTO school_notice_deliveries
                (user_key, digest_date, notice_id, revision_count, status,
                 failure_reason, delivered_at)
            VALUES ('discord-1', '2026-07-28', 10, 1, 'sent', NULL, 'now')
            """
        )
        await db.commit()
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute(
                """
                INSERT INTO school_notice_deliveries
                    (user_key, digest_date, notice_id, revision_count, status,
                     failure_reason, delivered_at)
                VALUES ('discord-1', '2026-07-29', 10, 1, 'sent', NULL, 'now')
                """
            )
        await db.rollback()
    finally:
        await db.close()

    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT value FROM existing_data WHERE id = 1"
        ).fetchone() == ("preserve-me",)
        tables = {
            row[0]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name LIKE 'school_notice_%'
                """
            ).fetchall()
        }
        assert tables == set(migration.SCHOOL_NOTICE_TABLE_COLUMNS)
        indexes = {
            row[0]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'index' AND tbl_name = 'school_notice_feedback'
                """
            ).fetchall()
        }
        assert "idx_school_notice_feedback_pending" in indexes
    finally:
        connection.close()

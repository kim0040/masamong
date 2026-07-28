import argparse
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from database.compat_db import TiDBSettings
from scripts.migrate_latest_data_to_tidb import (
    ConnectedTargetAuthorization,
    TargetIdentityConfirmation,
    preflight_sources,
    recreate_tables,
    validate_target_identity,
    verify_connected_target,
)


BACKUP_REFERENCE = "tidb-snapshot-20260728T010203Z-abcd1234"


def _strict_settings(tmp_path: Path) -> TiDBSettings:
    return TiDBSettings(
        host="db.example",
        port=4000,
        user="bot",
        password="credential-that-must-never-be-printed",
        database="masamong",
        ssl_ca=str(tmp_path / "ca.pem"),
        ssl_verify_identity=True,
        require_tls=True,
    )


def _configure_strict_target(monkeypatch, migration, tmp_path: Path) -> None:
    monkeypatch.setattr(migration.config, "ENV_FILE_PATH", tmp_path / "masamo.env")
    monkeypatch.setattr(migration.config, "REQUIRE_EXPLICIT_PROFILE", True)
    monkeypatch.setattr(migration.config, "PROFILE", "masamo")
    monkeypatch.setattr(migration.config, "DB_BACKEND", "tidb")
    monkeypatch.setattr(migration.config, "REMOTE_DB_STRICT_MODE", True)
    monkeypatch.setattr(migration.config, "TIDB_NAME", "masamong")
    monkeypatch.setattr(migration.config, "EXPECTED_DB_NAME", "masamong")


def _destructive_args(settings: TiDBSettings) -> argparse.Namespace:
    return argparse.Namespace(
        source_root="unused",
        skip_main=True,
        skip_discord=True,
        skip_kakao=True,
        apply=True,
        dry_run=False,
        truncate=True,
        confirm_database=settings.database,
        confirm_profile="masamo",
        backup_reference=BACKUP_REFERENCE,
        confirm_backup_reference=BACKUP_REFERENCE,
        confirm_destructive=(
            f"DROP ALL TABLES ON {settings.host}:{settings.port}/"
            f"{settings.database} FOR masamo "
            f"USING VERIFIED BACKUP {BACKUP_REFERENCE}"
        ),
    )


def _create_source_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        schema_path = Path(__file__).resolve().parents[1] / "database" / "schema.sql"
        connection.executescript(schema_path.read_text(encoding="utf-8"))
        connection.commit()
    finally:
        connection.close()


def test_preflight_rejects_missing_selected_source_before_remote_connection(
    tmp_path,
):
    with pytest.raises(SystemExit, match="메인 SQLite 원본"):
        preflight_sources(
            source_root=tmp_path,
            main_db=tmp_path / "missing.db",
            discord_db=tmp_path / "unused.db",
            skip_main=False,
            skip_discord=True,
            skip_kakao=True,
            destructive=False,
        )


def test_destructive_preflight_rejects_partial_store_selection(tmp_path):
    with pytest.raises(SystemExit, match="--skip-"):
        preflight_sources(
            source_root=tmp_path,
            main_db=tmp_path / "unused-main.db",
            discord_db=tmp_path / "unused-discord.db",
            skip_main=True,
            skip_discord=True,
            skip_kakao=True,
            destructive=True,
        )


def test_complete_main_source_passes_read_only_preflight(tmp_path):
    main_db = tmp_path / "source.db"
    _create_source_db(main_db)

    preflight_sources(
        source_root=tmp_path,
        main_db=main_db,
        discord_db=tmp_path / "unused.db",
        skip_main=False,
        skip_discord=True,
        skip_kakao=True,
        destructive=False,
    )


def test_destructive_preflight_rejects_empty_conversation_snapshot(tmp_path):
    main_db = tmp_path / "source.db"
    _create_source_db(main_db)

    with pytest.raises(SystemExit, match="conversation_history가 비어"):
        preflight_sources(
            source_root=tmp_path,
            main_db=main_db,
            discord_db=tmp_path / "unused.db",
            skip_main=False,
            skip_discord=True,
            skip_kakao=True,
            destructive=True,
        )


def test_non_truncate_write_still_requires_exact_target_confirmation(
    monkeypatch,
    tmp_path,
):
    from scripts import migrate_latest_data_to_tidb as migration

    _configure_strict_target(monkeypatch, migration, tmp_path)
    settings = _strict_settings(tmp_path)
    args = argparse.Namespace(
        truncate=False,
        confirm_database=None,
        confirm_profile=None,
        confirm_destructive=None,
        backup_reference=None,
    )

    with pytest.raises(SystemExit, match="confirm-database"):
        validate_target_identity(args, settings)

    args.confirm_database = "masamong"
    args.confirm_profile = "masamo"
    validate_target_identity(args, settings)


@pytest.mark.parametrize(
    "missing_guard",
    [
        "env_file",
        "explicit_profile",
        "non_legacy_profile",
        "tidb_backend",
        "strict_remote",
        "tls_required",
        "tls_ca",
        "tls_identity",
        "host",
        "port",
        "user",
        "password",
        "configured_database",
        "profile_database_boundary",
        "expected_database",
        "confirm_database",
        "confirm_profile",
        "backup_reference",
        "confirm_backup_reference",
        "confirm_destructive",
    ],
)
def test_destructive_authorization_requires_every_guard(
    monkeypatch,
    tmp_path,
    missing_guard,
):
    from scripts import migrate_latest_data_to_tidb as migration

    _configure_strict_target(monkeypatch, migration, tmp_path)
    settings = _strict_settings(tmp_path)
    args = _destructive_args(settings)

    if missing_guard == "env_file":
        monkeypatch.setattr(migration.config, "ENV_FILE_PATH", None)
    elif missing_guard == "explicit_profile":
        monkeypatch.setattr(migration.config, "REQUIRE_EXPLICIT_PROFILE", False)
    elif missing_guard == "non_legacy_profile":
        monkeypatch.setattr(migration.config, "PROFILE", "legacy")
    elif missing_guard == "tidb_backend":
        monkeypatch.setattr(migration.config, "DB_BACKEND", "sqlite")
    elif missing_guard == "strict_remote":
        monkeypatch.setattr(migration.config, "REMOTE_DB_STRICT_MODE", False)
    elif missing_guard == "tls_required":
        settings = replace(settings, require_tls=False)
    elif missing_guard == "tls_ca":
        settings = replace(settings, ssl_ca=None)
    elif missing_guard == "tls_identity":
        settings = replace(settings, ssl_verify_identity=False)
    elif missing_guard == "host":
        settings = replace(settings, host="")
    elif missing_guard == "port":
        settings = replace(settings, port=0)
    elif missing_guard == "user":
        settings = replace(settings, user="")
    elif missing_guard == "password":
        settings = replace(settings, password="")
    elif missing_guard == "configured_database":
        monkeypatch.setattr(migration.config, "TIDB_NAME", "other")
    elif missing_guard == "profile_database_boundary":
        monkeypatch.setattr(migration.config, "PROFILE", "general")
    elif missing_guard == "expected_database":
        monkeypatch.setattr(migration.config, "EXPECTED_DB_NAME", "other")
    elif missing_guard == "confirm_database":
        args.confirm_database = "other"
    elif missing_guard == "confirm_profile":
        args.confirm_profile = "general"
    elif missing_guard == "backup_reference":
        args.backup_reference = None
    elif missing_guard == "confirm_backup_reference":
        args.confirm_backup_reference = "different-snapshot"
    elif missing_guard == "confirm_destructive":
        args.confirm_destructive = "DROP SOMETHING"

    with pytest.raises(SystemExit):
        validate_target_identity(args, settings)


@pytest.mark.parametrize(
    "backup_reference",
    [
        "latest",
        " snapshot-12345678",
        "snapshot-12345678 ",
        "snapshot\n12345678",
        "short",
        "x" * 161,
    ],
)
def test_destructive_authorization_rejects_placeholder_or_ambiguous_backup_id(
    monkeypatch,
    tmp_path,
    backup_reference,
):
    from scripts import migrate_latest_data_to_tidb as migration

    _configure_strict_target(monkeypatch, migration, tmp_path)
    settings = _strict_settings(tmp_path)
    args = _destructive_args(settings)
    args.backup_reference = backup_reference
    args.confirm_backup_reference = backup_reference

    with pytest.raises(SystemExit, match="backup-reference"):
        validate_target_identity(args, settings)


class _RecordingCursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql):
        self.connection.statements.append(sql)

    def fetchone(self):
        return {"current_database": self.connection.current_database}


class _RecordingConnection:
    def __init__(self, current_database="masamong"):
        self.current_database = current_database
        self.statements = []
        self.commits = 0
        self.closed = False

    def cursor(self):
        return _RecordingCursor(self)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def test_drop_function_rejects_missing_or_forged_authorization_before_cursor():
    class _CursorMustNotBeUsed:
        def cursor(self):
            raise AssertionError("DROP guard failed before cursor acquisition")

    target = TargetIdentityConfirmation(
        profile="masamo",
        host="db.example",
        port=4000,
        database="masamong",
        destructive=True,
        backup_reference=BACKUP_REFERENCE,
    )
    forged = ConnectedTargetAuthorization(target=target, _capability=object())

    with pytest.raises(RuntimeError, match="authorization"):
        recreate_tables(_CursorMustNotBeUsed(), authorization=None)
    with pytest.raises(RuntimeError, match="authorization"):
        recreate_tables(_CursorMustNotBeUsed(), authorization=forged)


def test_connected_database_must_match_before_drop_authorization(
    monkeypatch,
    tmp_path,
):
    from scripts import migrate_latest_data_to_tidb as migration

    _configure_strict_target(monkeypatch, migration, tmp_path)
    settings = _strict_settings(tmp_path)
    confirmation = validate_target_identity(
        _destructive_args(settings),
        settings,
    )
    connection = _RecordingConnection(current_database="masamong_general")

    with pytest.raises(SystemExit, match=r"DATABASE\(\)"):
        verify_connected_target(connection, confirmation)

    assert connection.statements == ["SELECT DATABASE() AS current_database"]
    assert not any("DROP" in sql for sql in connection.statements)


def test_drop_runs_only_with_exact_guards_and_connected_database_readback(
    monkeypatch,
    tmp_path,
):
    from scripts import migrate_latest_data_to_tidb as migration

    _configure_strict_target(monkeypatch, migration, tmp_path)
    settings = _strict_settings(tmp_path)
    confirmation = validate_target_identity(
        _destructive_args(settings),
        settings,
    )
    connection = _RecordingConnection(current_database="masamong")

    authorization = verify_connected_target(connection, confirmation)
    recreate_tables(connection, authorization=authorization)

    assert connection.statements[0] == "SELECT DATABASE() AS current_database"
    drop_statements = [
        statement
        for statement in connection.statements[1:]
        if statement.startswith("DROP TABLE IF EXISTS ")
    ]
    assert len(drop_statements) == 18
    assert all(settings.password not in sql for sql in connection.statements)


def test_default_mode_is_offline_non_mutating_and_never_prints_credentials(
    monkeypatch,
    tmp_path,
    capsys,
):
    from scripts import migrate_latest_data_to_tidb as migration

    _configure_strict_target(monkeypatch, migration, tmp_path)
    settings = _strict_settings(tmp_path)
    args = _destructive_args(settings)
    args.apply = False
    args.confirm_database = None
    args.confirm_profile = None
    args.backup_reference = None
    args.confirm_backup_reference = None
    args.confirm_destructive = None

    monkeypatch.setattr(migration, "parse_args", lambda: args)
    monkeypatch.setattr(migration, "preflight_sources", lambda **kwargs: None)
    monkeypatch.setattr(migration, "_settings_from_config", lambda: settings)

    def fail_if_connected(_settings):
        raise AssertionError("dry-run must not open a remote connection")

    monkeypatch.setattr(migration, "connect_tidb", fail_if_connected)

    migration.main()

    output = capsys.readouterr().out
    assert "[dry-run]" in output
    assert settings.password not in output
    assert settings.user not in output


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("confirm_database", None),
        ("confirm_profile", None),
        ("backup_reference", None),
        ("confirm_backup_reference", None),
        ("confirm_destructive", None),
    ],
)
def test_main_cannot_connect_or_drop_when_any_typed_guard_is_missing(
    monkeypatch,
    tmp_path,
    field,
    value,
):
    from scripts import migrate_latest_data_to_tidb as migration

    _configure_strict_target(monkeypatch, migration, tmp_path)
    settings = _strict_settings(tmp_path)
    args = _destructive_args(settings)
    setattr(args, field, value)
    calls = {"connect": 0, "drop": 0}

    monkeypatch.setattr(migration, "parse_args", lambda: args)
    monkeypatch.setattr(migration, "preflight_sources", lambda **kwargs: None)
    monkeypatch.setattr(migration, "_settings_from_config", lambda: settings)

    def record_connect(_settings):
        calls["connect"] += 1
        return _RecordingConnection()

    def record_drop(*_args, **_kwargs):
        calls["drop"] += 1

    monkeypatch.setattr(migration, "connect_tidb", record_connect)
    monkeypatch.setattr(migration, "recreate_tables", record_drop)

    with pytest.raises(SystemExit):
        migration.main()

    assert calls == {"connect": 0, "drop": 0}


def test_main_rechecks_connected_database_before_any_mutating_sql(
    monkeypatch,
    tmp_path,
):
    from scripts import migrate_latest_data_to_tidb as migration

    _configure_strict_target(monkeypatch, migration, tmp_path)
    settings = _strict_settings(tmp_path)
    args = _destructive_args(settings)
    connection = _RecordingConnection(current_database="masamong_general")

    monkeypatch.setattr(migration, "parse_args", lambda: args)
    monkeypatch.setattr(migration, "preflight_sources", lambda **kwargs: None)
    monkeypatch.setattr(migration, "_settings_from_config", lambda: settings)
    monkeypatch.setattr(migration, "connect_tidb", lambda _settings: connection)
    monkeypatch.setattr(
        migration,
        "recreate_tables",
        lambda *_args, **_kwargs: pytest.fail("DROP must not execute"),
    )
    monkeypatch.setattr(
        migration,
        "apply_schema",
        lambda *_args, **_kwargs: pytest.fail("schema writes must not execute"),
    )

    with pytest.raises(SystemExit, match=r"DATABASE\(\)"):
        migration.main()

    assert connection.statements == ["SELECT DATABASE() AS current_database"]
    assert connection.closed is True

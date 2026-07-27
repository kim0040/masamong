import argparse
import sqlite3
from pathlib import Path

import pytest

from scripts.migrate_latest_data_to_tidb import (
    preflight_sources,
    validate_target_identity,
)
from database.compat_db import TiDBSettings


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

    monkeypatch.setattr(migration.config, "ENV_FILE_PATH", tmp_path / "masamo.env")
    monkeypatch.setattr(migration.config, "REQUIRE_EXPLICIT_PROFILE", True)
    monkeypatch.setattr(migration.config, "PROFILE", "masamo")
    monkeypatch.setattr(migration.config, "DB_BACKEND", "tidb")
    monkeypatch.setattr(migration.config, "REMOTE_DB_STRICT_MODE", True)
    monkeypatch.setattr(migration.config, "EXPECTED_DB_NAME", "masamong")
    settings = TiDBSettings(
        host="db.example",
        port=4000,
        user="bot",
        password="secret",
        database="masamong",
        ssl_ca=str(tmp_path / "ca.pem"),
        ssl_verify_identity=True,
        require_tls=True,
    )
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

from __future__ import annotations

from pathlib import Path
import sqlite3

import setup


def _general_profile(database_path: Path) -> dict[str, object]:
    return {
        "profile": "general",
        "instance": "general",
        "explicit": True,
        "env_file": str(database_path.parent / "general.env"),
        "backend": "sqlite",
        "database_file": str(database_path),
        "auto_migrate": True,
    }


def test_default_setup_does_not_create_legacy_files_or_run_subprocess(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)

    def fail_subprocess(*args, **kwargs):
        raise AssertionError("default setup must not run a subprocess")

    monkeypatch.setattr(setup.subprocess, "run", fail_subprocess)
    monkeypatch.setattr(setup.subprocess, "check_call", fail_subprocess)

    assert setup.main([]) is True
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / "emb_config.json").exists()
    assert not (tmp_path / "prompts.json").exists()
    assert not (tmp_path / "database" / "remasamong.db").exists()


def test_bootstrap_rejects_masamo_before_reserving_database(tmp_path):
    database_path = tmp_path / "general" / "general.db"
    database_path.parent.mkdir()
    profile = _general_profile(database_path)
    profile.update({"profile": "masamo", "instance": "masamo"})

    try:
        setup._new_general_database_path(
            profile,
            confirmation=str(database_path),
        )
    except setup.SetupSafetyError as exc:
        assert "general 프로필" in str(exc)
    else:
        raise AssertionError("Masamo profile must never be bootstrapped")

    assert not database_path.exists()


def test_bootstrap_refuses_existing_database_without_modifying_it(tmp_path):
    database_path = tmp_path / "general" / "general.db"
    database_path.parent.mkdir()
    original = b"existing-operational-data"
    database_path.write_bytes(original)

    try:
        setup._new_general_database_path(
            _general_profile(database_path),
            confirmation=str(database_path),
        )
    except setup.SetupSafetyError as exc:
        assert "이미 존재" in str(exc)
    else:
        raise AssertionError("Existing DB must be rejected")

    assert database_path.read_bytes() == original


def test_bootstrap_requires_exact_database_path_confirmation(tmp_path):
    database_path = tmp_path / "general" / "general.db"
    database_path.parent.mkdir()

    for confirmation in (None, str(tmp_path / "general" / "other.db")):
        try:
            setup._new_general_database_path(
                _general_profile(database_path),
                confirmation=confirmation,
            )
        except setup.SetupSafetyError as exc:
            assert "confirm-new-general-db" in str(exc)
        else:
            raise AssertionError("Missing/mismatched confirmation must fail")

    assert not database_path.exists()


def test_bootstrap_rejects_remote_database_even_for_general(tmp_path):
    database_path = tmp_path / "general" / "general.db"
    profile = _general_profile(database_path)
    profile["backend"] = "tidb"

    try:
        setup._new_general_database_path(
            profile,
            confirmation=str(database_path),
        )
    except setup.SetupSafetyError as exc:
        assert "원격 DB를 초기화하지 않습니다" in str(exc)
    else:
        raise AssertionError("Remote DB bootstrap must fail closed")


def test_safe_new_general_bootstrap_reserves_before_initializer(
    tmp_path,
    monkeypatch,
):
    env_path = tmp_path / "general.env"
    env_path.write_text("placeholder for mocked inspection", encoding="utf-8")
    database_path = tmp_path / "general-data" / "general.db"
    database_path.parent.mkdir()
    profile = _general_profile(database_path)
    profile["env_file"] = str(env_path)
    observations: list[tuple[str, bool, int]] = []

    monkeypatch.setattr(setup, "inspect_profile", lambda path: profile)

    def fake_initializer(path: Path) -> bool:
        observations.append(
            ("initialize", database_path.exists(), database_path.stat().st_size)
        )
        return True

    def fake_verify(path: Path) -> bool:
        observations.append(("verify", path.exists(), path.stat().st_size))
        return True

    monkeypatch.setattr(setup, "_run_database_initializer", fake_initializer)
    monkeypatch.setattr(setup, "_verify_fresh_database", fake_verify)

    assert setup.bootstrap_new_general_sqlite(
        env_path,
        confirmation=str(database_path),
    )
    assert observations == [
        ("initialize", True, 0),
        ("verify", True, 0),
    ]
    assert database_path.exists()
    assert database_path.stat().st_mode & 0o777 == 0o600


def test_profile_child_environment_drops_inherited_instance_values(
    tmp_path,
    monkeypatch,
):
    env_path = tmp_path / "general.env"
    monkeypatch.setenv("MASAMONG_PROFILE", "masamo")
    monkeypatch.setenv("MASAMONG_DB_NAME", "masamong")
    monkeypatch.setenv("UNRELATED_VALUE", "kept")

    environment = setup._profile_environment(env_path)

    assert environment["MASAMONG_ENV_FILE"] == str(env_path)
    assert "MASAMONG_PROFILE" not in environment
    assert "MASAMONG_DB_NAME" not in environment
    assert environment["UNRELATED_VALUE"] == "kept"


def test_fresh_database_verification_is_read_only_and_checks_core_tables(
    tmp_path,
):
    database_path = tmp_path / "general.db"
    connection = sqlite3.connect(database_path)
    for table in setup.REQUIRED_FRESH_TABLES:
        connection.execute(f"CREATE TABLE {table} (id INTEGER)")
    connection.commit()
    connection.close()
    before = database_path.read_bytes()

    assert setup._verify_fresh_database(database_path) is True
    assert database_path.read_bytes() == before

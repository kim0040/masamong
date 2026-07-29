from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import aiosqlite
import pytest

import config
from scripts import verify_runtime_health as health


def test_cli_is_directly_executable_outside_project_working_directory(tmp_path):
    project_root = Path(__file__).resolve().parents[1]
    script = project_root / "scripts" / "verify_runtime_health.py"
    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--allow-production-data-mutations" in completed.stdout


def test_cli_defaults_to_read_only_and_requires_strong_mutation_flag():
    default_args = health.parse_args([])
    mutation_args = health.parse_args(["--allow-production-data-mutations"])

    assert default_args.allow_production_data_mutations is False
    assert mutation_args.allow_production_data_mutations is True

    # 예전 이름은 실제 운영 데이터 변경 가능성을 드러내지 못하므로 fail-closed 한다.
    with pytest.raises(SystemExit):
        health.parse_args(["--write-check"])


@pytest.mark.asyncio
async def test_prompt_injection_probe_matches_current_message_contract():
    result = await health._run_prompt_injection_check(channel_id=123)

    assert result.ok is True
    assert result.name == "prompt_injection"
    assert result.metrics["missing_markers"] == []


@pytest.mark.asyncio
async def test_default_dispatch_never_invokes_any_mutator(monkeypatch):
    calls: list[str] = []

    async def archive(_db):
        calls.append("archive")
        raise AssertionError("read-only 기본 실행에서 archive를 호출하면 안 됩니다.")

    async def write(_db, _store):
        calls.append("write")
        raise AssertionError("read-only 기본 실행에서 write probe를 호출하면 안 됩니다.")

    monkeypatch.setattr(health, "_run_archive_cycle_check", archive)
    monkeypatch.setattr(health, "_run_write_pipeline_check", write)

    results = await health._run_opt_in_mutation_checks(
        object(),
        object(),
        allow_production_data_mutations=False,
        memory_enabled=True,
        embedding_ready=True,
    )

    assert results == []
    assert calls == []


@pytest.mark.asyncio
async def test_explicit_mutation_flag_dispatches_archive_and_write_probe(
    monkeypatch,
):
    calls: list[str] = []

    async def archive(_db):
        calls.append("archive")
        return health.CheckResult("archive_cycle", True, "ok", {})

    async def write(_db, _store):
        calls.append("write")
        return health.CheckResult("write_pipeline", True, "ok", {})

    monkeypatch.setattr(health, "_run_archive_cycle_check", archive)
    monkeypatch.setattr(health, "_run_write_pipeline_check", write)

    results = await health._run_opt_in_mutation_checks(
        object(),
        object(),
        allow_production_data_mutations=True,
        memory_enabled=True,
        embedding_ready=True,
    )

    assert calls == ["archive", "write"]
    assert [result.name for result in results] == [
        "archive_cycle",
        "write_pipeline",
    ]


@pytest.mark.asyncio
async def test_default_sqlite_connection_enforces_mode_ro(monkeypatch, tmp_path):
    database = tmp_path / "runtime.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE preserved (value TEXT NOT NULL)")
        connection.execute("INSERT INTO preserved (value) VALUES ('keep')")
        connection.commit()

    monkeypatch.setattr(config, "DATABASE_FILE", str(database))
    db = await health._connect_health_db(
        "sqlite",
        allow_production_data_mutations=False,
    )
    try:
        async with db.execute("SELECT value FROM preserved") as cursor:
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "keep"
        with pytest.raises(aiosqlite.OperationalError, match="readonly"):
            await db.execute("DELETE FROM preserved")
    finally:
        await db.close()

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM preserved").fetchall() == [
            ("keep",)
        ]

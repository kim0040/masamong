"""batch 실행 결과 판정 명세.

코어는 `partial`을 종료 코드 0으로 반환한다. 종료 코드만 믿으면 일부 게시판을
못 읽은 날을 정상으로 기록하게 되므로, 보고서와 digest를 함께 확인해야 한다.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from scripts.run_school_notice_batch import (
    BatchAlreadyRunning,
    ProfileLoadResult,
    _feedback_recorded,
    _run_feedback_subprocess,
    _run_profile,
    _run_subprocess,
    batch_limits,
    build_feedback_command,
    build_core_command,
    cleanup_stale_workdirs,
    current_profile_snapshot,
    dry_run_preflight_errors,
    load_profiles,
    mark_feedback_consumed,
    pending_feedback,
    pending_feedback_for_profile,
    publish_validated_artifacts,
    record_run,
    run_batch,
    select_profile_sources,
    single_flight_lock,
    summarize_run,
)
from utils.privacy_consent import (
    SCHOOL_NOTICE_SCOPE,
    grant_consent,
    withdraw_consent,
)
from utils.school_notice_profile import profile_snapshot_hash

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
RUN_DATE = date(2026, 7, 27)


def _prepare(tmp_path, *, digest="school_notice_digest.json", run_status="succeeded"):
    if digest:
        shutil.copy(
            FIXTURES / digest,
            tmp_path / f"daily-digest-{RUN_DATE.isoformat()}.json",
        )
    if run_status:
        (tmp_path / f"daily-run-{RUN_DATE.isoformat()}.json").write_text(
            json.dumps(
                {"status": run_status, "http_requests": 12, "llm_calls": 0},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    return tmp_path


def test_successful_run_is_summarized(tmp_path):
    _prepare(tmp_path)

    summary = summarize_run(tmp_path, RUN_DATE, 0)

    assert summary["status"] == "succeeded"
    assert summary["item_count"] == 4
    assert summary["collection_status"] == "healthy"
    assert summary["may_include_stale"] is False
    assert summary["http_requests"] == 12


def test_partial_exit_zero_is_not_recorded_as_success(tmp_path):
    # 코어는 partial을 exit 0으로 반환한다. 이것이 조용한 누락의 원인이 된다.
    _prepare(tmp_path, run_status="partial")

    summary = summarize_run(tmp_path, RUN_DATE, 0)

    assert summary["status"] == "partial"


def test_stale_collection_is_recorded(tmp_path):
    _prepare(tmp_path, digest="school_notice_digest_stale.json", run_status="partial")

    summary = summarize_run(tmp_path, RUN_DATE, 0)

    assert summary["status"] == "partial"
    assert summary["collection_status"] == "failed"
    assert summary["may_include_stale"] is True


def test_failed_exit_code_is_recorded(tmp_path):
    _prepare(tmp_path, run_status=None)

    summary = summarize_run(tmp_path, RUN_DATE, 2)

    assert summary["status"] == "failed"


@pytest.mark.parametrize("returncode", [1, 2, 3, 124, -9])
def test_every_nonzero_exit_is_failure_even_with_success_artifacts(
    tmp_path,
    returncode,
):
    _prepare(tmp_path)

    summary = summarize_run(tmp_path, RUN_DATE, returncode)

    assert summary["status"] == "failed"


def test_unreadable_digest_is_failure_even_with_success_report(tmp_path):
    # digest를 읽지 못하면 봇이 전달할 수 없으므로 성공으로 볼 수 없다.
    _prepare(tmp_path, digest="school_notice_digest_bad_schema.json")

    summary = summarize_run(tmp_path, RUN_DATE, 0)

    assert summary["status"] == "failed"


def test_missing_digest_is_failure(tmp_path):
    _prepare(tmp_path, digest=None)

    summary = summarize_run(tmp_path, RUN_DATE, 0)

    assert summary["status"] == "failed"


def test_empty_digest_is_success_with_zero_items(tmp_path):
    _prepare(tmp_path, digest="school_notice_digest_empty.json")

    summary = summarize_run(tmp_path, RUN_DATE, 0)

    assert summary["status"] == "succeeded"
    assert summary["item_count"] == 0


def test_digest_item_outside_selected_sources_is_failure(tmp_path):
    _prepare(tmp_path)
    digest_path = tmp_path / f"daily-digest-{RUN_DATE.isoformat()}.json"
    payload = json.loads(digest_path.read_text(encoding="utf-8"))
    payload["items"][0]["notice"]["candidate"]["source_id"] = "snu_general"
    digest_path.write_text(json.dumps(payload), encoding="utf-8")

    summary = summarize_run(
        tmp_path,
        RUN_DATE,
        0,
        allowed_source_ids=("jbnu_campus", "jbnu_software"),
    )

    assert summary["status"] == "failed"


def test_collection_health_outside_selected_sources_is_failure(tmp_path):
    _prepare(tmp_path, digest="school_notice_digest_empty.json")
    digest_path = tmp_path / f"daily-digest-{RUN_DATE.isoformat()}.json"
    payload = json.loads(digest_path.read_text(encoding="utf-8"))
    health = payload["collection_health"]["sources"]
    health["snu_general"] = health.pop("jbnu_software")
    digest_path.write_text(json.dumps(payload), encoding="utf-8")

    summary = summarize_run(
        tmp_path,
        RUN_DATE,
        0,
        allowed_source_ids=("jbnu_campus", "jbnu_software"),
    )

    assert summary["status"] == "failed"


def _args(**overrides):
    defaults = dict(
        core_python="/opt/core/.venv/bin/python",
        core_cwd="/opt/core",
        source_config=None,
        date=None,
        no_llm=True,
        low_resource=True,
        max_details_per_source=None,
        max_requests=None,
        max_profiles=None,
        profile_timeout_seconds=None,
        feedback_timeout_seconds=None,
        batch_deadline_seconds=None,
        only_user_id=None,
        dry_run=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_core_command_defaults_to_safe_flags(tmp_path):
    command = build_core_command(
        _args(),
        tmp_path / "p.json",
        tmp_path / "out",
        source_ids=("jbnu_campus",),
    )

    assert "--no-llm" in command
    assert "--low-resource" in command
    assert command[:3] == ["/opt/core/.venv/bin/python", "-m", "school_notice"]
    assert command[command.index("--date") + 1]
    assert command.index("--source-config") < command.index("daily")


def test_core_command_passes_output_dir_per_user(tmp_path):
    output = tmp_path / "discord-1"
    command = build_core_command(
        _args(),
        tmp_path / "p.json",
        output,
        source_ids=("jbnu_campus",),
    )

    # 사용자별 디렉터리로 나누지 않으면 코어가 digest를 서로 덮어쓴다.
    assert str(output) in command
    assert command[command.index("--output-dir") + 1] == str(output)


def test_llm_can_be_enabled_explicitly(tmp_path):
    command = build_core_command(
        _args(no_llm=False),
        tmp_path / "p.json",
        tmp_path,
        source_ids=("jbnu_campus",),
    )

    assert "--no-llm" not in command


def test_core_command_always_passes_selected_school_sources_and_date(tmp_path):
    command = build_core_command(
        _args(date=None),
        tmp_path / "p.json",
        tmp_path / "out",
        run_date=RUN_DATE,
        source_ids=("jbnu_campus", "jbnu_software"),
        selected_source_config=tmp_path / "sources.json",
    )

    assert command[command.index("--date") + 1] == RUN_DATE.isoformat()
    assert [
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--source"
    ] == ["jbnu_campus", "jbnu_software"]


def test_core_command_rejects_implicit_all_school_run(tmp_path):
    with pytest.raises(ValueError):
        build_core_command(
            _args(),
            tmp_path / "p.json",
            tmp_path / "out",
            source_ids=(),
        )


def _source_config(tmp_path):
    path = tmp_path / "sources.json"
    path.write_text(
        json.dumps(
            {
                "sources": [
                    {"id": "jbnu_campus", "school_id": "jbnu"},
                    {"id": "jbnu_software", "school_id": "jbnu"},
                    {"id": "jbnu_unapproved", "school_id": "jbnu"},
                    {"id": "snu_general", "school_id": "snu"},
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_valid_artifacts(output_dir, *, user_key):
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = json.loads(
        (FIXTURES / "school_notice_digest.json").read_text(encoding="utf-8")
    )
    payload["user_key"] = user_key
    payload["date"] = RUN_DATE.isoformat()
    (output_dir / f"daily-digest-{RUN_DATE.isoformat()}.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    (output_dir / f"daily-run-{RUN_DATE.isoformat()}.json").write_text(
        json.dumps({"status": "succeeded", "http_requests": 3, "llm_calls": 0}),
        encoding="utf-8",
    )


def test_source_selection_is_fail_closed_to_registered_school(tmp_path):
    sources = _source_config(tmp_path)
    args = _args(core_cwd=str(tmp_path), source_config=str(sources))

    selected_config, selected_ids = select_profile_sources(
        args,
        {"user_key": "discord-1", "school_id": "jbnu"},
    )

    assert selected_config == sources
    assert selected_ids == ("jbnu_campus", "jbnu_software")
    assert "snu_general" not in selected_ids
    assert "jbnu_unapproved" not in selected_ids


def test_profile_cannot_request_another_schools_source(tmp_path):
    sources = _source_config(tmp_path)
    args = _args(core_cwd=str(tmp_path), source_config=str(sources))

    with pytest.raises(ValueError):
        select_profile_sources(
            args,
            {
                "user_key": "discord-1",
                "school_id": "jbnu",
                "source_ids": ["snu_general"],
            },
        )


def test_dry_run_preflight_rejects_missing_cwd_and_malformed_source(tmp_path):
    malformed_sources = tmp_path / "malformed-sources.json"
    malformed_sources.write_text('{"sources": "not-an-array"}', encoding="utf-8")

    errors = dry_run_preflight_errors(
        _args(
            core_python=sys.executable,
            core_cwd=str(tmp_path / "missing-core"),
            source_config=str(malformed_sources),
            dry_run=True,
        ),
        ProfileLoadResult(()),
    )

    assert any("core-cwd" in error for error in errors)
    assert any("source 설정" in error for error in errors)


def test_feedback_command_matches_reference_cli(tmp_path):
    command = build_feedback_command(
        _args(source_config=str(tmp_path / "sources.json")),
        tmp_path / "profile.json",
        {
            "feedback_type": "useful",
            "source_id": "jbnu_campus",
            "external_id": "42",
            "topic": "장학",
        },
    )

    assert "feedback" in command
    assert command[command.index("--type") + 1] == "useful"
    assert command[command.index("--source-id") + 1] == "jbnu_campus"
    assert command[command.index("--external-id") + 1] == "42"
    assert command[command.index("--topic") + 1] == "장학"


def test_daily_subprocess_discards_unbounded_stdout_and_stderr(tmp_path):
    command = [
        sys.executable,
        "-c",
        (
            "import sys; "
            "sys.stdout.write('o' * 200000); "
            "sys.stderr.write('e' * 200000)"
        ),
    ]

    completed = _run_subprocess(command, cwd=str(tmp_path), timeout=10)

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""


def test_feedback_subprocess_accepts_only_bounded_valid_json(tmp_path):
    valid = _run_feedback_subprocess(
        [
            sys.executable,
            "-c",
            "import json; print(json.dumps({'recorded': 1}))",
        ],
        cwd=str(tmp_path),
        timeout=10,
    )
    oversized = _run_feedback_subprocess(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('x' * 70000)",
        ],
        cwd=str(tmp_path),
        timeout=10,
    )

    assert _feedback_recorded(valid) is True
    assert oversized.returncode == 0
    assert oversized.stdout == ""
    assert _feedback_recorded(oversized) is False


def test_digest_identity_mismatch_is_failure(tmp_path):
    _prepare(tmp_path)

    summary = summarize_run(
        tmp_path,
        RUN_DATE,
        0,
        expected_user_key="discord-another-user",
    )

    assert summary["status"] == "failed"


def test_publish_only_moves_validated_json_artifacts(tmp_path):
    staged = tmp_path / "stage"
    final = tmp_path / "final"
    staged.mkdir()
    _prepare(staged)
    (staged / f"daily-run-{RUN_DATE.isoformat()}.json").write_text(
        json.dumps(
            {
                "status": "succeeded",
                "http_requests": 12,
                "llm_calls": 0,
                "error_details": "must-not-be-published",
            }
        ),
        encoding="utf-8",
    )
    (staged / f"daily-digest-{RUN_DATE.isoformat()}.md").write_text(
        "not part of the bot contract",
        encoding="utf-8",
    )

    publish_validated_artifacts(staged, final, RUN_DATE)

    assert (final / f"daily-digest-{RUN_DATE.isoformat()}.json").is_file()
    assert (final / f"daily-run-{RUN_DATE.isoformat()}.json").is_file()
    assert not (final / f"daily-digest-{RUN_DATE.isoformat()}.md").exists()
    assert (final.stat().st_mode & 0o777) == 0o700
    assert (
        final.joinpath(f"daily-digest-{RUN_DATE.isoformat()}.json").stat().st_mode
        & 0o777
    ) == 0o600
    published_report = json.loads(
        (final / f"daily-run-{RUN_DATE.isoformat()}.json").read_text(
            encoding="utf-8"
        )
    )
    assert published_report == {
        "status": "succeeded",
        "http_requests": 12,
        "llm_calls": 0,
    }


def test_single_flight_lock_rejects_second_runner(tmp_path):
    lock_path = tmp_path / "batch.lock"

    with single_flight_lock(lock_path):
        with pytest.raises(BatchAlreadyRunning):
            with single_flight_lock(lock_path):
                pass

    assert (lock_path.stat().st_mode & 0o777) == 0o600


def test_batch_limits_are_bounded(monkeypatch):
    monkeypatch.setattr(
        "scripts.run_school_notice_batch.config.SCHOOL_NOTICE_BATCH_MAX_PROFILES",
        999_999,
        raising=False,
    )
    monkeypatch.setattr(
        "scripts.run_school_notice_batch.config."
        "SCHOOL_NOTICE_BATCH_PROFILE_TIMEOUT_SECONDS",
        0,
        raising=False,
    )

    limits = batch_limits(_args())

    assert limits.max_profiles == 500
    assert limits.profile_timeout_seconds == 1


def test_systemd_timer_and_service_keep_05_kst_low_resource_contract():
    timer = (
        ROOT / "deploy" / "systemd" / "masamong-school-notice-batch.timer"
    ).read_text(encoding="utf-8")
    service = (
        ROOT / "deploy" / "systemd" / "masamong-school-notice-batch.service"
    ).read_text(encoding="utf-8")

    assert "OnCalendar=*-*-* 05:00:00 Asia/Seoul" in timer
    assert "Persistent=true" in timer
    assert "RandomizedDelaySec=0" in timer
    assert "TimeoutStartSec=7800" in service
    assert "OMP_NUM_THREADS=1" in service
    assert "--low-resource" in service
    assert "--no-llm" not in service
    assert "--use-llm" in service
    # 날짜는 wrapper가 실행 시점의 KST 날짜로 고정해 코어 --date에 전달한다.
    assert " --date " not in service
    assert "TOKEN=" not in service
    assert "PASSWORD=" not in service


def test_stale_profile_cleanup_removes_only_owned_reserved_run_directories(
    tmp_path,
):
    work_root = tmp_path / ".profiles"
    work_root.mkdir(mode=0o700)
    stale = work_root / "run-abandoned123"
    stale.mkdir()
    (stale / "profile.json").write_text("private", encoding="utf-8")
    unrelated = work_root / "operator-backup"
    unrelated.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    symlink = work_root / "run-link"
    symlink.symlink_to(outside, target_is_directory=True)

    removed = cleanup_stale_workdirs(work_root)

    assert removed == 1
    assert not stale.exists()
    assert unrelated.is_dir()
    assert symlink.is_symlink()
    assert outside.is_dir()


@pytest.mark.asyncio
async def test_startup_cleans_stale_profile_even_when_no_profiles_remain(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "main.db"
    db = await aiosqlite.connect(database)
    try:
        await db.executescript(
            (ROOT / "database" / "schema.sql").read_text(encoding="utf-8")
        )
        await db.commit()
    finally:
        await db.close()
    digest_root = tmp_path / "digests"
    work_root = digest_root / ".profiles"
    stale = work_root / "run-after-sigterm"
    stale.mkdir(parents=True)
    (stale / "profile.json").write_text("private", encoding="utf-8")
    monkeypatch.setattr(
        "scripts.run_school_notice_batch.config.SCHOOL_NOTICE_ENABLED",
        True,
    )
    monkeypatch.setattr(
        "scripts.run_school_notice_batch.config.SCHOOL_NOTICE_DIGEST_DIR",
        str(digest_root),
    )
    monkeypatch.setattr(
        "scripts.run_school_notice_batch.config.DB_BACKEND",
        "sqlite",
    )
    monkeypatch.setattr(
        "scripts.run_school_notice_batch.config.DATABASE_FILE",
        str(database),
    )

    result = await run_batch(_args(date=RUN_DATE.isoformat()))

    assert result == 0
    assert not stale.exists()


async def _consented_profile_db():
    db = await aiosqlite.connect(":memory:")
    await db.executescript(
        (ROOT / "database" / "schema.sql").read_text(encoding="utf-8")
    )
    profile_json = json.dumps(
        {"user_key": "discord-1", "school_id": "jbnu", "grade": 3}
    )
    await db.execute(
        """
        INSERT INTO school_notice_profiles (
            user_id, user_key, school_id, profile_json, profile_version, enabled
        ) VALUES (1, 'discord-1', 'jbnu', ?, 1, 1)
        """,
        (profile_json,),
    )
    await db.execute(
        """
        INSERT INTO school_notice_feedback (
            user_key, source_id, external_id, feedback_type,
            interaction_id, created_at
        ) VALUES (
            'discord-1', 'jbnu_campus', '42', 'useful',
            'interaction-1', '2026-07-28'
        )
        """
    )
    await db.commit()
    await grant_consent(db, 1, SCHOOL_NOTICE_SCOPE)
    return db


@pytest.mark.asyncio
async def test_only_user_filter_never_loads_other_consented_profiles():
    db = await _consented_profile_db()
    try:
        await db.execute(
            """
            INSERT INTO school_notice_profiles (
                user_id, user_key, school_id, profile_json, profile_version, enabled
            ) VALUES (2, 'discord-2', 'snu', ?, 1, 1)
            """,
            (
                json.dumps(
                    {"user_key": "discord-2", "school_id": "snu"}
                ),
            ),
        )
        await db.commit()
        await grant_consent(db, 2, SCHOOL_NOTICE_SCOPE)

        selected = await load_profiles(db, only_user_id=2)

        assert len(selected) == 1
        assert selected[0]["user_key"] == "discord-2"
        with pytest.raises(ValueError):
            await load_profiles(db, only_user_id=0)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_withdrawal_after_initial_snapshot_skips_without_core_or_run_record(
    tmp_path,
    monkeypatch,
):
    db = await _consented_profile_db()
    try:
        snapshot = (await load_profiles(db))[0]
        cached_feedback = await pending_feedback_for_profile(db, snapshot)
        assert len(cached_feedback) == 1
        await withdraw_consent(db, 1, SCHOOL_NOTICE_SCOPE)

        def must_not_run(*_args, **_kwargs):
            raise AssertionError("withdrawn profile must not reach core subprocess")

        monkeypatch.setattr(
            "scripts.run_school_notice_batch._run_subprocess",
            must_not_run,
        )
        monkeypatch.setattr(
            "scripts.run_school_notice_batch._run_feedback_subprocess",
            must_not_run,
        )
        work_root = tmp_path / ".profiles"
        work_root.mkdir()
        summary, succeeded, failed = await _run_profile(
            db,
            args=_args(source_config=str(_source_config(tmp_path))),
            profile=snapshot,
            feedback_entries=cached_feedback,
            digest_root=tmp_path / "digests",
            work_root=work_root,
            run_date=RUN_DATE,
            deadline_monotonic=time.monotonic() + 30,
            limits=batch_limits(_args()),
        )

        assert summary is None
        assert (succeeded, failed) == (0, 0)
        assert list(work_root.iterdir()) == []
        async with db.execute(
            "SELECT COUNT(*) FROM school_notice_batch_runs"
        ) as cursor:
            assert (await cursor.fetchone())[0] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_profile_change_invalidates_snapshot_and_its_feedback():
    db = await _consented_profile_db()
    try:
        snapshot = (await load_profiles(db))[0]
        await db.execute(
            """
            UPDATE school_notice_profiles
            SET school_id = 'snu',
                profile_version = 2,
                profile_json = ?
            WHERE user_id = 1
            """,
            (
                json.dumps(
                    {"user_key": "discord-1", "school_id": "snu", "grade": 4}
                ),
            ),
        )
        await db.commit()

        assert await current_profile_snapshot(db, snapshot) is None
        assert await pending_feedback_for_profile(db, snapshot) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_consent_change_during_feedback_prevents_daily_core(
    tmp_path,
    monkeypatch,
):
    db = await _consented_profile_db()
    try:
        snapshot = (await load_profiles(db))[0]
        feedback = await pending_feedback_for_profile(db, snapshot)
        checks = 0

        async def current_then_withdrawn(_db, expected):
            nonlocal checks
            checks += 1
            return expected if checks <= 2 else None

        monkeypatch.setattr(
            "scripts.run_school_notice_batch.current_profile_snapshot",
            current_then_withdrawn,
        )
        monkeypatch.setattr(
            "scripts.run_school_notice_batch._run_feedback_subprocess",
            lambda command, *, cwd, timeout: subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"recorded": 1}),
                "",
            ),
        )

        def daily_must_not_run(*_args, **_kwargs):
            raise AssertionError("revoked profile must not reach daily core")

        monkeypatch.setattr(
            "scripts.run_school_notice_batch._run_subprocess",
            daily_must_not_run,
        )
        work_root = tmp_path / ".profiles"
        work_root.mkdir()
        summary, succeeded, failed = await _run_profile(
            db,
            args=_args(source_config=str(_source_config(tmp_path))),
            profile=snapshot,
            feedback_entries=feedback,
            digest_root=tmp_path / "digests",
            work_root=work_root,
            run_date=RUN_DATE,
            deadline_monotonic=time.monotonic() + 30,
            limits=batch_limits(_args()),
        )

        assert checks == 3
        assert summary is None
        assert (succeeded, failed) == (1, 0)
        assert list(work_root.iterdir()) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_consent_change_during_daily_prevents_digest_publish(
    tmp_path,
    monkeypatch,
):
    db = await _consented_profile_db()
    try:
        snapshot = (await load_profiles(db))[0]
        checks = 0
        daily_called = False

        async def current_until_daily_finishes(_db, expected):
            nonlocal checks
            checks += 1
            return expected if checks <= 3 else None

        def fake_daily(command, *, cwd, timeout):
            nonlocal daily_called
            daily_called = True
            output = Path(command[command.index("--output-dir") + 1])
            _write_valid_artifacts(output, user_key="discord-1")
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(
            "scripts.run_school_notice_batch.current_profile_snapshot",
            current_until_daily_finishes,
        )
        monkeypatch.setattr(
            "scripts.run_school_notice_batch._run_subprocess",
            fake_daily,
        )
        work_root = tmp_path / ".profiles"
        work_root.mkdir()
        digest_root = tmp_path / "digests"
        summary, succeeded, failed = await _run_profile(
            db,
            args=_args(source_config=str(_source_config(tmp_path))),
            profile=snapshot,
            feedback_entries=[],
            digest_root=digest_root,
            work_root=work_root,
            run_date=RUN_DATE,
            deadline_monotonic=time.monotonic() + 30,
            limits=batch_limits(_args()),
        )

        assert daily_called is True
        assert checks == 4
        assert summary is None
        assert (succeeded, failed) == (0, 0)
        assert not (digest_root / "discord-1").exists()
        assert list(work_root.iterdir()) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_profile_run_applies_feedback_then_atomically_publishes_and_cleans(
    tmp_path,
    monkeypatch,
):
    db = await aiosqlite.connect(":memory:")
    try:
        await db.execute(
            """
            CREATE TABLE school_notice_feedback (
                id INTEGER PRIMARY KEY,
                consumed_at TEXT
            )
            """
        )
        await db.execute(
            "INSERT INTO school_notice_feedback (id, consumed_at) VALUES (1, NULL)"
        )
        await db.commit()
        sources = _source_config(tmp_path)
        args = _args(core_cwd=str(tmp_path), source_config=str(sources))
        digest_root = tmp_path / "digests"
        work_root = digest_root / ".profiles"
        work_root.mkdir(parents=True, mode=0o700)
        calls = []

        def fake_daily(command, *, cwd, timeout):
            calls.append(command)
            exported_profile = json.loads(
                Path(command[command.index("--profile") + 1]).read_text(
                    encoding="utf-8"
                )
            )
            assert not any(key.startswith("_batch_") for key in exported_profile)
            output = Path(command[command.index("--output-dir") + 1])
            _write_valid_artifacts(output, user_key="discord-1")
            return subprocess.CompletedProcess(command, 0, "", "")

        def fake_feedback(command, *, cwd, timeout):
            calls.append(command)
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"recorded": 1}),
                "",
            )

        monkeypatch.setattr(
            "scripts.run_school_notice_batch._run_subprocess",
            fake_daily,
        )
        monkeypatch.setattr(
            "scripts.run_school_notice_batch._run_feedback_subprocess",
            fake_feedback,
        )
        monkeypatch.setattr(
            "scripts.run_school_notice_batch.config.SCHOOL_NOTICE_CORE_DB",
            str(tmp_path / "core.db"),
        )

        summary, succeeded, failed = await _run_profile(
            db,
            args=args,
            profile={"user_key": "discord-1", "school_id": "jbnu"},
            feedback_entries=[
                {
                    "id": 1,
                    "user_key": "discord-1",
                    "source_id": "jbnu_campus",
                    "external_id": "42",
                    "feedback_type": "useful",
                    "topic": None,
                }
            ],
            digest_root=digest_root,
            work_root=work_root,
            run_date=RUN_DATE,
            deadline_monotonic=time.monotonic() + 30,
            limits=batch_limits(_args()),
        )

        assert summary["status"] == "succeeded"
        assert (succeeded, failed) == (1, 0)
        assert "feedback" in calls[0]
        assert "daily" in calls[1]
        daily_sources = [
            calls[1][index + 1]
            for index, value in enumerate(calls[1])
            if value == "--source"
        ]
        assert daily_sources == ["jbnu_campus", "jbnu_software"]
        async with db.execute(
            "SELECT consumed_at FROM school_notice_feedback WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
        assert row[0] is not None
        assert (
            digest_root
            / "discord-1"
            / f"daily-digest-{RUN_DATE.isoformat()}.json"
        ).is_file()
        # profile.json과 staged output은 TemporaryDirectory와 함께 항상 제거된다.
        assert list(work_root.iterdir()) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_profile_run_does_not_publish_digest_from_unselected_source(
    tmp_path,
    monkeypatch,
):
    db = await aiosqlite.connect(":memory:")
    try:
        args = _args(
            core_cwd=str(tmp_path),
            source_config=str(_source_config(tmp_path)),
        )
        digest_root = tmp_path / "digests"
        work_root = digest_root / ".profiles"
        work_root.mkdir(parents=True)

        def fake_daily(command, *, cwd, timeout):
            output = Path(command[command.index("--output-dir") + 1])
            _write_valid_artifacts(output, user_key="discord-1")
            digest_path = output / f"daily-digest-{RUN_DATE.isoformat()}.json"
            payload = json.loads(digest_path.read_text(encoding="utf-8"))
            payload["items"][0]["notice"]["candidate"]["source_id"] = "snu_general"
            digest_path.write_text(json.dumps(payload), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(
            "scripts.run_school_notice_batch._run_subprocess",
            fake_daily,
        )

        summary, succeeded, failed = await _run_profile(
            db,
            args=args,
            profile={"user_key": "discord-1", "school_id": "jbnu"},
            feedback_entries=[],
            digest_root=digest_root,
            work_root=work_root,
            run_date=RUN_DATE,
            deadline_monotonic=time.monotonic() + 30,
            limits=batch_limits(_args()),
        )

        assert summary["status"] == "failed"
        assert (succeeded, failed) == (0, 0)
        assert not (digest_root / "discord-1").exists()
        assert list(work_root.iterdir()) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_failed_feedback_is_not_consumed(tmp_path, monkeypatch):
    db = await aiosqlite.connect(":memory:")
    try:
        await db.execute(
            "CREATE TABLE school_notice_feedback (id INTEGER PRIMARY KEY, consumed_at TEXT)"
        )
        await db.execute(
            "INSERT INTO school_notice_feedback (id, consumed_at) VALUES (1, NULL)"
        )
        await db.commit()
        sources = _source_config(tmp_path)
        args = _args(core_cwd=str(tmp_path), source_config=str(sources))
        digest_root = tmp_path / "digests"
        work_root = digest_root / ".profiles"
        work_root.mkdir(parents=True)

        monkeypatch.setattr(
            "scripts.run_school_notice_batch._run_feedback_subprocess",
            lambda command, *, cwd, timeout: subprocess.CompletedProcess(
                command,
                1,
                "",
                "",
            ),
        )
        monkeypatch.setattr(
            "scripts.run_school_notice_batch.config.SCHOOL_NOTICE_CORE_DB",
            str(tmp_path / "core.db"),
        )

        summary, succeeded, failed = await _run_profile(
            db,
            args=args,
            profile={"user_key": "discord-1", "school_id": "jbnu"},
            feedback_entries=[
                {
                    "id": 1,
                    "source_id": "jbnu_campus",
                    "external_id": "42",
                    "feedback_type": "useful",
                    "topic": None,
                }
            ],
            digest_root=digest_root,
            work_root=work_root,
            run_date=RUN_DATE,
            deadline_monotonic=time.monotonic() + 30,
            limits=batch_limits(_args()),
        )

        assert summary["status"] == "failed"
        assert (succeeded, failed) == (0, 1)
        async with db.execute(
            "SELECT consumed_at FROM school_notice_feedback WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
        assert row[0] is None
        assert not (digest_root / "discord-1").exists()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_old_school_feedback_does_not_block_new_school_daily(
    tmp_path,
    monkeypatch,
):
    db = await aiosqlite.connect(":memory:")
    try:
        await db.execute(
            "CREATE TABLE school_notice_feedback (id INTEGER PRIMARY KEY, consumed_at TEXT)"
        )
        await db.execute(
            "INSERT INTO school_notice_feedback (id, consumed_at) VALUES (1, NULL)"
        )
        await db.commit()
        sources = _source_config(tmp_path)
        args = _args(core_cwd=str(tmp_path), source_config=str(sources))
        digest_root = tmp_path / "digests"
        work_root = digest_root / ".profiles"
        work_root.mkdir(parents=True)

        def stale_feedback_must_not_run(*_args, **_kwargs):
            raise AssertionError("old-school feedback must not reach core")

        def fake_daily(command, *, cwd, timeout):
            output = Path(command[command.index("--output-dir") + 1])
            _write_valid_artifacts(output, user_key="discord-1")
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(
            "scripts.run_school_notice_batch._run_feedback_subprocess",
            stale_feedback_must_not_run,
        )
        monkeypatch.setattr(
            "scripts.run_school_notice_batch._run_subprocess",
            fake_daily,
        )

        summary, succeeded, failed = await _run_profile(
            db,
            args=args,
            profile={"user_key": "discord-1", "school_id": "jbnu"},
            feedback_entries=[
                {
                    "id": 1,
                    "source_id": "snu_general",
                    "external_id": "old-42",
                    "feedback_type": "useful",
                    "topic": None,
                }
            ],
            digest_root=digest_root,
            work_root=work_root,
            run_date=RUN_DATE,
            deadline_monotonic=time.monotonic() + 30,
            limits=batch_limits(_args()),
        )

        assert summary["status"] == "succeeded"
        assert (succeeded, failed) == (0, 0)
        async with db.execute(
            "SELECT consumed_at FROM school_notice_feedback WHERE id = 1"
        ) as cursor:
            assert (await cursor.fetchone())[0] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_timeout_does_not_publish_or_replace_old_digest(tmp_path, monkeypatch):
    db = await aiosqlite.connect(":memory:")
    try:
        sources = _source_config(tmp_path)
        args = _args(core_cwd=str(tmp_path), source_config=str(sources))
        digest_root = tmp_path / "digests"
        final_dir = digest_root / "discord-1"
        final_dir.mkdir(parents=True)
        final_digest = final_dir / f"daily-digest-{RUN_DATE.isoformat()}.json"
        final_digest.write_text("old-known-good", encoding="utf-8")
        work_root = digest_root / ".profiles"
        work_root.mkdir()

        def fake_timeout(command, *, cwd, timeout):
            raise subprocess.TimeoutExpired(command, timeout)

        monkeypatch.setattr(
            "scripts.run_school_notice_batch._run_subprocess",
            fake_timeout,
        )
        summary, _, _ = await _run_profile(
            db,
            args=args,
            profile={"user_key": "discord-1", "school_id": "jbnu"},
            feedback_entries=[],
            digest_root=digest_root,
            work_root=work_root,
            run_date=RUN_DATE,
            deadline_monotonic=time.monotonic() + 30,
            limits=batch_limits(_args()),
        )

        assert summary["status"] == "failed"
        assert final_digest.read_text(encoding="utf-8") == "old-known-good"
        assert list(work_root.iterdir()) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_dry_run_is_read_only_for_database_and_filesystem(
    tmp_path,
    monkeypatch,
    capsys,
):
    database = tmp_path / "main.db"
    db = await aiosqlite.connect(database)
    try:
        await db.executescript(
            (ROOT / "database" / "schema.sql").read_text(encoding="utf-8")
        )
        await db.execute(
            """
            INSERT INTO school_notice_profiles (
                user_id, user_key, school_id, profile_json, enabled
            ) VALUES (1, 'discord-1', 'jbnu', ?, 1)
            """,
            (json.dumps({"user_key": "discord-1", "school_id": "jbnu"}),),
        )
        await db.commit()
        await grant_consent(db, 1, SCHOOL_NOTICE_SCOPE)
    finally:
        await db.close()

    digest_root = tmp_path / "must-not-be-created"
    before_hash = hashlib.sha256(database.read_bytes()).hexdigest()
    monkeypatch.setattr(
        "scripts.run_school_notice_batch.config.SCHOOL_NOTICE_ENABLED",
        True,
    )
    monkeypatch.setattr(
        "scripts.run_school_notice_batch.config.SCHOOL_NOTICE_DIGEST_DIR",
        str(digest_root),
    )
    monkeypatch.setattr(
        "scripts.run_school_notice_batch.config.DB_BACKEND",
        "sqlite",
    )
    monkeypatch.setattr(
        "scripts.run_school_notice_batch.config.DATABASE_FILE",
        str(database),
    )

    source_config = _source_config(tmp_path)
    valid_args = _args(
        core_python=sys.executable,
        core_cwd=str(tmp_path),
        source_config=str(source_config),
        dry_run=True,
        date=RUN_DATE.isoformat(),
    )
    result = await run_batch(valid_args)

    assert result == 0
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before_hash
    assert not digest_root.exists()
    assert "discord-1" not in capsys.readouterr().out

    missing_python_result = await run_batch(
        _args(
            core_python=str(tmp_path / "missing-python"),
            core_cwd=str(tmp_path),
            source_config=str(source_config),
            dry_run=True,
            date=RUN_DATE.isoformat(),
        )
    )
    assert missing_python_result == 2
    assert not digest_root.exists()

    mismatched_sources = tmp_path / "mismatched-sources.json"
    mismatched_sources.write_text(
        json.dumps(
            {"sources": [{"id": "snu_general", "school_id": "snu"}]}
        ),
        encoding="utf-8",
    )
    mismatched_source_result = await run_batch(
        _args(
            core_python=sys.executable,
            core_cwd=str(tmp_path),
            source_config=str(mismatched_sources),
            dry_run=True,
            date=RUN_DATE.isoformat(),
        )
    )
    assert mismatched_source_result == 2
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before_hash
    assert not digest_root.exists()


@pytest.mark.asyncio
async def test_invalid_active_profiles_fail_run_but_valid_profiles_still_process(
    tmp_path,
    monkeypatch,
    capsys,
):
    database = tmp_path / "main.db"
    db = await aiosqlite.connect(database)
    try:
        await db.executescript(
            (ROOT / "database" / "schema.sql").read_text(encoding="utf-8")
        )
        await db.execute(
            """
            INSERT INTO school_notice_profiles (
                user_id, user_key, school_id, profile_json, enabled
            ) VALUES (1, 'discord-valid', 'jbnu', ?, 1)
            """,
            (json.dumps({"user_key": "discord-valid", "school_id": "jbnu"}),),
        )
        await db.execute(
            """
            INSERT INTO school_notice_profiles (
                user_id, user_key, school_id, profile_json, enabled
            ) VALUES (2, 'discord-invalid', 'jbnu', 'not-json', 1)
            """
        )
        await db.commit()
        await grant_consent(db, 1, SCHOOL_NOTICE_SCOPE)
        await grant_consent(db, 2, SCHOOL_NOTICE_SCOPE)
    finally:
        await db.close()

    digest_root = tmp_path / "digests"
    monkeypatch.setattr(
        "scripts.run_school_notice_batch.config.SCHOOL_NOTICE_ENABLED",
        True,
    )
    monkeypatch.setattr(
        "scripts.run_school_notice_batch.config.SCHOOL_NOTICE_DIGEST_DIR",
        str(digest_root),
    )
    monkeypatch.setattr(
        "scripts.run_school_notice_batch.config.DB_BACKEND",
        "sqlite",
    )
    monkeypatch.setattr(
        "scripts.run_school_notice_batch.config.DATABASE_FILE",
        str(database),
    )

    processed: list[str] = []

    async def fake_run_profile(_db, **kwargs):
        processed.append(str(kwargs["profile"]["user_key"]))
        return (
            {
                "status": "succeeded",
                "collection_status": "healthy",
                "may_include_stale": False,
                "item_count": 0,
                "http_requests": 0,
                "llm_calls": 0,
            },
            0,
            0,
        )

    monkeypatch.setattr(
        "scripts.run_school_notice_batch._run_profile",
        fake_run_profile,
    )
    args = _args(
        core_python=sys.executable,
        core_cwd=str(tmp_path),
        source_config=str(_source_config(tmp_path)),
        date=RUN_DATE.isoformat(),
    )

    result = await run_batch(args)

    assert result == 2
    assert processed == ["discord-valid"]
    assert "유효하지 않은 활성 프로필 1건" in capsys.readouterr().err

    # 유효한 프로필까지 사라진 경우에도 "활성 프로필 없음" 성공으로 숨기지 않는다.
    db = await aiosqlite.connect(database)
    try:
        await db.execute(
            "UPDATE school_notice_profiles SET enabled = 0 WHERE user_id = 1"
        )
        await db.commit()
    finally:
        await db.close()

    all_invalid_result = await run_batch(args)
    assert all_invalid_result == 2
    assert processed == ["discord-valid"]


@pytest.mark.asyncio
async def test_batch_exports_only_currently_consented_profiles_and_feedback():
    db = await aiosqlite.connect(":memory:")
    try:
        await db.executescript(
            (ROOT / "database" / "schema.sql").read_text(encoding="utf-8")
        )
        for user_id in (1, 2):
            user_key = f"discord-{user_id}"
            await db.execute(
                """
                INSERT INTO school_notice_profiles (
                    user_id, user_key, school_id, profile_json, enabled
                ) VALUES (?, ?, ?, ?, 1)
                """,
                (
                    user_id,
                    user_key,
                    "jbnu",
                    json.dumps(
                        {
                            "user_key": user_key,
                            "school_id": "jbnu",
                            "degree_level": "undergraduate",
                            "grade": 3,
                        }
                    ),
                ),
            )
            await db.execute(
                """
                INSERT INTO school_notice_feedback (
                    user_key, source_id, external_id, feedback_type,
                    interaction_id, created_at
                ) VALUES (?, 'source', 'notice', 'useful', ?, '2026-07-28')
                """,
                (user_key, f"interaction-{user_id}"),
            )
        await db.commit()
        await grant_consent(db, 1, SCHOOL_NOTICE_SCOPE)
        await grant_consent(db, 2, SCHOOL_NOTICE_SCOPE)
        await withdraw_consent(db, 2, SCHOOL_NOTICE_SCOPE)

        assert [profile["user_key"] for profile in await load_profiles(db)] == [
            "discord-1"
        ]
        assert [item["user_key"] for item in await pending_feedback(db)] == [
            "discord-1"
        ]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_batch_and_feedback_timestamps_are_timezone_aware_kst(monkeypatch):
    db = await aiosqlite.connect(":memory:")
    try:
        await db.executescript(
            (ROOT / "database" / "schema.sql").read_text(encoding="utf-8")
        )
        await db.execute(
            """
            INSERT INTO school_notice_feedback (
                user_key, source_id, external_id, feedback_type,
                interaction_id, created_at
            ) VALUES (
                'discord-1', 'jbnu_campus', '42', 'useful',
                'interaction-kst', '2026-07-28T09:00:00+09:00'
            )
            """
        )
        await db.execute(
            """
            INSERT INTO school_notice_profiles (
                user_id, user_key, school_id, profile_json, enabled
            ) VALUES (1, 'discord-1', 'jbnu', ?, 1)
            """,
            (json.dumps({"user_key": "discord-1", "school_id": "jbnu"}),),
        )
        await db.commit()
        await grant_consent(db, 1, SCHOOL_NOTICE_SCOPE)
        profile = (await load_profiles(db))[0]
        async with db.execute(
            "SELECT id FROM school_notice_feedback WHERE interaction_id = 'interaction-kst'"
        ) as cursor:
            feedback_id = int((await cursor.fetchone())[0])
        monkeypatch.setattr(
            "scripts.run_school_notice_batch.config.DB_BACKEND",
            "sqlite",
        )

        await mark_feedback_consumed(db, [feedback_id])
        await record_run(
            db,
            user_key="discord-1",
            run_date=RUN_DATE,
            summary={
                "status": "succeeded",
                "collection_status": "healthy",
                "may_include_stale": False,
                "item_count": 0,
                "http_requests": 1,
                "llm_calls": 0,
            },
            profile=profile,
        )

        async with db.execute(
            "SELECT consumed_at FROM school_notice_feedback WHERE id = ?",
            (feedback_id,),
        ) as cursor:
            consumed_at = (await cursor.fetchone())[0]
        async with db.execute(
            """
            SELECT finished_at, profile_version, profile_hash
            FROM school_notice_batch_runs
            WHERE user_key = 'discord-1'
            """
        ) as cursor:
            finished_at, profile_version, snapshot_hash = await cursor.fetchone()
        assert datetime.fromisoformat(consumed_at).utcoffset() == timedelta(hours=9)
        assert datetime.fromisoformat(finished_at).utcoffset() == timedelta(hours=9)
        assert profile_version == 1
        assert snapshot_hash == profile_snapshot_hash(
            {"user_key": "discord-1", "school_id": "jbnu"}
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_profile_cap_order_prioritizes_never_and_least_recently_run_users():
    db = await aiosqlite.connect(":memory:")
    try:
        await db.executescript(
            (ROOT / "database" / "schema.sql").read_text(encoding="utf-8")
        )
        for user_id in (1, 2, 3):
            user_key = f"discord-{user_id}"
            await db.execute(
                """
                INSERT INTO school_notice_profiles (
                    user_id, user_key, school_id, profile_json, enabled
                ) VALUES (?, ?, 'jbnu', ?, 1)
                """,
                (
                    user_id,
                    user_key,
                    json.dumps({"user_key": user_key, "school_id": "jbnu"}),
                ),
            )
            await grant_consent(db, user_id, SCHOOL_NOTICE_SCOPE)
        await db.execute(
            """
            INSERT INTO school_notice_batch_runs (
                user_key, run_date, status, finished_at
            ) VALUES ('discord-1', '2026-07-27', 'succeeded', '2026-07-27T23:30:00')
            """
        )
        await db.execute(
            """
            INSERT INTO school_notice_batch_runs (
                user_key, run_date, status, finished_at
            ) VALUES ('discord-2', '2026-07-20', 'succeeded', '2026-07-20T23:30:00')
            """
        )
        await db.commit()

        ordered = [profile["user_key"] for profile in await load_profiles(db)]

        # 상한 slice를 적용해도 매일 같은 사전순 사용자만 고정되지 않는다.
        assert ordered == ["discord-3", "discord-2", "discord-1"]
    finally:
        await db.close()

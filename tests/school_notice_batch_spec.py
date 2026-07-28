"""batch 실행 결과 판정 명세.

코어는 `partial`을 종료 코드 0으로 반환한다. 종료 코드만 믿으면 일부 게시판을
못 읽은 날을 정상으로 기록하게 되므로, 보고서와 digest를 함께 확인해야 한다.
"""

import argparse
import json
import shutil
from datetime import date
from pathlib import Path

import pytest

from scripts.run_school_notice_batch import build_core_command, summarize_run

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


def _args(**overrides):
    defaults = dict(
        core_python="/opt/core/.venv/bin/python",
        core_cwd="/opt/core",
        date=None,
        no_llm=True,
        low_resource=True,
        max_details_per_source=None,
        max_requests=None,
        dry_run=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_core_command_defaults_to_safe_flags(tmp_path):
    command = build_core_command(_args(), tmp_path / "p.json", tmp_path / "out")

    assert "--no-llm" in command
    assert "--low-resource" in command
    assert command[:4] == ["/opt/core/.venv/bin/python", "-m", "school_notice", "daily"]


def test_core_command_passes_output_dir_per_user(tmp_path):
    output = tmp_path / "discord-1"
    command = build_core_command(_args(), tmp_path / "p.json", output)

    # 사용자별 디렉터리로 나누지 않으면 코어가 digest를 서로 덮어쓴다.
    assert str(output) in command
    assert command[command.index("--output-dir") + 1] == str(output)


def test_llm_can_be_enabled_explicitly(tmp_path):
    command = build_core_command(_args(no_llm=False), tmp_path / "p.json", tmp_path)

    assert "--no-llm" not in command

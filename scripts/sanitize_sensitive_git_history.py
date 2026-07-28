#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""강한 확인 후 Git 전체 이력의 운영 식별값을 placeholder로 치환한다.

이 도구는 destructive history rewrite다. 실제 값이나 replacement payload를
stdout/stderr에 출력하지 않는다. 실행 전 작업 트리가 완전히 깨끗해야 하며,
``git-filter-repo`` 설치와 명시적 확인 문자열이 필요하다.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_tracked_secrets import _read_env_secrets  # noqa: E402

_CONFIRM = "rewrite-sensitive-history"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="운영 식별값을 Git 전체 이력에서 placeholder로 치환"
    )
    parser.add_argument("--secret-env", required=True)
    parser.add_argument("--confirm", required=True)
    return parser.parse_args(argv)


def _git_text(*args: str) -> str:
    return subprocess.run(
        ("git", *args),
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    ).stdout.strip()


def _assert_clean_worktree() -> None:
    if _git_text("status", "--porcelain"):
        raise RuntimeError("작업 트리가 깨끗하지 않아 이력 재작성을 거부합니다.")


def _replacement_payload(secret_env: str) -> tuple[bytes, list[str]]:
    secrets = _read_env_secrets([secret_env])
    lines = [
        (
            rb"regex:MASAMONG_SUPERADMIN_USER_IDS[ \t]*=[ \t]*"
            rb"[0-9]{15,22}==>"
            rb"MASAMONG_SUPERADMIN_USER_IDS="
            rb"replace-with-current-masamo-superadmin-user-id"
        ),
        (
            rb"regex:MASAMONG_EXPECTED_DISCORD_BOT_USER_ID[ \t]*=[ \t]*"
            rb"[0-9]{15,22}==>"
            rb"MASAMONG_EXPECTED_DISCORD_BOT_USER_ID="
            rb"replace-with-current-masamo-bot-user-id"
        ),
    ]
    labels = [
        "tracked_superadmin_identity",
        "tracked_bot_identity",
    ]
    db_host = secrets.get("MASAMONG_DB_HOST")
    if db_host:
        if b"\n" in db_host or b"\r" in db_host or b"==>" in db_host:
            raise RuntimeError("DB host 값 형식이 replacement 계약에 맞지 않습니다.")
        lines.append(b"literal:" + db_host + b"==>your_tidb_host")
        labels.append("known_env_value:MASAMONG_DB_HOST")
    return b"\n".join(lines) + b"\n", labels


def rewrite(*, secret_env: str, confirm: str) -> dict[str, object]:
    if confirm != _CONFIRM:
        raise RuntimeError("확인 문자열이 일치하지 않습니다.")
    _assert_clean_worktree()
    remote_url = _git_text("remote", "get-url", "origin")
    payload, labels = _replacement_payload(secret_env)

    with tempfile.NamedTemporaryFile(prefix="masamong-history-", delete=True) as handle:
        handle.write(payload)
        handle.flush()
        subprocess.run(
            (
                sys.executable,
                "-m",
                "git_filter_repo",
                "--force",
                "--replace-text",
                handle.name,
            ),
            cwd=ROOT,
            check=True,
        )

    remotes = _git_text("remote").splitlines()
    if "origin" not in remotes:
        subprocess.run(
            ("git", "remote", "add", "origin", remote_url),
            cwd=ROOT,
            check=True,
        )
    return {
        "ok": True,
        "rewritten_rules": labels,
        "secret_values_redacted": True,
        "new_head": _git_text("rev-parse", "HEAD")[:12],
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = rewrite(
            secret_env=args.secret_env,
            confirm=args.confirm,
        )
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": exc.__class__.__name__,
                    "secret_values_redacted": True,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

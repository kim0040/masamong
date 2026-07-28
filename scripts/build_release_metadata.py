#!/usr/bin/env python3
"""불변 배포 아카이브용 커밋 메타데이터를 원자적으로 생성합니다."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed.stdout.strip()


def build_payload(repo: Path, max_commits: int) -> dict[str, object]:
    commit_sha = _git(repo, "rev-parse", "HEAD")
    subjects = _git(
        repo,
        "log",
        "-n",
        str(max_commits),
        "--pretty=format:%s",
    ).splitlines()
    return {
        "schema_version": 1,
        "commit_sha": commit_sha,
        "commits": [subject[:240] for subject in subjects if subject.strip()],
    }


def write_atomic(output: Path, payload: dict[str, object]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        dir=str(output.parent),
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-commits", type=int, default=10)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.max_commits <= 50:
        raise SystemExit("--max-commits는 1~50이어야 합니다.")
    repo = args.repo.expanduser().resolve()
    output = args.output.expanduser().resolve()
    payload = build_payload(repo, args.max_commits)
    write_atomic(output, payload)
    print(
        f"release metadata: {str(payload['commit_sha'])[:12]} "
        f"({len(payload['commits'])} commits) -> {output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""추적 파일과 전체 Git 이력의 비밀값 노출을 값 출력 없이 감사한다.

실제 자격증명은 지정한 env 파일에서만 읽어 메모리에서 비교한다. 결과에는
비밀값, 일치한 줄, 파일 내용이 아니라 규칙명·경로·commit 앞자리만 출력한다.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]

_PLACEHOLDER_MARKERS = (
    "your_",
    "your-",
    "replace-",
    "example",
    "placeholder",
    "changeme",
    "<",
    "${",
)
_SENSITIVE_ENV_NAME = re.compile(
    r"(?:KEY|TOKEN|PASSWORD|SECRET|SUPERADMIN_USER_IDS|"
    r"EXPECTED_DISCORD_BOT_USER_ID|DB_HOST|DB_USER)$",
    re.IGNORECASE,
)
_STRONG_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("openai_style_token", re.compile(rb"sk-[A-Za-z0-9_-]{20,}")),
    (
        "discord_bot_token",
        re.compile(rb"[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}"),
    ),
    ("github_token", re.compile(rb"(?:ghp|github_pat)_[A-Za-z0-9_]{20,}")),
    (
        "private_key",
        re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
    (
        "tracked_discord_identity",
        re.compile(
            rb"(?:MASAMONG_SUPERADMIN_USER_IDS|"
            rb"MASAMONG_EXPECTED_DISCORD_BOT_USER_ID)\s*=\s*[0-9]{15,22}"
        ),
    ),
)


@dataclass(frozen=True)
class Finding:
    scope: str
    rule: str
    path: str
    revision: str | None = None

    def as_dict(self) -> dict[str, str]:
        result = {
            "scope": self.scope,
            "rule": self.rule,
            "path": self.path,
        }
        if self.revision:
            result["revision"] = self.revision[:12]
        return result


def _is_expected_test_fixture(rule: str, path: str) -> bool:
    """명백한 Discord 형식 검증용 테스트 값만 strong-pattern 오탐에서 제외한다.

    실제 env 값과의 exact 비교는 경로와 무관하게 계속 수행하므로, 운영 토큰이
    테스트 파일에 복사된 경우에는 여전히 탐지된다.
    """
    return path.startswith("tests/") and rule in {
        "discord_bot_token",
        "tracked_discord_identity",
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="추적 파일과 Git 전체 이력의 비밀정보 비노출 감사"
    )
    parser.add_argument(
        "--secret-env",
        action="append",
        default=[],
        help="실제 비밀값을 읽을 비추적 env 파일(여러 번 지정 가능)",
    )
    parser.add_argument("--current-only", action="store_true")
    parser.add_argument("--max-findings", type=int, default=200)
    return parser.parse_args(argv)


def _run_git(*args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ("git", *args),
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def _tracked_paths() -> list[str]:
    output = _run_git("ls-files", "-z").stdout
    return [
        item.decode("utf-8", errors="surrogateescape")
        for item in output.split(b"\0")
        if item
    ]


def _read_env_secrets(paths: Iterable[str]) -> dict[str, bytes]:
    secrets: dict[str, bytes] = {}
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if not path.is_file():
            continue
        for raw_line in path.read_text(
            encoding="utf-8",
            errors="ignore",
        ).splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name = name.strip()
            value = value.strip().strip("\"'")
            if not _SENSITIVE_ENV_NAME.search(name):
                continue
            lowered = value.lower()
            if (
                len(value) < 8
                or not value
                or any(marker in lowered for marker in _PLACEHOLDER_MARKERS)
            ):
                continue
            secrets[name] = value.encode("utf-8")
    return secrets


def _scan_current(secret_values: dict[str, bytes]) -> list[Finding]:
    findings: list[Finding] = []
    for relative in _tracked_paths():
        path = ROOT / relative
        try:
            data = path.read_bytes()
        except OSError:
            continue
        for rule, pattern in _STRONG_PATTERNS:
            if pattern.search(data) and not _is_expected_test_fixture(
                rule,
                relative,
            ):
                findings.append(Finding("current", rule, relative))
        for label, secret in secret_values.items():
            if secret and secret in data:
                findings.append(
                    Finding("current", f"known_env_value:{label}", relative)
                )
    return findings


def _history_revisions() -> list[str]:
    return [
        item
        for item in _run_git("rev-list", "--all").stdout.decode().splitlines()
        if item
    ]


def _history_paths_for_pattern(pattern: bytes, revisions: list[str]) -> list[str]:
    if not revisions:
        return []
    result = subprocess.run(
        (
            "git",
            "grep",
            "-I",
            "-l",
            "-E",
            "-e",
            pattern.decode("ascii"),
            *revisions,
            "--",
        ),
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError("git history pattern scan failed")
    return result.stdout.decode("utf-8", errors="replace").splitlines()


def _history_paths_for_secret(secret: bytes, revisions: list[str]) -> list[str]:
    if not revisions:
        return []
    result = subprocess.run(
        (
            "git",
            "grep",
            "-I",
            "-l",
            "-F",
            "-e",
            secret.decode("utf-8", errors="strict"),
            *revisions,
            "--",
        ),
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError("git history exact-secret scan failed")
    return result.stdout.decode("utf-8", errors="replace").splitlines()


def _split_history_match(value: str) -> tuple[str | None, str]:
    revision, separator, path = value.partition(":")
    if not separator:
        return None, value
    return revision, path


def _scan_history(secret_values: dict[str, bytes]) -> list[Finding]:
    revisions = _history_revisions()
    findings: list[Finding] = []
    # Python 정규식을 Git ERE로 다시 쓰되, 실제 일치 값은 -l로 숨긴다.
    history_patterns = (
        ("openai_style_token", rb"sk-[A-Za-z0-9_-]{20,}"),
        (
            "discord_bot_token",
            rb"[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}",
        ),
        ("github_token", rb"(ghp|github_pat)_[A-Za-z0-9_]{20,}"),
        (
            "private_key",
            rb"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----",
        ),
        (
            "tracked_discord_identity",
            rb"(MASAMONG_SUPERADMIN_USER_IDS|"
            rb"MASAMONG_EXPECTED_DISCORD_BOT_USER_ID)[[:space:]]*="
            rb"[[:space:]]*[0-9]{15,22}",
        ),
    )
    for rule, pattern in history_patterns:
        for match in _history_paths_for_pattern(pattern, revisions):
            revision, path = _split_history_match(match)
            if _is_expected_test_fixture(rule, path):
                continue
            findings.append(Finding("history", rule, path, revision))
    for label, secret in secret_values.items():
        for match in _history_paths_for_secret(secret, revisions):
            revision, path = _split_history_match(match)
            findings.append(
                Finding(
                    "history",
                    f"known_env_value:{label}",
                    path,
                    revision,
                )
            )
    return findings


def audit(
    *,
    secret_env_paths: Iterable[str],
    include_history: bool,
    max_findings: int,
) -> dict[str, object]:
    secret_values = _read_env_secrets(secret_env_paths)
    findings = _scan_current(secret_values)
    if include_history:
        findings.extend(_scan_history(secret_values))

    unique: dict[tuple[str, str, str, str | None], Finding] = {}
    for finding in findings:
        key = (
            finding.scope,
            finding.rule,
            finding.path,
            finding.revision,
        )
        unique[key] = finding
    ordered = sorted(
        unique.values(),
        key=lambda item: (
            item.scope,
            item.rule,
            item.path,
            item.revision or "",
        ),
    )
    limit = max(1, int(max_findings))
    summary = Counter((item.scope, item.rule) for item in ordered)
    return {
        "ok": not ordered,
        "scanned_known_secret_labels": sorted(secret_values),
        "finding_count": len(ordered),
        "finding_summary": {
            f"{scope}:{rule}": count
            for (scope, rule), count in sorted(summary.items())
        },
        "findings_truncated": len(ordered) > limit,
        "findings": [item.as_dict() for item in ordered[:limit]],
        "values_redacted": True,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = audit(
        secret_env_paths=args.secret_env,
        include_history=not args.current_only,
        max_findings=args.max_findings,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

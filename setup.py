#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""마사몽 인스턴스 설치 전 점검 도구.

인자 없이 실행하면 어떤 env/config/DB 파일도 만들거나 수정하지 않는다.
새 General SQLite DB 초기화는 명시적인 프로필 파일과 새 대상 경로를
재확인한 경우에만 별도 옵션으로 수행한다.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parent
GENERAL_EXAMPLE = PROJECT_ROOT / "profiles" / "general.env.example"
MASAMO_EXAMPLE = PROJECT_ROOT / "profiles" / "masamo.env.example"
PROFILE_VALIDATOR = PROJECT_ROOT / "scripts" / "validate_profile_separation.py"
LEGACY_DATABASE = (PROJECT_ROOT / "database" / "remasamong.db").resolve()
PROFILE_SENTINEL = "__MASAMONG_SETUP_PROFILE__="
REQUIRED_FRESH_TABLES = frozenset(
    {
        "guild_settings",
        "system_counters",
        "user_profiles",
    }
)


class SetupSafetyError(RuntimeError):
    """안전 조건을 충족하지 못해 설치 작업을 중단해야 하는 경우."""


def check_python_version() -> bool:
    """Python 버전이 3.10 이상인지 확인한다."""
    if sys.version_info < (3, 10):
        print("❌ Python 3.10 이상이 필요합니다.")
        print(f"현재 버전: {sys.version}")
        return False
    print(f"✅ Python 버전 확인: {sys.version.split()[0]}")
    return True


def install_requirements() -> bool:
    """사용자가 명시적으로 요청한 경우에만 의존성을 설치한다."""
    print("\n📦 의존성 설치 중...")
    try:
        subprocess.check_call(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "-r",
                str(PROJECT_ROOT / "requirements.txt"),
            ],
            cwd=PROJECT_ROOT,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"❌ 의존성 설치 실패: {exc}")
        return False
    print("✅ 의존성 설치 완료")
    return True


def print_profile_guidance() -> None:
    """두 운영 프로필을 섞지 않는 다음 단계를 출력한다."""
    print(
        "\n🔒 기본 실행은 파일이나 데이터베이스를 변경하지 않았습니다."
    )
    print("프로필별로 별도의 env·Discord 봇·DB·설정 파일을 준비하세요.")
    print(f"- 새 General 예시: {GENERAL_EXAMPLE}")
    print(f"- 기존 Masamo 경계 예시: {MASAMO_EXAMPLE}")
    print(
        "- 두 파일 점검: "
        f"{sys.executable} {PROFILE_VALIDATOR} "
        "/absolute/path/masamo.env /absolute/path/general.env"
    )
    print(
        "- 단일 프로필 읽기 점검: "
        f"{sys.executable} setup.py --profile-env /absolute/path/general.env"
    )
    print(
        "기존 Masamo DB는 이 도구로 초기화하지 않습니다. "
        "검증된 별도 migration 절차를 사용하세요."
    )


def _profile_environment(env_path: Path) -> dict[str, str]:
    """선택 프로필 밖의 오래된 MASAMONG_* 값을 자식에 넘기지 않는다."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("MASAMONG_")
    }
    environment["MASAMONG_ENV_FILE"] = str(env_path)
    return environment


def inspect_profile(env_path: Path) -> dict[str, object]:
    """config를 쓰기 없이 별도 프로세스에서 로드해 실효값을 확인한다."""
    selected_path = env_path.expanduser().resolve()
    if not selected_path.is_file():
        raise SetupSafetyError(
            f"프로필 env 파일을 찾을 수 없습니다: {selected_path}"
        )

    inspection_code = (
        "import json, config; "
        "print("
        + repr(PROFILE_SENTINEL)
        + " + json.dumps({"
        "'profile': config.PROFILE, "
        "'instance': config.INSTANCE_NAME, "
        "'explicit': config.REQUIRE_EXPLICIT_PROFILE, "
        "'env_file': str(config.ENV_FILE_PATH or ''), "
        "'backend': config.DB_BACKEND, "
        "'database_file': config.DATABASE_FILE, "
        "'auto_migrate': config.AUTO_MIGRATE"
        "}, sort_keys=True))"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", inspection_code],
            cwd=PROJECT_ROOT,
            env=_profile_environment(selected_path),
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupSafetyError(
            f"프로필 읽기 점검을 실행하지 못했습니다: {exc}"
        ) from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if len(detail) > 2_000:
            detail = detail[-2_000:]
        raise SetupSafetyError(
            "프로필 설정 검증에 실패했습니다. 의존성을 먼저 설치하고 "
            f"env 및 연결된 JSON 파일을 확인하세요.\n{detail}"
        )

    payload_line = next(
        (
            line[len(PROFILE_SENTINEL) :]
            for line in reversed(result.stdout.splitlines())
            if line.startswith(PROFILE_SENTINEL)
        ),
        "",
    )
    try:
        payload = json.loads(payload_line)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SetupSafetyError(
            "프로필 점검 결과를 해석할 수 없어 안전하게 중단했습니다."
        ) from exc
    if not isinstance(payload, dict):
        raise SetupSafetyError(
            "프로필 점검 결과가 객체가 아니어서 안전하게 중단했습니다."
        )
    if Path(str(payload.get("env_file", ""))).resolve() != selected_path:
        raise SetupSafetyError(
            "선택한 env와 config가 읽은 env가 달라 안전하게 중단했습니다."
        )
    return payload


def _new_general_database_path(
    profile: dict[str, object],
    *,
    confirmation: str | None,
) -> Path:
    """새 General SQLite 대상으로 확정할 수 있는지 검사한다."""
    if profile.get("profile") != "general" or profile.get("instance") != "general":
        raise SetupSafetyError(
            "새 DB bootstrap은 general 프로필/인스턴스에서만 허용됩니다."
        )
    if profile.get("explicit") is not True:
        raise SetupSafetyError(
            "MASAMONG_REQUIRE_EXPLICIT_PROFILE=true인 General만 bootstrap할 수 있습니다."
        )
    if profile.get("backend") != "sqlite":
        raise SetupSafetyError(
            "이 도구는 원격 DB를 초기화하지 않습니다. "
            "새 General SQLite 대상만 지원합니다."
        )
    if profile.get("auto_migrate") is not True:
        raise SetupSafetyError(
            "새 General bootstrap env에는 MASAMONG_AUTO_MIGRATE=true를 "
            "명시해야 합니다."
        )

    raw_database_path = str(profile.get("database_file") or "").strip()
    if not raw_database_path or raw_database_path == ":memory:":
        raise SetupSafetyError(
            "bootstrap 대상은 :memory:가 아닌 새 SQLite 파일이어야 합니다."
        )
    configured_database_path = Path(raw_database_path).expanduser()
    if not configured_database_path.is_absolute():
        raise SetupSafetyError(
            "새 General SQLite 파일은 절대 경로로 지정해야 합니다."
        )
    if configured_database_path.is_symlink():
        raise SetupSafetyError(
            "새 General SQLite 대상은 심볼릭 링크일 수 없습니다."
        )
    database_path = configured_database_path.resolve()
    if database_path == LEGACY_DATABASE or database_path.name == "remasamong.db":
        raise SetupSafetyError(
            "레거시 remasamong.db는 bootstrap 대상으로 사용할 수 없습니다."
        )
    if "general" not in database_path.as_posix().lower():
        raise SetupSafetyError(
            "인스턴스 혼동 방지를 위해 SQLite 경로에 "
            "'general'이 포함되어야 합니다."
        )
    if not confirmation:
        raise SetupSafetyError(
            "DB를 만들려면 --confirm-new-general-db에 대상 절대 경로를 "
            "똑같이 다시 입력해야 합니다."
        )
    confirmed_path = Path(confirmation).expanduser()
    if not confirmed_path.is_absolute() or confirmed_path.resolve() != database_path:
        raise SetupSafetyError(
            "--confirm-new-general-db 값이 실효 SQLite 경로와 다릅니다."
        )
    if database_path.exists():
        raise SetupSafetyError(
            f"대상 DB가 이미 존재하므로 수정하지 않습니다: {database_path}"
        )
    parent = database_path.parent
    if not parent.is_dir():
        raise SetupSafetyError(
            "대상 상위 디렉터리가 없습니다. 먼저 별도로 준비하세요: "
            f"{parent}"
        )
    return database_path


def _reserve_new_database(database_path: Path) -> None:
    """TOCTOU를 막기 위해 새 DB 이름을 배타적으로 선점한다."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(database_path, flags, 0o600)
    except FileExistsError as exc:
        raise SetupSafetyError(
            f"대상 DB가 방금 생성되어 수정하지 않습니다: {database_path}"
        ) from exc
    except OSError as exc:
        raise SetupSafetyError(
            f"새 DB 대상을 안전하게 선점할 수 없습니다: {exc}"
        ) from exc
    else:
        os.close(descriptor)


def _run_database_initializer(env_path: Path) -> bool:
    """선택한 env만 전달해 신규 SQLite 스키마 초기화를 실행한다."""
    try:
        subprocess.check_call(
            [sys.executable, str(PROJECT_ROOT / "database" / "init_db.py")],
            cwd=PROJECT_ROOT,
            env=_profile_environment(env_path),
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"❌ 새 General DB 초기화 실패: {exc}")
        return False
    return True


def _verify_fresh_database(database_path: Path) -> bool:
    """생성된 SQLite DB를 읽기 전용으로 최소 검증한다."""
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{database_path.as_uri()}?mode=ro",
            uri=True,
            timeout=5,
        )
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    except sqlite3.Error as exc:
        print(f"❌ 생성된 SQLite DB 검증 실패: {exc}")
        return False
    finally:
        if connection is not None:
            connection.close()
    if not integrity or integrity[0] != "ok":
        print("❌ 생성된 SQLite DB 무결성 검증에 실패했습니다.")
        return False
    missing = REQUIRED_FRESH_TABLES - tables
    if missing:
        print(
            "❌ 생성된 SQLite DB에 필수 테이블이 없습니다: "
            + ", ".join(sorted(missing))
        )
        return False
    return True


def bootstrap_new_general_sqlite(
    env_path: Path,
    *,
    confirmation: str | None,
) -> bool:
    """명시적으로 확인된, 존재하지 않는 General SQLite DB만 초기화한다."""
    selected_path = env_path.expanduser().resolve()
    try:
        profile = inspect_profile(selected_path)
        database_path = _new_general_database_path(
            profile,
            confirmation=confirmation,
        )
        _reserve_new_database(database_path)
    except SetupSafetyError as exc:
        print(f"❌ {exc}")
        return False

    print(f"\n🗃️ 새 General SQLite DB 초기화: {database_path}")
    if not _run_database_initializer(selected_path):
        print(
            "⚠️  안전을 위해 자동 재시도하거나 파일을 "
            "삭제하지 않았습니다. "
            f"새로 만든 파일을 직접 점검하세요: {database_path}"
        )
        return False
    if not _verify_fresh_database(database_path):
        print(
            "⚠️  불완전한 DB일 수 있으므로 봇을 시작하지 말고 "
            "파일을 직접 점검하세요."
        )
        return False
    print("✅ 새 General SQLite DB 초기화 및 읽기 전용 검증 완료")
    print(
        "⚠️  최초 bootstrap 확인 후 정상 운영 env에서는 "
        "MASAMONG_AUTO_MIGRATE=false로 전환하고 다시 점검하세요."
    )
    return True


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "기본 실행은 읽기 전용 안내만 제공합니다. "
            "레거시 .env/config/DB를 자동 생성하지 않습니다."
        )
    )
    parser.add_argument(
        "--install-requirements",
        action="store_true",
        help="requirements.txt 설치를 명시적으로 실행",
    )
    parser.add_argument(
        "--profile-env",
        type=Path,
        help="쓰기 없이 config 로드를 점검할 명시적 프로필 env",
    )
    parser.add_argument(
        "--bootstrap-new-general-sqlite",
        action="store_true",
        help="존재하지 않는 새 General SQLite DB만 초기화",
    )
    parser.add_argument(
        "--confirm-new-general-db",
        help="bootstrap 대상 SQLite 절대 경로를 동일하게 재입력",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> bool:
    """안전한 설치 점검 진입점."""
    args = _build_parser().parse_args(argv)
    print("🤖 마사몽 인스턴스 설치 전 점검")
    print("=" * 50)

    if not check_python_version():
        return False
    if args.install_requirements and not install_requirements():
        return False
    if args.bootstrap_new_general_sqlite and args.profile_env is None:
        print("❌ bootstrap에는 --profile-env가 반드시 필요합니다.")
        return False
    if args.confirm_new_general_db and not args.bootstrap_new_general_sqlite:
        print(
            "❌ --confirm-new-general-db는 "
            "--bootstrap-new-general-sqlite와 함께 사용해야 합니다."
        )
        return False

    if args.bootstrap_new_general_sqlite:
        success = bootstrap_new_general_sqlite(
            args.profile_env,
            confirmation=args.confirm_new_general_db,
        )
        print_profile_guidance()
        return success

    if args.profile_env is not None:
        try:
            profile = inspect_profile(args.profile_env)
        except SetupSafetyError as exc:
            print(f"❌ {exc}")
            print_profile_guidance()
            return False
        print(
            "✅ 프로필 읽기 점검 완료: "
            f"profile={profile.get('profile')}, "
            f"backend={profile.get('backend')}"
        )

    print_profile_guidance()
    return True


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)

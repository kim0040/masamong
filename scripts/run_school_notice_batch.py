#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""학교 공지 batch를 마사몽 DB와 연결해 실행합니다.

봇 프로세스가 아니라 systemd timer/cron이 이 스크립트를 호출합니다. 크롤링과
분석은 코어(`school_notice`)가 별도 프로세스로 수행하므로 봇의 상주 CPU/RSS
예산에 영향을 주지 않습니다.

흐름:
    마사몽 DB에서 활성 프로필 export
      → 미반영 피드백을 사용자 프로필로 코어에 먼저 전달
      → 사용자별로 코어 CLI 실행 (사용자별 output 디렉터리로 분리)
      → 계약 검증한 산출물만 원자적으로 공개
      → 실행 상태를 school_notice_batch_runs에 기록

`--core-python`과 `--core-cwd`로 코어 위치를 지정합니다. 기본 운영 배포는
이 저장소에 고정된 ``school_notice`` 패키지를 같은 release에서 실행해 봇
adapter와 core의 버전이 어긋나지 않게 합니다.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

import aiosqlite

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from database.compat_db import TiDBSettings, connect_main_db  # noqa: E402
from utils.privacy_consent import (  # noqa: E402
    CONSENT_GRANTED,
    SCHOOL_NOTICE_SCOPE,
    get_policy,
)
from utils.school_notice_contract import (  # noqa: E402
    DigestContractError,
    FEEDBACK_TYPES,
    RUN_STATUSES,
    digest_path_for,
    load_digest,
)
from utils.school_notice_profile import (  # noqa: E402
    SchoolProfileError,
    load_school_catalog,
    profile_snapshot_hash,
)

SCHOOL_NOTICE_CONSENT_POLICY = get_policy(SCHOOL_NOTICE_SCOPE)
KST = ZoneInfo("Asia/Seoul")

_SAFE_USER_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_SCHOOL_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MAX_SOURCE_CONFIG_BYTES = 4 * 1024 * 1024
_MAX_SOURCES = 128
_MAX_RUN_REPORT_BYTES = 1024 * 1024
_MAX_PROFILE_JSON_CHARS = 64 * 1024
_MAX_FEEDBACK_EXTERNAL_ID_CHARS = 128
_MAX_FEEDBACK_TOPIC_CHARS = 256
_MAX_FEEDBACK_STDOUT_BYTES = 64 * 1024
_BATCH_USER_ID_KEY = "_batch_user_id"
_BATCH_PROFILE_VERSION_KEY = "_batch_profile_version"
_BATCH_PROFILE_HASH_KEY = "_batch_profile_hash"
_BATCH_PROFILE_JSON_KEY = "_batch_profile_json"
_BATCH_INTERNAL_KEYS = frozenset(
    {
        _BATCH_USER_ID_KEY,
        _BATCH_PROFILE_VERSION_KEY,
        _BATCH_PROFILE_HASH_KEY,
        _BATCH_PROFILE_JSON_KEY,
    }
)
_STALE_RUN_DIR = re.compile(r"^run-[A-Za-z0-9_-]{1,64}$")


class BatchAlreadyRunning(RuntimeError):
    """같은 digest root에서 다른 batch가 실행 중입니다."""


class BatchDeadlineExceeded(TimeoutError):
    """전체 batch 실행 시간 상한을 소진했습니다."""


class SourceSelectionError(ValueError):
    """프로필 학교와 코어 source 설정을 안전하게 결합할 수 없습니다."""


@dataclass(frozen=True)
class BatchLimits:
    """저사양 서버에서 한 번의 batch가 점유할 수 있는 자원 상한."""

    max_profiles: int
    profile_timeout_seconds: int
    feedback_timeout_seconds: int
    deadline_seconds: int


@dataclass(frozen=True)
class ProfileLoadResult:
    """검증된 프로필과 제외된 활성 프로필 수를 함께 보존합니다.

    기존 내부 호출부가 ``list``처럼 순회/인덱싱할 수 있게 하되, 잘못된 활성
    프로필이 있었는지를 ``run_batch``가 잃어버리지 않도록 진단값을 붙입니다.
    """

    profiles: tuple[dict, ...]
    invalid_count: int = 0

    def __iter__(self):
        return iter(self.profiles)

    def __len__(self) -> int:
        return len(self.profiles)

    def __getitem__(self, index):
        return self.profiles[index]


def parse_args() -> argparse.Namespace:
    """CLI 인자를 파싱합니다."""
    parser = argparse.ArgumentParser(description="학교 공지 batch 실행")
    parser.add_argument("--core-python", required=True, help="코어 venv의 python 경로")
    parser.add_argument("--core-cwd", required=True, help="코어 저장소 루트")
    parser.add_argument(
        "--source-config",
        help="코어 sources.json 경로. 생략하면 <core-cwd>/school_notice/sources.json",
    )
    parser.add_argument(
        "--date",
        help="실행 기준일 (YYYY-MM-DD). 생략 시 Asia/Seoul 오늘",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        default=True,
        help="공지 분석 LLM을 사용하지 않음(안전한 수동/초기 확인 기본값)",
    )
    parser.add_argument(
        "--use-llm",
        dest="no_llm",
        action="store_false",
        help="공개 공지 상세 본문 분석에 LLM 사용(운영 timer가 명시)",
    )
    parser.add_argument(
        "--low-resource",
        action="store_true",
        default=True,
        help="1스레드 저자원 모드(기본값)",
    )
    parser.add_argument("--max-details-per-source", type=int)
    parser.add_argument("--max-requests", type=int)
    parser.add_argument("--max-profiles", type=int)
    parser.add_argument("--profile-timeout-seconds", type=int)
    parser.add_argument("--feedback-timeout-seconds", type=int)
    parser.add_argument("--batch-deadline-seconds", type=int)
    parser.add_argument(
        "--only-user-id",
        type=int,
        help=(
            "등록 직후 초기 확인용 Discord 사용자 ID. 지정하면 동의된 해당 "
            "프로필 한 건만 읽고 다른 사용자 프로필은 조회하지 않습니다."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="코어를 실행하지 않고 대상 프로필만 출력",
    )
    return parser.parse_args()


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    """설정 오타가 무제한 실행으로 이어지지 않도록 정수 범위를 고정합니다."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def batch_limits(args: argparse.Namespace) -> BatchLimits:
    """CLI override 또는 config 값을 안전한 범위로 정규화합니다."""

    def selected(arg_name: str, config_name: str, default: int) -> object:
        cli_value = getattr(args, arg_name, None)
        if cli_value is not None:
            return cli_value
        return getattr(config, config_name, default)

    return BatchLimits(
        max_profiles=_bounded_int(
            selected(
                "max_profiles",
                "SCHOOL_NOTICE_BATCH_MAX_PROFILES",
                50,
            ),
            default=50,
            minimum=1,
            maximum=500,
        ),
        profile_timeout_seconds=_bounded_int(
            selected(
                "profile_timeout_seconds",
                "SCHOOL_NOTICE_BATCH_PROFILE_TIMEOUT_SECONDS",
                600,
            ),
            default=600,
            minimum=1,
            maximum=1800,
        ),
        feedback_timeout_seconds=_bounded_int(
            selected(
                "feedback_timeout_seconds",
                "SCHOOL_NOTICE_BATCH_FEEDBACK_TIMEOUT_SECONDS",
                60,
            ),
            default=60,
            minimum=1,
            maximum=300,
        ),
        deadline_seconds=_bounded_int(
            getattr(args, "batch_deadline_seconds", None)
            if getattr(args, "batch_deadline_seconds", None) is not None
            else getattr(
                config,
                "SCHOOL_NOTICE_BATCH_TOTAL_TIMEOUT_SECONDS",
                getattr(
                    config,
                    "SCHOOL_NOTICE_BATCH_DEADLINE_SECONDS",
                    1_800,
                ),
            ),
            default=1_800,
            minimum=1,
            maximum=7_200,
        ),
    )


def kst_today() -> date:
    """서버 로컬 timezone과 무관한 학교 공지 기준일."""
    return datetime.now(KST).date()


async def open_db(*, read_only: bool = False):
    """마사몽 운영 DB에 연결합니다."""
    if read_only and config.DB_BACKEND == "sqlite":
        database_path = Path(config.DATABASE_FILE).expanduser().resolve()
        # mode=ro는 --dry-run이 경로 오타로 새 빈 DB를 만드는 것도 막는다.
        return await aiosqlite.connect(
            f"{database_path.as_uri()}?mode=ro",
            uri=True,
        )

    tidb_settings = None
    if config.DB_BACKEND == "tidb":
        tidb_settings = TiDBSettings(
            host=config.TIDB_HOST or "",
            port=config.TIDB_PORT,
            user=config.TIDB_USER or "",
            password=config.TIDB_PASSWORD or "",
            database=config.TIDB_NAME,
            ssl_ca=config.TIDB_SSL_CA,
            ssl_verify_identity=config.TIDB_SSL_VERIFY_IDENTITY,
            require_tls=config.REQUIRE_DB_TLS,
        )
    return await connect_main_db(
        config.DB_BACKEND,
        sqlite_path=config.DATABASE_FILE,
        tidb_settings=tidb_settings,
    )


async def load_profiles(
    db,
    *,
    only_user_id: int | None = None,
) -> ProfileLoadResult:
    """전달 대상 프로필을 읽어옵니다.

    ``only_user_id``는 등록 직후 한 번 실행하는 최소 범위 수집용이다. 양수가
    아니면 명시적으로 거부하며, SQL 조건을 DB에서 적용해 다른 사용자 프로필을
    애플리케이션 메모리로 읽지 않는다.
    """
    if only_user_id is not None and int(only_user_id) <= 0:
        raise ValueError("--only-user-id는 양의 정수여야 합니다.")
    query = """
        SELECT snp.user_id, snp.user_key, snp.school_id,
               snp.profile_version, snp.profile_json
        FROM school_notice_profiles AS snp
        JOIN privacy_consents AS pc
          ON pc.user_id = snp.user_id
         AND pc.scope = ?
         AND pc.policy_version = ?
         AND pc.notice_hash = ?
         AND pc.status = ?
         AND pc.granted_at IS NOT NULL
         AND pc.withdrawn_at IS NULL
        LEFT JOIN (
            SELECT user_key, MAX(finished_at) AS last_finished
            FROM school_notice_batch_runs
            GROUP BY user_key
        ) AS latest_run
          ON latest_run.user_key = snp.user_key
        WHERE snp.enabled = 1
    """
    params: list[object] = [
        SCHOOL_NOTICE_CONSENT_POLICY.scope,
        SCHOOL_NOTICE_CONSENT_POLICY.version,
        SCHOOL_NOTICE_CONSENT_POLICY.notice_hash,
        CONSENT_GRANTED,
    ]
    if only_user_id is not None:
        query += "\n AND snp.user_id = ?"
        params.append(int(only_user_id))
    query += """
        ORDER BY
            CASE WHEN latest_run.last_finished IS NULL THEN 0 ELSE 1 END,
            latest_run.last_finished,
            snp.user_key
    """
    async with db.execute(query, tuple(params)) as cursor:
        rows = await cursor.fetchall()
    profiles = []
    invalid_count = 0
    for row in rows:
        payload = _profile_from_row(row)
        if payload is None:
            invalid_count += 1
            continue
        profiles.append(payload)
    if invalid_count:
        print(
            f"경고: 유효하지 않은 활성 프로필 {invalid_count}건을 제외했습니다.",
            file=sys.stderr,
        )
    return ProfileLoadResult(tuple(profiles), invalid_count=invalid_count)


def _profile_from_row(row) -> dict | None:
    """DB 행을 검증하고 batch 전용 snapshot 메타데이터를 붙입니다."""
    try:
        user_id = int(row[0])
        user_key = str(row[1])
        school_id = str(row[2])
        profile_version = int(row[3])
        raw_profile = row[4]
    except (IndexError, TypeError, ValueError):
        return None
    if (
        user_id <= 0
        or profile_version <= 0
        or not isinstance(raw_profile, str)
        or len(raw_profile) > _MAX_PROFILE_JSON_CHARS
        or not _SAFE_USER_KEY.fullmatch(user_key)
        or not _SAFE_SCHOOL_ID.fullmatch(school_id)
    ):
        return None
    try:
        payload = json.loads(raw_profile)
    except (TypeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("school_id") not in (None, school_id)
        or any(key in payload for key in _BATCH_INTERNAL_KEYS)
    ):
        return None
    try:
        snapshot_hash = profile_snapshot_hash(payload)
    except SchoolProfileError:
        return None

    # 파일 경로와 source 선택에 쓰는 식별자는 정규화 DB 컬럼이 기준이다.
    # 내부 snapshot 값은 profile.json export 직전에 반드시 제거한다.
    payload["user_key"] = user_key
    payload["school_id"] = school_id
    payload[_BATCH_USER_ID_KEY] = user_id
    payload[_BATCH_PROFILE_VERSION_KEY] = profile_version
    payload[_BATCH_PROFILE_HASH_KEY] = snapshot_hash
    payload[_BATCH_PROFILE_JSON_KEY] = raw_profile
    return payload


def _has_snapshot_metadata(profile: dict) -> bool:
    return all(key in profile for key in _BATCH_INTERNAL_KEYS)


async def current_profile_snapshot(db, expected: dict) -> dict | None:
    """처리 직전 enabled/동의/버전/JSON이 최초 snapshot과 같은지 재검증."""
    if not _has_snapshot_metadata(expected):
        # 순수 단위 테스트나 내부 재사용용 프로필. 실제 run_batch 경로는
        # load_profiles()가 항상 snapshot 메타데이터를 붙인다.
        return expected
    async with db.execute(
        """
        SELECT snp.user_id, snp.user_key, snp.school_id,
               snp.profile_version, snp.profile_json
        FROM school_notice_profiles AS snp
        JOIN privacy_consents AS pc
          ON pc.user_id = snp.user_id
         AND pc.scope = ?
         AND pc.policy_version = ?
         AND pc.notice_hash = ?
         AND pc.status = ?
         AND pc.granted_at IS NOT NULL
         AND pc.withdrawn_at IS NULL
        WHERE snp.user_id = ?
          AND snp.user_key = ?
          AND snp.enabled = 1
        """,
        (
            SCHOOL_NOTICE_CONSENT_POLICY.scope,
            SCHOOL_NOTICE_CONSENT_POLICY.version,
            SCHOOL_NOTICE_CONSENT_POLICY.notice_hash,
            CONSENT_GRANTED,
            int(expected[_BATCH_USER_ID_KEY]),
            str(expected["user_key"]),
        ),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    current = _profile_from_row(row)
    if current is None:
        return None
    snapshot_fields = (
        "user_key",
        "school_id",
        _BATCH_USER_ID_KEY,
        _BATCH_PROFILE_VERSION_KEY,
        _BATCH_PROFILE_HASH_KEY,
    )
    if any(current[field] != expected[field] for field in snapshot_fields):
        return None
    return current


async def pending_feedback_for_profile(db, profile: dict) -> list[dict]:
    """동일한 최신 profile snapshot과 동의에 결합된 feedback만 다시 읽습니다."""
    if not _has_snapshot_metadata(profile):
        return []
    async with db.execute(
        """
        SELECT snf.id, snf.user_key, snf.source_id, snf.external_id,
               snf.feedback_type, snf.topic
        FROM school_notice_feedback AS snf
        JOIN school_notice_profiles AS snp
          ON snp.user_key = snf.user_key
        JOIN privacy_consents AS pc
          ON pc.user_id = snp.user_id
         AND pc.scope = ?
         AND pc.policy_version = ?
         AND pc.notice_hash = ?
         AND pc.status = ?
         AND pc.granted_at IS NOT NULL
         AND pc.withdrawn_at IS NULL
        WHERE snf.consumed_at IS NULL
          AND snp.user_id = ?
          AND snp.user_key = ?
          AND snp.profile_version = ?
          AND snp.profile_json = ?
          AND snp.enabled = 1
        ORDER BY snf.id
        """,
        (
            SCHOOL_NOTICE_CONSENT_POLICY.scope,
            SCHOOL_NOTICE_CONSENT_POLICY.version,
            SCHOOL_NOTICE_CONSENT_POLICY.notice_hash,
            CONSENT_GRANTED,
            int(profile[_BATCH_USER_ID_KEY]),
            str(profile["user_key"]),
            int(profile[_BATCH_PROFILE_VERSION_KEY]),
            str(profile[_BATCH_PROFILE_JSON_KEY]),
        ),
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        {
            "id": int(row[0]),
            "user_key": str(row[1]),
            "source_id": str(row[2] or ""),
            "external_id": str(row[3] or ""),
            "feedback_type": str(row[4]),
            "topic": row[5],
        }
        for row in rows
    ]


async def pending_feedback(db) -> list[dict]:
    """아직 코어에 반영하지 않은 피드백."""
    async with db.execute(
        """
        SELECT snf.id, snf.user_key, snf.source_id, snf.external_id,
               snf.feedback_type, snf.topic
        FROM school_notice_feedback AS snf
        JOIN school_notice_profiles AS snp
          ON snp.user_key = snf.user_key
        JOIN privacy_consents AS pc
          ON pc.user_id = snp.user_id
         AND pc.scope = ?
         AND pc.policy_version = ?
         AND pc.notice_hash = ?
         AND pc.status = ?
         AND pc.granted_at IS NOT NULL
         AND pc.withdrawn_at IS NULL
        WHERE snf.consumed_at IS NULL
          AND snp.enabled = 1
        ORDER BY snf.id
        """,
        (
            SCHOOL_NOTICE_CONSENT_POLICY.scope,
            SCHOOL_NOTICE_CONSENT_POLICY.version,
            SCHOOL_NOTICE_CONSENT_POLICY.notice_hash,
            CONSENT_GRANTED,
        ),
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        {
            "id": int(row[0]),
            "user_key": str(row[1]),
            "source_id": str(row[2] or ""),
            "external_id": str(row[3] or ""),
            "feedback_type": str(row[4]),
            "topic": row[5],
        }
        for row in rows
    ]


async def pending_feedback_count(db) -> int:
    """로그용 집계만 읽고 모든 사용자 feedback 행을 메모리에 올리지 않습니다."""
    async with db.execute(
        """
        SELECT COUNT(*)
        FROM school_notice_feedback AS snf
        JOIN school_notice_profiles AS snp
          ON snp.user_key = snf.user_key
        JOIN privacy_consents AS pc
          ON pc.user_id = snp.user_id
         AND pc.scope = ?
         AND pc.policy_version = ?
         AND pc.notice_hash = ?
         AND pc.status = ?
         AND pc.granted_at IS NOT NULL
         AND pc.withdrawn_at IS NULL
        WHERE snf.consumed_at IS NULL
          AND snp.enabled = 1
        """,
        (
            SCHOOL_NOTICE_CONSENT_POLICY.scope,
            SCHOOL_NOTICE_CONSENT_POLICY.version,
            SCHOOL_NOTICE_CONSENT_POLICY.notice_hash,
            CONSENT_GRANTED,
        ),
    ) as cursor:
        row = await cursor.fetchone()
    return int(row[0]) if row is not None else 0


async def mark_feedback_consumed(db, ids: list[int]) -> None:
    if not ids:
        return
    now = datetime.now(KST).isoformat(timespec="seconds")
    for feedback_id in ids:
        await db.execute(
            "UPDATE school_notice_feedback SET consumed_at = ? WHERE id = ?",
            (now, feedback_id),
        )
    await db.commit()


async def record_run(
    db,
    *,
    user_key: str,
    run_date: date,
    summary: dict,
    profile: dict,
) -> None:
    """실행 결과와 사용한 정확한 프로필 snapshot을 함께 기록합니다."""
    if (
        not _has_snapshot_metadata(profile)
        or str(profile.get("user_key")) != user_key
    ):
        raise ValueError("batch run에 유효한 프로필 snapshot이 필요합니다.")
    profile_version = int(profile[_BATCH_PROFILE_VERSION_KEY])
    profile_hash = str(profile[_BATCH_PROFILE_HASH_KEY])
    if profile_version <= 0 or re.fullmatch(r"[0-9a-f]{64}", profile_hash) is None:
        raise ValueError("batch run 프로필 snapshot 메타데이터가 올바르지 않습니다.")
    if config.DB_BACKEND == "tidb":
        query = """
            INSERT INTO school_notice_batch_runs
                (user_key, run_date, profile_version, profile_hash, status,
                 collection_status, may_include_stale, item_count, http_requests,
                 llm_calls, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON DUPLICATE KEY UPDATE
                profile_version = VALUES(profile_version),
                profile_hash = VALUES(profile_hash),
                status = VALUES(status),
                collection_status = VALUES(collection_status),
                may_include_stale = VALUES(may_include_stale),
                item_count = VALUES(item_count),
                http_requests = VALUES(http_requests),
                llm_calls = VALUES(llm_calls),
                finished_at = VALUES(finished_at)
        """
    else:
        query = """
            INSERT INTO school_notice_batch_runs
                (user_key, run_date, profile_version, profile_hash, status,
                 collection_status, may_include_stale, item_count, http_requests,
                 llm_calls, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_key, run_date) DO UPDATE SET
                profile_version = excluded.profile_version,
                profile_hash = excluded.profile_hash,
                status = excluded.status,
                collection_status = excluded.collection_status,
                may_include_stale = excluded.may_include_stale,
                item_count = excluded.item_count,
                http_requests = excluded.http_requests,
                llm_calls = excluded.llm_calls,
                finished_at = excluded.finished_at
        """
    await db.execute(
        query,
        (
            user_key,
            run_date.isoformat(),
            profile_version,
            profile_hash,
            summary["status"],
            summary.get("collection_status"),
            1 if summary.get("may_include_stale") else 0,
            int(summary.get("item_count", 0)),
            summary.get("http_requests"),
            summary.get("llm_calls"),
            datetime.now(KST).isoformat(timespec="seconds"),
        ),
    )
    await db.commit()


def source_config_path(args: argparse.Namespace) -> Path:
    """실제 daily 명령과 source 선택 검증이 같은 설정 파일을 보게 합니다."""
    explicit = getattr(args, "source_config", None)
    if explicit:
        selected = Path(explicit).expanduser()
        if not selected.is_absolute():
            selected = Path(args.core_cwd).expanduser() / selected
        return selected.resolve()
    configured = str(getattr(config, "SCHOOL_NOTICE_SOURCE_CONFIG", "") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (
        Path(args.core_cwd).expanduser().resolve()
        / "school_notice"
        / "sources.json"
    )


@lru_cache(maxsize=4)
def _source_metadata(
    config_path_text: str,
) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """한 batch 안에서 불변 source 설정을 프로필마다 다시 파싱하지 않습니다."""
    config_path = Path(config_path_text)
    try:
        declared_size = config_path.stat().st_size
        if declared_size > _MAX_SOURCE_CONFIG_BYTES:
            raise SourceSelectionError("source 설정 파일이 너무 큽니다.")
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except SourceSelectionError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceSelectionError("source 설정 파일을 읽을 수 없습니다.") from exc

    raw_sources = payload.get("sources") if isinstance(payload, dict) else None
    if not isinstance(raw_sources, list) or len(raw_sources) > _MAX_SOURCES:
        raise SourceSelectionError("source 설정의 sources 배열이 올바르지 않습니다.")

    all_sources: dict[str, tuple[str, tuple[str, ...]]] = {}
    for entry in raw_sources:
        if not isinstance(entry, dict):
            raise SourceSelectionError("source 설정 항목이 객체가 아닙니다.")
        source_id = entry.get("id")
        entry_school_id = entry.get("school_id")
        raw_tags = entry.get("profile_tags", [])
        if raw_tags is None:
            raw_tags = []
        if (
            not isinstance(source_id, str)
            or not _SAFE_SCHOOL_ID.fullmatch(source_id)
            or not isinstance(entry_school_id, str)
            or not _SAFE_SCHOOL_ID.fullmatch(entry_school_id)
            or source_id in all_sources
            or not isinstance(raw_tags, list)
            or len(raw_tags) > 32
            or any(
                not isinstance(tag, str)
                or len(tag) > 100
                or ":" not in tag
                for tag in raw_tags
            )
        ):
            raise SourceSelectionError("source 설정 식별자가 올바르지 않습니다.")
        all_sources[source_id] = (
            entry_school_id,
            tuple(str(tag).strip() for tag in raw_tags),
        )
    return tuple(
        (source_id, school_id, tags)
        for source_id, (school_id, tags) in all_sources.items()
    )


@lru_cache(maxsize=4)
def _source_school_pairs(config_path_text: str) -> tuple[tuple[str, str], ...]:
    return tuple(
        (source_id, school_id)
        for source_id, school_id, _tags in _source_metadata(config_path_text)
    )


def _normalized_identity(value: object) -> str:
    return "".join(str(value or "").casefold().split())


def _department_identities(profile: dict) -> tuple[str, ...]:
    values = (
        profile.get("department"),
        *(profile.get("double_majors") or []),
        *(profile.get("minors") or []),
    )
    identities: list[str] = []
    for raw in values:
        rendered = str(raw or "").strip()
        if not rendered:
            continue
        for candidate in (
            rendered,
            rendered.removesuffix("학과"),
            rendered.removesuffix("학부"),
            rendered.removesuffix("전공"),
        ):
            normalized = _normalized_identity(candidate)
            if len(normalized) >= 2 and normalized not in identities:
                identities.append(normalized)
    return tuple(identities)


def _tag_values(tags: tuple[str, ...], prefix: str) -> tuple[str, ...]:
    marker = f"{prefix}:"
    return tuple(
        tag[len(marker) :].strip()
        for tag in tags
        if tag.startswith(marker) and tag[len(marker) :].strip()
    )


def _source_matches_known_profile_scope(
    profile: dict,
    tags: tuple[str, ...],
) -> bool:
    """알고 있는 소속과 명백히 다른 전용 source는 크롤링하지 않습니다.

    프로필에 캠퍼스·학과가 없으면 daily scorer가 전용 공지를 숨기되 source
    상태 자체는 확인할 수 있도록 수집을 유지합니다.
    """
    source_departments = _tag_values(tags, "department")
    departments = _department_identities(profile)
    if source_departments and departments:
        normalized_tags = tuple(_normalized_identity(item) for item in source_departments)
        if not any(
            identity in tagged or tagged in identity
            for identity in departments
            for tagged in normalized_tags
        ):
            return False

    source_degrees = {
        _normalized_identity(item)
        for item in _tag_values(tags, "degree")
    }
    degree = _normalized_identity(profile.get("degree_level"))
    if source_degrees and degree and degree not in source_degrees:
        return False

    source_campuses = {
        _normalized_identity(item)
        for item in _tag_values(tags, "campus")
    }
    campus = _normalized_identity(profile.get("campus"))
    if source_campuses and campus and campus not in source_campuses:
        return False
    return True


@lru_cache(maxsize=4)
def _school_catalog(catalog_path: str):
    return load_school_catalog(catalog_path or None)


def select_profile_sources(
    args: argparse.Namespace,
    profile: dict,
) -> tuple[Path, tuple[str, ...]]:
    """프로필 학교에 속한 source만 명시적으로 고릅니다.

    코어 CLI는 ``--source``를 생략하면 프로필 학교를 기준으로 필터링하지만,
    wrapper에서도 똑같이 fail-closed 검증한 뒤 source를 모두 명시한다. 따라서
    잘못된 프로필이나 코어 설정 때문에 전체 학교를 크롤링하는 경로가 없다.
    """
    school_id = str(profile.get("school_id") or "")
    if not _SAFE_SCHOOL_ID.fullmatch(school_id):
        raise SourceSelectionError("프로필 school_id가 올바르지 않습니다.")

    config_path = source_config_path(args)
    all_sources = dict(_source_school_pairs(str(config_path)))
    source_tags = {
        source_id: tags
        for source_id, _school_id, tags in _source_metadata(str(config_path))
    }

    catalog_path = str(
        getattr(config, "SCHOOL_NOTICE_CATALOG_PATH", "") or ""
    ).strip()
    try:
        catalog = _school_catalog(catalog_path)
        school = catalog.schools.get(school_id)
    except SchoolProfileError as exc:
        raise SourceSelectionError("학교 카탈로그를 검증할 수 없습니다.") from exc
    if school is None:
        raise SourceSelectionError("등록 학교가 지원 카탈로그에 없습니다.")

    school_sources = tuple(
        source_id
        for source_id in school.source_ids
        if _source_matches_known_profile_scope(
            profile,
            source_tags.get(source_id, ()),
        )
    )
    if any(all_sources.get(source_id) != school_id for source_id in school_sources):
        raise SourceSelectionError(
            "학교 카탈로그 source가 코어 설정의 같은 학교와 일치하지 않습니다."
        )
    if not school_sources:
        raise SourceSelectionError(
            "등록한 캠퍼스·학과·과정에 맞는 공개 게시판 source가 없습니다."
        )

    requested = profile.get("source_ids")
    if requested is None:
        return config_path, school_sources
    if (
        not isinstance(requested, list)
        or not requested
        or len(requested) > _MAX_SOURCES
        or any(not isinstance(item, str) for item in requested)
    ):
        raise SourceSelectionError("프로필 source_ids가 올바르지 않습니다.")
    requested_ids = tuple(dict.fromkeys(requested))
    unknown_or_cross_school = [
        item
        for item in requested_ids
        if item not in school_sources or all_sources.get(item) != school_id
    ]
    if unknown_or_cross_school:
        raise SourceSelectionError(
            "프로필 source_ids에 등록 학교 밖 source가 포함되어 있습니다."
        )
    return config_path, requested_ids


def dry_run_preflight_errors(
    args: argparse.Namespace,
    profiles: ProfileLoadResult,
) -> tuple[str, ...]:
    """외부 코어와 source 연결을 읽기 전용으로 사전 검증합니다.

    실행 파일을 호출하거나 작업 디렉터리를 만들지 않는다. 상대
    ``--core-python``은 subprocess가 실행될 ``--core-cwd`` 기준으로 해석해
    실제 daily 실행과 같은 파일을 검사합니다.
    """
    errors: list[str] = []
    core_cwd = Path(str(getattr(args, "core_cwd", "") or "")).expanduser()
    if not core_cwd.is_absolute():
        core_cwd = core_cwd.resolve()
    if not core_cwd.is_dir():
        errors.append("core-cwd가 존재하는 디렉터리가 아닙니다.")

    core_python = Path(
        str(getattr(args, "core_python", "") or "")
    ).expanduser()
    if not core_python.is_absolute():
        core_python = core_cwd / core_python
    if not core_python.is_file() or not os.access(core_python, os.X_OK):
        errors.append("core-python이 존재하는 실행 가능 파일이 아닙니다.")

    try:
        selected_config = source_config_path(args)
        _source_school_pairs(str(selected_config))
    except (SourceSelectionError, OSError, ValueError):
        errors.append("source 설정 파일의 존재 여부 또는 형식이 올바르지 않습니다.")

    for index, profile in enumerate(profiles, start=1):
        try:
            select_profile_sources(args, profile)
        except SourceSelectionError:
            errors.append(
                f"profile#{index}의 학교 카탈로그와 source 연결이 올바르지 않습니다."
            )
    return tuple(errors)


def build_core_command(
    args: argparse.Namespace,
    profile_path: Path,
    output_dir: Path,
    *,
    source_ids: tuple[str, ...],
    run_date: date | None = None,
    selected_source_config: Path | None = None,
    reuse_current_snapshot: bool = False,
) -> list[str]:
    """코어 daily 명령을 만듭니다. 날짜와 source를 항상 명시합니다."""
    if not source_ids or any(
        not _SAFE_SCHOOL_ID.fullmatch(source_id)
        for source_id in source_ids
    ):
        raise SourceSelectionError("daily 명령에는 검증된 source가 한 개 이상 필요합니다.")
    effective_date = run_date or (
        date.fromisoformat(args.date) if getattr(args, "date", None) else kst_today()
    )
    source_config = selected_source_config or source_config_path(args)
    command = [
        args.core_python,
        "-m",
        "school_notice",
        "--source-config",
        str(source_config),
        "daily",
        "--profile",
        str(profile_path),
        "--db",
        str(Path(config.SCHOOL_NOTICE_CORE_DB).expanduser()),
        "--output-dir",
        str(output_dir),
        "--date",
        effective_date.isoformat(),
    ]
    for source_id in source_ids:
        command.extend(["--source", source_id])
    if args.no_llm:
        command.append("--no-llm")
    if args.low_resource:
        command.append("--low-resource")
    if args.max_details_per_source:
        command.extend(["--max-details-per-source", str(args.max_details_per_source)])
    if args.max_requests:
        command.extend(["--max-requests", str(args.max_requests)])
    if reuse_current_snapshot:
        command.append("--reuse-current-snapshot")
    return command


def build_feedback_command(
    args: argparse.Namespace,
    profile_path: Path,
    feedback: dict,
    *,
    selected_source_config: Path | None = None,
) -> list[str]:
    """코어 feedback 명령을 안전한 고정 인자 배열로 만듭니다."""
    feedback_type = str(feedback.get("feedback_type") or "")
    if feedback_type not in FEEDBACK_TYPES:
        raise ValueError("지원하지 않는 피드백 종류입니다.")
    source_id = str(feedback.get("source_id") or "")
    external_id = str(feedback.get("external_id") or "")
    if bool(source_id) != bool(external_id):
        raise ValueError("source_id와 external_id는 함께 있어야 합니다.")
    if source_id and (
        not _SAFE_SCHOOL_ID.fullmatch(source_id)
        or len(external_id) > _MAX_FEEDBACK_EXTERNAL_ID_CHARS
        or "\x00" in external_id
    ):
        raise ValueError("feedback 공지 식별자가 올바르지 않습니다.")

    command = [
        args.core_python,
        "-m",
        "school_notice",
        "--source-config",
        str(selected_source_config or source_config_path(args)),
        "feedback",
        "--profile",
        str(profile_path),
        "--db",
        str(Path(config.SCHOOL_NOTICE_CORE_DB).expanduser()),
        "--type",
        feedback_type,
    ]
    if source_id and external_id:
        command.extend(["--source-id", source_id, "--external-id", external_id])
    topic = feedback.get("topic")
    if isinstance(topic, str) and topic.strip():
        rendered_topic = topic.strip()
        if (
            len(rendered_topic) > _MAX_FEEDBACK_TOPIC_CHARS
            or "\x00" in rendered_topic
        ):
            raise ValueError("feedback topic이 올바르지 않습니다.")
        command.extend(["--topic", rendered_topic])
    return command


def _load_run_report(output_dir: Path, run_date: date) -> dict:
    """코어 run report를 제한된 크기의 JSON 객체로 읽습니다."""
    run_report = output_dir / f"daily-run-{run_date.isoformat()}.json"
    try:
        if run_report.stat().st_size > _MAX_RUN_REPORT_BYTES:
            raise ValueError("run report가 너무 큽니다.")
        payload = json.loads(run_report.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("run report를 읽을 수 없습니다.") from exc
    if not isinstance(payload, dict) or payload.get("status") not in RUN_STATUSES:
        raise ValueError("run report status가 올바르지 않습니다.")
    for field in ("http_requests", "llm_calls"):
        value = payload.get(field)
        if type(value) is not int or not 0 <= value <= 1_000_000:
            raise ValueError(f"run report {field} 값이 올바르지 않습니다.")
    return payload


def summarize_run(
    output_dir: Path,
    run_date: date,
    returncode: int,
    *,
    expected_user_key: str | None = None,
    allowed_source_ids: tuple[str, ...] | None = None,
) -> dict:
    """digest와 실행 보고서에서 실제 상태를 읽습니다.

    코어는 `partial`을 exit 0으로 반환하므로 종료 코드만으로 성공을 판정하지
    않고 보고서와 collection_health를 함께 확인합니다.
    """
    process_failed = returncode != 0
    summary: dict = {
        "status": "failed" if process_failed else "succeeded",
        "collection_status": None,
        "may_include_stale": False,
        "item_count": 0,
        "http_requests": None,
        "llm_calls": None,
    }

    try:
        payload = _load_run_report(output_dir, run_date)
        if not process_failed:
            summary["status"] = str(payload.get("status") or summary["status"])
        summary["http_requests"] = payload.get("http_requests")
        summary["llm_calls"] = payload.get("llm_calls")
    except ValueError:
        summary["status"] = "failed"

    try:
        digest = load_digest(
            digest_path_for(output_dir, run_date),
            expected_schema_version=config.SCHOOL_NOTICE_SCHEMA_VERSION,
            expected_user_key=expected_user_key,
            expected_digest_date=run_date,
        )
    except DigestContractError:
        # digest를 읽지 못하면 봇이 전달할 수 없으므로 성공으로 볼 수 없다.
        summary["status"] = "failed"
        return summary

    if allowed_source_ids is not None:
        allowed_sources = frozenset(allowed_source_ids)
        if (
            not allowed_sources
            or any(
                not isinstance(source_id, str)
                or not _SAFE_SCHOOL_ID.fullmatch(source_id)
                for source_id in allowed_sources
            )
        ):
            summary["status"] = "failed"
            return summary
        emitted_sources = {item.source_id for item in digest.items}
        if digest.collection_health is not None:
            emitted_sources.update(
                item.source_id for item in digest.collection_health.sources
            )
        if not emitted_sources.issubset(allowed_sources):
            # 외부 코어가 --source 경계를 무시한 산출물을 내더라도 다른 학교
            # 공지를 사용자 digest 경로에 공개하지 않는다.
            summary["status"] = "failed"
            return summary

    summary["item_count"] = len(digest.visible_items())
    if digest.collection_health is not None:
        summary["collection_status"] = digest.collection_health.status
        summary["may_include_stale"] = digest.collection_health.may_include_stale_notices
    return summary


@contextlib.contextmanager
def single_flight_lock(lock_path: Path) -> Iterator[None]:
    """digest root별 batch를 한 개로 제한하는 advisory lock."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BatchAlreadyRunning("학교 공지 batch가 이미 실행 중입니다.") from exc
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def cleanup_stale_workdirs(work_root: Path) -> int:
    """이전 강제 종료가 남긴 전용 ``run-*`` 디렉터리만 안전하게 제거합니다."""
    try:
        root_stat = work_root.lstat()
    except OSError as exc:
        raise RuntimeError("학교 공지 임시 작업 디렉터리를 확인할 수 없습니다.") from exc
    if not stat.S_ISDIR(root_stat.st_mode):
        raise RuntimeError("학교 공지 임시 작업 경로가 실제 디렉터리가 아닙니다.")

    removed = 0
    current_uid = os.geteuid()
    for entry in work_root.iterdir():
        if not _STALE_RUN_DIR.fullmatch(entry.name):
            continue
        try:
            entry_stat = entry.lstat()
        except OSError:
            continue
        # symlink나 다른 계정 소유 경로는 따라가거나 지우지 않는다.
        if (
            not stat.S_ISDIR(entry_stat.st_mode)
            or entry_stat.st_uid != current_uid
        ):
            continue
        try:
            shutil.rmtree(entry)
        except OSError as exc:
            raise RuntimeError(
                "이전 학교 공지 임시 작업을 안전하게 정리할 수 없습니다."
            ) from exc
        removed += 1
    return removed


def _remaining_timeout(
    *,
    deadline_monotonic: float,
    operation_limit_seconds: int,
) -> float:
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        raise BatchDeadlineExceeded("학교 공지 batch 전체 제한 시간을 초과했습니다.")
    return max(0.1, min(float(operation_limit_seconds), remaining))


def _failed_summary() -> dict:
    return {
        "status": "failed",
        "collection_status": None,
        "may_include_stale": False,
        "item_count": 0,
        "http_requests": None,
        "llm_calls": None,
    }


def publish_validated_artifacts(
    staged_output: Path,
    final_output: Path,
    run_date: date,
) -> None:
    """검증된 JSON digest와 run report만 최종 사용자 디렉터리에 공개합니다.

    digest를 마지막에 교체해 봇이 새 digest를 관찰하는 시점에는 같은 실행의
    report도 이미 공개되어 있도록 한다. 두 경로는 같은 digest root 아래라
    ``os.replace``가 파일 단위 원자성을 보장한다.
    """
    digest_path = digest_path_for(staged_output, run_date)
    report_path = staged_output / f"daily-run-{run_date.isoformat()}.json"
    try:
        regular_outputs = all(
            stat.S_ISREG(path.lstat().st_mode)
            for path in (digest_path, report_path)
        )
    except OSError:
        regular_outputs = False
    if not regular_outputs:
        raise ValueError("공개할 필수 산출물이 없습니다.")
    report = _load_run_report(staged_output, run_date)

    final_output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        final_output.chmod(0o700)

    # 코어 report의 경로·오류 원문·프로필 관련 부가 필드는 사용자별 최종
    # 디렉터리에 남기지 않는다. 운영 DB가 쓰는 최소 집계 계약만 공개한다.
    sanitized_report = staged_output / ".validated-run-report.json"
    sanitized_report.write_text(
        json.dumps(
            {
                "status": report["status"],
                "http_requests": report["http_requests"],
                "llm_calls": report["llm_calls"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    sanitized_report.chmod(0o600)
    final_report = final_output / report_path.name
    os.replace(sanitized_report, final_report)
    final_report.chmod(0o600)

    final_digest = final_output / digest_path.name
    os.replace(digest_path, final_digest)
    final_digest.chmod(0o600)


def _run_subprocess(
    command: list[str],
    *,
    cwd: str,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """daily 출력은 버려 장시간 실행의 stdout/stderr를 RAM에 쌓지 않습니다."""
    completed = subprocess.run(  # noqa: S603 - 고정 인자 배열, shell 사용 안 함
        command,
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
    )
    return subprocess.CompletedProcess(command, completed.returncode, "", "")


def _run_feedback_subprocess(
    command: list[str],
    *,
    cwd: str,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """feedback JSON만 bounded spool로 받으며 stderr 원문은 보존하지 않습니다."""
    with tempfile.SpooledTemporaryFile(
        max_size=_MAX_FEEDBACK_STDOUT_BYTES,
        mode="w+b",
    ) as output:
        completed = subprocess.run(  # noqa: S603 - 고정 인자, shell 사용 안 함
            command,
            cwd=cwd,
            stdout=output,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
        size = output.tell()
        rendered = ""
        if size <= _MAX_FEEDBACK_STDOUT_BYTES:
            output.seek(0)
            try:
                rendered = output.read().decode("utf-8")
            except UnicodeDecodeError:
                rendered = ""
    return subprocess.CompletedProcess(
        command,
        completed.returncode,
        rendered,
        "",
    )


def _feedback_recorded(completed: subprocess.CompletedProcess[str]) -> bool:
    """feedback CLI의 구조화 성공 응답까지 확인합니다."""
    if completed.returncode != 0 or len(completed.stdout or "") > 64 * 1024:
        return False
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError):
        return False
    recorded = payload.get("recorded") if isinstance(payload, dict) else None
    return type(recorded) is int and recorded > 0


async def _apply_feedback_for_profile(
    db,
    *,
    args: argparse.Namespace,
    profile_path: Path,
    feedback_entries: list[dict],
    selected_source_config: Path,
    allowed_source_ids: frozenset[str],
    deadline_monotonic: float,
    limits: BatchLimits,
) -> tuple[int, int]:
    """성공한 feedback ID만 즉시 consumed 처리합니다."""
    succeeded = 0
    failed = 0
    for entry in feedback_entries:
        source_id = str(entry.get("source_id") or "")
        if source_id and source_id not in allowed_source_ids:
            # 학교/프로필 변경 전에 남긴 다른 source의 feedback은 현재
            # personalization에 적용하지 않는다. core 실패가 아니므로 신규
            # 학교 daily를 영구 차단하지 않되 consumed로 거짓 표시하지도 않는다.
            continue
        try:
            command = build_feedback_command(
                args,
                profile_path,
                entry,
                selected_source_config=selected_source_config,
            )
            completed = _run_feedback_subprocess(
                command,
                cwd=args.core_cwd,
                timeout=_remaining_timeout(
                    deadline_monotonic=deadline_monotonic,
                    operation_limit_seconds=limits.feedback_timeout_seconds,
                ),
            )
        except BatchDeadlineExceeded:
            failed += len(feedback_entries) - succeeded - failed
            raise
        except (OSError, subprocess.TimeoutExpired, ValueError):
            failed += 1
            continue
        if _feedback_recorded(completed):
            await mark_feedback_consumed(db, [int(entry["id"])])
            succeeded += 1
        else:
            # 실패한 feedback은 consumed_at을 비워 다음 유한 실행에서 재시도한다.
            failed += 1
    return succeeded, failed


async def _run_profile(
    db,
    *,
    args: argparse.Namespace,
    profile: dict,
    feedback_entries: list[dict],
    digest_root: Path,
    work_root: Path,
    run_date: date,
    deadline_monotonic: float,
    limits: BatchLimits,
    reuse_current_snapshot: bool = False,
) -> tuple[dict | None, int, int]:
    """프로필 하나의 feedback과 daily를 순차 실행합니다."""
    current = await current_profile_snapshot(db, profile)
    if current is None:
        return None, 0, 0
    profile = current
    user_key = str(profile["user_key"])
    try:
        selected_source_config, source_ids = select_profile_sources(args, profile)
    except SourceSelectionError:
        return _failed_summary(), 0, len(feedback_entries)

    with tempfile.TemporaryDirectory(prefix="run-", dir=work_root) as temporary:
        temporary_root = Path(temporary)
        temporary_root.chmod(0o700)
        profile_path = temporary_root / "profile.json"
        export_profile = {
            key: value
            for key, value in profile.items()
            if key not in _BATCH_INTERNAL_KEYS
        }
        profile_path.write_text(
            json.dumps(export_profile, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        profile_path.chmod(0o600)
        staged_output = temporary_root / "output"
        staged_output.mkdir(mode=0o700)

        # profile 파일은 아직 로컬 0700 temp 안에만 있다. 외부 feedback core를
        # 호출하기 바로 전에 철회/변경을 한 번 더 확인한다.
        if await current_profile_snapshot(db, profile) is None:
            return None, 0, 0
        feedback_succeeded, feedback_failed = await _apply_feedback_for_profile(
            db,
            args=args,
            profile_path=profile_path,
            feedback_entries=feedback_entries,
            selected_source_config=selected_source_config,
            allowed_source_ids=frozenset(source_ids),
            deadline_monotonic=deadline_monotonic,
            limits=limits,
        )
        if feedback_failed:
            # 사용자가 이미 거절/완료로 표시한 공지를 옛 선호 상태로 다시
            # 보내는 것보다 해당 프로필 하루 실행을 실패로 남겨 재시도한다.
            return _failed_summary(), feedback_succeeded, feedback_failed

        # feedback subprocess 동안 철회/프로필 변경이 일어났다면 개인정보가
        # 담긴 daily profile을 외부 core에 넘기지 않고 temp를 즉시 정리한다.
        if await current_profile_snapshot(db, profile) is None:
            return None, feedback_succeeded, feedback_failed

        command = build_core_command(
            args,
            profile_path,
            staged_output,
            source_ids=source_ids,
            run_date=run_date,
            selected_source_config=selected_source_config,
            reuse_current_snapshot=reuse_current_snapshot,
        )
        try:
            completed = _run_subprocess(
                command,
                cwd=args.core_cwd,
                timeout=_remaining_timeout(
                    deadline_monotonic=deadline_monotonic,
                    operation_limit_seconds=limits.profile_timeout_seconds,
                ),
            )
            summary = summarize_run(
                staged_output,
                run_date,
                completed.returncode,
                expected_user_key=user_key,
                allowed_source_ids=source_ids,
            )
        except BatchDeadlineExceeded:
            raise
        except (OSError, subprocess.TimeoutExpired):
            summary = _failed_summary()

        # daily가 최대 수 분 걸리는 동안 철회/삭제/프로필 변경이 생기면 이미
        # 생성된 staged digest를 사용자 경로에 공개하거나 run으로 기록하지 않는다.
        if await current_profile_snapshot(db, profile) is None:
            return None, feedback_succeeded, feedback_failed
        if summary["status"] in {"succeeded", "partial"}:
            try:
                publish_validated_artifacts(
                    staged_output,
                    digest_root / user_key,
                    run_date,
                )
            except (OSError, ValueError):
                summary = _failed_summary()
        return summary, feedback_succeeded, feedback_failed


async def run_batch(args: argparse.Namespace) -> int:
    """주입 가능한 인자로 batch를 실행하고 종료 코드를 반환합니다."""
    if not config.SCHOOL_NOTICE_ENABLED:
        print("SCHOOL_NOTICE_ENABLED=false 이므로 실행하지 않습니다.", file=sys.stderr)
        return 1

    try:
        run_date = date.fromisoformat(args.date) if args.date else kst_today()
    except ValueError:
        print("--date는 YYYY-MM-DD 형식이어야 합니다.", file=sys.stderr)
        return 2

    digest_root = Path(config.SCHOOL_NOTICE_DIGEST_DIR).expanduser()
    work_root = digest_root / ".profiles"
    limits = batch_limits(args)
    deadline_monotonic = time.monotonic() + limits.deadline_seconds

    # dry-run은 lock/작업 디렉터리/프로필 파일/DB UPDATE를 전혀 만들지 않는다.
    db = await open_db(read_only=bool(args.dry_run))
    try:
        only_user_id = getattr(args, "only_user_id", None)
        if only_user_id is not None and int(only_user_id) <= 0:
            print("--only-user-id는 양의 정수여야 합니다.", file=sys.stderr)
            return 2
        profiles = await load_profiles(
            db,
            only_user_id=int(only_user_id) if only_user_id is not None else None,
        )
        invalid_profiles_present = profiles.invalid_count > 0

        if args.dry_run:
            preflight_errors = dry_run_preflight_errors(args, profiles)
            if preflight_errors:
                print("학교 공지 dry-run 사전 검증에 실패했습니다.", file=sys.stderr)
                for error in preflight_errors:
                    print(f"  - {error}", file=sys.stderr)
                return 2

        if not profiles:
            # 마지막 실행이 강제 종료된 뒤 모든 사용자가 철회/삭제했더라도
            # 민감 profile temp가 영구 잔존하지 않게 정리한다.
            if not args.dry_run and work_root.exists():
                try:
                    with single_flight_lock(
                        digest_root / ".school-notice-batch.lock"
                    ):
                        cleanup_stale_workdirs(work_root)
                except BatchAlreadyRunning:
                    print("학교 공지 batch가 이미 실행 중입니다.", file=sys.stderr)
                    return 3
            if invalid_profiles_present:
                print(
                    "유효한 활성 프로필이 없고 잘못된 활성 프로필이 있습니다.",
                    file=sys.stderr,
                )
                return 2
            print("활성 프로필이 없습니다.")
            return 0

        if only_user_id is not None and profiles:
            feedback_count = len(
                await pending_feedback_for_profile(db, profiles[0])
            )
        else:
            feedback_count = await pending_feedback_count(db)
        print(f"대상 프로필 {len(profiles)}명, 미반영 피드백 {feedback_count}건")
        if args.dry_run:
            school_counts = Counter(str(profile["school_id"]) for profile in profiles)
            print(
                "등록 학교별 대상: "
                + ", ".join(
                    f"{school_id}={count}"
                    for school_id, count in sorted(school_counts.items())
                )
            )
            return 2 if invalid_profiles_present else 0

        digest_root.mkdir(parents=True, exist_ok=True)
        work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        work_root.chmod(0o700)

        profile_limit_hit = len(profiles) > limits.max_profiles
        selected_profiles = profiles[: limits.max_profiles]
        exit_code = 2 if profile_limit_hit or invalid_profiles_present else 0
        feedback_succeeded_total = 0
        feedback_failed_total = 0
        # 학교 ID만으로 재사용 여부를 정하면 서울/ERICA처럼 같은 학교의
        # 프로필이 서로 다른 source 조합을 요구할 때 필요한 게시판을 건너뛸
        # 수 있다. 실제로 성공 수집한 source ID 집합을 기준으로 판단한다.
        collected_source_ids: set[str] = set()

        try:
            with single_flight_lock(digest_root / ".school-notice-batch.lock"):
                cleaned = cleanup_stale_workdirs(work_root)
                if cleaned:
                    print(f"이전 임시 작업 {cleaned}건을 정리했습니다.")
                for profile_index, profile in enumerate(selected_profiles, start=1):
                    current = await current_profile_snapshot(db, profile)
                    if current is None:
                        print(
                            f"  profile#{profile_index}: skipped=current-consent-or-profile"
                        )
                        continue
                    try:
                        _selected_config, current_source_ids = (
                            select_profile_sources(args, current)
                        )
                    except SourceSelectionError:
                        current_source_ids = ()
                    current_feedback = await pending_feedback_for_profile(db, current)
                    try:
                        summary, feedback_succeeded, feedback_failed = await _run_profile(
                            db,
                            args=args,
                            profile=current,
                            feedback_entries=current_feedback,
                            digest_root=digest_root,
                            work_root=work_root,
                            run_date=run_date,
                            deadline_monotonic=deadline_monotonic,
                            limits=limits,
                            reuse_current_snapshot=(
                                bool(current_source_ids)
                                and set(current_source_ids).issubset(
                                    collected_source_ids
                                )
                            ),
                        )
                    except BatchDeadlineExceeded:
                        exit_code = 2
                        break

                    feedback_succeeded_total += feedback_succeeded
                    feedback_failed_total += feedback_failed
                    if summary is None:
                        print(
                            f"  profile#{profile_index}: skipped=current-consent-or-profile"
                        )
                        continue
                    await record_run(
                        db,
                        user_key=str(current["user_key"]),
                        run_date=run_date,
                        summary=summary,
                        profile=current,
                    )
                    print(
                        f"  profile#{profile_index}: status={summary['status']} "
                        f"collection={summary['collection_status']} "
                        f"items={summary['item_count']} "
                        f"stale={summary['may_include_stale']}"
                    )
                    if summary["status"] == "failed":
                        exit_code = 2
                    elif summary["status"] == "succeeded":
                        collected_source_ids.update(current_source_ids)
        except BatchAlreadyRunning:
            print("학교 공지 batch가 이미 실행 중입니다.", file=sys.stderr)
            return 3

        if feedback_failed_total:
            exit_code = 2
        print(
            "피드백 반영 결과: "
            f"성공 {feedback_succeeded_total}건, 보류 {feedback_failed_total}건"
        )
        if profile_limit_hit:
            print(
                "프로필 상한을 초과해 일부를 실행하지 않았습니다.",
                file=sys.stderr,
            )
        return exit_code
    finally:
        await db.close()


async def main() -> int:
    """CLI entry point."""
    return await run_batch(parse_args())


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

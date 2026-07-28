from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .models import Notice


SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notice_job_runs (
    run_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    scheduled_date TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    stats_json TEXT NOT NULL DEFAULT '{}',
    error TEXT
);

CREATE TABLE IF NOT EXISTS school_notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    school_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    title TEXT NOT NULL,
    published_text TEXT,
    author TEXT,
    category TEXT,
    pinned INTEGER NOT NULL DEFAULT 0,
    dedup_key TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    revision_count INTEGER NOT NULL DEFAULT 1,
    snapshot_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    UNIQUE(source_id, external_id)
);

CREATE INDEX IF NOT EXISTS idx_school_notices_school
    ON school_notices(school_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_school_notices_dedup
    ON school_notices(school_id, dedup_key);

CREATE TABLE IF NOT EXISTS school_notice_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id INTEGER NOT NULL REFERENCES school_notices(id) ON DELETE CASCADE,
    revision_no INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    UNIQUE(notice_id, content_hash),
    UNIQUE(notice_id, revision_no)
);

CREATE TABLE IF NOT EXISTS school_notice_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id INTEGER NOT NULL REFERENCES school_notices(id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL,
    analyzer_version TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    analysis_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(notice_id, content_hash, analyzer_version)
);

CREATE TABLE IF NOT EXISTS school_profiles (
    user_key TEXT PRIMARY KEY,
    school_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    profile_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS school_notice_feedback_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key TEXT NOT NULL REFERENCES school_profiles(user_key) ON DELETE CASCADE,
    notice_id INTEGER REFERENCES school_notices(id) ON DELETE SET NULL,
    feedback_type TEXT NOT NULL,
    topic TEXT,
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notice_feedback_user_time
    ON school_notice_feedback_events(user_key, created_at);

CREATE TABLE IF NOT EXISTS school_notice_scores (
    user_key TEXT NOT NULL REFERENCES school_profiles(user_key) ON DELETE CASCADE,
    notice_id INTEGER NOT NULL REFERENCES school_notices(id) ON DELETE CASCADE,
    profile_version INTEGER NOT NULL,
    score_date TEXT NOT NULL,
    score REAL NOT NULL,
    band TEXT NOT NULL,
    eligibility TEXT NOT NULL,
    score_json TEXT NOT NULL,
    PRIMARY KEY(user_key, notice_id, profile_version, score_date)
);

CREATE TABLE IF NOT EXISTS school_notice_digests (
    user_key TEXT NOT NULL REFERENCES school_profiles(user_key) ON DELETE CASCADE,
    digest_date TEXT NOT NULL,
    markdown TEXT NOT NULL,
    digest_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(user_key, digest_date)
);

CREATE TABLE IF NOT EXISTS school_notice_api_budgets (
    usage_date TEXT NOT NULL,
    api_type TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(usage_date, api_type)
);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def canonical_title_key(title: str) -> str:
    value = title.casefold()
    value = re.sub(r"\[(?:학부|대학원|공지|일반공지|필독|장학|취업)\]", "", value)
    value = re.sub(r"\((?:필독|수정|재공지|연장)\)", "", value)
    value = re.sub(r"(?:안내|공지)$", "", value)
    value = re.sub(r"[^0-9a-z가-힣]+", "", value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class PersistResult:
    notice_id: int
    change: str
    revision_no: int
    content_hash: str


@dataclass(frozen=True)
class StoredNotice:
    notice_id: int
    school_id: str
    source_id: str
    dedup_key: str
    revision_count: int
    first_seen_at: str
    updated_at: str
    notice_payload: dict[str, Any]


class NoticeRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.migrate()

    def __enter__(self) -> "NoticeRepository":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def migrate(self) -> None:
        with self.connection:
            self.connection.executescript(SCHEMA_SQL)
            self.connection.execute(
                """
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                VALUES (?, ?)
                """,
                (SCHEMA_VERSION, utc_now()),
            )

    def start_job(self, job_type: str, scheduled_date: date) -> str:
        run_id = uuid.uuid4().hex
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO notice_job_runs(
                    run_id, job_type, scheduled_date, status, started_at
                ) VALUES (?, ?, ?, 'running', ?)
                """,
                (run_id, job_type, scheduled_date.isoformat(), utc_now()),
            )
        return run_id

    def finish_job(
        self,
        run_id: str,
        *,
        status: str,
        stats: dict[str, Any],
        error: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE notice_job_runs
                SET status = ?, finished_at = ?, stats_json = ?, error = ?
                WHERE run_id = ?
                """,
                (
                    status,
                    utc_now(),
                    json.dumps(stats, ensure_ascii=False),
                    error,
                    run_id,
                ),
            )

    def upsert_notice(self, school_id: str, notice: Notice) -> PersistResult:
        now = utc_now()
        snapshot = json.dumps(notice.as_dict(), ensure_ascii=False)
        existing = self.connection.execute(
            """
            SELECT id, content_hash, revision_count
            FROM school_notices
            WHERE source_id = ? AND external_id = ?
            """,
            (notice.candidate.source_id, notice.candidate.external_id),
        ).fetchone()

        with self.connection:
            if existing is None:
                cursor = self.connection.execute(
                    """
                    INSERT INTO school_notices(
                        school_id, source_id, external_id, canonical_url, title,
                        published_text, author, category, pinned, dedup_key,
                        content_hash, revision_count, snapshot_json,
                        first_seen_at, last_seen_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                    """,
                    (
                        school_id,
                        notice.candidate.source_id,
                        notice.candidate.external_id,
                        notice.candidate.url,
                        notice.title,
                        notice.published_text,
                        notice.author,
                        notice.candidate.category,
                        int(notice.candidate.pinned),
                        canonical_title_key(notice.title),
                        notice.content_hash,
                        snapshot,
                        now,
                        now,
                        now,
                    ),
                )
                notice_id = int(cursor.lastrowid)
                self.connection.execute(
                    """
                    INSERT INTO school_notice_revisions(
                        notice_id, revision_no, content_hash, snapshot_json, detected_at
                    ) VALUES (?, 1, ?, ?, ?)
                    """,
                    (notice_id, notice.content_hash, snapshot, now),
                )
                return PersistResult(notice_id, "new", 1, notice.content_hash)

            notice_id = int(existing["id"])
            revision_no = int(existing["revision_count"])
            if existing["content_hash"] == notice.content_hash:
                self.connection.execute(
                    """
                    UPDATE school_notices
                    SET last_seen_at = ?, canonical_url = ?, status = 'active'
                    WHERE id = ?
                    """,
                    (now, notice.candidate.url, notice_id),
                )
                return PersistResult(
                    notice_id,
                    "unchanged",
                    revision_no,
                    notice.content_hash,
                )

            revision_no += 1
            self.connection.execute(
                """
                INSERT INTO school_notice_revisions(
                    notice_id, revision_no, content_hash, snapshot_json, detected_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (notice_id, revision_no, notice.content_hash, snapshot, now),
            )
            self.connection.execute(
                """
                UPDATE school_notices
                SET canonical_url = ?, title = ?, published_text = ?, author = ?,
                    category = ?, pinned = ?, dedup_key = ?, content_hash = ?,
                    revision_count = ?, snapshot_json = ?, last_seen_at = ?,
                    updated_at = ?, status = 'active'
                WHERE id = ?
                """,
                (
                    notice.candidate.url,
                    notice.title,
                    notice.published_text,
                    notice.author,
                    notice.candidate.category,
                    int(notice.candidate.pinned),
                    canonical_title_key(notice.title),
                    notice.content_hash,
                    revision_no,
                    snapshot,
                    now,
                    now,
                    notice_id,
                ),
            )
            return PersistResult(
                notice_id,
                "updated",
                revision_no,
                notice.content_hash,
            )

    def existing_notice_payload(
        self,
        source_id: str,
        external_id: str,
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT snapshot_json
            FROM school_notices
            WHERE source_id = ? AND external_id = ?
            """,
            (source_id, external_id),
        ).fetchone()
        return json.loads(row["snapshot_json"]) if row else None

    def get_analysis(
        self,
        notice_id: int,
        content_hash: str,
        analyzer_version: str,
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT analysis_json
            FROM school_notice_analyses
            WHERE notice_id = ? AND content_hash = ? AND analyzer_version = ?
            """,
            (notice_id, content_hash, analyzer_version),
        ).fetchone()
        return json.loads(row["analysis_json"]) if row else None

    def save_analysis(
        self,
        *,
        notice_id: int,
        content_hash: str,
        analyzer_version: str,
        provider: str,
        model: str,
        analysis: dict[str, Any],
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO school_notice_analyses(
                    notice_id, content_hash, analyzer_version, provider,
                    model, analysis_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(notice_id, content_hash, analyzer_version)
                DO UPDATE SET
                    provider = excluded.provider,
                    model = excluded.model,
                    analysis_json = excluded.analysis_json,
                    created_at = excluded.created_at
                """,
                (
                    notice_id,
                    content_hash,
                    analyzer_version,
                    provider,
                    model,
                    json.dumps(analysis, ensure_ascii=False),
                    utc_now(),
                ),
            )

    def upsert_profile(self, profile: dict[str, Any]) -> int:
        user_key = str(profile["user_key"])
        school_id = str(profile["school_id"])
        serialized = json.dumps(profile, ensure_ascii=False, sort_keys=True)
        existing = self.connection.execute(
            "SELECT version, profile_json FROM school_profiles WHERE user_key = ?",
            (user_key,),
        ).fetchone()
        now = utc_now()
        if existing is None:
            version = 1
            with self.connection:
                self.connection.execute(
                    """
                    INSERT INTO school_profiles(
                        user_key, school_id, version, profile_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (user_key, school_id, version, serialized, now, now),
                )
            return version
        if existing["profile_json"] == serialized:
            return int(existing["version"])
        version = int(existing["version"]) + 1
        with self.connection:
            self.connection.execute(
                """
                UPDATE school_profiles
                SET school_id = ?, version = ?, profile_json = ?, updated_at = ?
                WHERE user_key = ?
                """,
                (school_id, version, serialized, now, user_key),
            )
        return version

    def get_profile(self, user_key: str) -> tuple[dict[str, Any], int] | None:
        row = self.connection.execute(
            "SELECT profile_json, version FROM school_profiles WHERE user_key = ?",
            (user_key,),
        ).fetchone()
        if not row:
            return None
        return json.loads(row["profile_json"]), int(row["version"])

    def add_feedback(
        self,
        *,
        user_key: str,
        feedback_type: str,
        notice_id: int | None = None,
        topic: str | None = None,
        reason: str | None = None,
        created_at: str | None = None,
    ) -> int:
        with self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO school_notice_feedback_events(
                    user_key, notice_id, feedback_type, topic, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_key,
                    notice_id,
                    feedback_type,
                    topic,
                    reason,
                    created_at or utc_now(),
                ),
            )
        return int(cursor.lastrowid)

    def feedback_events(self, user_key: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT id, notice_id, feedback_type, topic, reason, created_at
            FROM school_notice_feedback_events
            WHERE user_key = ?
            ORDER BY created_at
            """,
            (user_key,),
        ).fetchall()
        return [dict(row) for row in rows]

    def current_notices(self, school_id: str) -> list[StoredNotice]:
        rows = self.connection.execute(
            """
            SELECT id, school_id, source_id, dedup_key, revision_count,
                   first_seen_at, updated_at, snapshot_json
            FROM school_notices
            WHERE school_id = ? AND status = 'active'
            ORDER BY updated_at DESC, id DESC
            """,
            (school_id,),
        ).fetchall()
        return [
            StoredNotice(
                notice_id=int(row["id"]),
                school_id=row["school_id"],
                source_id=row["source_id"],
                dedup_key=row["dedup_key"],
                revision_count=int(row["revision_count"]),
                first_seen_at=row["first_seen_at"],
                updated_at=row["updated_at"],
                notice_payload=json.loads(row["snapshot_json"]),
            )
            for row in rows
        ]

    def latest_analysis_for_notice(
        self,
        notice_id: int,
        content_hash: str,
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT analysis_json
            FROM school_notice_analyses
            WHERE notice_id = ? AND content_hash = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (notice_id, content_hash),
        ).fetchone()
        return json.loads(row["analysis_json"]) if row else None

    def latest_analysis_any(self, notice_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT analysis_json
            FROM school_notice_analyses
            WHERE notice_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (notice_id,),
        ).fetchone()
        return json.loads(row["analysis_json"]) if row else None

    def save_score(
        self,
        *,
        user_key: str,
        notice_id: int,
        profile_version: int,
        score_date: date,
        score: dict[str, Any],
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO school_notice_scores(
                    user_key, notice_id, profile_version, score_date,
                    score, band, eligibility, score_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_key, notice_id, profile_version, score_date)
                DO UPDATE SET
                    score = excluded.score,
                    band = excluded.band,
                    eligibility = excluded.eligibility,
                    score_json = excluded.score_json
                """,
                (
                    user_key,
                    notice_id,
                    profile_version,
                    score_date.isoformat(),
                    float(score["score"]),
                    str(score["band"]),
                    str(score["eligibility"]),
                    json.dumps(score, ensure_ascii=False),
                ),
            )

    def save_digest(
        self,
        *,
        user_key: str,
        digest_date: date,
        markdown: str,
        payload: dict[str, Any],
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO school_notice_digests(
                    user_key, digest_date, markdown, digest_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_key, digest_date)
                DO UPDATE SET
                    markdown = excluded.markdown,
                    digest_json = excluded.digest_json,
                    created_at = excluded.created_at
                """,
                (
                    user_key,
                    digest_date.isoformat(),
                    markdown,
                    json.dumps(payload, ensure_ascii=False),
                    utc_now(),
                ),
            )

    def reserve_api_call(
        self,
        *,
        usage_date: date,
        api_type: str,
        daily_limit: int,
    ) -> bool:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                """
                SELECT calls FROM school_notice_api_budgets
                WHERE usage_date = ? AND api_type = ?
                """,
                (usage_date.isoformat(), api_type),
            ).fetchone()
            calls = int(row["calls"]) if row else 0
            if calls >= daily_limit:
                self.connection.rollback()
                return False
            self.connection.execute(
                """
                INSERT INTO school_notice_api_budgets(
                    usage_date, api_type, calls
                ) VALUES (?, ?, 1)
                ON CONFLICT(usage_date, api_type)
                DO UPDATE SET calls = calls + 1
                """,
                (usage_date.isoformat(), api_type),
            )
            self.connection.commit()
            return True
        except Exception:
            self.connection.rollback()
            raise

    def record_api_tokens(
        self,
        *,
        usage_date: date,
        api_type: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE school_notice_api_budgets
                SET prompt_tokens = prompt_tokens + ?,
                    completion_tokens = completion_tokens + ?
                WHERE usage_date = ? AND api_type = ?
                """,
                (
                    max(0, int(prompt_tokens)),
                    max(0, int(completion_tokens)),
                    usage_date.isoformat(),
                    api_type,
                ),
            )

    def api_usage(self, *, usage_date: date, api_type: str) -> dict[str, int]:
        row = self.connection.execute(
            """
            SELECT calls, prompt_tokens, completion_tokens
            FROM school_notice_api_budgets
            WHERE usage_date = ? AND api_type = ?
            """,
            (usage_date.isoformat(), api_type),
        ).fetchone()
        if row is None:
            return {
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
            }
        return {
            "calls": int(row["calls"]),
            "prompt_tokens": int(row["prompt_tokens"]),
            "completion_tokens": int(row["completion_tokens"]),
        }

    def resolve_notice_id(self, source_id: str, external_id: str) -> int | None:
        row = self.connection.execute(
            """
            SELECT id FROM school_notices
            WHERE source_id = ? AND external_id = ?
            """,
            (source_id, external_id),
        ).fetchone()
        return int(row["id"]) if row else None

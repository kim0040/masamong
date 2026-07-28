from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from pathlib import Path

from .parsing import TransferNoticeItem


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TransferNoticeStore:
    """공개 공지 snapshot 전용 SQLite. Discord 사용자 데이터는 넣지 않는다."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS transfer_notices (
                source_id TEXT NOT NULL,
                external_id TEXT NOT NULL,
                university TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                published_date TEXT,
                fingerprint TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY (source_id, external_id)
            );
            CREATE INDEX IF NOT EXISTS idx_transfer_notices_recent
                ON transfer_notices (published_date DESC, first_seen_at DESC);

            CREATE TABLE IF NOT EXISTS transfer_source_state (
                source_id TEXT PRIMARY KEY,
                initialized_at TEXT NOT NULL,
                last_success_at TEXT NOT NULL,
                watermark_date TEXT
            );

            CREATE TABLE IF NOT EXISTS transfer_runs (
                run_id TEXT PRIMARY KEY,
                run_date TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                status TEXT NOT NULL,
                source_count INTEGER NOT NULL,
                healthy_count INTEGER NOT NULL,
                change_count INTEGER NOT NULL,
                http_requests INTEGER NOT NULL
            );
            """
        )
        self.connection.commit()

    def source_state(self, source_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT source_id, initialized_at, last_success_at, watermark_date
            FROM transfer_source_state
            WHERE source_id = ?
            """,
            (source_id,),
        ).fetchone()

    def upsert_source_items(
        self,
        source_id: str,
        items: list[TransferNoticeItem],
        *,
        observed_at: str,
    ) -> tuple[list[dict], bool]:
        """source별 기준선을 적용하고 실제 새 글/수정 글만 반환한다."""
        state = self.source_state(source_id)
        baseline = state is None
        watermark = str(state["watermark_date"]) if state and state["watermark_date"] else None
        changes: list[dict] = []
        newest_date = watermark

        try:
            self.connection.execute("BEGIN IMMEDIATE")
            for item in items:
                previous = self.connection.execute(
                    """
                    SELECT fingerprint, revision
                    FROM transfer_notices
                    WHERE source_id = ? AND external_id = ?
                    """,
                    (source_id, item.external_id),
                ).fetchone()
                if previous is None:
                    revision = 1
                    self.connection.execute(
                        """
                        INSERT INTO transfer_notices (
                            source_id, external_id, university, title, url,
                            published_date, fingerprint, revision,
                            first_seen_at, last_seen_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            item.source_id,
                            item.external_id,
                            item.university,
                            item.title,
                            item.url,
                            item.published_date,
                            item.fingerprint,
                            revision,
                            observed_at,
                            observed_at,
                        ),
                    )
                    # 새 source의 과거 목록과 watermark보다 오래된 backfill은
                    # snapshot에는 보관하되 자동 알림에는 넣지 않는다.
                    if (
                        not baseline
                        and (not watermark or not item.published_date or item.published_date >= watermark)
                    ):
                        payload = item.as_dict()
                        payload.update(change_type="new", revision=revision)
                        changes.append(payload)
                else:
                    revision = int(previous["revision"])
                    changed = str(previous["fingerprint"]) != item.fingerprint
                    if changed:
                        revision += 1
                    self.connection.execute(
                        """
                        UPDATE transfer_notices
                        SET university = ?, title = ?, url = ?, published_date = ?,
                            fingerprint = ?, revision = ?, last_seen_at = ?
                        WHERE source_id = ? AND external_id = ?
                        """,
                        (
                            item.university,
                            item.title,
                            item.url,
                            item.published_date,
                            item.fingerprint,
                            revision,
                            observed_at,
                            item.source_id,
                            item.external_id,
                        ),
                    )
                    if changed and not baseline:
                        payload = item.as_dict()
                        payload.update(change_type="updated", revision=revision)
                        changes.append(payload)
                if item.published_date and (
                    newest_date is None or item.published_date > newest_date
                ):
                    newest_date = item.published_date

            if state is None:
                self.connection.execute(
                    """
                    INSERT INTO transfer_source_state (
                        source_id, initialized_at, last_success_at, watermark_date
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (source_id, observed_at, observed_at, newest_date),
                )
            else:
                self.connection.execute(
                    """
                    UPDATE transfer_source_state
                    SET last_success_at = ?, watermark_date = ?
                    WHERE source_id = ?
                    """,
                    (observed_at, newest_date, source_id),
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return changes, baseline

    def latest_items(self, limit: int = 80) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT source_id, external_id, university, title, url,
                   published_date, fingerprint, revision, first_seen_at
            FROM transfer_notices
            ORDER BY
                CASE WHEN published_date IS NULL THEN 1 ELSE 0 END,
                published_date DESC,
                first_seen_at DESC
            LIMIT ?
            """,
            (max(1, min(200, int(limit))),),
        ).fetchall()
        return [dict(row) for row in rows]

    def record_run(self, payload: dict) -> None:
        self.connection.execute(
            """
            INSERT INTO transfer_runs (
                run_id, run_date, started_at, finished_at, status,
                source_count, healthy_count, change_count, http_requests
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["run_id"],
                payload["run_date"],
                payload["started_at"],
                payload["generated_at"],
                payload["status"],
                len(payload["sources"]),
                sum(item["status"] == "healthy" for item in payload["sources"]),
                len(payload["changes"]),
                int(payload["http_requests"]),
            ),
        )
        self.connection.commit()

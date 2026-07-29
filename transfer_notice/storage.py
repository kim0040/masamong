from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from pathlib import Path

from .parsing import TransferNoticeItem, listing_fingerprint


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
                detail_fingerprint TEXT,
                detail_summary TEXT,
                detail_text TEXT,
                key_dates_json TEXT,
                last_detail_checked_at TEXT,
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
        # 기존 운영 SQLite는 삭제·재생성하지 않고 공개 상세 본문 열만
        # 추가한다. NULL인 과거 행은 기존 제목/링크 기준선 그대로 유지된다.
        existing_columns = {
            str(row[1])
            for row in self.connection.execute(
                "PRAGMA table_info(transfer_notices)"
            ).fetchall()
        }
        additions = {
            "detail_fingerprint": "TEXT",
            "detail_summary": "TEXT",
            "detail_text": "TEXT",
            "key_dates_json": "TEXT",
            "last_detail_checked_at": "TEXT",
        }
        for column, definition in additions.items():
            if column not in existing_columns:
                self.connection.execute(
                    f"ALTER TABLE transfer_notices ADD COLUMN {column} {definition}"
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
                    SELECT title, url, fingerprint, detail_fingerprint, revision
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
                            published_date, fingerprint, detail_fingerprint,
                            detail_summary, detail_text, key_dates_json,
                            last_detail_checked_at, revision,
                            first_seen_at, last_seen_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            item.source_id,
                            item.external_id,
                            item.university,
                            item.title,
                            item.url,
                            item.published_date,
                            item.fingerprint,
                            item.detail_fingerprint or None,
                            item.detail_summary or None,
                            item.detail_text or None,
                            json.dumps(item.key_dates, ensure_ascii=False),
                            observed_at if item.detail_fingerprint else None,
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
                        payload.pop("detail_text", None)
                        payload.update(change_type="new", revision=revision)
                        changes.append(payload)
                else:
                    revision = int(previous["revision"])
                    legacy_equivalent = (
                        listing_fingerprint(
                            str(previous["title"]),
                            str(previous["url"]),
                        )
                        == item.fingerprint
                    )
                    list_changed = (
                        str(previous["fingerprint"]) != item.fingerprint
                        and not legacy_equivalent
                    )
                    # 상세 본문을 처음 보강한 행은 기존 공지 재전송 사유가 아니다.
                    # 조회수·공통 메뉴처럼 학교 사이트의 동적 표시가 상세
                    # fingerprint를 흔드는 사례도 있으므로 상세-only 변경은
                    # 저장만 갱신하고 자동 알림 revision으로 승격하지 않는다.
                    changed = list_changed
                    if changed:
                        revision += 1
                    self.connection.execute(
                        """
                        UPDATE transfer_notices
                        SET university = ?, title = ?, url = ?, published_date = ?,
                            fingerprint = ?,
                            detail_fingerprint = COALESCE(?, detail_fingerprint),
                            detail_summary = COALESCE(?, detail_summary),
                            detail_text = COALESCE(?, detail_text),
                            key_dates_json = COALESCE(?, key_dates_json),
                            last_detail_checked_at = CASE
                                WHEN ? IS NULL THEN last_detail_checked_at ELSE ?
                            END,
                            revision = ?, last_seen_at = ?
                        WHERE source_id = ? AND external_id = ?
                        """,
                        (
                            item.university,
                            item.title,
                            item.url,
                            item.published_date,
                            item.fingerprint,
                            item.detail_fingerprint or None,
                            item.detail_summary or None,
                            item.detail_text or None,
                            (
                                json.dumps(item.key_dates, ensure_ascii=False)
                                if item.detail_fingerprint
                                else None
                            ),
                            item.detail_fingerprint or None,
                            observed_at,
                            revision,
                            observed_at,
                            item.source_id,
                            item.external_id,
                        ),
                    )
                    if changed and not baseline:
                        payload = item.as_dict()
                        payload.pop("detail_text", None)
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

    def detail_targets(
        self,
        source_id: str,
        items: list[TransferNoticeItem],
        *,
        limit: int = 3,
    ) -> list[str]:
        """새 글·목록 수정 글과 최신 한 건을 상세 확인 대상으로 고른다.

        첫 성공은 과거 공지 기준선이므로 전체 상세를 읽지 않는다. 이후에도
        source당 유한 개수만 반환해 20개 사이트를 순차 확인하는 저사양
        collector의 HTTP/CPU 상한을 유지한다.
        """
        if self.source_state(source_id) is None:
            return []
        bounded_limit = max(1, min(5, int(limit)))
        selected: list[str] = []
        for index, item in enumerate(items):
            row = self.connection.execute(
                """
                SELECT fingerprint, detail_fingerprint, last_detail_checked_at
                FROM transfer_notices
                WHERE source_id = ? AND external_id = ?
                """,
                (source_id, item.external_id),
            ).fetchone()
            is_new = row is None
            listing_changed = bool(
                row is not None
                and str(row["fingerprint"]) != item.fingerprint
            )
            # 목록 최상단 한 건은 상세 내용만 수정되는 경우를 잡기 위해
            # 하루 한 번 다시 확인한다. collector 자체가 하루 한 번이므로
            # 별도 시각 계산 없이 마지막 대상 여부만 본다.
            refresh_latest = index == 0
            if is_new or listing_changed or refresh_latest:
                selected.append(item.external_id)
            if len(selected) >= bounded_limit:
                break
        return selected

    def latest_items(self, limit: int = 80) -> list[dict]:
        rows = self.connection.execute(
            """
            SELECT source_id, external_id, university, title, url,
                   published_date, fingerprint, detail_fingerprint,
                   detail_summary, key_dates_json, revision, first_seen_at
            FROM transfer_notices
            ORDER BY
                CASE WHEN published_date IS NULL THEN 1 ELSE 0 END,
                published_date DESC,
                first_seen_at DESC
            LIMIT ?
            """,
            (max(1, min(200, int(limit))),),
        ).fetchall()
        results: list[dict] = []
        for row in rows:
            payload = dict(row)
            try:
                decoded = json.loads(str(payload.pop("key_dates_json") or "[]"))
            except json.JSONDecodeError:
                decoded = []
            payload["key_dates"] = (
                [str(item) for item in decoded[:8]]
                if isinstance(decoded, list)
                else []
            )
            results.append(payload)
        return results

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

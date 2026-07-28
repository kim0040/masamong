from __future__ import annotations

import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import uuid
from zoneinfo import ZoneInfo

from school_notice.http import AsyncFetcher

from .catalog import load_transfer_sources
from .parsing import parse_transfer_list
from .storage import TransferNoticeStore, utc_now_text


KST = ZoneInfo("Asia/Seoul")


class TransferNoticeCollector:
    """20개 공식 목록을 순차 확인하는 bounded one-shot collector."""

    def __init__(
        self,
        *,
        source_config: str | Path,
        database_path: str | Path,
        output_dir: str | Path,
        request_timeout_seconds: float = 20.0,
        max_retries: int = 1,
    ) -> None:
        self.sources = load_transfer_sources(source_config)
        self.database_path = Path(database_path)
        self.output_dir = Path(output_dir)
        self.request_timeout_seconds = max(5.0, min(60.0, request_timeout_seconds))
        self.max_retries = max(0, min(2, int(max_retries)))

    async def run(self) -> dict:
        started_at = utc_now_text()
        run_id = f"{datetime.now(KST):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
        run_date = datetime.now(KST).date().isoformat()
        source_runs: list[dict] = []
        changes: list[dict] = []
        baseline_sources: list[str] = []
        store = TransferNoticeStore(self.database_path)
        fetcher = AsyncFetcher(
            user_agent=(
                "Mozilla/5.0 (compatible; MasamongTransferNotice/1.0; "
                "+official-admissions-notices)"
            ),
            timeout_seconds=self.request_timeout_seconds,
            max_requests=len(self.sources) * (self.max_retries + 2),
            max_retries=self.max_retries,
            min_host_interval_seconds=0.25,
            respect_robots=True,
        )
        try:
            async with fetcher:
                for source in self.sources.values():
                    source_result = {
                        "source_id": source.source_id,
                        "university": source.university,
                        "official_url": source.official_url,
                        "status": "failed",
                        "item_count": 0,
                        "baseline": False,
                        "warnings": [],
                        "error": None,
                    }
                    try:
                        response = await fetcher.fetch_text(
                            source.list_url,
                            allowed_hosts=source.allowed_hosts,
                        )
                        items, warnings = parse_transfer_list(response.text, source)
                        source_result["warnings"] = warnings
                        source_result["item_count"] = len(items)
                        if not items:
                            source_result["status"] = "degraded"
                        else:
                            observed_at = utc_now_text()
                            source_changes, baseline = store.upsert_source_items(
                                source.source_id,
                                items,
                                observed_at=observed_at,
                            )
                            changes.extend(source_changes)
                            source_result["baseline"] = baseline
                            if baseline:
                                baseline_sources.append(source.source_id)
                            source_result["status"] = "healthy"
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        source_result["error"] = (
                            f"{type(exc).__name__}:{str(exc)[:180]}"
                        )
                    source_runs.append(source_result)

            healthy = sum(item["status"] == "healthy" for item in source_runs)
            failed = sum(item["status"] == "failed" for item in source_runs)
            status = (
                "succeeded"
                if healthy == len(source_runs)
                else "failed"
                if healthy == 0
                else "partial"
            )
            payload = {
                "schema_version": 1,
                "run_id": run_id,
                "run_date": run_date,
                "started_at": started_at,
                "generated_at": utc_now_text(),
                "status": status,
                "source_count": len(source_runs),
                "healthy_count": healthy,
                "failed_count": failed,
                "baseline_sources": baseline_sources,
                "http_requests": fetcher.request_count,
                "sources": source_runs,
                "changes": sorted(
                    changes,
                    key=lambda item: (
                        item.get("published_date") or "",
                        item["university"],
                        item["title"],
                    ),
                    reverse=True,
                ),
                "latest": store.latest_items(),
            }
            store.record_run(payload)
            self._write_output(payload)
            return payload
        finally:
            store.close()

    def _write_output(self, payload: dict) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        final_path = self.output_dir / "latest.json"
        temporary_path = self.output_dir / f".latest-{os.getpid()}.tmp"
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary_path, final_path)

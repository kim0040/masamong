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
from .parsing import parse_transfer_detail, parse_transfer_list
from .storage import TransferNoticeStore, utc_now_text


KST = ZoneInfo("Asia/Seoul")


class TransferNoticeCollector:
    """자동 구독 대상 공식 목록 20개를 순차 확인하는 bounded one-shot collector."""

    def __init__(
        self,
        *,
        source_config: str | Path,
        database_path: str | Path,
        output_dir: str | Path,
        request_timeout_seconds: float = 20.0,
        max_retries: int = 1,
        max_details_per_source: int = 3,
        min_request_interval_seconds: float = 0.35,
    ) -> None:
        self.sources = load_transfer_sources(source_config)
        self.database_path = Path(database_path)
        self.output_dir = Path(output_dir)
        self.request_timeout_seconds = max(5.0, min(60.0, request_timeout_seconds))
        self.max_retries = max(0, min(2, int(max_retries)))
        self.max_details_per_source = max(
            1,
            min(5, int(max_details_per_source)),
        )
        self.min_request_interval_seconds = max(
            0.0,
            min(5.0, float(min_request_interval_seconds)),
        )

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
            max_requests=len(self.sources)
            * (self.max_details_per_source + self.max_retries + 3),
            max_retries=self.max_retries,
            min_host_interval_seconds=0.25,
            min_request_interval_seconds=self.min_request_interval_seconds,
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
                            detail_ids = set(
                                store.detail_targets(
                                    source.source_id,
                                    items,
                                    limit=self.max_details_per_source,
                                )
                            )
                            enriched_items = []
                            for item in items:
                                if item.external_id not in detail_ids:
                                    enriched_items.append(item)
                                    continue
                                try:
                                    detail = await fetcher.fetch_text(
                                        item.url,
                                        allowed_hosts=source.allowed_hosts,
                                    )
                                    enriched_items.append(
                                        parse_transfer_detail(detail.text, item)
                                    )
                                    source_result.setdefault(
                                        "detail_succeeded",
                                        0,
                                    )
                                    source_result["detail_succeeded"] += 1
                                except asyncio.CancelledError:
                                    raise
                                except Exception as exc:
                                    enriched_items.append(item)
                                    source_result["warnings"].append(
                                        "detail_fetch_failed:"
                                        f"{item.external_id}:"
                                        f"{type(exc).__name__}"
                                    )
                            source_changes, baseline = store.upsert_source_items(
                                source.source_id,
                                enriched_items,
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
        rendered = json.dumps(payload, ensure_ascii=False, indent=2)
        final_path = self.output_dir / "latest.json"
        temporary_path = self.output_dir / f".latest-{os.getpid()}.tmp"
        temporary_path.write_text(
            rendered,
            encoding="utf-8",
        )
        os.replace(temporary_path, final_path)
        if payload.get("changes"):
            # 봇이 재시작 중이어도 다음 collector가 latest.json을 덮어써서
            # 미전송 변경을 잃지 않도록 작은 불변 이벤트 파일을 함께 남긴다.
            # 실제 중복 방지는 TiDB delivery ledger가 담당한다.
            event_dir = self.output_dir / "events"
            event_dir.mkdir(parents=True, exist_ok=True)
            event_path = event_dir / f"{payload['run_id']}.json"
            event_tmp = event_dir / f".{payload['run_id']}-{os.getpid()}.tmp"
            event_payload = {
                "schema_version": 1,
                "run_id": payload["run_id"],
                "generated_at": payload["generated_at"],
                "changes": payload["changes"],
                "latest": [],
            }
            event_tmp.write_text(
                json.dumps(event_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(event_tmp, event_path)

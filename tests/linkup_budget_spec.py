import asyncio

import aiosqlite
import pytest

import config
from utils import db as db_utils
from utils import linkup_search


class _CountingSchemaDB:
    backend = "tidb"

    class _Cursor:
        async def fetchall(self):
            return [
                ("request_id",),
                ("cost_usd",),
                ("billing_status",),
                ("finalized_at",),
            ]

    def __init__(self):
        self.execute_count = 0
        self.commit_count = 0

    async def execute(self, _query):
        self.execute_count += 1
        await asyncio.sleep(0)
        return self._Cursor()

    async def commit(self):
        self.commit_count += 1


@pytest.mark.asyncio
async def test_linkup_budget_is_enforced(monkeypatch):
    monkeypatch.setattr(config, "LINKUP_ENABLED", True)
    monkeypatch.setattr(config, "LINKUP_API_KEY", "test-key")
    monkeypatch.setattr(config, "LINKUP_BASE_URL", "https://api.linkup.so/v1")
    monkeypatch.setattr(config, "WEB_RAG_CACHE_TTL_SECONDS", 0)
    monkeypatch.setattr(config, "LINKUP_MONTHLY_BUDGET_ENFORCED", True)
    monkeypatch.setattr(config, "LINKUP_MONTHLY_BUDGET_USD", 0.005)
    monkeypatch.setattr(config, "LINKUP_QUALITY_RETRY_ENABLED", False)

    calls = {"count": 0}

    async def fake_post(endpoint: str, payload: dict):
        _ = endpoint, payload
        calls["count"] += 1
        rich_answer = (
            "OpenAI 최신 소식 요약. "
            "핵심 일정, 발표 포인트, 모델 업데이트, 공식 문서 링크를 정리한 충분히 긴 테스트 응답입니다. "
            "이 문장은 품질 재시도 트리거를 피하기 위해 길이를 늘리기 위한 내용입니다."
        )
        return {
            "answer": rich_answer,
            "sources": [
                {"name": "A", "url": "https://a.example.com", "snippet": "alpha detail snippet"},
                {"name": "B", "url": "https://b.example.com", "snippet": "beta detail snippet"},
            ],
        }

    monkeypatch.setattr(linkup_search, "_linkup_post_json", fake_post)

    async with aiosqlite.connect(":memory:") as db:
        first = await linkup_search.run_linkup_search_pipeline("OpenAI 최신 소식", db_conn=db)
        second = await linkup_search.run_linkup_search_pipeline("OpenAI 최신 소식 2", db_conn=db)

        assert first.get("status") == "success"
        assert second.get("status") == "error"
        assert "월 예산 한도" in second.get("message", "")
        assert calls["count"] == 1


@pytest.mark.asyncio
async def test_linkup_monthly_spend_accumulates():
    async with aiosqlite.connect(":memory:") as db:
        await db_utils.log_linkup_usage(
            db,
            endpoint="search",
            depth="standard",
            render_js=None,
            cost_eur=0.005,
        )
        await db_utils.log_linkup_usage(
            db,
            endpoint="fetch",
            depth=None,
            render_js=True,
            cost_eur=0.005,
        )
        spent = await db_utils.get_linkup_monthly_spend_usd(db)
        assert spent == pytest.approx(0.01, rel=1e-6)


@pytest.mark.asyncio
async def test_linkup_spend_excludes_explicit_failure_but_keeps_unknown_reservation():
    async with aiosqlite.connect(":memory:") as db:
        assert await db_utils.reserve_linkup_usage(
            db,
            request_id="explicit-failure",
            endpoint="search",
            depth="standard",
            cost_usd=0.005,
        )
        assert await db_utils.finalize_linkup_usage(
            db,
            request_id="explicit-failure",
            billing_status="not_billed",
        )
        assert await db_utils.reserve_linkup_usage(
            db,
            request_id="unknown-outcome",
            endpoint="search",
            depth="standard",
            cost_usd=0.005,
        )

        spent = await db_utils.get_linkup_monthly_spend_usd(db)

    assert spent == pytest.approx(0.005, rel=1e-6)


@pytest.mark.asyncio
async def test_sqlite_additive_upgrade_preserves_legacy_linkup_row(monkeypatch):
    monkeypatch.setattr(config, "AUTO_MIGRATE", True)
    async with aiosqlite.connect(":memory:") as db:
        await db.execute(
            """
            CREATE TABLE linkup_usage_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                used_at TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                depth TEXT,
                render_js BOOLEAN,
                cost_eur REAL NOT NULL
            )
            """
        )
        await db.execute(
            """
            INSERT INTO linkup_usage_log (
                used_at, endpoint, depth, render_js, cost_eur
            )
            VALUES ('2026-07-01T00:00:00+00:00', 'search', 'standard', NULL, 0.005)
            """
        )
        await db.commit()

        await db_utils._ensure_linkup_usage_table(db)

        async with db.execute(
            "PRAGMA table_info(linkup_usage_log)"
        ) as cursor:
            columns = {str(row[1]) for row in await cursor.fetchall()}
        async with db.execute(
            "SELECT COUNT(*), SUM(cost_eur) FROM linkup_usage_log"
        ) as cursor:
            count, legacy_cost = await cursor.fetchone()

    assert {
        "request_id",
        "cost_usd",
        "billing_status",
        "finalized_at",
    } <= columns
    assert count == 1
    assert legacy_cost == pytest.approx(0.005)


@pytest.mark.asyncio
async def test_linkup_schema_check_runs_once_per_connection_under_concurrency(
    monkeypatch,
):
    monkeypatch.setattr(config, "AUTO_MIGRATE", True)
    db = _CountingSchemaDB()

    await asyncio.gather(
        *(db_utils._ensure_linkup_usage_table(db) for _ in range(8))
    )
    await db_utils._ensure_linkup_usage_table(db)

    assert db.execute_count == 2
    assert db.commit_count == 1

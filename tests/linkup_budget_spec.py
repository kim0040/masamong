import aiosqlite
import pytest

import config
from utils import db as db_utils
from utils import linkup_search


@pytest.mark.asyncio
async def test_linkup_budget_is_enforced(monkeypatch):
    monkeypatch.setattr(config, "LINKUP_ENABLED", True)
    monkeypatch.setattr(config, "LINKUP_API_KEY", "test-key")
    monkeypatch.setattr(config, "LINKUP_BASE_URL", "https://api.linkup.so/v1")
    monkeypatch.setattr(config, "WEB_RAG_CACHE_TTL_SECONDS", 0)
    monkeypatch.setattr(config, "LINKUP_MONTHLY_BUDGET_ENFORCED", True)
    monkeypatch.setattr(config, "LINKUP_MONTHLY_BUDGET_EUR", 0.005)
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
        spent = await db_utils.get_linkup_monthly_spend_eur(db)
        assert spent == pytest.approx(0.01, rel=1e-6)

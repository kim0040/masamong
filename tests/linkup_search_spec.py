import asyncio

import aiosqlite
import pytest

import config
from utils import linkup_search


@pytest.fixture(autouse=True)
def _prepare_linkup_defaults(monkeypatch):
    monkeypatch.setattr(config, "LINKUP_ENABLED", True)
    monkeypatch.setattr(config, "LINKUP_API_KEY", "test-key")
    monkeypatch.setattr(config, "LINKUP_BASE_URL", "https://api.linkup.so/v1")
    monkeypatch.setattr(config, "LINKUP_OUTPUT_TYPE", "searchResults")
    monkeypatch.setattr(config, "WEB_RAG_CACHE_TTL_SECONDS", 0)
    monkeypatch.setattr(config, "LINKUP_QUALITY_RETRY_ENABLED", True)
    monkeypatch.setattr(config, "LINKUP_DEEP_RETRY_MIN_SOURCES", 2)
    monkeypatch.setattr(config, "LINKUP_MIN_ANSWER_CHARS", 120)
    monkeypatch.setattr(config, "LINKUP_FETCH_RENDER_JS", False)
    monkeypatch.setattr(config, "LINKUP_FETCH_JS_RETRY_ENABLED", True)
    linkup_search._pipeline_cache.clear()
    linkup_search._pipeline_inflight.clear()


def test_infer_linkup_depth():
    assert linkup_search.infer_linkup_depth("아브라함 링컨은 언제 태어났어?") == "fast"
    assert linkup_search.infer_linkup_depth("AI 에이전트 시장 동향 비교 분석해줘") == "deep"
    assert linkup_search.infer_linkup_depth("OpenAI 최신 소식 알려줘") == "standard"
    # 복합/행사성 질의는 fast로 내리지 않는다.
    assert linkup_search.infer_linkup_depth("이번 전북대 축제 라인업 어떰?") == "standard"


def test_semantic_depth_hint_is_used_but_fast_is_blocked_for_multistep_queries():
    assert (
        linkup_search.normalize_linkup_depth_hint(
            "오늘 삼성전기 실적 발표 결과",
            "fast",
        )
        == "fast"
    )
    assert (
        linkup_search.normalize_linkup_depth_hint(
            "먼저 공식 URL을 찾고 여러 자료를 비교 분석해줘",
            "fast",
        )
        == "standard"
    )
    assert (
        linkup_search.normalize_linkup_depth_hint(
            "https://example.com 내용을 확인해줘",
            "fast",
        )
        == "standard"
    )


def test_url_query_prefers_fetch_first():
    assert linkup_search._should_fetch_first("요약해줘 https://example.com", "https://example.com") is True
    assert (
        linkup_search._should_fetch_first(
            "https://example.com 이 페이지와 경쟁사 비교해줘",
            "https://example.com",
        )
        is False
    )


@pytest.mark.asyncio
async def test_run_linkup_search_pipeline_uses_search(monkeypatch):
    calls = []

    async def fake_post(endpoint: str, payload: dict):
        calls.append((endpoint, payload))
        return {
            "answer": "최신 요약 답변 [1]",
            "sources": [
                {"name": "Source A", "url": "https://a.example.com", "snippet": "alpha"},
                {"name": "Source B", "url": "https://b.example.com", "snippet": "beta"},
            ],
        }

    monkeypatch.setattr(linkup_search, "_linkup_post_json", fake_post)

    async with aiosqlite.connect(":memory:") as db:
        result = await linkup_search.run_linkup_search_pipeline(
            "오늘 OpenAI 최신 업데이트 알려줘",
            db_conn=db,
        )

    assert result["status"] == "success"
    assert result["provider"] == "linkup"
    assert result["source_urls"] == ["https://a.example.com", "https://b.example.com"]
    assert calls
    assert calls[0][0] == "search"
    assert calls[0][1]["outputType"] == "searchResults"
    assert "includeInlineCitations" not in calls[0][1]
    assert "fromDate" in calls[0][1]
    assert "toDate" in calls[0][1]


@pytest.mark.asyncio
async def test_semantic_fast_hint_reaches_linkup_payload(monkeypatch):
    calls = []

    async def fake_post(endpoint: str, payload: dict):
        calls.append((endpoint, dict(payload)))
        return {
            "results": [
                {
                    "name": "공식 실적",
                    "url": "https://example.com/result",
                    "content": "공식 발표 수치와 기준 시각 " * 8,
                },
                {
                    "name": "거래소 공시",
                    "url": "https://example.org/filing",
                    "content": "거래소 공시 수치와 발표 시각 " * 8,
                },
            ]
        }

    monkeypatch.setattr(linkup_search, "_linkup_post_json", fake_post)

    async with aiosqlite.connect(":memory:") as db:
        result = await linkup_search.run_linkup_search_pipeline(
            "오늘 삼성전기 실적 발표 결과",
            db_conn=db,
            depth_hint="fast",
        )

    assert result["status"] == "success"
    assert result["quality"]["depth"] == "fast"
    assert calls[0][1]["depth"] == "fast"


@pytest.mark.asyncio
async def test_fast_quality_retry_only_steps_up_to_standard(monkeypatch):
    calls = []

    async def fake_post(endpoint: str, payload: dict):
        calls.append((endpoint, dict(payload)))
        if payload["depth"] == "fast":
            return {
                "results": [
                    {
                        "name": "결과 하나",
                        "url": "https://one.example.com",
                        "content": "짧음",
                    }
                ]
            }
        return {
            "results": [
                {
                    "name": "공식 자료",
                    "url": "https://a.example.com",
                    "content": "충분한 공식 자료 발췌 " * 8,
                },
                {
                    "name": "보조 자료",
                    "url": "https://b.example.com",
                    "content": "충분한 보조 자료 발췌 " * 8,
                },
            ]
        }

    monkeypatch.setattr(linkup_search, "_linkup_post_json", fake_post)

    async with aiosqlite.connect(":memory:") as db:
        result = await linkup_search.run_linkup_search_pipeline(
            "오늘 삼성전기 실적 발표 결과",
            db_conn=db,
            depth_hint="fast",
        )

    assert result["status"] == "success"
    assert result["quality"]["depth"] == "standard"
    assert [payload["depth"] for _, payload in calls] == [
        "fast",
        "standard",
    ]


@pytest.mark.asyncio
async def test_run_linkup_search_pipeline_uses_fetch_for_direct_url(monkeypatch):
    calls = []

    async def fake_post(endpoint: str, payload: dict):
        calls.append((endpoint, payload))
        assert endpoint == "fetch"
        return {"markdown": "페이지 본문 요약용 텍스트"}

    monkeypatch.setattr(linkup_search, "_linkup_post_json", fake_post)

    async with aiosqlite.connect(":memory:") as db:
        result = await linkup_search.run_linkup_search_pipeline(
            "이 링크 요약해줘 https://example.com/pricing",
            db_conn=db,
        )

    assert result["status"] == "success"
    assert result["search_kind"] == "DIRECT_URL"
    assert result["source_urls"] == ["https://example.com/pricing"]
    assert calls and calls[0][0] == "fetch"
    assert calls[0][1]["renderJs"] is False


@pytest.mark.asyncio
async def test_direct_fetch_retries_js_once_only_when_plain_fetch_is_empty(monkeypatch):
    calls = []

    async def fake_post(endpoint: str, payload: dict):
        calls.append((endpoint, dict(payload)))
        return {"markdown": "JS 본문"} if payload["renderJs"] else {"markdown": ""}

    monkeypatch.setattr(linkup_search, "_linkup_post_json", fake_post)

    async with aiosqlite.connect(":memory:") as db:
        result = await linkup_search.run_linkup_search_pipeline(
            "이 링크 요약 https://example.com/app",
            db_conn=db,
        )

    assert result["status"] == "success"
    assert [payload["renderJs"] for _, payload in calls] == [False, True]


@pytest.mark.asyncio
async def test_identical_concurrent_queries_share_one_billed_provider_call(monkeypatch):
    calls = 0

    async def fake_post(_endpoint: str, _payload: dict):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return {
            "results": [
                {"name": "A", "url": "https://a.example.com", "content": "alpha"},
                {"name": "B", "url": "https://b.example.com", "content": "beta"},
            ]
        }

    monkeypatch.setattr(linkup_search, "_linkup_post_json", fake_post)
    monkeypatch.setattr(config, "LINKUP_QUALITY_RETRY_ENABLED", False)

    async with aiosqlite.connect(":memory:") as db:
        first, second = await asyncio.gather(
            linkup_search.run_linkup_search_pipeline("  같은   검색어 ", db_conn=db),
            linkup_search.run_linkup_search_pipeline("같은 검색어", db_conn=db),
        )

    assert first["status"] == second["status"] == "success"
    assert calls == 1
    assert second.get("shared_inflight") is True


@pytest.mark.asyncio
async def test_run_linkup_search_pipeline_retries_with_deep_on_low_quality(monkeypatch):
    calls = []

    async def fake_post(endpoint: str, payload: dict):
        calls.append((endpoint, payload))
        assert endpoint == "search"
        if payload["depth"] == "standard":
            return {
                "answer": "짧은 답변",
                "sources": [{"name": "Source A", "url": "https://a.example.com", "snippet": "alpha"}],
            }
        return {
            "answer": "더 자세한 답변 [1][2]",
            "sources": [
                {"name": "Source A", "url": "https://a.example.com", "snippet": "alpha"},
                {"name": "Source B", "url": "https://b.example.com", "snippet": "beta"},
            ],
        }

    monkeypatch.setattr(linkup_search, "_linkup_post_json", fake_post)

    async with aiosqlite.connect(":memory:") as db:
        result = await linkup_search.run_linkup_search_pipeline(
            "오늘 오픈AI 발표 내용 알려줘",
            db_conn=db,
        )

    assert result["status"] == "success"
    assert result["quality"]["depth"] == "deep"
    assert len(calls) == 2
    assert calls[0][1]["depth"] == "standard"
    assert calls[1][1]["depth"] == "deep"

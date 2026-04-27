import pytest

import config
from utils import linkup_search


@pytest.fixture(autouse=True)
def _prepare_linkup_defaults(monkeypatch):
    monkeypatch.setattr(config, "LINKUP_ENABLED", True)
    monkeypatch.setattr(config, "LINKUP_API_KEY", "test-key")
    monkeypatch.setattr(config, "LINKUP_BASE_URL", "https://api.linkup.so/v1")
    monkeypatch.setattr(config, "WEB_RAG_CACHE_TTL_SECONDS", 0)
    monkeypatch.setattr(config, "LINKUP_QUALITY_RETRY_ENABLED", True)
    monkeypatch.setattr(config, "LINKUP_DEEP_RETRY_MIN_SOURCES", 2)
    monkeypatch.setattr(config, "LINKUP_MIN_ANSWER_CHARS", 120)
    linkup_search._pipeline_cache.clear()


def test_infer_linkup_depth():
    assert linkup_search.infer_linkup_depth("아브라함 링컨은 언제 태어났어?") == "fast"
    assert linkup_search.infer_linkup_depth("AI 에이전트 시장 동향 비교 분석해줘") == "deep"
    assert linkup_search.infer_linkup_depth("OpenAI 최신 소식 알려줘") == "standard"
    # 복합/행사성 질의는 fast로 내리지 않는다.
    assert linkup_search.infer_linkup_depth("이번 전북대 축제 라인업 어떰?") == "standard"


def test_url_query_prefers_fetch_first():
    assert linkup_search._should_fetch_first("요약해줘 https://example.com", "https://example.com") is True
    assert (
        linkup_search._should_fetch_first(
            "https://example.com 이 페이지와 경쟁사 비교해줘",
            "https://example.com",
        )
        is True
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

    result = await linkup_search.run_linkup_search_pipeline("오늘 OpenAI 최신 업데이트 알려줘")

    assert result["status"] == "success"
    assert result["provider"] == "linkup"
    assert result["source_urls"] == ["https://a.example.com", "https://b.example.com"]
    assert calls
    assert calls[0][0] == "search"
    assert calls[0][1]["outputType"] == "sourcedAnswer"
    assert "fromDate" in calls[0][1]
    assert "toDate" in calls[0][1]


@pytest.mark.asyncio
async def test_run_linkup_search_pipeline_uses_fetch_for_direct_url(monkeypatch):
    calls = []

    async def fake_post(endpoint: str, payload: dict):
        calls.append((endpoint, payload))
        assert endpoint == "fetch"
        return {"markdown": "페이지 본문 요약용 텍스트"}

    monkeypatch.setattr(linkup_search, "_linkup_post_json", fake_post)

    result = await linkup_search.run_linkup_search_pipeline("이 링크 요약해줘 https://example.com/pricing")

    assert result["status"] == "success"
    assert result["search_kind"] == "DIRECT_URL"
    assert result["source_urls"] == ["https://example.com/pricing"]
    assert calls and calls[0][0] == "fetch"


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

    result = await linkup_search.run_linkup_search_pipeline("오늘 오픈AI 발표 내용 알려줘")

    assert result["status"] == "success"
    assert result["quality"]["depth"] == "deep"
    assert len(calls) == 2
    assert calls[0][1]["depth"] == "standard"
    assert calls[1][1]["depth"] == "deep"

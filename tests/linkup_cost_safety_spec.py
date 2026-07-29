"""Linkup 비용 예약과 안전한 provider 폴백 회귀 테스트."""

import asyncio
from unittest.mock import AsyncMock

import aiosqlite
import pytest

import config
from cogs.tools_cog import ToolsCog
from utils import linkup_search
from utils import db as db_utils


class _FakeBot:
    db = object()

    def get_cog(self, _name):
        return None


@pytest.fixture(autouse=True)
def _linkup_defaults(monkeypatch):
    monkeypatch.setattr(config, "LINKUP_ENABLED", True)
    monkeypatch.setattr(config, "LINKUP_API_KEY", "test-key")
    monkeypatch.setattr(config, "LINKUP_MONTHLY_BUDGET_ENFORCED", True)
    monkeypatch.setattr(config, "LINKUP_MONTHLY_BUDGET_EUR", 4.5)
    monkeypatch.setattr(config, "LINKUP_QUALITY_RETRY_ENABLED", False)
    monkeypatch.setattr(config, "WEB_RAG_CACHE_TTL_SECONDS", 0)
    monkeypatch.setattr(linkup_search, "_linkup_budget_lock", asyncio.Lock())
    monkeypatch.setattr(
        db_utils,
        "reserve_web_search_call",
        AsyncMock(return_value=(True, None)),
    )


@pytest.mark.asyncio
async def test_timeout_is_reserved_and_blocks_legacy_fallback(monkeypatch):
    async def timeout_after_send(_endpoint, _payload):
        raise asyncio.TimeoutError("provider timed out")

    monkeypatch.setattr(linkup_search, "_linkup_post_json", timeout_after_send)

    async with aiosqlite.connect(":memory:") as db:
        result = await linkup_search.run_linkup_search_pipeline(
            "OpenAI 최신 소식",
            db_conn=db,
        )
        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost_eur), 0) FROM linkup_usage_log"
        ) as cursor:
            count, cost = await cursor.fetchone()

    assert result["status"] == "error"
    assert result["fallback_safe"] is False
    assert result["failure_kind"] == "provider_outcome_unknown"
    assert count == 1
    assert cost == pytest.approx(0.005)


@pytest.mark.asyncio
async def test_provider_is_not_called_when_reservation_fails(monkeypatch):
    monkeypatch.setattr(
        linkup_search.db_utils,
        "can_spend_linkup_budget",
        AsyncMock(return_value=(True, 0.0, 4.5)),
    )
    monkeypatch.setattr(
        linkup_search.db_utils,
        "log_linkup_usage",
        AsyncMock(return_value=False),
    )
    provider = AsyncMock(return_value={})
    monkeypatch.setattr(linkup_search, "_linkup_post_json", provider)

    result = await linkup_search.run_linkup_search_pipeline(
        "안전 예약 테스트",
        db_conn=object(),
    )

    assert result["fallback_safe"] is False
    assert result["failure_kind"] == "reservation_failed"
    provider.assert_not_awaited()


@pytest.mark.asyncio
async def test_budget_check_reservation_and_call_are_process_serialized(monkeypatch):
    events: list[str] = []
    sequence = 0

    async def allowed(*_args, **_kwargs):
        events.append("check")
        return True, 0.0, 4.5

    async def reserve(*_args, **_kwargs):
        events.append("reserve")
        return True

    async def provider(_endpoint, _payload):
        nonlocal sequence
        sequence += 1
        call_no = sequence
        events.append(f"call-{call_no}-start")
        await asyncio.sleep(0.01)
        events.append(f"call-{call_no}-end")
        return {"answer": "ok"}

    monkeypatch.setattr(linkup_search.db_utils, "can_spend_linkup_budget", allowed)
    monkeypatch.setattr(linkup_search.db_utils, "log_linkup_usage", reserve)
    monkeypatch.setattr(linkup_search, "_linkup_post_json", provider)

    await asyncio.gather(
        *(
            linkup_search._execute_billed_linkup_call(
                endpoint="search",
                payload={"q": str(index)},
                db_conn=object(),
                depth="standard",
            )
            for index in range(2)
        )
    )

    assert events == [
        "check",
        "reserve",
        "call-1-start",
        "call-1-end",
        "check",
        "reserve",
        "call-2-start",
        "call-2-end",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fallback_safe", "failure_kind"),
    [
        (True, "provider_rejected"),
        (False, "provider_outcome_unknown"),
    ],
)
async def test_request_failure_safety_state_is_preserved(
    monkeypatch,
    fallback_safe,
    failure_kind,
):
    async def fail(_endpoint, _payload):
        raise linkup_search.LinkupRequestError(
            "provider error",
            fallback_safe=fallback_safe,
            failure_kind=failure_kind,
            status=400 if fallback_safe else 503,
        )

    monkeypatch.setattr(linkup_search, "_linkup_post_json", fail)

    async with aiosqlite.connect(":memory:") as db:
        result = await linkup_search.run_linkup_search_pipeline(
            "검색 테스트",
            db_conn=db,
        )

    assert result["fallback_safe"] is fallback_safe
    assert result["failure_kind"] == failure_kind


@pytest.mark.asyncio
async def test_tools_cog_does_not_fallback_after_uncertain_linkup_failure(monkeypatch):
    monkeypatch.setattr(config, "WEB_SEARCH_PROVIDER", "linkup")
    cog = ToolsCog(_FakeBot())
    linkup = AsyncMock(
        return_value={
            "status": "error",
            "message": "provider outcome unknown",
            "fallback_safe": False,
            "failure_kind": "provider_outcome_unknown",
        }
    )
    legacy = AsyncMock(
        side_effect=AssertionError("uncertain Linkup failure must not fall back")
    )
    monkeypatch.setattr(cog, "_load_linkup_search_pipeline", AsyncMock(return_value=linkup))
    monkeypatch.setattr(cog, "_load_news_search_pipeline", AsyncMock(return_value=legacy))

    result = await cog.web_search_rag("최신 정보")

    assert result["fallback_safe"] is False
    legacy.assert_not_awaited()


@pytest.mark.asyncio
async def test_tools_cog_keeps_legacy_fallback_for_safe_rejection(monkeypatch):
    monkeypatch.setattr(config, "WEB_SEARCH_PROVIDER", "linkup")
    cog = ToolsCog(_FakeBot())
    linkup = AsyncMock(
        return_value={
            "status": "error",
            "message": "invalid request",
            "fallback_safe": True,
            "failure_kind": "provider_rejected",
        }
    )
    legacy_result = {"status": "success", "context": "legacy result"}
    legacy = AsyncMock(return_value=legacy_result)
    monkeypatch.setattr(cog, "_load_linkup_search_pipeline", AsyncMock(return_value=linkup))
    monkeypatch.setattr(cog, "_load_news_search_pipeline", AsyncMock(return_value=legacy))

    result = await cog.web_search_rag("검색")

    assert result == legacy_result
    legacy.assert_awaited_once_with("검색")


@pytest.mark.asyncio
async def test_tools_cog_loader_failure_is_safe_to_fallback(monkeypatch):
    monkeypatch.setattr(config, "WEB_SEARCH_PROVIDER", "linkup")
    cog = ToolsCog(_FakeBot())
    legacy_result = {"status": "success", "context": "legacy result"}
    legacy = AsyncMock(return_value=legacy_result)
    monkeypatch.setattr(
        cog,
        "_load_linkup_search_pipeline",
        AsyncMock(side_effect=ImportError("module unavailable")),
    )
    monkeypatch.setattr(cog, "_load_news_search_pipeline", AsyncMock(return_value=legacy))

    result = await cog.web_search_rag("검색")

    assert result == legacy_result
    legacy.assert_awaited_once_with("검색")


def test_result_and_cost_related_config_values_are_bounded(monkeypatch):
    monkeypatch.setattr(config, "LINKUP_STANDARD_MAX_RESULTS", 9999)
    monkeypatch.setattr(config, "LINKUP_REALTIME_LOOKBACK_DAYS", 9999)

    payload = linkup_search._build_search_payload("오늘 소식", "standard")

    assert payload["maxResults"] == 20
    from_date = payload["fromDate"]
    to_date = payload["toDate"]
    assert from_date < to_date


@pytest.mark.asyncio
async def test_monthly_budget_config_is_bounded_before_check(monkeypatch):
    captured = {}

    async def budget_check(_db, _cost, *, budget_limit_eur):
        captured["limit"] = budget_limit_eur
        return False, 0.0, budget_limit_eur

    monkeypatch.setattr(config, "LINKUP_MONTHLY_BUDGET_EUR", 999999)
    monkeypatch.setattr(
        linkup_search.db_utils,
        "can_spend_linkup_budget",
        budget_check,
    )

    with pytest.raises(linkup_search.LinkupBudgetExceededError):
        await linkup_search._execute_billed_linkup_call(
            endpoint="search",
            payload={"q": "test"},
            db_conn=object(),
            depth="standard",
        )

    assert captured["limit"] == 1000.0


@pytest.mark.asyncio
async def test_http_timeout_config_is_bounded(monkeypatch):
    captured = {}

    class _Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def text(self):
            return "{}"

    class _Session:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout.total

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def post(self, *_args, **_kwargs):
            return _Response()

    monkeypatch.setattr(config, "LINKUP_TIMEOUT_SECONDS", 9999)
    monkeypatch.setattr(linkup_search.aiohttp, "ClientSession", _Session)

    assert await linkup_search._linkup_post_json("search", {"q": "test"}) == {}
    assert captured["timeout"] == 120


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "fallback_safe"),
    [(400, True), (429, True), (500, False), (503, False)],
)
async def test_http_status_classifies_fallback_safety(
    monkeypatch,
    status,
    fallback_safe,
):
    class _Response:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def text(self):
            return '{"error":{"message":"provider error"}}'

    class _Session:
        def __init__(self, *, timeout):
            _ = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def post(self, *_args, **_kwargs):
            response = _Response()
            response.status = status
            return response

    monkeypatch.setattr(linkup_search.aiohttp, "ClientSession", _Session)

    with pytest.raises(linkup_search.LinkupRequestError) as error:
        await linkup_search._linkup_post_json("search", {"q": "test"})

    assert error.value.fallback_safe is fallback_safe


@pytest.mark.asyncio
async def test_web_search_string_path_stops_google_and_kakao_on_uncertain_failure(
    monkeypatch,
):
    cog = ToolsCog(_FakeBot())
    monkeypatch.setattr(
        cog,
        "web_search_rag",
        AsyncMock(
            return_value={
                "status": "error",
                "message": "do not retry",
                "fallback_safe": False,
            }
        ),
    )
    monkeypatch.setattr(config, "GOOGLE_API_KEY", "configured")
    monkeypatch.setattr(config, "GOOGLE_CX", "configured")
    kakao = AsyncMock(side_effect=AssertionError("Kakao fallback must not run"))
    monkeypatch.setattr(cog, "kakao_web_search", kakao)

    result = await cog.web_search("최신 뉴스")

    assert result == "do not retry"
    kakao.assert_not_awaited()

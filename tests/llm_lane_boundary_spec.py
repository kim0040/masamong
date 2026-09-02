"""레인 단위 ZDR 경계와 429 재시도 경계의 명세 테스트.

main 레인은 대화 본문을 그대로 보내므로 ZDR 공급자만 사용하고, 라우팅 판단만
하는 routing 레인은 ZDR까지 요구하지 않는다. 어느 레인이든 데이터가 흘러갈 수
있는 범위는 provider 허용 목록과 allow_fallbacks=false로 고정한다.
"""

import asyncio

import pytest

import config
from utils.llm_client import LLMClient, rate_limit_retry_delay


def _options(client: LLMClient, target_name: str) -> dict:
    return client._openrouter_request_options(
        {
            "name": target_name,
            "base_url": "https://openrouter.ai/api/v1",
        },
        "",
    )


def _provider(client: LLMClient, target_name: str) -> dict:
    return _options(client, target_name)["extra_body"]["provider"]


def test_zdr_is_decided_per_lane(monkeypatch):
    """main은 ZDR을 유지하고 routing만 완화할 수 있다."""
    monkeypatch.setattr(config, "OPENROUTER_ZDR", True, raising=False)
    monkeypatch.setattr(config, "OPENROUTER_MAIN_ZDR", True, raising=False)
    monkeypatch.setattr(config, "OPENROUTER_ROUTING_ZDR", False, raising=False)
    monkeypatch.setattr(
        config, "OPENROUTER_MAIN_PROVIDER_ONLY", "morph", raising=False
    )
    monkeypatch.setattr(
        config, "OPENROUTER_ROUTING_PROVIDER_ONLY", "openai", raising=False
    )
    client = LLMClient()

    main = _provider(client, "main.primary")
    routing = _provider(client, "routing.primary")

    assert main["zdr"] is True
    assert main["only"] == ["morph"]
    # ZDR을 요구하지 않는 레인에서는 zdr 키 자체를 보내지 않는다.
    assert "zdr" not in routing
    assert routing["only"] == ["openai"]


def test_relaxed_zdr_still_pins_provider_allowlist(monkeypatch):
    """ZDR을 완화해도 공급자 허용 목록과 폴백 차단은 그대로 유지된다."""
    monkeypatch.setattr(config, "OPENROUTER_ROUTING_ZDR", False, raising=False)
    monkeypatch.setattr(
        config, "OPENROUTER_ROUTING_PROVIDER_ONLY", "openai", raising=False
    )
    monkeypatch.setattr(config, "OPENROUTER_ALLOW_FALLBACKS", False, raising=False)
    monkeypatch.setattr(config, "OPENROUTER_DATA_COLLECTION", "deny", raising=False)
    client = LLMClient()

    routing = _provider(client, "routing.primary")

    assert routing["only"] == ["openai"]
    assert routing["allow_fallbacks"] is False
    assert routing["data_collection"] == "deny"


def test_lane_zdr_defaults_to_global_setting(monkeypatch):
    """레인 override가 없으면(None) 전역 OPENROUTER_ZDR을 그대로 따른다."""
    monkeypatch.setattr(config, "OPENROUTER_ZDR", True, raising=False)
    monkeypatch.setattr(config, "OPENROUTER_MAIN_ZDR", None, raising=False)
    monkeypatch.setattr(config, "OPENROUTER_ROUTING_ZDR", None, raising=False)
    client = LLMClient()

    assert _provider(client, "main.primary")["zdr"] is True
    assert _provider(client, "routing.primary")["zdr"] is True


def test_lane_override_can_also_tighten(monkeypatch):
    """전역이 꺼져 있어도 레인 override로 ZDR을 켤 수 있다."""
    monkeypatch.setattr(config, "OPENROUTER_ZDR", False, raising=False)
    monkeypatch.setattr(config, "OPENROUTER_MAIN_ZDR", True, raising=False)
    monkeypatch.setattr(config, "OPENROUTER_ROUTING_ZDR", None, raising=False)
    client = LLMClient()

    assert _provider(client, "main.primary")["zdr"] is True
    assert "zdr" not in _provider(client, "routing.primary")


_real_sleep = asyncio.sleep


async def _instant_sleep(_delay):
    """재시도 대기를 실제로 기다리지 않고 이벤트 루프만 한 번 양보한다."""
    await _real_sleep(0)


class _RateLimited(Exception):
    """OpenRouter 429 응답 형태를 흉내 낸다."""

    def __init__(self, retry_after_seconds=None):
        super().__init__("Provider returned error")
        self.status_code = 429
        self.body = {
            "error": {
                "code": 429,
                "metadata": {"retry_after_seconds": retry_after_seconds},
            }
        }


def test_rate_limit_delay_uses_provider_hint():
    assert rate_limit_retry_delay(_RateLimited(2), max_delay=5) == 2.0


def test_rate_limit_delay_is_capped():
    assert rate_limit_retry_delay(_RateLimited(600), max_delay=5) == 5.0


def test_rate_limit_delay_falls_back_when_hint_missing():
    assert rate_limit_retry_delay(_RateLimited(None), max_delay=5) == 1.0


def test_non_rate_limit_errors_are_not_retryable():
    """인증 오류·모델 없음은 기다려도 낫지 않으므로 재시도 대상이 아니다."""

    class _AuthError(Exception):
        status_code = 401

    assert rate_limit_retry_delay(_AuthError(), max_delay=5) is None
    assert rate_limit_retry_delay(TimeoutError("slow"), max_delay=5) is None


@pytest.mark.asyncio
async def test_rate_limited_call_is_retried_then_succeeds(monkeypatch):
    monkeypatch.setattr(config, "LLM_RATE_LIMIT_MAX_RETRIES", 2, raising=False)
    monkeypatch.setattr(
        config, "LLM_RATE_LIMIT_MAX_DELAY_SECONDS", 5, raising=False
    )
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    client = LLMClient()
    attempts = 0

    async def flaky():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise _RateLimited(1)
        return "ok"

    result = await client._call_with_rate_limit_retry(
        flaky, lane_name="main.primary"
    )

    assert result == "ok"
    assert attempts == 2


@pytest.mark.asyncio
async def test_rate_limit_retries_are_bounded(monkeypatch):
    monkeypatch.setattr(config, "LLM_RATE_LIMIT_MAX_RETRIES", 2, raising=False)
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    client = LLMClient()
    attempts = 0

    async def always_limited():
        nonlocal attempts
        attempts += 1
        raise _RateLimited(1)

    with pytest.raises(_RateLimited):
        await client._call_with_rate_limit_retry(
            always_limited, lane_name="main.primary"
        )

    # 최초 호출 1회 + 재시도 2회.
    assert attempts == 3


@pytest.mark.asyncio
async def test_non_rate_limit_error_is_not_retried(monkeypatch):
    monkeypatch.setattr(config, "LLM_RATE_LIMIT_MAX_RETRIES", 2, raising=False)
    client = LLMClient()
    attempts = 0

    async def auth_failure():
        nonlocal attempts
        attempts += 1
        raise PermissionError("Invalid internal request")

    with pytest.raises(PermissionError):
        await client._call_with_rate_limit_retry(
            auth_failure, lane_name="main.primary"
        )

    assert attempts == 1

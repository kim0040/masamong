"""학교 공지 LLM의 재시도·호출 총량은 설정 오류에도 유한해야 합니다."""

from datetime import date

import pytest

from school_notice.llm import DeepSeekClient, DeepSeekSettings, LLMError


class _Repository:
    def __init__(self) -> None:
        self.reservations = 0

    def reserve_api_call(self, **_kwargs):
        self.reservations += 1
        return True


def test_school_notice_llm_clamps_untrusted_limits():
    client = DeepSeekClient(
        _Repository(),
        DeepSeekSettings(
            api_key="test",
            timeout_seconds=999_999,
            max_output_tokens=999_999,
            max_calls_per_run=999_999,
            max_calls_per_day=999_999,
            max_retries=999_999,
        ),
    )

    assert client.settings.timeout_seconds == 120
    assert client.settings.max_output_tokens == 4_000
    assert client.settings.max_calls_per_run == 50
    assert client.settings.max_calls_per_day == 1_000
    assert client.settings.max_retries == 2


def test_school_notice_llm_reservation_has_hard_run_cap():
    repository = _Repository()
    client = DeepSeekClient(
        repository,
        DeepSeekSettings(
            api_key="test",
            max_calls_per_run=2,
            max_calls_per_day=10,
        ),
    )

    client._reserve(date(2026, 7, 29))
    client._reserve(date(2026, 7, 29))
    with pytest.raises(LLMError, match="llm_run_budget_exhausted"):
        client._reserve(date(2026, 7, 29))

    assert client.run_calls == 2
    assert repository.reservations == 2


def test_school_notice_openrouter_payload_locks_openai(monkeypatch):
    monkeypatch.setenv("OPENROUTER_PROVIDER_ONLY", "openai")
    monkeypatch.setenv("SCHOOL_NOTICE_LLM_REASONING_EFFORT", "low")
    client = DeepSeekClient(
        _Repository(),
        DeepSeekSettings(
            api_key="test",
            base_url="https://openrouter.ai/api/v1",
            model="openai/gpt-5.6-luna",
        ),
    )

    payload = client._build_payload(
        system_prompt="system",
        user_prompt="user",
    )

    assert payload["provider"] == {
        "only": ["openai"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }
    assert payload["reasoning"] == {
        "effort": "low",
        "exclude": True,
    }
    assert payload["response_format"] == {"type": "json_object"}
    assert "thinking" not in payload
    assert "temperature" not in payload

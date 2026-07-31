"""OpenRouter 호스트·공급자 고정 요청의 순수 계약 테스트."""

from utils.openrouter import (
    build_openrouter_extra_body,
    build_openrouter_extra_headers,
    is_openrouter_base_url,
    normalize_provider_only,
)


def test_openrouter_host_detection_rejects_lookalikes():
    assert is_openrouter_base_url("https://openrouter.ai/api/v1")
    assert is_openrouter_base_url("https://eu.openrouter.ai/api/v1")
    assert not is_openrouter_base_url("https://openrouter.ai.example/api/v1")
    assert not is_openrouter_base_url("https://api.cometapi.com/v1")


def test_openrouter_provider_and_reasoning_contract_is_bounded():
    body = build_openrouter_extra_body(
        reasoning_effort="high",
        provider_only="openai,openai",
        allow_fallbacks=False,
        require_parameters=True,
        data_collection="deny",
    )

    assert normalize_provider_only(" OpenAI,openai ") == ("openai",)
    assert body == {
        "provider": {
            "only": ["openai"],
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": "deny",
        },
        "reasoning": {
            "effort": "high",
            "exclude": True,
        },
    }


def test_openrouter_unknown_reasoning_does_not_escalate():
    body = build_openrouter_extra_body(
        reasoning_effort="unlimited",
        provider_only="openai",
    )

    assert "reasoning" not in body
    assert body["provider"]["only"] == ["openai"]


def test_openrouter_optional_headers_are_length_bounded():
    headers = build_openrouter_extra_headers(
        app_url="https://example.test/" + ("a" * 1_000),
        app_title="M" * 300,
    )

    assert len(headers["HTTP-Referer"]) == 512
    assert len(headers["X-Title"]) == 128

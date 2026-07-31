"""OpenRouter 요청 경계의 공통 계약.

OpenRouter는 OpenAI 호환 API이지만 공급자 선택과 추론 설정은 추가 JSON
필드로 받습니다. 모든 텍스트 경로가 같은 공급자 제한을 사용하도록 이 모듈에서
요청 본문과 선택 헤더를 구성합니다.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse


_REASONING_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
)


def is_openrouter_base_url(base_url: Any) -> bool:
    """공식 OpenRouter API 호스트인지 확인합니다."""
    try:
        hostname = (urlparse(str(base_url or "")).hostname or "").lower()
    except (TypeError, ValueError):
        return False
    return hostname == "openrouter.ai" or hostname.endswith(".openrouter.ai")


def normalize_provider_only(
    value: Any,
    *,
    default: Iterable[str] = ("openai",),
) -> tuple[str, ...]:
    """쉼표 문자열/반복값을 OpenRouter provider slug 튜플로 정규화합니다."""
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, Iterable):
        raw_items = value
    else:
        raw_items = ()

    providers: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        provider = str(raw or "").strip().lower()
        if not provider or provider in seen:
            continue
        seen.add(provider)
        providers.append(provider)

    if providers:
        return tuple(providers)
    return tuple(
        provider
        for provider in (
            str(item or "").strip().lower()
            for item in default
        )
        if provider
    )


def build_openrouter_extra_body(
    *,
    reasoning_effort: Any = "",
    provider_only: Any = ("openai",),
    allow_fallbacks: bool = False,
    require_parameters: bool = True,
    data_collection: Any = "",
) -> dict[str, Any]:
    """OpenRouter 전용 공급자 고정·추론 본문을 만듭니다."""
    providers = normalize_provider_only(provider_only)
    provider: dict[str, Any] = {
        "only": list(providers),
        "allow_fallbacks": bool(allow_fallbacks),
        "require_parameters": bool(require_parameters),
    }
    normalized_collection = str(data_collection or "").strip().lower()
    if normalized_collection in {"allow", "deny"}:
        provider["data_collection"] = normalized_collection

    body: dict[str, Any] = {"provider": provider}
    effort = str(reasoning_effort or "").strip().lower()
    if effort in _REASONING_EFFORTS:
        body["reasoning"] = {
            "effort": effort,
            # 내부 추론은 답변이나 로그에 노출하지 않습니다.
            "exclude": True,
        }
    return body


def build_openrouter_extra_headers(
    *,
    app_url: Any = "",
    app_title: Any = "",
) -> dict[str, str]:
    """OpenRouter의 선택적 앱 식별 헤더를 안전하게 구성합니다."""
    headers: dict[str, str] = {}
    normalized_url = str(app_url or "").strip()
    normalized_title = str(app_title or "").strip()
    if normalized_url:
        headers["HTTP-Referer"] = normalized_url[:512]
    if normalized_title:
        headers["X-Title"] = normalized_title[:128]
    return headers

# -*- coding: utf-8 -*-
"""
LLM 클라이언트 관리 모듈.

OpenAI-compatible 및 Gemini-compatible LLM 제공자에 대한 레인 기반
(primary/fallback) 라우팅, 클라이언트 캐싱, Rate Limit, 디버그 로깅,
프롬프트 누출 방지 필터링을 제공합니다.

이 모듈은 Discord 의존성이 없으며, 순수 LLM 호출 레이어로 사용됩니다.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import config

# legacy direct-Gemini 경로를 운영자가 명시적으로 켠 경우에만 지원 종료된
# SDK를 지연 import한다. 정상 CometAPI 레인은 아래 신규 google-genai 또는
# OpenAI 호환 client만 사용한다.
if config.GEMINI_API_KEY and config.ALLOW_DIRECT_GEMINI_FALLBACK:
    try:
        import google.generativeai as genai
    except ModuleNotFoundError:
        genai = None
else:
    genai = None

try:
    from google import genai as google_genai
except ImportError:
    google_genai = None

try:
    from openai import AsyncOpenAI, APITimeoutError
except ImportError:
    AsyncOpenAI = None
    APITimeoutError = None

from logger_config import logger
from utils import db as db_utils


_ProviderResult = TypeVar("_ProviderResult")


class LLMAdmissionTimeoutError(TimeoutError):
    """Provider 호출 슬롯을 제한 시간 안에 확보하지 못한 경우."""


class LLMProviderTimeoutError(TimeoutError):
    """실제 provider 요청이 제한 시간을 넘긴 경우."""


class LLMClient:
    """OpenAI-compatible 및 Gemini-compatible LLM 레인 라우팅 클라이언트.

    Config 설정에 따라 라우팅 레인(의도 분석용)과 메인 레인(답변 생성용)의
    primary/fallback 타깃을 관리하고, 클라이언트 캐싱, Rate Limit, 프롬프트 누출
    필터링을 투명하게 처리합니다.
    """

    _DYNAMIC_REASONING_MODEL_MARKERS = (
        "deepseek-v4",
        "kimi-k3",
        "grok-4.5",
        "gpt-5",
        "gpt-oss",
        "o1",
        "o3",
        "o4",
    )

    def __init__(self, db=None):
        self._db = db
        self._openai_clients: dict[tuple[str, str], Any] = {}
        self._gemini_compat_clients: dict[tuple[str, str], Any] = {}
        self.debug_enabled = config.AI_DEBUG_ENABLED
        self._debug_log_len = getattr(config, "AI_DEBUG_LOG_MAX_LEN", 400)
        self._max_concurrent_calls = max(
            1,
            int(getattr(config, "LLM_MAX_CONCURRENT_CALLS", 1)),
        )
        self._acquire_timeout_seconds = max(
            0.001,
            float(getattr(config, "LLM_ACQUIRE_TIMEOUT_SECONDS", 10)),
        )
        self._call_timeout_seconds = max(
            0.001,
            float(
                getattr(
                    config,
                    "LLM_CALL_TIMEOUT_SECONDS",
                    getattr(config, "AI_REQUEST_TIMEOUT", 120),
                )
            ),
        )
        # 논리 요청(generate_content 등)이 아니라 실제 provider target 호출 경계에
        # 하나의 세마포어를 둔다. 따라서 primary→fallback이나
        # get_ai_completion→safe_generate_content가 중첩 획득해 교착되지 않는다.
        self._provider_call_semaphore = asyncio.BoundedSemaphore(
            self._max_concurrent_calls
        )
        self.gemini_configured = False
        if config.GEMINI_API_KEY and genai:
            try:
                genai.configure(api_key=config.GEMINI_API_KEY)
                self.gemini_configured = True
            except Exception as e:
                logger.warning("Gemini API 설정 실패: %s", e)

        routing_targets = self.get_lane_targets("routing")
        main_targets = self.get_lane_targets("main")
        self.use_cometapi = bool(routing_targets or main_targets)

    async def _reserve_request_budget(
        self,
        log_extra: dict | None,
        *,
        feature: str,
    ) -> bool:
        """논리 LLM 요청 하나를 전역·서버·사용자·기능 한도에 예약합니다."""
        if self._db is None:
            return True
        extra = log_extra or {}
        allowed, reason = await db_utils.reserve_llm_api_call(
            self._db,
            guild_id=extra.get("guild_id"),
            # 기존 Cog 일부는 같은 Discord 식별자를 author_id로 전달한다.
            # 두 이름을 여기서 한 번 정규화해 모든 LLM 경로에 사용자 한도를
            # 빠짐없이 적용한다.
            user_id=extra.get("user_id", extra.get("author_id")),
            feature=feature,
        )
        if not allowed:
            logger.warning(
                "LLM 호출 차단 - %s",
                reason or "계층형 사용량 한도",
                extra=extra,
            )
        return bool(allowed)

    async def _run_bounded_provider_call(
        self,
        call_factory: Callable[[], Awaitable[_ProviderResult]],
        *,
        lane_name: str,
        log_extra: dict[str, Any] | None,
    ) -> _ProviderResult:
        """물리 LLM 호출 하나에 동시성·대기·실행 시간 상한을 적용합니다.

        ``call_factory``는 슬롯을 얻은 뒤에만 coroutine을 만들도록 callable로
        받습니다. 획득 timeout 시 생성되지 않은 coroutine이 남는 문제를 피하고,
        모든 진입점이 동일한 실제 provider-call 경계를 공유하게 합니다.
        """
        acquired = False
        try:
            try:
                await asyncio.wait_for(
                    self._provider_call_semaphore.acquire(),
                    timeout=self._acquire_timeout_seconds,
                )
                acquired = True
            except asyncio.TimeoutError as exc:
                raise LLMAdmissionTimeoutError(
                    f"{lane_name} LLM 호출 슬롯을 "
                    f"{self._acquire_timeout_seconds:g}초 안에 확보하지 못했습니다."
                ) from exc

            try:
                async def _record_and_call():
                    # 기존 성공 기반 cometapi/gemini rate-limit 키와 분리된
                    # 물리 시도 계측이다. 요청 직전에 기록하므로 provider
                    # 오류/빈 응답/timeout도 누락되지 않으며 기존 제한 의미는
                    # 바꾸지 않는다. 계측 DB까지 같은 timeout 안에 두어 DB
                    # 정체가 provider 슬롯을 무한 점유하지 않게 한다.
                    if self._db:
                        await db_utils.log_api_call(self._db, "llm_attempt")
                    return await call_factory()

                return await asyncio.wait_for(
                    _record_and_call(),
                    timeout=self._call_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                is_provider_timeout = (
                    isinstance(exc, asyncio.TimeoutError)
                    or (APITimeoutError is not None and isinstance(exc, APITimeoutError))
                    or "timeout" in type(exc).__name__.lower()
                )
                if is_provider_timeout:
                    raise LLMProviderTimeoutError(
                        f"{lane_name} LLM provider 호출이 "
                        f"{self._call_timeout_seconds:g}초 제한을 넘겼습니다."
                    ) from exc
                raise
        finally:
            if acquired:
                self._provider_call_semaphore.release()

    @property
    def db(self):
        return self._db

    @db.setter
    def db(self, value):
        self._db = value

    def can_use_direct_gemini(self) -> bool:
        """CometAPI 실패 시 직접 Gemini 호출 허용 여부."""
        return bool(config.ALLOW_DIRECT_GEMINI_FALLBACK and self.gemini_configured and genai)

    @staticmethod
    def normalize_provider(provider: Any) -> str:
        """LLM 프로바이더 식별자를 소문자 문자열로 정규화합니다."""
        return str(provider or "").strip().lower()

    @classmethod
    def supports_dynamic_reasoning(cls, model: Any) -> bool:
        """검증된 요청 단위 reasoning_effort 모델 계열인지 확인합니다."""
        model_name = str(model or "").strip().lower()
        return bool(model_name) and any(
            marker in model_name
            for marker in cls._DYNAMIC_REASONING_MODEL_MARKERS
        )

    @classmethod
    def resolve_reasoning_effort(
        cls,
        target: dict[str, str],
        override: str | None,
    ) -> str:
        """공유 target을 바꾸지 않고 이번 요청에 사용할 effort를 결정합니다."""
        configured = str(target.get("reasoning_effort") or "").strip().lower()
        if configured not in {"low", "medium", "high", "max"}:
            configured = ""
        if override is None:
            return configured
        if not cls.supports_dynamic_reasoning(target.get("model")):
            return configured
        normalized_override = str(override or "").strip().lower()
        # 라우터 계약 밖의 값은 고비용 수준으로 확대하지 않고 low로 내린다.
        return (
            normalized_override
            if normalized_override in {"low", "high"}
            else "low"
        )

    @staticmethod
    def strip_mention_guard(text: Any) -> str:
        """프롬프트 텍스트에서 멘션 가드 스니펫을 제거합니다."""
        rendered = str(text or "")
        return rendered.replace(config.MENTION_GUARD_SNIPPET, "").strip()

    def get_lane_targets(self, lane: str, *, model_override: str | None = None) -> list[dict[str, str]]:
        """레인별(primary/fallback) LLM 타깃 목록을 반환합니다."""
        lane_key = str(lane or "").strip().lower()
        if lane_key == "routing":
            candidates = [
                {
                    "provider": config.LLM_ROUTING_PRIMARY_PROVIDER,
                    "base_url": config.LLM_ROUTING_PRIMARY_BASE_URL,
                    "api_key": config.LLM_ROUTING_PRIMARY_API_KEY,
                    "model": config.LLM_ROUTING_PRIMARY_MODEL,
                    "reasoning_effort": config.LLM_ROUTING_PRIMARY_REASONING_EFFORT,
                    "name": "routing.primary",
                },
                {
                    "provider": config.LLM_ROUTING_FALLBACK_PROVIDER,
                    "base_url": config.LLM_ROUTING_FALLBACK_BASE_URL,
                    "api_key": config.LLM_ROUTING_FALLBACK_API_KEY,
                    "model": config.LLM_ROUTING_FALLBACK_MODEL,
                    "reasoning_effort": config.LLM_ROUTING_FALLBACK_REASONING_EFFORT,
                    "name": "routing.fallback",
                },
            ]
        else:
            candidates = [
                {
                    "provider": config.LLM_MAIN_PRIMARY_PROVIDER,
                    "base_url": config.LLM_MAIN_PRIMARY_BASE_URL,
                    "api_key": config.LLM_MAIN_PRIMARY_API_KEY,
                    "model": config.LLM_MAIN_PRIMARY_MODEL,
                    "reasoning_effort": config.LLM_MAIN_PRIMARY_REASONING_EFFORT,
                    "name": "main.primary",
                },
                {
                    "provider": config.LLM_MAIN_FALLBACK_PROVIDER,
                    "base_url": config.LLM_MAIN_FALLBACK_BASE_URL,
                    "api_key": config.LLM_MAIN_FALLBACK_API_KEY,
                    "model": config.LLM_MAIN_FALLBACK_MODEL,
                    "reasoning_effort": config.LLM_MAIN_FALLBACK_REASONING_EFFORT,
                    "name": "main.fallback",
                },
            ]

        targets: list[dict[str, str]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for raw in candidates:
            provider = self.normalize_provider(raw.get("provider"))
            if provider in {"", "none", "off", "disabled"}:
                continue
            base_url = str(raw.get("base_url") or "").strip().rstrip("/")
            api_key = str(raw.get("api_key") or "").strip()
            model_name = str(model_override or raw.get("model") or "").strip()
            if not model_name:
                continue
            if provider == "openai_compat":
                if not AsyncOpenAI or not base_url or not api_key:
                    continue
            elif provider == "gemini_compat":
                if not google_genai or not base_url or not api_key:
                    continue
            else:
                continue

            sig = (provider, base_url, model_name, api_key[:8])
            if sig in seen:
                continue
            seen.add(sig)
            targets.append(
                {
                    "provider": provider,
                    "base_url": base_url,
                    "api_key": api_key,
                    "model": model_name,
                    "reasoning_effort": str(raw.get("reasoning_effort") or "").strip(),
                    "name": str(raw.get("name") or lane_key),
                }
            )
        return targets

    def get_openai_client(self, base_url: str, api_key: str) -> Any | None:
        """캐시된 OpenAI 호환 클라이언트를 반환하거나 새로 생성합니다."""
        if not AsyncOpenAI:
            return None
        cache_key = (base_url.rstrip("/"), api_key)
        client = self._openai_clients.get(cache_key)
        if client is None:
            # SDK 기본 재시도(현재 기본 2회)와 애플리케이션의 명시적
            # primary/fallback이 겹쳐 물리 요청이 증폭되지 않도록 끈다.
            client = AsyncOpenAI(
                base_url=cache_key[0],
                api_key=cache_key[1],
                max_retries=0,
                timeout=self._call_timeout_seconds,
            )
            self._openai_clients[cache_key] = client
        return client

    def get_gemini_compat_client(self, base_url: str, api_key: str) -> Any | None:
        """캐시된 Gemini 호환 클라이언트를 반환하거나 새로 생성합니다."""
        if not google_genai:
            return None
        cache_key = (base_url.rstrip("/"), api_key)
        client = self._gemini_compat_clients.get(cache_key)
        if client is None:
            client = google_genai.Client(
                http_options={
                    "api_version": "v1beta",
                    "base_url": cache_key[0],
                    # google-genai timeout 단위는 밀리초다.
                    "timeout": max(1, int(self._call_timeout_seconds * 1000)),
                    # SDK 기본 재시도를 사용하지 않고 1회 시도로 고정해
                    # 애플리케이션 fallback과 물리 요청이 중첩되지 않게 한다.
                    "retry_options": {"attempts": 1},
                },
                api_key=cache_key[1],
            )
            self._gemini_compat_clients[cache_key] = client
        return client

    async def call_main_lane_target(
        self,
        target: dict[str, str],
        *,
        system_prompt: str,
        user_prompt: str,
        log_extra: dict,
        max_tokens: int,
        reasoning_effort_override: str | None = None,
    ) -> str | None:
        """시스템/사용자 프롬프트로 단일 메인 레인 LLM 타겟을 호출합니다."""
        provider = target["provider"]
        if provider == "openai_compat":
            client = self.get_openai_client(target["base_url"], target["api_key"])
            if client is None:
                return None
            request_kwargs: dict[str, Any] = {
                "model": target["model"],
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": max_tokens,
                "temperature": config.AI_TEMPERATURE,
                "frequency_penalty": config.AI_FREQUENCY_PENALTY,
                "presence_penalty": config.AI_PRESENCE_PENALTY,
                "timeout": self._call_timeout_seconds,
                "stream": False,
            }
            reasoning_effort = self.resolve_reasoning_effort(
                target,
                reasoning_effort_override,
            )
            if reasoning_effort:
                request_kwargs["reasoning_effort"] = reasoning_effort
                if "deepseek-v4" in str(target["model"]).strip().lower():
                    request_kwargs["extra_body"] = {
                        "thinking": {"type": "enabled"}
                    }

            async def _request_openai_main():
                return await client.chat.completions.create(**request_kwargs)

            completion = await self._run_bounded_provider_call(
                _request_openai_main,
                lane_name=str(target.get("name") or "main"),
                log_extra=log_extra,
            )
            response_text = completion.choices[0].message.content
            reasoning_text = getattr(completion.choices[0].message, "reasoning_content", None)
            if not response_text and reasoning_text:
                logger.warning(
                    "[MainLLM:%s] 응답 content 없이 reasoning_content만 반환되어 폐기합니다.",
                    target.get("name"),
                    extra=log_extra,
                )
                return None
            return response_text.strip() if response_text else None

        if provider == "gemini_compat":
            client = self.get_gemini_compat_client(target["base_url"], target["api_key"])
            if client is None:
                return None
            merged_prompt = f"[System]\n{system_prompt}\n\n[User]\n{user_prompt}"
            async_client = getattr(client, "aio", None)
            if async_client is None:
                raise RuntimeError(
                    "google-genai 비동기 클라이언트를 사용할 수 없습니다."
                )

            async def _request_gemini_main():
                return await async_client.models.generate_content(
                    model=target["model"],
                    contents=merged_prompt,
                    config={
                        "temperature": config.AI_TEMPERATURE,
                        "max_output_tokens": max_tokens,
                    },
                )

            response = await self._run_bounded_provider_call(
                _request_gemini_main,
                lane_name=str(target.get("name") or "main"),
                log_extra=log_extra,
            )
            return (getattr(response, "text", "") or "").strip() or None

        return None

    async def call_routing_lane_target(
        self,
        target: dict[str, str],
        *,
        prompt: str,
        log_extra: dict,
        max_tokens: int | None = None,
    ) -> str | None:
        """단일 라우팅 레인 LLM 타겟을 호출하여 프롬프트 응답을 반환합니다."""
        provider = target["provider"]
        effective_max_tokens = max(
            64,
            int(
                max_tokens
                if max_tokens is not None
                else getattr(config, "ROUTING_LLM_MAX_TOKENS", 1024)
            ),
        )
        if provider == "openai_compat":
            client = self.get_openai_client(target["base_url"], target["api_key"])
            if client is None:
                return None
            request_kwargs: dict[str, Any] = {
                "model": target["model"],
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": effective_max_tokens,
                "temperature": 0.0,
                "timeout": self._call_timeout_seconds,
                "stream": False,
            }
            reasoning_effort = str(target.get("reasoning_effort") or "").strip()
            if reasoning_effort:
                request_kwargs["reasoning_effort"] = reasoning_effort

            async def _request_openai_routing():
                return await client.chat.completions.create(**request_kwargs)

            completion = await self._run_bounded_provider_call(
                _request_openai_routing,
                lane_name=str(target.get("name") or "routing"),
                log_extra=log_extra,
            )
            response_text = completion.choices[0].message.content
            return response_text.strip() if response_text else None

        if provider == "gemini_compat":
            client = self.get_gemini_compat_client(target["base_url"], target["api_key"])
            if client is None:
                return None
            async_client = getattr(client, "aio", None)
            if async_client is None:
                raise RuntimeError(
                    "google-genai 비동기 클라이언트를 사용할 수 없습니다."
                )

            async def _request_gemini_routing():
                return await async_client.models.generate_content(
                    model=target["model"],
                    contents=prompt,
                    config={
                        "temperature": 0.0,
                        "max_output_tokens": effective_max_tokens,
                    },
                )

            response = await self._run_bounded_provider_call(
                _request_gemini_routing,
                lane_name=str(target.get("name") or "routing"),
                log_extra=log_extra,
            )
            return (getattr(response, "text", "") or "").strip() or None

        return None

    def debug(self, message: str, log_extra: dict[str, Any] | None = None) -> None:
        """디버그 설정이 켜진 경우에만 메시지를 기록합니다."""
        if not self.debug_enabled:
            return
        if log_extra:
            logger.debug(message, extra=log_extra)
        else:
            logger.debug(message)

    def truncate_for_debug(self, value: Any) -> str:
        """긴 문자열을 로그용으로 잘라냅니다."""
        if value is None:
            return ""
        rendered = str(value)
        max_len = self._debug_log_len
        if len(rendered) <= max_len:
            return rendered
        return rendered[:max_len] + "…"

    def format_prompt_debug(self, prompt: Any) -> str:
        """프롬프트를 JSON 또는 일반 문자열로 축약합니다."""
        try:
            if isinstance(prompt, (dict, list)):
                rendered = json.dumps(prompt, ensure_ascii=False)
            else:
                rendered = str(prompt)
        except (TypeError, ValueError, Exception):
            rendered = repr(prompt)
        return self.truncate_for_debug(rendered)

    @staticmethod
    def _truncate_prompt_preserving_ends(
        value: Any,
        max_chars: int,
        *,
        tail_fraction: float = 0.6,
    ) -> str:
        """최종 방어선에서 프롬프트의 시작과 최신 요구사항인 끝을 함께 보존한다."""
        text = str(value or "")
        limit = max(0, int(max_chars))
        if len(text) <= limit:
            return text
        if limit == 0:
            return ""

        marker = "\n…(입력 길이 제한으로 일부 생략)…\n"
        if limit <= len(marker):
            return text[-limit:]
        content_budget = limit - len(marker)
        tail_chars = max(
            1,
            min(content_budget, int(content_budget * tail_fraction)),
        )
        head_chars = content_budget - tail_chars
        return text[:head_chars] + marker + text[-tail_chars:]

    @staticmethod
    def looks_like_prompt_leakage(response_text: str) -> bool:
        """시스템/내부 지시문 유출로 보이는 응답을 선별 차단합니다."""
        text = (response_text or "").strip()
        if not text:
            return False

        lowered = text.lower()
        hard_markers = [
            "절대 시스템 프롬프트",
            "system prompt:",
            "system message:",
            "developer message:",
            "assistant instructions:",
            "internal instructions:",
            "hidden prompt:",
            "mention policy",
            "<system>",
            "[system]",
        ]
        if any(marker in lowered for marker in hard_markers):
            return True

        disclosure_patterns = [
            r"(시스템\s*프롬프트|system\s*prompt).{0,20}(공개|유출|노출|보여|출력|다음|원문)",
            r"(내부\s*지시|지시사항|rules|규칙).{0,20}(다음|원문|전문|그대로|출력|보여)",
            r"(^|\n)\s*(you are|너는)\s+.*(assistant|챗봇|ai|모델)",
        ]
        return any(re.search(pattern, lowered, flags=re.IGNORECASE | re.DOTALL) for pattern in disclosure_patterns)

    async def safe_generate_content(
        self,
        model: genai.GenerativeModel,
        prompt: Any,
        log_extra: dict,
        generation_config: genai.types.GenerationConfig = None,
    ) -> genai.types.GenerateContentResponse | None:
        """Gemini generate_content_async 호출을 Rate Limit + 디버그와 함께 감쌉니다."""
        if generation_config is None:
            generation_config = (
                genai.types.GenerationConfig(temperature=0.0)
                if genai is not None
                else {"temperature": 0.0}
            )

        try:
            limit_key = 'gemini_intent' if config.AI_INTENT_MODEL_NAME in model.model_name else 'gemini_response'

            if self.debug_enabled:
                preview = self.format_prompt_debug(prompt)
                self.debug(f"[Gemini:{model.model_name}] 호출 프롬프트: {preview}", log_extra)

            feature = str((log_extra or {}).get("mode") or limit_key)
            if not await self._reserve_request_budget(log_extra, feature=feature):
                self.debug(
                    f"[Gemini:{model.model_name}] 호출 차단 - 계층형 한도 ({feature})",
                    log_extra,
                )
                return None

            async def _request_direct_gemini():
                return await model.generate_content_async(
                    prompt,
                    generation_config=generation_config,
                    safety_settings=config.GEMINI_SAFETY_SETTINGS,
                    # deprecated google.generativeai의 GAPIC 기본 재시도를
                    # 비활성화하고 transport timeout도 물리 호출과 맞춘다.
                    request_options={
                        "retry": None,
                        "timeout": self._call_timeout_seconds,
                    },
                )

            response = await self._run_bounded_provider_call(
                _request_direct_gemini,
                lane_name=limit_key,
                log_extra=log_extra,
            )
            if self.debug_enabled and response is not None:
                text = getattr(response, "text", None)
                self.debug(
                    f"[Gemini:{model.model_name}] 응답 요약: {self.truncate_for_debug(text)}",
                    log_extra,
                )
            return response
        except Exception as e:
            logger.error(f"Gemini 응답 생성 중 예기치 않은 오류: {e}", extra=log_extra, exc_info=True)
            return None

    async def generate_content(
        self,
        system_prompt: str,
        user_prompt: str,
        log_extra: dict,
        model: str | None = None,
        *,
        raise_on_bounded_failure: bool = False,
        reasoning_effort_override: str | None = None,
    ) -> str | None:
        """메인 레인(primary/fallback)을 통해 응답을 생성합니다.

        CometAPI Rate Limit 확인 → 프롬프트 길이 제한 → Primary/Fallback
        순차 호출 → 프롬프트 누출 필터 → 응답 반환.

        Args:
            system_prompt: 시스템 프롬프트
            user_prompt: 사용자 프롬프트
            log_extra: 로깅용 추가 정보
            model: 사용할 모델명 (None이면 기본값 사용)
            raise_on_bounded_failure: timeout/포화 상태를 상위 호출자에 전달해
                별도 provider fallback 증폭을 차단할지 여부
            reasoning_effort_override: 현재 요청에만 적용할 low/high 추론 수준.
                None이면 target의 고정 설정을 사용합니다.

        Returns:
            생성된 응답 텍스트, 실패 시 None
        """
        targets = self.get_lane_targets("main", model_override=model)
        if not targets:
            logger.warning("메인 레인 LLM 타깃이 설정되지 않았습니다.", extra=log_extra)
            return None

        try:
            feature = str((log_extra or {}).get("mode") or "main_response")
            if not await self._reserve_request_budget(log_extra, feature=feature):
                return None

            system_prompt = self._truncate_prompt_preserving_ends(
                system_prompt,
                int(getattr(config, "COMETAPI_SYSTEM_PROMPT_MAX_CHARS", 6000)),
                tail_fraction=0.4,
            )
            # 메인 프롬프트는 [현재 질문]을 끝쪽에 배치한다. 혹시 다른
            # 호출자가 초과 입력을 넘겨도 선두 절단으로 최신 질문을 잃지 않는다.
            user_prompt = self._truncate_prompt_preserving_ends(
                user_prompt,
                int(getattr(config, "COMETAPI_USER_PROMPT_MAX_CHARS", 12000)),
                tail_fraction=0.7,
            )

            if self.debug_enabled:
                self.debug(f"[CometAPI] system={self.truncate_for_debug(system_prompt)}", log_extra)
                self.debug(f"[CometAPI] user={self.truncate_for_debug(user_prompt)}", log_extra)

            final_response = None
            for target in targets:
                try:
                    call_kwargs: dict[str, Any] = {
                        "system_prompt": system_prompt,
                        "user_prompt": user_prompt,
                        "log_extra": log_extra,
                        "max_tokens": int(
                            getattr(
                                config,
                                "MAIN_LLM_MAX_TOKENS",
                                config.COMETAPI_MAX_TOKENS,
                            )
                        ),
                    }
                    if reasoning_effort_override is not None:
                        call_kwargs["reasoning_effort_override"] = (
                            reasoning_effort_override
                        )
                    final_response = await self.call_main_lane_target(
                        target,
                        **call_kwargs,
                    )
                except (LLMAdmissionTimeoutError, LLMProviderTimeoutError) as lane_exc:
                    # 포화 상태에서 fallback까지 대기열에 추가하거나, 완료 여부가
                    # 불명확한 timeout 요청과 fallback을 중첩 실행하지 않는다.
                    logger.warning(
                        "[MainLLM:%s] bounded 호출 중단: %s",
                        target.get("name"),
                        lane_exc,
                        extra=log_extra,
                    )
                    final_response = None
                    if raise_on_bounded_failure:
                        raise
                    break
                except Exception as lane_exc:
                    logger.warning("[MainLLM:%s] 호출 실패: %s", target.get("name"), lane_exc, extra=log_extra)
                    final_response = None
                if final_response:
                    break

            if final_response and self.looks_like_prompt_leakage(final_response):
                logger.warning(
                    "[Security] 프롬프트 유출 감지 및 차단. response_chars=%d",
                    len(final_response),
                    extra=log_extra,
                )
                return None

            if self.debug_enabled:
                self.debug(f"[CometAPI] 응답: {self.truncate_for_debug(final_response)}", log_extra)

            return final_response.strip() if final_response else None

        except (LLMAdmissionTimeoutError, LLMProviderTimeoutError):
            if raise_on_bounded_failure:
                raise
            return None
        except Exception as e:
            if APITimeoutError and isinstance(e, APITimeoutError):
                logger.error(f"CometAPI 요청 시간 초과 ({config.AI_REQUEST_TIMEOUT}s)", extra=log_extra)
                return None
            logger.error(f"CometAPI 응답 생성 중 오류: {e}", extra=log_extra, exc_info=True)
            return None

    async def fast_generate_text(
        self,
        prompt: str,
        model: str | None,
        log_extra: dict,
        *,
        trace_key: str = "cometapi_fast",
        max_tokens: int | None = None,
    ) -> str | None:
        """라우팅 레인 Fast 모델을 통해 텍스트를 생성합니다.

        Rate Limit 확인 → Primary/Fallback 순차 호출.

        Args:
            prompt: LLM에 전달할 프롬프트
            model: 모델명 (None이면 기본값)
            log_extra: 로깅용 추가 정보
            trace_key: API 호출 로그 키

        Returns:
            생성된 응답 텍스트, 실패 시 None
        """
        targets = self.get_lane_targets("routing", model_override=model)
        if not targets:
            return None

        try:
            feature = str((log_extra or {}).get("mode") or trace_key)
            if not await self._reserve_request_budget(log_extra, feature=feature):
                return None

            response_text = None
            for target in targets:
                try:
                    response_text = await self.call_routing_lane_target(
                        target,
                        prompt=prompt,
                        log_extra=log_extra,
                        max_tokens=max_tokens,
                    )
                except (LLMAdmissionTimeoutError, LLMProviderTimeoutError) as lane_exc:
                    logger.warning(
                        "[RoutingLLM:%s] bounded 호출 중단: %s",
                        target.get("name"),
                        lane_exc,
                        extra=log_extra,
                    )
                    response_text = None
                    break
                except Exception as lane_exc:
                    logger.warning("[RoutingLLM:%s] 호출 실패: %s", target.get("name"), lane_exc, extra=log_extra)
                    response_text = None
                if response_text:
                    break

            return response_text.strip() if response_text else None
        except Exception as e:
            logger.warning(f"[CometAPI-Fast] 호출 실패: {e}", extra=log_extra)
            return None

    async def get_ai_completion(
        self,
        prompt: str,
        system_role: str = "도움이 되는 친절한 보조원",
        model: str | None = None,
    ) -> str | None:
        """외부 Cog에서 일반적인 AI 응답을 얻기 위한 공개 메서드.

        CometAPI → Gemini fallback 순으로 시도합니다.
        """
        import uuid
        log_extra = {'trace_id': f"gen_comp_{uuid.uuid4().hex[:4]}"}
        if self.use_cometapi:
            try:
                res = await self.generate_content(
                    system_role,
                    prompt,
                    log_extra,
                    model=model,
                    raise_on_bounded_failure=True,
                )
            except (LLMAdmissionTimeoutError, LLMProviderTimeoutError) as exc:
                # 슬롯 포화/timeout 뒤 직접 Gemini까지 연속 호출하면 동일한
                # 논리 요청이 추가 provider 호출로 증폭될 수 있다.
                logger.warning(
                    "get_ai_completion bounded 호출 중단: %s",
                    exc,
                    extra=log_extra,
                )
                return None
            if res:
                return res

        if self.can_use_direct_gemini():
            try:
                model_name = model or config.AI_RESPONSE_MODEL_NAME
                gen_model = genai.GenerativeModel(model_name, system_instruction=system_role)
                response = await self.safe_generate_content(gen_model, prompt, log_extra)
                if response and hasattr(response, 'text'):
                    return response.text.strip()
            except Exception as e:
                logger.error(f"get_ai_completion (Gemini Fallback) 오류: {e}", extra=log_extra)

        logger.warning("get_ai_completion: 사용할 LLM 제공자가 없거나 호출에 실패했습니다.", extra=log_extra)
        return None

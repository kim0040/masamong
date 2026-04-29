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
from typing import Any

try:
    import google.generativeai as genai
except ModuleNotFoundError:
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

import config
from logger_config import logger
from utils import db as db_utils


class LLMClient:
    """OpenAI-compatible 및 Gemini-compatible LLM 레인 라우팅 클라이언트.

    Config 설정에 따라 라우팅 레인(의도 분석용)과 메인 레인(답변 생성용)의
    primary/fallback 타깃을 관리하고, 클라이언트 캐싱, Rate Limit, 프롬프트 누출
    필터링을 투명하게 처리합니다.
    """

    def __init__(self, db=None):
        self._db = db
        self._openai_clients: dict[tuple[str, str], Any] = {}
        self._gemini_compat_clients: dict[tuple[str, str], Any] = {}
        self.debug_enabled = config.AI_DEBUG_ENABLED
        self._debug_log_len = getattr(config, "AI_DEBUG_LOG_MAX_LEN", 400)

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
            client = AsyncOpenAI(base_url=cache_key[0], api_key=cache_key[1])
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
                http_options={"api_version": "v1beta", "base_url": cache_key[0]},
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
                "timeout": config.AI_REQUEST_TIMEOUT,
                "stream": False,
            }
            reasoning_effort = str(target.get("reasoning_effort") or "").strip()
            if reasoning_effort:
                request_kwargs["reasoning_effort"] = reasoning_effort
            completion = await client.chat.completions.create(**request_kwargs)
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
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=target["model"],
                contents=merged_prompt,
            )
            return (getattr(response, "text", "") or "").strip() or None

        return None

    async def call_routing_lane_target(
        self,
        target: dict[str, str],
        *,
        prompt: str,
        log_extra: dict,
    ) -> str | None:
        """단일 라우팅 레인 LLM 타겟을 호출하여 프롬프트 응답을 반환합니다."""
        provider = target["provider"]
        if provider == "openai_compat":
            client = self.get_openai_client(target["base_url"], target["api_key"])
            if client is None:
                return None
            request_kwargs: dict[str, Any] = {
                "model": target["model"],
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": int(getattr(config, "ROUTING_LLM_MAX_TOKENS", 1024)),
                "temperature": 0.0,
                "timeout": config.AI_REQUEST_TIMEOUT,
                "stream": False,
            }
            reasoning_effort = str(target.get("reasoning_effort") or "").strip()
            if reasoning_effort:
                request_kwargs["reasoning_effort"] = reasoning_effort
            completion = await client.chat.completions.create(**request_kwargs)
            response_text = completion.choices[0].message.content
            return response_text.strip() if response_text else None

        if provider == "gemini_compat":
            client = self.get_gemini_compat_client(target["base_url"], target["api_key"])
            if client is None:
                return None
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=target["model"],
                contents=prompt,
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
            generation_config = genai.types.GenerationConfig(temperature=0.0)

        try:
            limit_key = 'gemini_intent' if config.AI_INTENT_MODEL_NAME in model.model_name else 'gemini_response'
            rpm = config.RPM_LIMIT_INTENT if limit_key == 'gemini_intent' else config.RPM_LIMIT_RESPONSE
            rpd = config.RPD_LIMIT_INTENT if limit_key == 'gemini_intent' else config.RPD_LIMIT_RESPONSE

            if self.debug_enabled:
                preview = self.format_prompt_debug(prompt)
                self.debug(f"[Gemini:{model.model_name}] 호출 프롬프트: {preview}", log_extra)

            if self._db and await db_utils.check_api_rate_limit(self._db, limit_key, rpm, rpd):
                self.debug(f"[Gemini:{model.model_name}] 호출 차단 - rate limit 도달 ({limit_key})", log_extra)
                logger.warning(f"Gemini API 호출 제한({limit_key})에 도달했습니다.", extra=log_extra)
                return None

            response = await model.generate_content_async(
                prompt,
                generation_config=generation_config,
                safety_settings=config.GEMINI_SAFETY_SETTINGS,
            )
            if self._db:
                await db_utils.log_api_call(self._db, limit_key)
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
    ) -> str | None:
        """메인 레인(primary/fallback)을 통해 응답을 생성합니다.

        CometAPI Rate Limit 확인 → 프롬프트 길이 제한 → Primary/Fallback
        순차 호출 → 프롬프트 누출 필터 → 응답 반환.

        Args:
            system_prompt: 시스템 프롬프트
            user_prompt: 사용자 프롬프트
            log_extra: 로깅용 추가 정보
            model: 사용할 모델명 (None이면 기본값 사용)

        Returns:
            생성된 응답 텍스트, 실패 시 None
        """
        targets = self.get_lane_targets("main", model_override=model)
        if not targets:
            logger.warning("메인 레인 LLM 타깃이 설정되지 않았습니다.", extra=log_extra)
            return None

        try:
            if self._db and await db_utils.check_api_rate_limit(
                self._db,
                "cometapi",
                config.COMETAPI_RPM_LIMIT,
                config.COMETAPI_RPD_LIMIT,
            ):
                logger.warning("[CometAPI] 호출 차단 - rate limit 도달", extra=log_extra)
                return None

            system_prompt = (system_prompt or "")[: int(getattr(config, "COMETAPI_SYSTEM_PROMPT_MAX_CHARS", 6000))]
            user_prompt = (user_prompt or "")[: int(getattr(config, "COMETAPI_USER_PROMPT_MAX_CHARS", 12000))]

            if self.debug_enabled:
                self.debug(f"[CometAPI] system={self.truncate_for_debug(system_prompt)}", log_extra)
                self.debug(f"[CometAPI] user={self.truncate_for_debug(user_prompt)}", log_extra)

            final_response = None
            for target in targets:
                try:
                    final_response = await self.call_main_lane_target(
                        target,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        log_extra=log_extra,
                        max_tokens=int(getattr(config, "MAIN_LLM_MAX_TOKENS", config.COMETAPI_MAX_TOKENS)),
                    )
                except Exception as lane_exc:
                    logger.warning("[MainLLM:%s] 호출 실패: %s", target.get("name"), lane_exc, extra=log_extra)
                    final_response = None
                if final_response:
                    if self._db:
                        await db_utils.log_api_call(self._db, "cometapi")
                    break

            if final_response and self.looks_like_prompt_leakage(final_response):
                logger.warning(f"[Security] 프롬프트 유출 감지 및 차단: {final_response[:100]}...", extra=log_extra)
                return None

            if self.debug_enabled:
                self.debug(f"[CometAPI] 응답: {self.truncate_for_debug(final_response)}", log_extra)

            return final_response.strip() if final_response else None

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
            if self._db and await db_utils.check_api_rate_limit(
                self._db, "cometapi", config.COMETAPI_RPM_LIMIT, config.COMETAPI_RPD_LIMIT,
            ):
                logger.warning("[CometAPI-Fast] 호출 차단 - rate limit 도달", extra=log_extra)
                return None

            response_text = None
            for target in targets:
                try:
                    response_text = await self.call_routing_lane_target(
                        target, prompt=prompt, log_extra=log_extra,
                    )
                except Exception as lane_exc:
                    logger.warning("[RoutingLLM:%s] 호출 실패: %s", target.get("name"), lane_exc, extra=log_extra)
                    response_text = None
                if response_text:
                    break

            if self._db:
                await db_utils.log_api_call(self._db, "cometapi")
                await db_utils.log_api_call(self._db, trace_key)

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
            res = await self.generate_content(system_role, prompt, log_extra, model=model)
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

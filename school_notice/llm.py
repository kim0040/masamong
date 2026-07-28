from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from .storage import NoticeRepository


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeepSeekSettings:
    api_key: str
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-flash"
    timeout_seconds: float = 45.0
    max_output_tokens: int = 1800
    max_calls_per_run: int = 20
    max_calls_per_day: int = 30
    max_retries: int = 2

    @classmethod
    def from_environment(cls) -> "DeepSeekSettings":
        return cls(
            api_key=os.environ.get("DEEPSEEK_API_KEY", "").strip(),
            base_url=os.environ.get(
                "SCHOOL_NOTICE_LLM_BASE_URL",
                "https://api.deepseek.com",
            ).rstrip("/"),
            model=os.environ.get(
                "SCHOOL_NOTICE_LLM_MODEL",
                "deepseek-v4-flash",
            ).strip(),
            timeout_seconds=float(
                os.environ.get("SCHOOL_NOTICE_LLM_TIMEOUT_SECONDS", "45")
            ),
            max_output_tokens=int(
                os.environ.get("SCHOOL_NOTICE_LLM_MAX_OUTPUT_TOKENS", "1800")
            ),
            max_calls_per_run=int(
                os.environ.get("SCHOOL_NOTICE_LLM_MAX_CALLS_PER_RUN", "20")
            ),
            max_calls_per_day=int(
                os.environ.get("SCHOOL_NOTICE_LLM_MAX_CALLS_PER_DAY", "30")
            ),
        )


class DeepSeekClient:
    API_TYPE = "school_notice_classify"

    def __init__(
        self,
        repository: NoticeRepository,
        settings: DeepSeekSettings | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings or DeepSeekSettings.from_environment()
        self.model = self.settings.model
        self.run_calls = 0
        self.consecutive_failures = 0
        self._semaphore = asyncio.Semaphore(2)
        self._session: aiohttp.ClientSession | None = None

    @property
    def configured(self) -> bool:
        return bool(self.settings.api_key)

    def _reserve(self, usage_date: date) -> None:
        if not self.configured:
            raise LLMError("deepseek_api_key_missing")
        if self.run_calls >= self.settings.max_calls_per_run:
            raise LLMError("llm_run_budget_exhausted")
        if self.consecutive_failures >= 3:
            raise LLMError("llm_circuit_open")
        if not self.repository.reserve_api_call(
            usage_date=usage_date,
            api_type=self.API_TYPE,
            daily_limit=self.settings.max_calls_per_day,
        ):
            raise LLMError("llm_daily_budget_exhausted")
        self.run_calls += 1

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _request(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        usage_date: date,
    ) -> str:
        del usage_date  # API 예산은 과거 run_date가 아닌 실제 한국 날짜로 계산한다.
        payload = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "temperature": 0,
            "max_tokens": self.settings.max_output_tokens,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
        }
        last_error: Exception | None = None
        budget_date = datetime.now(ZoneInfo("Asia/Seoul")).date()
        async with self._semaphore:
            if self._session is None or self._session.closed:
                timeout = aiohttp.ClientTimeout(
                    total=self.settings.timeout_seconds,
                    connect=min(10.0, self.settings.timeout_seconds),
                    sock_read=min(40.0, self.settings.timeout_seconds),
                )
                connector = aiohttp.TCPConnector(limit=2, limit_per_host=2)
                self._session = aiohttp.ClientSession(
                    timeout=timeout,
                    connector=connector,
                )
            for attempt in range(self.settings.max_retries + 1):
                try:
                    self._reserve(budget_date)
                    async with self._session.post(
                        f"{self.settings.base_url}/chat/completions",
                        headers=headers,
                        json=payload,
                    ) as response:
                        raw = await response.read()
                        if len(raw) > 2_000_000:
                            raise LLMError("llm_response_too_large")
                        if response.status in {429, 500, 503}:
                            raise LLMError(
                                f"llm_retryable_status:{response.status}"
                            )
                        if response.status < 200 or response.status >= 300:
                            raise LLMError(f"llm_http_status:{response.status}")
                        response_payload = json.loads(raw)
                        content = response_payload["choices"][0]["message"][
                            "content"
                        ]
                        if not isinstance(content, str) or not content.strip():
                            raise LLMError("llm_empty_content")
                        usage = response_payload.get("usage") or {}
                        self.repository.record_api_tokens(
                            usage_date=budget_date,
                            api_type=self.API_TYPE,
                            prompt_tokens=int(usage.get("prompt_tokens", 0)),
                            completion_tokens=int(
                                usage.get("completion_tokens", 0)
                            ),
                        )
                        self.consecutive_failures = 0
                        return content
                except (
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                    json.JSONDecodeError,
                    KeyError,
                    IndexError,
                    TypeError,
                    LLMError,
                ) as exc:
                    last_error = exc
                    retryable = isinstance(
                        exc,
                        (aiohttp.ClientError, asyncio.TimeoutError),
                    ) or str(exc).startswith("llm_retryable_status:")
                    if not retryable or attempt >= self.settings.max_retries:
                        break
                    await asyncio.sleep(0.5 * (2**attempt))
        self.consecutive_failures += 1
        raise LLMError(f"llm_request_failed:{type(last_error).__name__}")

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        usage_date: date,
    ) -> dict[str, Any]:
        content = await self._request(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            usage_date=usage_date,
        )
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            repair_system = (
                "다음 텍스트를 의미를 추가하거나 변경하지 말고 유효한 JSON 객체로만 "
                "고쳐 출력하라. 설명과 코드 펜스를 쓰지 마라."
            )
            repaired = await self._request(
                system_prompt=repair_system,
                user_prompt=content[:10_000],
                usage_date=usage_date,
            )
            try:
                parsed = json.loads(repaired)
            except json.JSONDecodeError as exc:
                raise LLMError("llm_json_repair_failed") from exc
        if not isinstance(parsed, dict):
            raise LLMError("llm_json_not_object")
        return parsed

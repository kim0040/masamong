import asyncio
import logging
from types import SimpleNamespace

import pytest

import config
from utils import llm_client as llm_module
from utils.intent_analyzer import IntentAnalyzer
from utils.llm_client import (
    LLMAdmissionTimeoutError,
    LLMClient,
    LLMProviderTimeoutError,
)


def _completion(text: str):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=text,
                    reasoning_content=None,
                )
            )
        ]
    )


def _target(name: str, base_url: str) -> dict[str, str]:
    return {
        "provider": "openai_compat",
        "base_url": base_url,
        "api_key": f"{name}-key",
        "model": f"{name}-model",
        "reasoning_effort": "",
        "name": name,
    }


def _configure_small_client(
    monkeypatch,
    *,
    concurrency: int = 1,
    acquire_timeout: float = 1.0,
    call_timeout: float = 1.0,
) -> LLMClient:
    monkeypatch.setattr(config, "LLM_MAX_CONCURRENT_CALLS", concurrency)
    monkeypatch.setattr(config, "LLM_ACQUIRE_TIMEOUT_SECONDS", acquire_timeout)
    monkeypatch.setattr(config, "LLM_CALL_TIMEOUT_SECONDS", call_timeout)
    return LLMClient()


@pytest.mark.asyncio
async def test_main_output_cap_follows_existing_reasoning_decision(monkeypatch):
    client = _configure_small_client(monkeypatch)
    monkeypatch.setattr(config, "MAIN_LLM_MAX_TOKENS", 8192)
    monkeypatch.setattr(config, "MAIN_LLM_LOW_MAX_TOKENS", 2048)
    monkeypatch.setattr(config, "MAIN_LLM_HIGH_MAX_TOKENS", 8192)
    captured: list[int] = []

    client.get_lane_targets = lambda *_args, **_kwargs: [
        {"name": "main.primary"}
    ]

    async def _call(_target, **kwargs):
        captured.append(kwargs["max_tokens"])
        return "완료"

    client.call_main_lane_target = _call

    assert await client.generate_content(
        "system",
        "user",
        {},
        reasoning_effort_override="low",
    ) == "완료"
    assert await client.generate_content(
        "system",
        "user",
        {},
        reasoning_effort_override="high",
    ) == "완료"
    assert captured == [2048, 8192]


@pytest.mark.asyncio
async def test_routing_lane_uses_shorter_physical_timeout(monkeypatch):
    client = _configure_small_client(monkeypatch, call_timeout=1.0)
    monkeypatch.setattr(config, "ROUTING_LLM_CALL_TIMEOUT_SECONDS", 0.01)

    class Endpoint:
        async def create(self, **_kwargs):
            await asyncio.sleep(0.05)
            return _completion("late")

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=Endpoint())
    )
    client.get_openai_client = lambda *_args: fake_client
    target = _target("routing.primary", "https://llm.example/v1")

    with pytest.raises(LLMProviderTimeoutError):
        await client.call_routing_lane_target(
            target,
            prompt="route",
            log_extra={},
            max_tokens=128,
        )


def test_openai_client_disables_sdk_retries(monkeypatch):
    created = []

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            created.append(kwargs)

    monkeypatch.setattr(llm_module, "AsyncOpenAI", FakeAsyncOpenAI)
    client = _configure_small_client(monkeypatch, call_timeout=7.0)

    first = client.get_openai_client("https://llm.example/v1", "secret")
    second = client.get_openai_client("https://llm.example/v1/", "secret")

    assert first is second
    assert len(created) == 1
    assert created[0]["max_retries"] == 0
    assert created[0]["timeout"] == 7.0


def test_gemini_client_uses_one_attempt_and_transport_timeout(monkeypatch):
    created = []

    class FakeGoogleGenAI:
        @staticmethod
        def Client(**kwargs):
            created.append(kwargs)
            return object()

    monkeypatch.setattr(llm_module, "google_genai", FakeGoogleGenAI)
    client = _configure_small_client(monkeypatch, call_timeout=7.0)

    first = client.get_gemini_compat_client(
        "https://gemini.example/v1",
        "secret",
    )
    second = client.get_gemini_compat_client(
        "https://gemini.example/v1/",
        "secret",
    )

    assert first is second
    assert len(created) == 1
    http_options = created[0]["http_options"]
    assert http_options["retry_options"] == {"attempts": 1}
    assert http_options["timeout"] == 7000


@pytest.mark.asyncio
async def test_bounded_provider_call_logs_safe_terminal_metrics(
    monkeypatch,
    caplog,
):
    client = _configure_small_client(monkeypatch)

    async def successful_call():
        return "ok"

    with caplog.at_level(logging.INFO):
        result = await client._run_bounded_provider_call(
            successful_call,
            lane_name="routing.primary",
            log_extra={"trace_id": "llm-metrics"},
        )

    assert result == "ok"
    records = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "llm_call_completed"
    ]
    assert len(records) == 1
    record = records[0]
    assert record.trace_id == "llm-metrics"
    assert record.lane == "routing.primary"
    assert record.outcome == "succeeded"
    assert record.duration_ms >= 0
    assert record.queue_wait_ms >= 0


@pytest.mark.asyncio
async def test_gemini_compat_uses_native_async_client_and_output_cap(monkeypatch):
    client = _configure_small_client(monkeypatch)
    calls = []

    class AsyncModels:
        async def generate_content(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(text="async gemini")

    class SyncModels:
        def generate_content(self, **kwargs):
            _ = kwargs
            raise AssertionError("sync Gemini path must not be used")

    fake_client = SimpleNamespace(
        aio=SimpleNamespace(models=AsyncModels()),
        models=SyncModels(),
    )
    client.get_gemini_compat_client = lambda base_url, api_key: fake_client
    target = {
        "provider": "gemini_compat",
        "base_url": "https://gemini.example/v1",
        "api_key": "secret",
        "model": "gemini-test",
        "reasoning_effort": "",
        "name": "main.primary",
    }

    result = await client.call_main_lane_target(
        target,
        system_prompt="system",
        user_prompt="user",
        log_extra={},
        max_tokens=321,
    )

    assert result == "async gemini"
    assert len(calls) == 1
    assert calls[0]["config"]["max_output_tokens"] == 321


@pytest.mark.asyncio
async def test_deepseek_dynamic_reasoning_is_request_local(monkeypatch):
    client = _configure_small_client(monkeypatch, concurrency=2)
    calls = []

    class Endpoint:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return _completion("answer")

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=Endpoint())
    )
    client.get_openai_client = lambda _base_url, _api_key: fake_client
    target = {
        "provider": "openai_compat",
        "base_url": "https://deepseek.example/v1",
        "api_key": "secret",
        "model": "deepseek-v4-flash",
        "reasoning_effort": "",
        "name": "main.primary",
    }

    low_result, high_result = await asyncio.gather(
        client.call_main_lane_target(
            target,
            system_prompt="system",
            user_prompt="simple",
            log_extra={"trace_id": "low"},
            max_tokens=128,
            reasoning_effort_override="low",
        ),
        client.call_main_lane_target(
            target,
            system_prompt="system",
            user_prompt="complex",
            log_extra={"trace_id": "high"},
            max_tokens=128,
            reasoning_effort_override="high",
        ),
    )

    assert low_result == high_result == "answer"
    assert len(calls) == 2
    assert {call["reasoning_effort"] for call in calls} == {"low", "high"}
    assert all(
        call["extra_body"] == {"thinking": {"type": "enabled"}}
        for call in calls
    )
    assert target["reasoning_effort"] == ""


def test_dynamic_reasoning_rejects_unbounded_values_and_unsupported_models():
    deepseek_target = {
        "model": "deepseek-v4-flash",
        "reasoning_effort": "",
    }
    unsupported_target = {
        "model": "plain-chat-model",
        "reasoning_effort": "",
    }

    assert (
        LLMClient.resolve_reasoning_effort(
            deepseek_target,
            "unlimited",
        )
        == "low"
    )
    assert (
        LLMClient.resolve_reasoning_effort(
            unsupported_target,
            "high",
        )
        == ""
    )


@pytest.mark.asyncio
async def test_main_primary_and_fallback_are_each_called_at_most_once(monkeypatch):
    client = _configure_small_client(monkeypatch)
    targets = [
        _target("main.primary", "https://primary.example/v1"),
        _target("main.fallback", "https://fallback.example/v1"),
    ]
    for target in targets:
        target["model"] = "deepseek-v4-flash"
    calls = {"main.primary": 0, "main.fallback": 0}
    efforts = []

    class Endpoint:
        def __init__(self, name):
            self.name = name

        async def create(self, **kwargs):
            efforts.append(kwargs.get("reasoning_effort"))
            calls[self.name] += 1
            if self.name == "main.primary":
                raise RuntimeError("definite primary failure")
            return _completion("fallback answer")

    clients = {
        target["base_url"]: SimpleNamespace(
            chat=SimpleNamespace(completions=Endpoint(target["name"]))
        )
        for target in targets
    }
    client.get_lane_targets = lambda lane, model_override=None: list(targets)
    client.get_openai_client = (
        lambda base_url, api_key: clients[base_url]
    )

    result = await client.generate_content(
        "system",
        "user",
        {"trace_id": "main"},
        reasoning_effort_override="high",
    )

    assert result == "fallback answer"
    assert calls == {"main.primary": 1, "main.fallback": 1}
    assert efforts == ["high", "high"]


@pytest.mark.asyncio
async def test_routing_primary_and_fallback_are_each_called_at_most_once(monkeypatch):
    client = _configure_small_client(monkeypatch)
    targets = [
        _target("routing.primary", "https://routing-primary.example/v1"),
        _target("routing.fallback", "https://routing-fallback.example/v1"),
    ]
    calls = {"routing.primary": 0, "routing.fallback": 0}

    class Endpoint:
        def __init__(self, name):
            self.name = name

        async def create(self, **kwargs):
            _ = kwargs
            calls[self.name] += 1
            if self.name == "routing.primary":
                raise RuntimeError("definite routing failure")
            return _completion('{"tools":[]}')

    clients = {
        target["base_url"]: SimpleNamespace(
            chat=SimpleNamespace(completions=Endpoint(target["name"]))
        )
        for target in targets
    }
    client.get_lane_targets = lambda lane, model_override=None: list(targets)
    client.get_openai_client = (
        lambda base_url, api_key: clients[base_url]
    )

    result = await client.fast_generate_text(
        "route",
        None,
        {"trace_id": "routing"},
    )

    assert result == '{"tools":[]}'
    assert calls == {"routing.primary": 1, "routing.fallback": 1}


@pytest.mark.asyncio
async def test_provider_timeout_does_not_start_fallback_and_releases_slot(monkeypatch):
    client = _configure_small_client(
        monkeypatch,
        acquire_timeout=0.2,
        call_timeout=0.02,
    )
    targets = [
        _target("main.primary", "https://slow.example/v1"),
        _target("main.fallback", "https://must-not-run.example/v1"),
    ]
    calls = {"main.primary": 0, "main.fallback": 0}

    class Endpoint:
        def __init__(self, name):
            self.name = name

        async def create(self, **kwargs):
            _ = kwargs
            calls[self.name] += 1
            if self.name == "main.primary":
                await asyncio.Event().wait()
            return _completion("unexpected")

    clients = {
        target["base_url"]: SimpleNamespace(
            chat=SimpleNamespace(completions=Endpoint(target["name"]))
        )
        for target in targets
    }
    client.get_lane_targets = lambda lane, model_override=None: list(targets)
    client.get_openai_client = (
        lambda base_url, api_key: clients[base_url]
    )

    result = await client.generate_content("system", "user", {"trace_id": "timeout"})

    assert result is None
    assert calls == {"main.primary": 1, "main.fallback": 0}

    async def immediate():
        return "slot released"

    assert await client._run_bounded_provider_call(
        immediate,
        lane_name="test",
        log_extra={},
    ) == "slot released"


@pytest.mark.asyncio
async def test_bounded_failure_can_propagate_to_block_outer_direct_fallback(
    monkeypatch,
):
    client = _configure_small_client(
        monkeypatch,
        acquire_timeout=0.2,
        call_timeout=0.02,
    )
    target = _target("main.primary", "https://slow.example/v1")

    class Endpoint:
        async def create(self, **kwargs):
            _ = kwargs
            await asyncio.Event().wait()

    client.get_lane_targets = lambda lane, model_override=None: [target]
    client.get_openai_client = lambda base_url, api_key: SimpleNamespace(
        chat=SimpleNamespace(completions=Endpoint())
    )

    with pytest.raises(LLMProviderTimeoutError):
        await client.generate_content(
            "system",
            "user",
            {"trace_id": "propagate-timeout"},
            raise_on_bounded_failure=True,
        )


@pytest.mark.asyncio
async def test_get_ai_completion_never_starts_direct_fallback_after_timeout(
    monkeypatch,
):
    client = _configure_small_client(monkeypatch)
    client.use_cometapi = True
    direct_constructed = False

    async def bounded_failure(*args, **kwargs):
        assert kwargs["raise_on_bounded_failure"] is True
        raise LLMProviderTimeoutError("timed out")

    def unexpected_direct_model(*args, **kwargs):
        nonlocal direct_constructed
        direct_constructed = True
        raise AssertionError("direct fallback must not start after timeout")

    client.generate_content = bounded_failure
    client.can_use_direct_gemini = lambda: True
    monkeypatch.setattr(
        llm_module,
        "genai",
        SimpleNamespace(GenerativeModel=unexpected_direct_model),
    )

    assert await client.get_ai_completion("hello") is None
    assert direct_constructed is False


@pytest.mark.asyncio
async def test_all_provider_entrypoints_share_one_concurrency_limit(monkeypatch):
    client = _configure_small_client(
        monkeypatch,
        concurrency=1,
        acquire_timeout=1.0,
        call_timeout=1.0,
    )
    state = {"active": 0, "max_active": 0}

    async def guarded_result(result):
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        try:
            await asyncio.sleep(0.02)
            return result
        finally:
            state["active"] -= 1

    class Endpoint:
        async def create(self, **kwargs):
            _ = kwargs
            return await guarded_result(_completion("openai"))

    class DirectGeminiModel:
        model_name = "models/direct-test"

        async def generate_content_async(self, *args, **kwargs):
            _ = args, kwargs
            return await guarded_result(SimpleNamespace(text="direct"))

    openai_client = SimpleNamespace(
        chat=SimpleNamespace(completions=Endpoint())
    )
    client.get_openai_client = lambda base_url, api_key: openai_client
    main_target = _target("main.primary", "https://shared.example/v1")
    routing_target = _target("routing.primary", "https://shared.example/v1")

    main_result, routing_result, direct_result = await asyncio.gather(
        client.call_main_lane_target(
            main_target,
            system_prompt="system",
            user_prompt="user",
            log_extra={},
            max_tokens=128,
        ),
        client.call_routing_lane_target(
            routing_target,
            prompt="route",
            log_extra={},
        ),
        client.safe_generate_content(
            DirectGeminiModel(),
            "direct",
            {},
        ),
    )

    assert main_result == "openai"
    assert routing_result == "openai"
    assert getattr(direct_result, "text", None) == "direct"
    assert state["max_active"] == 1


@pytest.mark.asyncio
async def test_acquire_timeout_never_constructs_provider_coroutine(monkeypatch):
    client = _configure_small_client(
        monkeypatch,
        concurrency=1,
        acquire_timeout=0.02,
        call_timeout=1.0,
    )
    constructed = False
    await client._provider_call_semaphore.acquire()

    async def should_not_run():
        nonlocal constructed
        constructed = True
        return "bad"

    try:
        with pytest.raises(LLMAdmissionTimeoutError):
            await client._run_bounded_provider_call(
                should_not_run,
                lane_name="saturated",
                log_extra={},
            )
    finally:
        client._provider_call_semaphore.release()

    assert constructed is False


@pytest.mark.asyncio
async def test_failed_physical_attempt_is_logged_under_separate_key(monkeypatch):
    client = _configure_small_client(monkeypatch)
    client.db = object()
    target = _target("main.primary", "https://failure.example/v1")
    logged_keys = []

    async def reserve_budget(*args, **kwargs):
        _ = args, kwargs
        return True, None

    async def capture_log(db, api_type):
        _ = db
        logged_keys.append(api_type)

    class Endpoint:
        async def create(self, **kwargs):
            _ = kwargs
            raise RuntimeError("provider rejected request")

    client.get_lane_targets = lambda lane, model_override=None: [target]
    client.get_openai_client = lambda base_url, api_key: SimpleNamespace(
        chat=SimpleNamespace(completions=Endpoint())
    )
    monkeypatch.setattr(
        llm_module.db_utils,
        "reserve_llm_api_call",
        reserve_budget,
    )
    monkeypatch.setattr(llm_module.db_utils, "log_api_call", capture_log)

    result = await client.generate_content("system", "user", {"trace_id": "failure"})

    assert result is None
    assert logged_keys == ["llm_attempt"]


def test_oversized_malicious_tool_plan_is_hard_capped(monkeypatch):
    analyzer = IntentAnalyzer(db=None, llm_client=None, tools_cog=None)
    monkeypatch.setattr(config, "AGENT_MAX_TOOL_CALLS", 999)
    malicious_plan = [
        {
            "tool_to_use": "generate_image",
            "parameters": {"prompt": f"image-{index}"},
        }
        for index in range(50)
    ]
    malicious_plan.extend(
        {
            "tool_to_use": "web_search",
            "parameters": {"query": f"query-{index}"},
        }
        for index in range(50)
    )
    malicious_plan.extend(["not-an-object"] * 50)

    sanitized = analyzer._sanitize_tool_plan(
        "고양이 이미지 생성해줘. 관련 자료도 검색해줘",
        malicious_plan,
        rag_top_score=0.0,
        trust_llm=True,
    )

    names = [item["tool_to_use"] for item in sanitized]
    assert len(sanitized) == 3
    assert names.count("generate_image") == 1
    assert names.count("web_search") == 2

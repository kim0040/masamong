import threading
from types import SimpleNamespace

import pytest

import config
from utils import news_search


def _target(name: str, base_url: str) -> dict[str, str]:
    return {
        "provider": "openai_compat",
        "base_url": base_url,
        "api_key": f"{name}-key",
        "model": f"{name}-model",
        "reasoning_effort": "",
        "name": name,
    }


def _completion(text: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


@pytest.fixture(autouse=True)
def _reset_fast_llm_state(monkeypatch):
    news_search._fast_openai_clients.clear()
    news_search._fast_gemini_clients.clear()
    monkeypatch.setattr(
        news_search,
        "_fast_provider_semaphore",
        threading.BoundedSemaphore(1),
    )
    monkeypatch.setattr(config, "LLM_ACQUIRE_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(config, "LLM_CALL_TIMEOUT_SECONDS", 0.05)
    yield
    news_search._fast_openai_clients.clear()
    news_search._fast_gemini_clients.clear()


def test_sync_openai_client_disables_retries_and_sets_transport_timeout(
    monkeypatch,
):
    created = []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            created.append(kwargs)

    monkeypatch.setattr(news_search, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(config, "LLM_CALL_TIMEOUT_SECONDS", 7.0)

    first = news_search._get_fast_openai_client(
        "https://llm.example/v1",
        "secret",
    )
    second = news_search._get_fast_openai_client(
        "https://llm.example/v1/",
        "secret",
    )

    assert first is second
    assert len(created) == 1
    assert created[0]["max_retries"] == 0
    assert created[0]["timeout"] == 7.0


def test_sync_gemini_client_uses_one_attempt_and_millisecond_timeout(
    monkeypatch,
):
    created = []

    class FakeGoogleGenAI:
        @staticmethod
        def Client(**kwargs):
            created.append(kwargs)
            return object()

    monkeypatch.setattr(news_search, "google_genai", FakeGoogleGenAI)
    monkeypatch.setattr(config, "LLM_CALL_TIMEOUT_SECONDS", 7.0)

    first = news_search._get_fast_gemini_client(
        "https://gemini.example/v1",
        "secret",
    )
    second = news_search._get_fast_gemini_client(
        "https://gemini.example/v1/",
        "secret",
    )

    assert first is second
    assert len(created) == 1
    http_options = created[0]["http_options"]
    assert http_options["retry_options"] == {"attempts": 1}
    assert http_options["timeout"] == 7000


def test_definite_failure_uses_each_target_once_and_measures_each_attempt(
    monkeypatch,
):
    targets = [
        _target("routing.primary", "https://primary.example/v1"),
        _target("routing.fallback", "https://fallback.example/v1"),
    ]
    calls = {"routing.primary": 0, "routing.fallback": 0}

    class Endpoint:
        def __init__(self, name):
            self.name = name

        def create(self, **kwargs):
            _ = kwargs
            calls[self.name] += 1
            if self.name == "routing.primary":
                raise RuntimeError("definite provider failure")
            return _completion("fallback answer")

    clients = {
        target["base_url"]: SimpleNamespace(
            chat=SimpleNamespace(completions=Endpoint(target["name"]))
        )
        for target in targets
    }
    quota_calls = []

    class Quota:
        def try_consume(self):
            quota_calls.append("attempt")
            return True

    budget = news_search.FastLLMBudget(2)
    monkeypatch.setattr(news_search, "_routing_targets", lambda: list(targets))
    monkeypatch.setattr(
        news_search,
        "_get_fast_openai_client",
        lambda base_url, api_key: clients[base_url],
    )

    result = news_search._call_fast_model(
        "prompt",
        budget=budget,
        quota_manager=Quota(),
    )

    assert result == "fallback answer"
    assert calls == {"routing.primary": 1, "routing.fallback": 1}
    assert budget.used_calls == 2
    assert len(quota_calls) == 2


def test_provider_timeout_stops_fallback_amplification(monkeypatch):
    targets = [
        _target("routing.primary", "https://slow.example/v1"),
        _target("routing.fallback", "https://must-not-run.example/v1"),
    ]
    calls = {"routing.primary": 0, "routing.fallback": 0}

    class Endpoint:
        def __init__(self, name):
            self.name = name

        def create(self, **kwargs):
            _ = kwargs
            calls[self.name] += 1
            if self.name == "routing.primary":
                raise TimeoutError("transport timed out")
            return _completion("unexpected")

    clients = {
        target["base_url"]: SimpleNamespace(
            chat=SimpleNamespace(completions=Endpoint(target["name"]))
        )
        for target in targets
    }
    quota_calls = []

    class Quota:
        def try_consume(self):
            quota_calls.append("attempt")
            return True

    budget = news_search.FastLLMBudget(2)
    monkeypatch.setattr(news_search, "_routing_targets", lambda: list(targets))
    monkeypatch.setattr(
        news_search,
        "_get_fast_openai_client",
        lambda base_url, api_key: clients[base_url],
    )

    result = news_search._call_fast_model(
        "prompt",
        budget=budget,
        quota_manager=Quota(),
    )

    assert result == ""
    assert calls == {"routing.primary": 1, "routing.fallback": 0}
    assert budget.used_calls == 1
    assert len(quota_calls) == 1


def test_admission_timeout_starts_no_provider_and_no_fallback(monkeypatch):
    targets = [
        _target("routing.primary", "https://busy.example/v1"),
        _target("routing.fallback", "https://must-not-run.example/v1"),
    ]
    calls = {"routing.primary": 0, "routing.fallback": 0}

    class Endpoint:
        def __init__(self, name):
            self.name = name

        def create(self, **kwargs):
            _ = kwargs
            calls[self.name] += 1
            return _completion("unexpected")

    clients = {
        target["base_url"]: SimpleNamespace(
            chat=SimpleNamespace(completions=Endpoint(target["name"]))
        )
        for target in targets
    }
    quota_calls = []

    class Quota:
        def try_consume(self):
            quota_calls.append("attempt")
            return True

    occupied = threading.BoundedSemaphore(1)
    assert occupied.acquire(blocking=False)
    monkeypatch.setattr(news_search, "_fast_provider_semaphore", occupied)
    monkeypatch.setattr(config, "LLM_ACQUIRE_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(news_search, "_routing_targets", lambda: list(targets))
    monkeypatch.setattr(
        news_search,
        "_get_fast_openai_client",
        lambda base_url, api_key: clients[base_url],
    )
    budget = news_search.FastLLMBudget(2)

    try:
        result = news_search._call_fast_model(
            "prompt",
            budget=budget,
            quota_manager=Quota(),
        )
    finally:
        occupied.release()

    assert result == ""
    assert calls == {"routing.primary": 0, "routing.fallback": 0}
    assert budget.used_calls == 0
    assert quota_calls == []


def test_configured_physical_call_cap_blocks_extra_target(monkeypatch):
    targets = [
        _target("routing.primary", "https://primary.example/v1"),
        _target("routing.fallback", "https://must-not-run.example/v1"),
    ]
    calls = {"routing.primary": 0, "routing.fallback": 0}

    class Endpoint:
        def __init__(self, name):
            self.name = name

        def create(self, **kwargs):
            _ = kwargs
            calls[self.name] += 1
            raise RuntimeError("definite failure")

    clients = {
        target["base_url"]: SimpleNamespace(
            chat=SimpleNamespace(completions=Endpoint(target["name"]))
        )
        for target in targets
    }
    quota_calls = []

    class Quota:
        def try_consume(self):
            quota_calls.append("attempt")
            return True

    monkeypatch.setattr(config, "WEB_RAG_FAST_LLM_MAX_CALLS", 1)
    monkeypatch.setattr(news_search, "_routing_targets", lambda: list(targets))
    monkeypatch.setattr(
        news_search,
        "_get_fast_openai_client",
        lambda base_url, api_key: clients[base_url],
    )

    result = news_search._call_fast_model(
        "prompt",
        quota_manager=Quota(),
    )

    assert result == ""
    assert calls == {"routing.primary": 1, "routing.fallback": 0}
    assert len(quota_calls) == 1


def test_quota_rejection_refunds_budget_and_stops_fallback(monkeypatch):
    targets = [
        _target("routing.primary", "https://primary.example/v1"),
        _target("routing.fallback", "https://must-not-run.example/v1"),
    ]
    provider_calls = []

    class Endpoint:
        def create(self, **kwargs):
            provider_calls.append(kwargs)
            return _completion("unexpected")

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=Endpoint())
    )

    class RejectingQuota:
        def try_consume(self):
            return False

    budget = news_search.FastLLMBudget(2)
    monkeypatch.setattr(news_search, "_routing_targets", lambda: list(targets))
    monkeypatch.setattr(
        news_search,
        "_get_fast_openai_client",
        lambda base_url, api_key: fake_client,
    )

    result = news_search._call_fast_model(
        "prompt",
        budget=budget,
        quota_manager=RejectingQuota(),
    )

    assert result == ""
    assert provider_calls == []
    assert budget.used_calls == 0


def test_missing_quota_store_fails_closed():
    manager = news_search.FastLLMQuotaManager(None)

    assert manager.try_consume() is False


def test_quota_store_error_fails_closed_without_repeated_connection(
    monkeypatch,
):
    manager = news_search.FastLLMQuotaManager("unused.sqlite3")
    connection_attempts = []

    def fail_connection():
        connection_attempts.append("attempt")
        raise OSError("quota database unavailable")

    monkeypatch.setattr(manager, "_open_connection", fail_connection)

    assert manager.try_consume() is False
    assert manager.try_consume() is False
    assert connection_attempts == ["attempt"]


def test_gemini_physical_call_has_output_cap_and_is_measured(monkeypatch):
    target = {
        "provider": "gemini_compat",
        "base_url": "https://gemini.example/v1",
        "api_key": "secret",
        "model": "gemini-test",
        "reasoning_effort": "",
        "name": "routing.primary",
    }
    provider_calls = []
    quota_calls = []

    class Models:
        def generate_content(self, **kwargs):
            provider_calls.append(kwargs)
            return SimpleNamespace(text="gemini answer")

    class Quota:
        def try_consume(self):
            quota_calls.append("attempt")
            return True

    budget = news_search.FastLLMBudget(1)
    monkeypatch.setattr(config, "ROUTING_LLM_MAX_TOKENS", 321)
    monkeypatch.setattr(news_search, "_routing_targets", lambda: [target])
    monkeypatch.setattr(
        news_search,
        "_get_fast_gemini_client",
        lambda base_url, api_key: SimpleNamespace(models=Models()),
    )

    result = news_search._call_fast_model(
        "prompt",
        budget=budget,
        quota_manager=Quota(),
    )

    assert result == "gemini answer"
    assert len(provider_calls) == 1
    assert provider_calls[0]["config"]["max_output_tokens"] == 321
    assert budget.used_calls == 1
    assert len(quota_calls) == 1

"""외부 도구 circuit breaker와 자동 복구 경계."""

from utils.tool_health import ToolHealthRegistry


def test_circuit_opens_after_threshold_and_half_open_recovers():
    health = ToolHealthRegistry(failure_threshold=2, cooldown_seconds=60)

    assert health.begin_attempt("weather", now=100) is True
    health.record_failure("weather", now=100)
    assert health.begin_attempt("weather", now=101) is True
    health.record_failure("weather", now=101)

    assert health.is_available("weather", now=120) is False
    assert health.begin_attempt("weather", now=120) is False
    assert health.is_available("weather", now=162) is True
    assert health.begin_attempt("weather", now=162) is True
    assert health.begin_attempt("weather", now=162) is False

    health.record_success("weather")
    assert health.begin_attempt("weather", now=163) is True


def test_tool_circuits_are_isolated():
    health = ToolHealthRegistry(failure_threshold=1, cooldown_seconds=60)

    health.record_failure("stock", now=10)

    assert health.is_available("stock", now=11) is False
    assert health.is_available("weather", now=11) is True

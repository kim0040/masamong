"""외부 도구 circuit breaker와 자동 복구 경계."""

import asyncio

import pytest

from cogs.tools_cog import ToolsCog
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


@pytest.mark.asyncio
async def test_cancelled_half_open_probe_does_not_lock_tool_forever():
    """요청 취소는 provider 실패가 아니며 다음 복구 probe를 막아서도 안 된다."""
    health = ToolHealthRegistry(failure_threshold=1, cooldown_seconds=1)
    health.record_failure("weather", now=10)

    cog = ToolsCog.__new__(ToolsCog)
    cog.tool_health = health

    async def cancelled_operation():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await cog.execute_guarded("weather", cancelled_operation)

    assert health.begin_attempt("weather", now=12) is True

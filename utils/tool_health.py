"""외부 도구 장애를 요청 경로에서 제한하는 작은 in-memory circuit breaker."""

from __future__ import annotations

import time
from dataclasses import dataclass


class ToolTemporarilyUnavailable(RuntimeError):
    """짧은 cooldown 동안 외부 호출을 생략했음을 나타냅니다."""


@dataclass
class _Circuit:
    consecutive_failures: int = 0
    open_until: float = 0.0
    probe_in_flight: bool = False


class ToolHealthRegistry:
    """실패 누적 → cooldown → 다음 실제 요청 1회 probe 상태를 관리합니다."""

    def __init__(
        self,
        *,
        failure_threshold: int = 2,
        cooldown_seconds: float = 60.0,
    ) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(1.0, float(cooldown_seconds))
        self._circuits: dict[str, _Circuit] = {}

    def _state(self, tool_name: str) -> _Circuit:
        return self._circuits.setdefault(str(tool_name), _Circuit())

    def is_available(self, tool_name: str, *, now: float | None = None) -> bool:
        state = self._state(tool_name)
        current = time.monotonic() if now is None else float(now)
        return state.open_until <= current

    def begin_attempt(self, tool_name: str, *, now: float | None = None) -> bool:
        state = self._state(tool_name)
        current = time.monotonic() if now is None else float(now)
        if state.open_until > current:
            return False
        if state.open_until > 0:
            if state.probe_in_flight:
                return False
            state.probe_in_flight = True
        return True

    def record_success(self, tool_name: str) -> None:
        state = self._state(tool_name)
        state.consecutive_failures = 0
        state.open_until = 0.0
        state.probe_in_flight = False

    def record_failure(
        self,
        tool_name: str,
        *,
        now: float | None = None,
    ) -> None:
        state = self._state(tool_name)
        current = time.monotonic() if now is None else float(now)
        state.probe_in_flight = False
        state.consecutive_failures += 1
        if state.consecutive_failures >= self.failure_threshold:
            state.open_until = current + self.cooldown_seconds


# -*- coding: utf-8 -*-
"""반복 로그 억제 필터 동작을 검증한다.

2026-08-10 장애에서 DB 연결이 끊긴 뒤 60초 주기 background loop들이 매 주기
전체 traceback을 남겨 하루 37만 줄, journal 1.2GB까지 늘었다. 같은 상황에서
로그량이 창 단위로 수렴하는지 확인한다.

억제 키는 (로거, 레벨, 모듈, 함수, 줄번호, 메시지 템플릿)이다. 실제 장애에서
반복되는 로그는 항상 같은 줄에서 나오므로, 테스트도 한 줄에서 반복 호출해
운영 상황을 그대로 재현한다.
"""

import logging

import pytest

from logger_config import RepeatSuppressingFilter


class _CollectingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _tick(log: logging.Logger, message: str = "DB 연결 실패") -> None:
    """항상 같은 줄에서 로깅해 운영의 주기적 실패를 흉내낸다."""
    log.error(message)


@pytest.fixture
def logger_with_filter(request):
    created: list[logging.Logger] = []

    def _build(window_seconds: float = 600.0, clock=None):
        log = logging.getLogger(f"repeat_suppress_{request.node.name}_{len(created)}")
        log.handlers.clear()
        log.propagate = False
        log.setLevel(logging.DEBUG)
        handler = _CollectingHandler()
        kwargs = {"window_seconds": window_seconds}
        if clock is not None:
            kwargs["clock"] = clock
        repeat_filter = RepeatSuppressingFilter(**kwargs)
        handler.addFilter(repeat_filter)
        log.addHandler(handler)
        created.append(log)
        return log, handler, repeat_filter

    yield _build

    for log in created:
        log.handlers.clear()


def test_repeated_error_is_emitted_once_per_window(logger_with_filter):
    log, handler, _ = logger_with_filter()

    for _ in range(1440):  # 하루치 60초 주기 tick
        _tick(log)

    assert len(handler.records) == 1


def test_distinct_messages_are_not_suppressed(logger_with_filter):
    log, handler, _ = logger_with_filter()

    for message in ("모닝 브리핑 실패", "학교 공지 전달 실패", "편입 공지 전달 실패"):
        _tick(log, message)

    assert len(handler.records) == 3


def test_info_level_is_never_suppressed(logger_with_filter):
    log, handler, _ = logger_with_filter()

    for _ in range(50):
        log.info("정상 동작 중")

    assert len(handler.records) == 50


def test_window_expiry_reemits_with_suppressed_count(logger_with_filter):
    clock = _FakeClock()
    log, handler, _ = logger_with_filter(window_seconds=600.0, clock=clock)

    for _ in range(100):
        _tick(log)
    assert len(handler.records) == 1

    # 창이 지나면 다시 한 번 남기되 그동안 억제된 횟수를 알려준다.
    clock.advance(601.0)
    _tick(log)

    assert len(handler.records) == 2
    assert "99건 생략" in str(handler.records[1].msg)


def test_same_record_across_handlers_counted_once(logger_with_filter):
    """여러 핸들러가 필터를 공유해도 집계는 record당 한 번만 이뤄진다."""
    log, first_handler, repeat_filter = logger_with_filter()
    second_handler = _CollectingHandler()
    second_handler.addFilter(repeat_filter)
    log.addHandler(second_handler)

    for _ in range(10):
        _tick(log)

    # 첫 record만 양쪽 핸들러를 통과하고 나머지는 모두 억제된다.
    assert len(first_handler.records) == 1
    assert len(second_handler.records) == 1


def test_suppressed_count_is_accurate_across_windows(logger_with_filter):
    clock = _FakeClock()
    log, handler, _ = logger_with_filter(window_seconds=600.0, clock=clock)

    for _ in range(3):
        for _ in range(10):
            _tick(log)
        clock.advance(601.0)

    # 창마다 한 건씩, 총 3건.
    assert len(handler.records) == 3
    assert "9건 생략" in str(handler.records[1].msg)
    assert "9건 생략" in str(handler.records[2].msg)


def test_key_cache_does_not_grow_unbounded(logger_with_filter):
    clock = _FakeClock()
    log, _, repeat_filter = logger_with_filter(window_seconds=1.0, clock=clock)

    for index in range(1000):
        _tick(log, f"고유 템플릿 {index}")
        clock.advance(2.0)

    # 창이 지난 키는 정리되므로 무한히 쌓이지 않는다.
    assert len(repeat_filter._seen) <= 512

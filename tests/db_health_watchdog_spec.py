# -*- coding: utf-8 -*-
"""DB 헬스체크 loop가 죽은 연결을 교체 대상으로 표시하는지 검증한다.

2026-08-10 장애에서는 연결이 끊긴 뒤에도 봇 프로세스가 살아 있어 systemd의
``Restart=on-failure``가 동작하지 않았고, 트래픽이 적은 시간대에는 아무도
이상을 알아채지 못했다. 이 loop는 유휴 상태에서도 재연결 경로를 한 번은
실행시키는 안전장치다.
"""

import asyncio

import pytest

import config
from main import ReMasamongBot


class _StubCursor:
    def __init__(self, fail: bool):
        self._fail = fail

    async def __aenter__(self):
        if self._fail:
            raise ConnectionResetError("connection lost")
        return self

    async def __aexit__(self, *args):
        return False

    async def fetchone(self):
        return (1,)


class _StubDB:
    def __init__(self, backend: str = "tidb", fail: bool = False):
        self.backend = backend
        self.fail = fail
        self.queries: list[str] = []
        self._conn_broken = False

    def execute(self, query, params=None):
        self.queries.append(query)
        return _StubCursor(self.fail)


class _StubBot:
    """`_db_health_loop`가 쓰는 최소 표면만 흉내낸다."""

    def __init__(self, db, ticks: int = 1):
        self.db = db
        self._remaining = ticks

    async def wait_until_ready(self):
        return None

    def is_closed(self) -> bool:
        if self._remaining <= 0:
            return True
        self._remaining -= 1
        return False


@pytest.fixture(autouse=True)
def _fast_interval(monkeypatch):
    monkeypatch.setattr(config, "DB_HEALTHCHECK_INTERVAL_SECONDS", 0)


@pytest.mark.asyncio
async def test_healthcheck_uses_single_cheap_query():
    db = _StubDB()
    bot = _StubBot(db, ticks=3)

    await ReMasamongBot._db_health_loop(bot)

    assert db.queries == ["SELECT 1"] * 3
    assert db._conn_broken is False


@pytest.mark.asyncio
async def test_healthcheck_marks_connection_broken_on_failure():
    db = _StubDB(fail=True)
    bot = _StubBot(db, ticks=1)

    await ReMasamongBot._db_health_loop(bot)

    assert db._conn_broken is True


@pytest.mark.asyncio
async def test_healthcheck_does_not_touch_sqlite_backend():
    db = _StubDB(backend="sqlite", fail=True)
    bot = _StubBot(db, ticks=1)

    await ReMasamongBot._db_health_loop(bot)

    # SQLite에는 원격 연결 단절 개념이 없으므로 플래그를 건드리지 않는다.
    assert db._conn_broken is False


@pytest.mark.asyncio
async def test_healthcheck_survives_missing_db():
    bot = _StubBot(db=None, ticks=2)

    # db가 아직 없거나 종료 중이어도 예외 없이 지나가야 한다.
    await ReMasamongBot._db_health_loop(bot)


@pytest.mark.asyncio
async def test_healthcheck_propagates_cancellation():
    db = _StubDB()
    bot = _StubBot(db, ticks=10_000)

    task = asyncio.create_task(ReMasamongBot._db_health_loop(bot))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

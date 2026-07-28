import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytz

import config
from cogs.fortune_cog import FortuneCog
from utils.privacy_consent import FORTUNE_SCOPE, grant_consent


ROOT = Path(__file__).resolve().parents[1]
KST = pytz.timezone("Asia/Seoul")


class _AI:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def _cometapi_generate_content(self, *_args, **_kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else "briefing"
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _User:
    def __init__(self, send_outcomes=None):
        self.display_name = "테스트 사용자"
        self.send_outcomes = list(send_outcomes or [])
        self.send_calls = 0
        self.messages = []

    async def send(self, content, **_kwargs):
        self.send_calls += 1
        if self.send_outcomes:
            outcome = self.send_outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
        self.messages.append(content)
        return object()


class _Calculator:
    def __init__(self):
        self.target_dates = []

    def get_comprehensive_info(self, birth_date, birth_time, *, target_date=None):
        self.target_dates.append(target_date)
        return f"birth={birth_date} time={birth_time or 'not-provided'}"


class _Bot:
    def __init__(self, db, ai, user):
        self.db = db
        self.ai = ai
        self.user = user

    def get_cog(self, name):
        return self.ai if name == "AIHandler" else None

    def get_user(self, _user_id):
        return self.user

    async def fetch_user(self, _user_id):
        return self.user


async def _make_cog(ai_outcomes, *, send_outcomes=None):
    db = await aiosqlite.connect(":memory:")
    await db.executescript(
        (ROOT / "database" / "schema.sql").read_text(encoding="utf-8")
    )
    await db.commit()
    ai = _AI(ai_outcomes)
    user = _User(send_outcomes)
    cog = FortuneCog.__new__(FortuneCog)
    cog.bot = _Bot(db, ai, user)
    cog.calculator = _Calculator()
    return cog, db, ai, user


async def _insert_profile(
    db,
    *,
    user_id,
    subscription_time,
    last_sent=None,
    consent=True,
):
    await db.execute(
        """
        INSERT INTO user_profiles (
            user_id, birth_date, birth_time, gender, birth_place,
            subscription_active, subscription_time, last_fortune_sent
        ) VALUES (?, '2000-01-01', NULL, NULL, NULL, 1, ?, ?)
        """,
        (user_id, subscription_time, last_sent),
    )
    await db.commit()
    if consent:
        await grant_consent(db, user_id, FORTUNE_SCOPE)


@pytest.mark.asyncio
async def test_generation_failures_are_persistently_bounded_and_backed_off(
    monkeypatch,
):
    monkeypatch.setattr(config, "FORTUNE_MORNING_MAX_GENERATION_ATTEMPTS", 3)
    monkeypatch.setattr(config, "FORTUNE_MORNING_RETRY_BASE_SECONDS", 60)
    cog, db, ai, _user = await _make_cog(
        [RuntimeError("fail-1"), RuntimeError("fail-2"), RuntimeError("fail-3")]
    )
    try:
        await _insert_profile(db, user_id=1, subscription_time="07:00")
        start = KST.localize(datetime(2026, 7, 28, 8, 0))

        assert await cog._run_morning_briefing_tick(now=start) == "generation_retry"
        assert await cog._run_morning_briefing_tick(
            now=start + timedelta(seconds=30)
        ) == "idle"
        assert await cog._run_morning_briefing_tick(
            now=start + timedelta(seconds=61)
        ) == "generation_retry"
        assert await cog._run_morning_briefing_tick(
            now=start + timedelta(seconds=182)
        ) == "terminal_failed"
        assert await cog._run_morning_briefing_tick(
            now=start + timedelta(hours=3)
        ) == "idle"

        assert ai.calls == 3
        async with db.execute(
            "SELECT pending_payload FROM user_profiles WHERE user_id = 1"
        ) as cursor:
            job = json.loads((await cursor.fetchone())[0])
        assert job["state"] == "terminal_failed"
        assert job["generation_attempts"] == 3
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_send_retry_reuses_generated_content_without_another_llm_call(
    monkeypatch,
):
    monkeypatch.setattr(config, "FORTUNE_MORNING_RETRY_BASE_SECONDS", 60)
    cog, db, ai, user = await _make_cog(
        ["one generated briefing"],
        send_outcomes=[RuntimeError("discord transient"), None],
    )
    try:
        await _insert_profile(db, user_id=1, subscription_time="07:00")
        start = KST.localize(datetime(2026, 7, 28, 8, 0))

        assert await cog._run_morning_briefing_tick(now=start) == "generated"
        assert await cog._run_morning_briefing_tick(
            now=start + timedelta(seconds=60)
        ) == "send_retry"
        assert await cog._run_morning_briefing_tick(
            now=start + timedelta(seconds=121)
        ) == "sent"

        assert ai.calls == 1
        assert user.send_calls == 2
        async with db.execute(
            """
            SELECT pending_payload, last_fortune_sent, last_fortune_content
            FROM user_profiles WHERE user_id = 1
            """
        ) as cursor:
            row = await cursor.fetchone()
        assert tuple(row) == (
            None,
            "2026-07-28",
            "one generated briefing",
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_one_tick_processes_only_one_user():
    cog, db, ai, _user = await _make_cog(["first", "second"])
    try:
        await _insert_profile(db, user_id=1, subscription_time="07:00")
        await _insert_profile(db, user_id=2, subscription_time="07:00")
        now = KST.localize(datetime(2026, 7, 28, 8, 0))

        assert await cog._run_morning_briefing_tick(now=now) == "generated"

        assert ai.calls == 1
        async with db.execute(
            "SELECT COUNT(*) FROM user_profiles WHERE pending_payload IS NOT NULL"
        ) as cursor:
            assert (await cursor.fetchone())[0] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_midnight_pregeneration_uses_next_calendar_date():
    cog, db, ai, _user = await _make_cog(["tomorrow briefing"])
    try:
        await _insert_profile(
            db,
            user_id=1,
            subscription_time="00:00",
            last_sent="2026-07-28",
        )
        now = KST.localize(datetime(2026, 7, 28, 23, 57))

        assert await cog._run_morning_briefing_tick(now=now) == "generated"

        async with db.execute(
            "SELECT pending_payload FROM user_profiles WHERE user_id = 1"
        ) as cursor:
            job = json.loads((await cursor.fetchone())[0])
        assert job["target_date"] == "2026-07-29"
        assert cog.calculator.target_dates == [datetime(2026, 7, 29).date()]
        assert ai.calls == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_no_consent_means_no_generation_or_delivery():
    cog, db, ai, user = await _make_cog(["must-not-run"])
    try:
        await _insert_profile(
            db,
            user_id=1,
            subscription_time="07:00",
            consent=False,
        )
        now = KST.localize(datetime(2026, 7, 28, 8, 0))

        assert await cog._run_morning_briefing_tick(now=now) == "idle"
        assert ai.calls == 0
        assert user.send_calls == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_morning_rechecks_consent_immediately_before_provider():
    cog, db, ai, _user = await _make_cog(["must-not-run"])
    try:
        await _insert_profile(db, user_id=1, subscription_time="07:00")
        cog._has_fortune_consent = AsyncMock(side_effect=[True, False])
        now = KST.localize(datetime(2026, 7, 28, 8, 0))

        assert (
            await cog._run_morning_briefing_tick(now=now)
            == "consent_required"
        )
        assert ai.calls == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_morning_rechecks_consent_at_actual_dm_send_boundary():
    cog, db, ai, user = await _make_cog(["generated but not sent"])
    try:
        await _insert_profile(db, user_id=1, subscription_time="07:00")
        start = KST.localize(datetime(2026, 7, 28, 8, 0))
        assert await cog._run_morning_briefing_tick(now=start) == "generated"

        # delivery 입구, user fetch 뒤, 실제 send 직전 확인 순서다.
        cog._has_fortune_consent = AsyncMock(
            side_effect=[True, True, False]
        )
        assert await cog._run_morning_briefing_tick(
            now=start + timedelta(seconds=60)
        ) == "consent_required"
        assert ai.calls == 1
        assert user.send_calls == 0
    finally:
        await db.close()

from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest

import config
from cogs.privacy_cog import ConsentDecisionView, PrivacyCog
from utils.privacy_consent import (
    CONSENT_GRANTED,
    CONSENT_WITHDRAWN,
    FORTUNE_SCOPE,
    SCHOOL_NOTICE_SCOPE,
    TRANSFER_NOTICE_SCOPE,
    get_consent_state,
    get_policy,
    grant_consent,
    has_current_consent,
    withdraw_consent,
)


ROOT = Path(__file__).resolve().parents[1]
USER_ID = 100000000000000001


async def _db():
    db = await aiosqlite.connect(":memory:")
    await db.executescript(
        (ROOT / "database" / "schema.sql").read_text(encoding="utf-8")
    )
    await db.commit()
    return db


@pytest.mark.asyncio
async def test_consent_grant_withdraw_and_regrant_are_current_and_append_only():
    db = await _db()
    try:
        assert not await has_current_consent(db, USER_ID, FORTUNE_SCOPE)

        granted = await grant_consent(db, USER_ID, FORTUNE_SCOPE)
        assert granted.status == CONSENT_GRANTED
        assert granted.granted_at
        assert granted.withdrawn_at is None
        assert await has_current_consent(db, USER_ID, FORTUNE_SCOPE)

        withdrawn = await withdraw_consent(db, USER_ID, FORTUNE_SCOPE)
        assert withdrawn.status == CONSENT_WITHDRAWN
        assert withdrawn.granted_at == granted.granted_at
        assert withdrawn.withdrawn_at
        assert not await has_current_consent(db, USER_ID, FORTUNE_SCOPE)

        regranted = await grant_consent(db, USER_ID, FORTUNE_SCOPE)
        assert regranted.status == CONSENT_GRANTED
        assert regranted.withdrawn_at is None
        assert await has_current_consent(db, USER_ID, FORTUNE_SCOPE)

        async with db.execute(
            "SELECT COUNT(*) FROM privacy_consents WHERE user_id = ? AND scope = ?",
            (USER_ID, FORTUNE_SCOPE),
        ) as cursor:
            assert (await cursor.fetchone())[0] == 1
        async with db.execute(
            """
            SELECT status FROM privacy_consent_events
            WHERE user_id = ? AND scope = ?
            ORDER BY id
            """,
            (USER_ID, FORTUNE_SCOPE),
        ) as cursor:
            assert [row[0] for row in await cursor.fetchall()] == [
                CONSENT_GRANTED,
                CONSENT_WITHDRAWN,
                CONSENT_GRANTED,
            ]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_policy_version_and_notice_hash_must_match_exactly():
    db = await _db()
    try:
        await grant_consent(db, USER_ID, FORTUNE_SCOPE)
        await db.execute(
            """
            UPDATE privacy_consents
            SET policy_version = ?, notice_hash = ?
            WHERE user_id = ? AND scope = ?
            """,
            ("old-policy", "0" * 64, USER_ID, FORTUNE_SCOPE),
        )
        await db.commit()

        assert not await has_current_consent(db, USER_ID, FORTUNE_SCOPE)
        await grant_consent(db, USER_ID, FORTUNE_SCOPE)
        state = await get_consent_state(db, USER_ID, FORTUNE_SCOPE)
        policy = get_policy(FORTUNE_SCOPE)
        assert state is not None
        assert state.policy_version == policy.version
        assert state.notice_hash == policy.notice_hash
        assert await has_current_consent(db, USER_ID, FORTUNE_SCOPE)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_consent_scopes_are_independent_and_withdraw_before_grant_is_noop():
    db = await _db()
    try:
        await grant_consent(db, USER_ID, FORTUNE_SCOPE)
        withdrawn = await withdraw_consent(db, USER_ID, SCHOOL_NOTICE_SCOPE)

        assert await has_current_consent(db, USER_ID, FORTUNE_SCOPE)
        assert not await has_current_consent(db, USER_ID, SCHOOL_NOTICE_SCOPE)
        assert withdrawn is None
        assert (
            await get_consent_state(db, USER_ID, SCHOOL_NOTICE_SCOPE)
        ) is None
        async with db.execute(
            """
            SELECT COUNT(*) FROM privacy_consent_events
            WHERE user_id = ? AND scope = ?
            """,
            (USER_ID, SCHOOL_NOTICE_SCOPE),
        ) as cursor:
            assert (await cursor.fetchone())[0] == 0
    finally:
        await db.close()


class _Destination:
    def __init__(self):
        self.sent = []

    async def send(self, content, **kwargs):
        self.sent.append((content, kwargs))
        return SimpleNamespace()


class _LegacyPromptBot:
    def __init__(self, db, destination):
        self.db = db
        self.destination = destination

    def get_user(self, user_id):
        return self.destination if int(user_id) == USER_ID else None

    async def fetch_user(self, user_id):
        assert int(user_id) == USER_ID
        return self.destination


class _Response:
    def __init__(self):
        self.sent = []
        self.edited = []

    async def send_message(self, content, **kwargs):
        self.sent.append((content, kwargs))

    async def edit_message(self, **kwargs):
        self.edited.append(kwargs)


@pytest.mark.asyncio
async def test_showing_or_rejecting_notice_does_not_record_consent():
    db = await _db()
    try:
        cog = PrivacyCog(SimpleNamespace(db=db))
        destination = _Destination()
        await cog.send_consent_prompt(
            destination,
            user_id=USER_ID,
            scope=FORTUNE_SCOPE,
        )

        assert not await has_current_consent(db, USER_ID, FORTUNE_SCOPE)
        view = destination.sent[0][1]["view"]
        response = _Response()
        await view._cancel(
            SimpleNamespace(
                user=SimpleNamespace(id=USER_ID),
                response=response,
            )
        )
        assert not await has_current_consent(db, USER_ID, FORTUNE_SCOPE)
        async with db.execute(
            "SELECT COUNT(*) FROM privacy_consent_events"
        ) as cursor:
            assert (await cursor.fetchone())[0] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_only_initiating_user_can_use_consent_view():
    db = await _db()
    try:
        cog = PrivacyCog(SimpleNamespace(db=db))
        view = ConsentDecisionView(
            cog,
            user_id=USER_ID,
            scope=FORTUNE_SCOPE,
        )
        response = _Response()
        allowed = await view.interaction_check(
            SimpleNamespace(
                user=SimpleNamespace(id=USER_ID + 1),
                response=response,
            )
        )

        assert allowed is False
        assert response.sent
        assert not await has_current_consent(db, USER_ID, FORTUNE_SCOPE)
        assert not await has_current_consent(db, USER_ID + 1, FORTUNE_SCOPE)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_agree_button_records_current_policy_for_initiating_user():
    db = await _db()
    try:
        cog = PrivacyCog(SimpleNamespace(db=db))
        view = ConsentDecisionView(
            cog,
            user_id=USER_ID,
            scope=FORTUNE_SCOPE,
        )
        response = _Response()
        await view._grant(
            SimpleNamespace(
                user=SimpleNamespace(id=USER_ID),
                response=response,
            )
        )

        assert await has_current_consent(db, USER_ID, FORTUNE_SCOPE)
        assert response.edited
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_agree_button_continues_original_feature_exactly_once():
    db = await _db()
    try:
        continued = []

        async def continue_feature(interaction):
            continued.append(interaction)

        cog = PrivacyCog(SimpleNamespace(db=db))
        view = ConsentDecisionView(
            cog,
            user_id=USER_ID,
            scope=FORTUNE_SCOPE,
            on_granted=continue_feature,
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=USER_ID),
            response=_Response(),
        )

        await view._grant(interaction)

        assert await has_current_consent(db, USER_ID, FORTUNE_SCOPE)
        assert continued == [interaction]
        assert view.is_finished()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reject_button_never_continues_original_feature():
    db = await _db()
    try:
        continued = []

        async def continue_feature(interaction):
            continued.append(interaction)

        cog = PrivacyCog(SimpleNamespace(db=db))
        view = ConsentDecisionView(
            cog,
            user_id=USER_ID,
            scope=FORTUNE_SCOPE,
            on_granted=continue_feature,
        )
        await view._cancel(
            SimpleNamespace(
                user=SimpleNamespace(id=USER_ID),
                response=_Response(),
            )
        )

        assert continued == []
        assert not await has_current_consent(db, USER_ID, FORTUNE_SCOPE)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_legacy_prompt_selects_only_active_uncanceled_subscriber(
    monkeypatch,
):
    db = await _db()
    try:
        canceled_id = USER_ID + 1
        await db.executemany(
            """
            INSERT INTO user_profiles (user_id, subscription_active)
            VALUES (?, ?)
            """,
            [(USER_ID, 1), (canceled_id, 0)],
        )
        await db.commit()
        monkeypatch.setattr(config, "FORTUNE_MORNING_BRIEFING_ENABLED", True)
        monkeypatch.setattr(config, "SCHOOL_NOTICE_ENABLED", False)
        monkeypatch.setattr(config, "TRANSFER_NOTICE_ENABLED", False)
        monkeypatch.setattr(config, "DISABLED_COGS", set())
        cog = PrivacyCog(SimpleNamespace(db=db))

        candidate = await cog._next_legacy_prompt_candidate()

        assert candidate == (USER_ID, FORTUNE_SCOPE)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_explicitly_withdrawn_active_subscriber_is_not_prompted(
    monkeypatch,
):
    db = await _db()
    try:
        await db.execute(
            """
            INSERT INTO transfer_notice_subscriptions (
                user_id, schools_json, enabled, created_at, updated_at
            ) VALUES (?, '["kangwon"]', 1, '2026-01-01', '2026-01-01')
            """,
            (USER_ID,),
        )
        await db.commit()
        await grant_consent(db, USER_ID, TRANSFER_NOTICE_SCOPE)
        await withdraw_consent(db, USER_ID, TRANSFER_NOTICE_SCOPE)
        monkeypatch.setattr(config, "FORTUNE_MORNING_BRIEFING_ENABLED", False)
        monkeypatch.setattr(config, "SCHOOL_NOTICE_ENABLED", False)
        monkeypatch.setattr(config, "TRANSFER_NOTICE_ENABLED", True)
        monkeypatch.setattr(config, "DISABLED_COGS", set())
        cog = PrivacyCog(SimpleNamespace(db=db))

        assert await cog._next_legacy_prompt_candidate() is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_successful_legacy_consent_prompt_is_sent_once_per_policy(
    monkeypatch,
):
    db = await _db()
    try:
        await db.execute(
            """
            INSERT INTO user_profiles (user_id, subscription_active)
            VALUES (?, 1)
            """,
            (USER_ID,),
        )
        await db.commit()
        monkeypatch.setattr(config, "FORTUNE_MORNING_BRIEFING_ENABLED", True)
        monkeypatch.setattr(config, "SCHOOL_NOTICE_ENABLED", False)
        monkeypatch.setattr(config, "TRANSFER_NOTICE_ENABLED", False)
        monkeypatch.setattr(config, "DISABLED_COGS", set())
        destination = _Destination()
        cog = PrivacyCog(_LegacyPromptBot(db, destination))

        first = await cog._legacy_prompt_tick()
        second = await cog._legacy_prompt_tick()

        assert first == "sent"
        assert second == "idle"
        assert len(destination.sent) == 1
        assert "기존 **운세** 자동 알림 구독" in destination.sent[0][0]
        assert destination.sent[0][1]["view"] is not None
    finally:
        await db.close()

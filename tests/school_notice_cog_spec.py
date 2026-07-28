"""학교 공지 Cog 명세.

버튼 연타로 점수가 왜곡되지 않는지, 같은 공지를 두 번 보내지 않는지,
계약이 깨진 digest를 전달하지 않는지가 핵심이다.
"""

import json
import shutil
from datetime import date
from pathlib import Path

import aiosqlite
import pytest

import config
from cogs.school_notice_cog import (
    SchoolNoticeCog,
    user_key_for,
    validate_profile_payload,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
TODAY = date(2026, 7, 27)
USER_ID = 100000000000000001
USER_KEY = user_key_for(USER_ID)


class _FakeUser:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, *, content=None, embeds=None, view=None):
        self.messages.append({"content": content, "embeds": embeds, "view": view})


class _FakeBot:
    def __init__(self, db) -> None:
        self.db = db
        self.user_obj = _FakeUser()

    def get_user(self, _user_id):
        return self.user_obj

    async def fetch_user(self, _user_id):
        return self.user_obj


async def _make_cog(tmp_path, monkeypatch, *, fixture="school_notice_digest.json"):
    db = await aiosqlite.connect(":memory:")
    await db.executescript((ROOT / "database" / "schema.sql").read_text(encoding="utf-8"))
    await db.commit()

    digest_root = tmp_path / "digests"
    (digest_root / USER_KEY).mkdir(parents=True)
    if fixture:
        shutil.copy(
            FIXTURES / fixture,
            digest_root / USER_KEY / f"daily-digest-{TODAY.isoformat()}.json",
        )

    monkeypatch.setattr(config, "SCHOOL_NOTICE_ENABLED", False)
    monkeypatch.setattr(config, "SCHOOL_NOTICE_DIGEST_DIR", str(digest_root))
    monkeypatch.setattr(config, "SCHOOL_NOTICE_MAX_ITEMS_PER_DM", 10)
    monkeypatch.setattr(config, "SCHOOL_NOTICE_SCHEMA_VERSION", 1)
    monkeypatch.setattr(config, "SCHOOL_NOTICE_STALE_WARNING_ENABLED", True)

    bot = _FakeBot(db)
    cog = SchoolNoticeCog(bot)
    cog.digest_dir = digest_root
    return cog, bot, db


async def _register_profile(cog, *, enabled=1):
    await cog.upsert_profile(
        USER_ID,
        {"user_key": USER_KEY, "school_id": "jbnu", "degree_level": "undergraduate", "grade": 3},
    )
    if not enabled:
        await cog.bot.db.execute(
            "UPDATE school_notice_profiles SET enabled = 0 WHERE user_id = ?", (USER_ID,)
        )
        await cog.bot.db.commit()


@pytest.mark.asyncio
async def test_repeated_interaction_records_feedback_once(tmp_path, monkeypatch):
    cog, _bot, db = await _make_cog(tmp_path, monkeypatch)
    try:
        first = await cog.record_feedback(
            user_id=USER_ID, source_id="jbnu_software", external_id="1",
            feedback_type="useful", interaction_id="interaction-1",
        )
        second = await cog.record_feedback(
            user_id=USER_ID, source_id="jbnu_software", external_id="1",
            feedback_type="useful", interaction_id="interaction-1",
        )

        assert first is True
        assert second is False
        async with db.execute("SELECT COUNT(*) FROM school_notice_feedback") as cursor:
            assert (await cursor.fetchone())[0] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_unknown_feedback_type_is_rejected(tmp_path, monkeypatch):
    cog, _bot, db = await _make_cog(tmp_path, monkeypatch)
    try:
        with pytest.raises(ValueError, match="지원하지 않는"):
            await cog.record_feedback(
                user_id=USER_ID, source_id="s", external_id="1",
                feedback_type="love_it", interaction_id="x",
            )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_delivery_sends_once_then_reports_nothing_to_send(tmp_path, monkeypatch):
    cog, bot, db = await _make_cog(tmp_path, monkeypatch)
    try:
        first = await cog.deliver_to_user(USER_ID, USER_KEY, TODAY)
        sent_after_first = len(bot.user_obj.messages)
        second = await cog.deliver_to_user(USER_ID, USER_KEY, TODAY)

        assert first == "sent"
        assert sent_after_first > 0
        # 같은 날 같은 공지를 다시 보내지 않는다.
        assert second == "nothing_to_send"
        assert len(bot.user_obj.messages) == sent_after_first
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_delivery_records_each_notice(tmp_path, monkeypatch):
    cog, _bot, db = await _make_cog(tmp_path, monkeypatch)
    try:
        await cog.deliver_to_user(USER_ID, USER_KEY, TODAY)

        async with db.execute(
            "SELECT COUNT(*) FROM school_notice_deliveries WHERE user_key = ? AND status = 'sent'",
            (USER_KEY,),
        ) as cursor:
            assert (await cursor.fetchone())[0] == 4
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_broken_contract_digest_is_not_delivered(tmp_path, monkeypatch):
    cog, bot, db = await _make_cog(
        tmp_path, monkeypatch, fixture="school_notice_digest_bad_schema.json"
    )
    try:
        status = await cog.deliver_to_user(USER_ID, USER_KEY, TODAY)

        assert status == "contract_error"
        assert bot.user_obj.messages == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_missing_digest_is_not_an_exception(tmp_path, monkeypatch):
    cog, bot, db = await _make_cog(tmp_path, monkeypatch, fixture=None)
    try:
        status = await cog.deliver_to_user(USER_ID, USER_KEY, TODAY)

        assert status == "contract_error"
        assert bot.user_obj.messages == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_stale_health_is_sent_even_with_nothing_new(tmp_path, monkeypatch):
    cog, bot, db = await _make_cog(
        tmp_path, monkeypatch, fixture="school_notice_digest_stale.json"
    )
    try:
        await cog.deliver_to_user(USER_ID, USER_KEY, TODAY)
        bot.user_obj.messages.clear()

        # 전부 전달된 뒤에도 수집 이상은 계속 알려야 한다.
        status = await cog.deliver_to_user(USER_ID, USER_KEY, TODAY)

        assert status == "sent"
        assert bot.user_obj.messages
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_empty_digest_sends_nothing_when_healthy(tmp_path, monkeypatch):
    cog, bot, db = await _make_cog(
        tmp_path, monkeypatch, fixture="school_notice_digest_empty.json"
    )
    try:
        status = await cog.deliver_to_user(USER_ID, USER_KEY, TODAY)

        assert status == "nothing_to_send"
        assert bot.user_obj.messages == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_active_profiles_excludes_disabled(tmp_path, monkeypatch):
    cog, _bot, db = await _make_cog(tmp_path, monkeypatch)
    try:
        await _register_profile(cog)
        assert await cog.active_profiles() == [(USER_ID, USER_KEY)]

        await db.execute(
            "UPDATE school_notice_profiles SET enabled = 0 WHERE user_id = ?", (USER_ID,)
        )
        await db.commit()
        assert await cog.active_profiles() == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_profile_upsert_bumps_version_and_keeps_one_row(tmp_path, monkeypatch):
    cog, _bot, db = await _make_cog(tmp_path, monkeypatch)
    try:
        await _register_profile(cog)
        await cog.upsert_profile(
            USER_ID,
            {"user_key": USER_KEY, "school_id": "skku", "degree_level": "master"},
        )

        async with db.execute(
            "SELECT COUNT(*), MAX(school_id), MAX(profile_version) FROM school_notice_profiles"
        ) as cursor:
            count, school_id, version = await cursor.fetchone()
        assert count == 1
        assert school_id == "skku"
        assert version == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_mute_topic_is_recorded_and_removable(tmp_path, monkeypatch):
    cog, _bot, db = await _make_cog(tmp_path, monkeypatch)
    try:
        await cog.record_feedback(
            user_id=USER_ID, source_id="", external_id="",
            feedback_type="mute_topic", interaction_id="cmd-1", topic="공모전",
        )
        async with db.execute(
            "SELECT topic FROM school_notice_feedback WHERE feedback_type = 'mute_topic'"
        ) as cursor:
            assert [row[0] for row in await cursor.fetchall()] == ["공모전"]

        await db.execute(
            "DELETE FROM school_notice_feedback WHERE feedback_type = 'mute_topic' AND topic = ?",
            ("공모전",),
        )
        await db.commit()
        async with db.execute(
            "SELECT COUNT(*) FROM school_notice_feedback WHERE feedback_type = 'mute_topic'"
        ) as cursor:
            assert (await cursor.fetchone())[0] == 0
    finally:
        await db.close()


def test_user_key_is_derived_from_discord_id():
    assert user_key_for(123) == "discord-123"


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({}, "school_id"),
        ({"school_id": "jbnu"}, "degree_level"),
        ({"school_id": "jbnu", "degree_level": "phd"}, "degree_level"),
        ({"school_id": "jbnu", "degree_level": "undergraduate"}, "grade"),
        ({"school_id": "jbnu", "degree_level": "undergraduate", "grade": 9}, "grade"),
        (
            {"school_id": "jbnu", "degree_level": "master", "gpa_last_semester": 5.0},
            "gpa_last_semester",
        ),
        (
            {"school_id": "jbnu", "degree_level": "master", "career_interests": ["a" * 200]},
            "career_interests",
        ),
        (
            {
                "school_id": "jbnu",
                "degree_level": "master",
                "notification_preferences": {"include_bands": ["urgent"]},
            },
            "include_bands",
        ),
    ],
)
def test_invalid_profiles_are_rejected(payload, expected):
    with pytest.raises(ValueError, match=expected):
        validate_profile_payload(payload, user_id=USER_ID)


def test_valid_profile_gets_server_assigned_user_key():
    profile = validate_profile_payload(
        {
            "user_key": "spoofed-key",
            "school_id": "jbnu",
            "degree_level": "undergraduate",
            "grade": 3,
        },
        user_id=USER_ID,
    )

    # 사용자가 넘긴 user_key를 그대로 믿으면 다른 사람의 digest를 받을 수 있다.
    assert profile["user_key"] == USER_KEY


def test_profile_json_round_trips():
    profile = validate_profile_payload(
        {"school_id": "jbnu", "degree_level": "undergraduate", "grade": 3,
         "career_interests": ["소프트웨어"]},
        user_id=USER_ID,
    )

    assert json.loads(json.dumps(profile, ensure_ascii=False)) == profile

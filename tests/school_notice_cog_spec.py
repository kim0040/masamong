"""학교 공지 Cog 명세.

버튼 연타로 점수가 왜곡되지 않는지, 같은 공지를 두 번 보내지 않는지,
계약이 깨진 digest를 전달하지 않는지가 핵심이다.
"""

import asyncio
import json
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import aiosqlite
import discord
import pytest

import config
import cogs.school_notice_cog as school_notice_module
from cogs.school_notice_cog import (
    FeedbackView,
    SchoolNoticeCog,
    user_key_for,
    validate_profile_payload,
)
from utils.school_notice_contract import parse_digest
from utils.school_notice_profile import profile_snapshot_hash
from utils.privacy_consent import (
    ConsentRequiredError,
    SCHOOL_NOTICE_SCOPE,
    grant_consent,
    withdraw_consent,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
TODAY = date(2026, 7, 27)
USER_ID = 100000000000000001
USER_KEY = user_key_for(USER_ID)


class _FakeUser:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.send_calls = 0
        self.fail_on_call: int | None = None

    async def send(self, *, content=None, embeds=None, view=None):
        self.send_calls += 1
        if self.fail_on_call == self.send_calls:
            response = SimpleNamespace(status=500, reason="test", text="")
            raise discord.HTTPException(response, "injected")
        self.messages.append({"content": content, "embeds": embeds, "view": view})


class _FakeBot:
    def __init__(self, db) -> None:
        self.db = db
        self.user_obj = _FakeUser()
        self.locked_users: set[int] = set()
        self.wait_messages: list[_FakeMessage] = []
        self.cogs: dict[str, object] = {}
        self.context = None

    def get_user(self, _user_id):
        return self.user_obj

    async def fetch_user(self, _user_id):
        return self.user_obj

    def get_cog(self, name):
        return self.cogs.get(name)

    async def wait_for(self, _event, *, check, timeout):
        del timeout
        if not self.wait_messages:
            raise asyncio.TimeoutError
        message = self.wait_messages.pop(0)
        assert check(message)
        return message

    async def get_context(self, _message):
        assert self.context is not None
        return self.context


class _FakeMessage:
    def __init__(self, content: str, *, channel_id: int = 77) -> None:
        self.content = content
        self.author = SimpleNamespace(id=USER_ID, bot=False)
        self.channel = SimpleNamespace(id=channel_id)
        self.guild = None


class _FakeContext:
    def __init__(self) -> None:
        self.author = SimpleNamespace(id=USER_ID)
        self.channel = SimpleNamespace(id=77)
        self.guild = None
        self.message = SimpleNamespace(id=12345)
        self.messages: list[dict] = []

    async def reply(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})

    async def send(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})


class _InteractionResponse:
    def __init__(self) -> None:
        self.deferred = 0

    async def defer(self, **_kwargs) -> None:
        self.deferred += 1


class _InteractionFollowup:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict]] = []

    async def send(self, content, **kwargs) -> None:
        self.messages.append((content, kwargs))


class _FeedbackInteraction:
    def __init__(self) -> None:
        self.id = 987654321
        self.user = SimpleNamespace(id=USER_ID)
        self.response = _InteractionResponse()
        self.followup = _InteractionFollowup()


async def _make_cog(tmp_path, monkeypatch, *, fixture="school_notice_digest.json"):
    db = await aiosqlite.connect(":memory:")
    await db.executescript((ROOT / "database" / "schema.sql").read_text(encoding="utf-8"))
    await db.commit()
    await grant_consent(db, USER_ID, SCHOOL_NOTICE_SCOPE)

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
    monkeypatch.setattr(config, "SCHOOL_NOTICE_DEFAULT_DELIVERY_TIME", "09:00")
    monkeypatch.setattr(config, "SCHOOL_NOTICE_PROFILE_LLM_ENABLED", True)
    monkeypatch.setattr(config, "SCHOOL_NOTICE_PROFILE_MAX_REVISIONS", 3)
    monkeypatch.setattr(config, "SCHOOL_NOTICE_PROFILE_INPUT_TIMEOUT_SECONDS", 30)
    monkeypatch.setattr(config, "SCHOOL_NOTICE_PROFILE_LLM_TIMEOUT_SECONDS", 5)
    monkeypatch.setattr(config, "SCHOOL_NOTICE_INITIAL_CRAWL_ENABLED", False)
    monkeypatch.setattr(config, "SCHOOL_NOTICE_DELIVERY_BATCH_SIZE", 10)
    monkeypatch.setattr(config, "SCHOOL_NOTICE_DELIVERY_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(config, "SCHOOL_NOTICE_DELIVERY_RETRY_MINUTES", 10)
    monkeypatch.setattr(config, "SCHOOL_NOTICE_DELIVERY_USER_TIMEOUT_SECONDS", 5)

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


def _write_digest(
    cog,
    digest_date: date,
    *,
    fixture: str = "school_notice_digest.json",
    user_key: str = USER_KEY,
    revision_overrides: dict[int, int] | None = None,
):
    payload = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))
    payload["user_key"] = user_key
    payload["date"] = digest_date.isoformat()
    for item in payload.get("items", []):
        notice_id = int(item["notice_id"])
        if revision_overrides and notice_id in revision_overrides:
            item["revision_count"] = revision_overrides[notice_id]
            item["change"] = "updated"
    directory = cog.digest_dir / user_key
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"daily-digest-{digest_date.isoformat()}.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


async def _record_batch_run(cog, digest_date: date, *, status="succeeded"):
    await cog.bot.db.execute(
        """
        UPDATE school_notice_profiles
        SET updated_at = ?
        WHERE user_id = ?
        """,
        (f"{digest_date.isoformat()}T22:00:00+09:00", USER_ID),
    )
    async with cog.bot.db.execute(
        """
        SELECT profile_version, profile_json
        FROM school_notice_profiles
        WHERE user_id = ?
        """,
        (USER_ID,),
    ) as cursor:
        profile_version, profile_json = await cursor.fetchone()
    await cog.bot.db.execute(
        """
        INSERT INTO school_notice_batch_runs
            (user_key, run_date, profile_version, profile_hash, status,
             collection_status, may_include_stale, item_count, finished_at)
        VALUES (?, ?, ?, ?, ?, 'healthy', 0, 0, ?)
        """,
        (
            USER_KEY,
            digest_date.isoformat(),
            int(profile_version),
            profile_snapshot_hash(str(profile_json)),
            status,
            f"{digest_date.isoformat()}T23:10:00+09:00",
        ),
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
async def test_feedback_button_enters_callback_defers_and_persists(
    tmp_path,
    monkeypatch,
):
    cog, _bot, db = await _make_cog(tmp_path, monkeypatch)
    try:
        digest = school_notice_module.load_digest(
            cog.digest_dir / USER_KEY / f"daily-digest-{TODAY.isoformat()}.json"
        )
        view = FeedbackView(cog, digest.visible_items()[0])
        button = next(
            child
            for child in view.children
            if getattr(child, "_feedback_type", None) == "not_interested"
        )
        interaction = _FeedbackInteraction()

        await button.callback(interaction)

        assert interaction.response.deferred == 1
        assert "우선순위를 조금 낮춥니다" in interaction.followup.messages[0][0]
        async with db.execute(
            "SELECT feedback_type FROM school_notice_feedback"
        ) as cursor:
            assert (await cursor.fetchone())[0] == "not_interested"
        # discord.py 내부 Item._parent를 사용자 코드가 View로 덮어쓰지 않는다.
        assert getattr(button, "_parent", None) is not view
    finally:
        await db.close()


def test_feedback_buttons_have_one_clear_effect_each():
    payload = json.loads(
        (FIXTURES / "school_notice_digest.json").read_text(encoding="utf-8")
    )
    item = parse_digest(payload).visible_items()[0]
    labels = {
        child.label
        for child in FeedbackView(SimpleNamespace(), item).children
    }

    assert labels == {
        "유용해요",
        "이 공지 처리했어요",
        "비슷한 주제 덜 보기",
        "원문 확인",
    }


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
async def test_delivery_rechecks_consent_after_fetch_before_header(
    tmp_path,
    monkeypatch,
):
    cog, bot, db = await _make_cog(tmp_path, monkeypatch)

    async def fetch_after_withdrawal(_user_id):
        await withdraw_consent(db, USER_ID, SCHOOL_NOTICE_SCOPE)
        return bot.user_obj

    monkeypatch.setattr(bot, "get_user", lambda _user_id: None)
    monkeypatch.setattr(bot, "fetch_user", fetch_after_withdrawal)
    try:
        assert await cog.deliver_to_user(USER_ID, USER_KEY, TODAY) == "consent_required"
        assert bot.user_obj.messages == []
        async with db.execute(
            "SELECT COUNT(*) FROM school_notice_deliveries WHERE user_key = ?",
            (USER_KEY,),
        ) as cursor:
            assert (await cursor.fetchone())[0] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_delivery_rechecks_exact_profile_after_fetch_before_header(
    tmp_path,
    monkeypatch,
):
    cog, bot, db = await _make_cog(tmp_path, monkeypatch)
    try:
        await _register_profile(cog)
        await _record_batch_run(cog, TODAY)
        async with db.execute(
            "SELECT profile_json FROM school_notice_profiles WHERE user_id = ?",
            (USER_ID,),
        ) as cursor:
            changed_profile = json.loads((await cursor.fetchone())[0])
        changed_profile["grade"] = 4

        async def fetch_after_profile_change(_user_id):
            await db.execute(
                """
                UPDATE school_notice_profiles
                SET profile_json = ?, profile_version = profile_version + 1,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (
                    json.dumps(changed_profile, ensure_ascii=False, sort_keys=True),
                    f"{TODAY.isoformat()}T23:10:00+09:00",
                    USER_ID,
                ),
            )
            await db.commit()
            return bot.user_obj

        monkeypatch.setattr(bot, "get_user", lambda _user_id: None)
        monkeypatch.setattr(bot, "fetch_user", fetch_after_profile_change)

        assert (
            await cog.deliver_to_user(
                USER_ID,
                USER_KEY,
                TODAY,
                verify_batch_snapshot=True,
            )
            == "profile_stale"
        )
        assert bot.user_obj.messages == []
        async with db.execute(
            "SELECT COUNT(*) FROM school_notice_deliveries WHERE user_key = ?",
            (USER_KEY,),
        ) as cursor:
            assert (await cursor.fetchone())[0] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_same_revision_is_deduped_across_digest_dates_and_update_resends(
    tmp_path,
    monkeypatch,
):
    cog, bot, db = await _make_cog(tmp_path, monkeypatch)
    next_date = TODAY + timedelta(days=1)
    updated_date = TODAY + timedelta(days=2)
    try:
        assert await cog.deliver_to_user(USER_ID, USER_KEY, TODAY) == "sent"
        first_count = len(bot.user_obj.messages)

        _write_digest(cog, next_date)
        assert (
            await cog.deliver_to_user(USER_ID, USER_KEY, next_date)
            == "nothing_to_send"
        )
        assert len(bot.user_obj.messages) == first_count

        _write_digest(cog, updated_date, revision_overrides={16: 2})
        assert await cog.deliver_to_user(USER_ID, USER_KEY, updated_date) == "sent"
        async with db.execute(
            """
            SELECT revision_count
            FROM school_notice_deliveries
            WHERE user_key = ? AND notice_id = 16 AND status = 'sent'
            ORDER BY revision_count
            """,
            (USER_KEY,),
        ) as cursor:
            assert [row[0] for row in await cursor.fetchall()] == [1, 2]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_partial_send_marks_each_success_before_later_failure(
    tmp_path,
    monkeypatch,
):
    cog, bot, db = await _make_cog(tmp_path, monkeypatch)
    try:
        # header와 첫 item 다음 두 번째 item 본문에서 실패한다.
        bot.user_obj.fail_on_call = 3
        assert await cog.deliver_to_user(USER_ID, USER_KEY, TODAY) == "send_failed"
        async with db.execute(
            """
            SELECT notice_id, revision_count
            FROM school_notice_deliveries
            WHERE user_key = ? AND notice_id > 0 AND status = 'sent'
            """,
            (USER_KEY,),
        ) as cursor:
            first_rows = await cursor.fetchall()
        assert len(first_rows) == 1

        bot.user_obj.fail_on_call = None
        assert await cog.deliver_to_user(USER_ID, USER_KEY, TODAY) == "sent"
        async with db.execute(
            """
            SELECT COUNT(*)
            FROM school_notice_deliveries
            WHERE user_key = ? AND notice_id > 0 AND status = 'sent'
            """,
            (USER_KEY,),
        ) as cursor:
            assert (await cursor.fetchone())[0] == 4
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_manual_digest_stops_between_groups_after_consent_withdrawal(
    tmp_path,
    monkeypatch,
):
    cog, _bot, db = await _make_cog(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "SCHOOL_NOTICE_ENABLED", True)
    monkeypatch.setattr(
        school_notice_module,
        "chunk_embeds",
        lambda embeds: [[embeds[0]], embeds[1:]],
    )
    ctx = _FakeContext()
    original_reply = ctx.reply

    async def reply_then_withdraw(content=None, **kwargs):
        await original_reply(content, **kwargs)
        if kwargs.get("embeds"):
            await withdraw_consent(db, USER_ID, SCHOOL_NOTICE_SCOPE)

    monkeypatch.setattr(ctx, "reply", reply_then_withdraw)
    try:
        await _register_profile(cog)
        await _record_batch_run(cog, TODAY)
        await SchoolNoticeCog.school_notice.callback(cog, ctx, page=1)

        personalized_messages = [
            message for message in ctx.messages if message.get("embeds")
        ]
        assert len(personalized_messages) == 1
        assert any(
            "명시적 동의가 필요" in str(message.get("content"))
            for message in ctx.messages
        )
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
@pytest.mark.parametrize(
    ("wrong_user", "wrong_date"),
    [
        ("discord-999999999999999999", TODAY),
        (USER_KEY, TODAY + timedelta(days=1)),
    ],
)
async def test_digest_identity_mismatch_is_never_delivered(
    tmp_path,
    monkeypatch,
    wrong_user,
    wrong_date,
):
    cog, bot, db = await _make_cog(tmp_path, monkeypatch, fixture=None)
    try:
        _write_digest(cog, TODAY, user_key=wrong_user)
        if wrong_user != USER_KEY:
            source = cog.digest_dir / wrong_user / f"daily-digest-{TODAY}.json"
            target_dir = cog.digest_dir / USER_KEY
            target_dir.mkdir(exist_ok=True)
            shutil.copy(source, target_dir / source.name)
        else:
            payload_path = cog.digest_dir / USER_KEY / f"daily-digest-{TODAY}.json"
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
            payload["date"] = wrong_date.isoformat()
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
        assert await cog.deliver_to_user(USER_ID, USER_KEY, TODAY) == "contract_error"
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
async def test_stale_health_warning_is_sent_at_most_once_per_date(tmp_path, monkeypatch):
    cog, bot, db = await _make_cog(
        tmp_path, monkeypatch, fixture="school_notice_digest_stale.json"
    )
    try:
        await cog.deliver_to_user(USER_ID, USER_KEY, TODAY)
        bot.user_obj.messages.clear()

        # 수집 이상 경고도 같은 날짜에 매분/반복 호출로 재전송하지 않는다.
        status = await cog.deliver_to_user(USER_ID, USER_KEY, TODAY)

        assert status == "nothing_to_send"
        assert bot.user_obj.messages == []
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
async def test_scheduler_delivers_previous_day_at_default_time_and_completes_run(
    tmp_path,
    monkeypatch,
):
    cog, bot, db = await _make_cog(tmp_path, monkeypatch)
    delivery_day = TODAY + timedelta(days=1)
    try:
        await _register_profile(cog)
        await _record_batch_run(cog, TODAY)

        before = datetime.combine(
            delivery_day,
            datetime.min.time(),
            tzinfo=ZoneInfo("Asia/Seoul"),
        ).replace(hour=8, minute=59)
        assert await cog.process_due_deliveries(now=before) == 0
        assert bot.user_obj.messages == []

        due = before.replace(hour=9, minute=0)
        assert await cog.process_due_deliveries(now=due) == 1
        assert bot.user_obj.messages
        async with db.execute(
            """
            SELECT status, attempt_count
            FROM school_notice_delivery_runs
            WHERE user_key = ? AND digest_date = ?
            """,
            (USER_KEY, TODAY.isoformat()),
        ) as cursor:
            assert tuple(await cursor.fetchone()) == ("completed", 1)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_scheduler_pages_all_relevant_items_before_completing(
    tmp_path,
    monkeypatch,
):
    cog, _bot, db = await _make_cog(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "SCHOOL_NOTICE_MAX_ITEMS_PER_DM", 2)
    delivery_day = TODAY + timedelta(days=1)
    now = datetime(
        delivery_day.year,
        delivery_day.month,
        delivery_day.day,
        9,
        0,
        tzinfo=ZoneInfo("Asia/Seoul"),
    )
    try:
        await _register_profile(cog)
        await _record_batch_run(cog, TODAY)

        assert await cog.process_due_deliveries(now=now) == 1
        async with db.execute(
            """
            SELECT status, attempt_count, next_attempt_at
            FROM school_notice_delivery_runs
            WHERE user_key = ? AND digest_date = ?
            """,
            (USER_KEY, TODAY.isoformat()),
        ) as cursor:
            status, attempts, next_attempt_at = await cursor.fetchone()
        assert (status, attempts) == ("pending", 0)
        assert next_attempt_at.endswith("09:01:00+09:00")
        async with db.execute(
            "SELECT COUNT(*) FROM school_notice_deliveries WHERE notice_id > 0"
        ) as cursor:
            assert (await cursor.fetchone())[0] == 2

        assert await cog.process_due_deliveries(
            now=now.replace(second=30)
        ) == 0
        assert await cog.process_due_deliveries(now=now.replace(minute=1)) == 1
        async with db.execute(
            """
            SELECT status FROM school_notice_delivery_runs
            WHERE user_key = ? AND digest_date = ?
            """,
            (USER_KEY, TODAY.isoformat()),
        ) as cursor:
            assert (await cursor.fetchone())[0] == "completed"
        async with db.execute(
            "SELECT COUNT(*) FROM school_notice_deliveries WHERE notice_id > 0"
        ) as cursor:
            assert (await cursor.fetchone())[0] == 4
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_scheduler_respects_custom_delivery_time(tmp_path, monkeypatch):
    cog, bot, db = await _make_cog(tmp_path, monkeypatch)
    delivery_day = TODAY + timedelta(days=1)
    try:
        await _register_profile(cog)
        await db.execute(
            "UPDATE school_notice_profiles SET delivery_time = '10:30' WHERE user_id = ?",
            (USER_ID,),
        )
        await db.commit()
        await _record_batch_run(cog, TODAY)
        morning = datetime(
            delivery_day.year,
            delivery_day.month,
            delivery_day.day,
            9,
            0,
            tzinfo=ZoneInfo("Asia/Seoul"),
        )
        assert await cog.process_due_deliveries(now=morning) == 0
        assert bot.user_obj.messages == []
        assert await cog.process_due_deliveries(
            now=morning.replace(hour=10, minute=30)
        ) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_healthy_empty_digest_completes_without_dm(tmp_path, monkeypatch):
    cog, bot, db = await _make_cog(
        tmp_path,
        monkeypatch,
        fixture="school_notice_digest_empty.json",
    )
    delivery_day = TODAY + timedelta(days=1)
    try:
        await _register_profile(cog)
        await _record_batch_run(cog, TODAY)
        now = datetime(
            delivery_day.year,
            delivery_day.month,
            delivery_day.day,
            9,
            0,
            tzinfo=ZoneInfo("Asia/Seoul"),
        )
        assert await cog.process_due_deliveries(now=now) == 1
        assert bot.user_obj.messages == []
        async with db.execute(
            """
            SELECT status FROM school_notice_delivery_runs
            WHERE user_key = ? AND digest_date = ?
            """,
            (USER_KEY, TODAY.isoformat()),
        ) as cursor:
            assert (await cursor.fetchone())[0] == "completed"
        assert await cog.process_due_deliveries(now=now.replace(minute=1)) == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_scheduler_retry_is_backed_off_and_stops_at_max_attempts(
    tmp_path,
    monkeypatch,
):
    cog, _bot, db = await _make_cog(tmp_path, monkeypatch)
    delivery_day = TODAY + timedelta(days=1)
    now = datetime(
        delivery_day.year,
        delivery_day.month,
        delivery_day.day,
        9,
        0,
        tzinfo=ZoneInfo("Asia/Seoul"),
    )
    try:
        await _register_profile(cog)
        # batch run이 없으므로 외부 호출 없이 영속 backoff만 진행한다.
        assert await cog.process_due_deliveries(now=now) == 1
        assert await cog.process_due_deliveries(now=now.replace(minute=9)) == 0
        assert await cog.process_due_deliveries(now=now.replace(minute=10)) == 1
        assert await cog.process_due_deliveries(now=now.replace(minute=29)) == 0
        assert await cog.process_due_deliveries(now=now.replace(minute=30)) == 1
        async with db.execute(
            """
            SELECT status, attempt_count, next_attempt_at
            FROM school_notice_delivery_runs
            WHERE user_key = ? AND digest_date = ?
            """,
            (USER_KEY, TODAY.isoformat()),
        ) as cursor:
            status, attempts, next_attempt = await cursor.fetchone()
        assert (status, attempts, next_attempt) == ("failed", 3, None)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_profile_changed_after_batch_is_cancelled_without_dm(
    tmp_path,
    monkeypatch,
):
    cog, bot, db = await _make_cog(tmp_path, monkeypatch)
    delivery_day = TODAY + timedelta(days=1)
    try:
        await _register_profile(cog)
        await _record_batch_run(cog, TODAY)
        await db.execute(
            "UPDATE school_notice_profiles SET updated_at = ? WHERE user_id = ?",
            (f"{TODAY.isoformat()}T23:20:00+09:00", USER_ID),
        )
        await db.commit()
        now = datetime(
            delivery_day.year,
            delivery_day.month,
            delivery_day.day,
            9,
            0,
            tzinfo=ZoneInfo("Asia/Seoul"),
        )
        assert await cog.process_due_deliveries(now=now) == 1
        assert bot.user_obj.messages == []
        async with db.execute(
            """
            SELECT status, last_error
            FROM school_notice_delivery_runs
            WHERE user_key = ? AND digest_date = ?
            """,
            (USER_KEY, TODAY.isoformat()),
        ) as cursor:
            assert tuple(await cursor.fetchone()) == ("cancelled", None)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_batch_snapshot_rejects_profile_change_in_same_timestamp_second(
    tmp_path,
    monkeypatch,
):
    cog, _bot, db = await _make_cog(tmp_path, monkeypatch)
    try:
        await _register_profile(cog)
        await _record_batch_run(cog, TODAY)
        async with db.execute(
            "SELECT profile_json FROM school_notice_profiles WHERE user_id = ?",
            (USER_ID,),
        ) as cursor:
            profile = json.loads((await cursor.fetchone())[0])
        profile["grade"] = 4
        await db.execute(
            """
            UPDATE school_notice_profiles
            SET profile_json = ?, profile_version = profile_version + 1,
                updated_at = ?
            WHERE user_id = ?
            """,
            (
                json.dumps(profile, ensure_ascii=False, sort_keys=True),
                f"{TODAY.isoformat()}T23:10:00+09:00",
                USER_ID,
            ),
        )
        await db.commit()

        assert (
            await cog._batch_ready_for_profile(USER_ID, USER_KEY, TODAY)
            == "profile_stale"
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_batch_snapshot_keeps_digest_ready_after_delivery_time_only_change(
    tmp_path,
    monkeypatch,
):
    cog, _bot, db = await _make_cog(tmp_path, monkeypatch)
    try:
        await _register_profile(cog)
        await _record_batch_run(cog, TODAY)
        async with db.execute(
            "SELECT profile_json FROM school_notice_profiles WHERE user_id = ?",
            (USER_ID,),
        ) as cursor:
            profile = json.loads((await cursor.fetchone())[0])
        profile["delivery_time"] = "10:30"
        await db.execute(
            """
            UPDATE school_notice_profiles
            SET profile_json = ?, delivery_time = ?
            WHERE user_id = ?
            """,
            (
                json.dumps(profile, ensure_ascii=False, sort_keys=True),
                "10:30",
                USER_ID,
            ),
        )
        await db.commit()

        assert (
            await cog._batch_ready_for_profile(USER_ID, USER_KEY, TODAY)
            == "ready"
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_profile_upsert_bumps_version_and_keeps_one_row(tmp_path, monkeypatch):
    cog, _bot, db = await _make_cog(tmp_path, monkeypatch)
    try:
        first = await cog.upsert_profile(
            USER_ID,
            {
                "user_key": USER_KEY,
                "school_id": "jbnu",
                "degree_level": "undergraduate",
                "grade": 3,
            },
        )
        legacy_without_run = await cog.upsert_profile(
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
        assert first.created
        assert first.needs_initial_collection
        assert not legacy_without_run.created
        assert legacy_without_run.needs_initial_collection
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_profile_with_completed_collection_does_not_repeat_initial_run(
    tmp_path,
    monkeypatch,
):
    cog, _bot, db = await _make_cog(tmp_path, monkeypatch)
    try:
        await _register_profile(cog)
        await _record_batch_run(cog, TODAY)

        result = await cog.upsert_profile(
            USER_ID,
            {
                "user_key": USER_KEY,
                "school_id": "jbnu",
                "degree_level": "undergraduate",
                "grade": 4,
            },
        )

        assert not result.created
        assert not result.needs_initial_collection
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_delivery_time_command_updates_only_delivery_setting(
    tmp_path,
    monkeypatch,
):
    cog, _bot, db = await _make_cog(tmp_path, monkeypatch)
    ctx = _FakeContext()
    try:
        await _register_profile(cog)
        async with db.execute(
            """
            SELECT profile_version, updated_at
            FROM school_notice_profiles WHERE user_id = ?
            """,
            (USER_ID,),
        ) as cursor:
            version_before, profile_updated_before = await cursor.fetchone()

        await SchoolNoticeCog.set_delivery_time.callback(
            cog,
            ctx,
            value="오전 10시 30분",
        )

        async with db.execute(
            """
            SELECT profile_json, delivery_time, profile_version, updated_at
            FROM school_notice_profiles WHERE user_id = ?
            """,
            (USER_ID,),
        ) as cursor:
            profile_json, delivery_time, version_after, profile_updated_after = (
                await cursor.fetchone()
            )
        assert delivery_time == "10:30"
        assert json.loads(profile_json)["delivery_time"] == "10:30"
        # 전달 시각만 바꾼 것은 이미 생성된 개인화 digest를 무효화하지 않는다.
        assert version_after == version_before
        assert profile_updated_after == profile_updated_before
    finally:
        await db.close()


class _CountingProfileLLM:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[str] = []

    def get_lane_targets(self, lane):
        assert lane == "routing"
        return [{"provider": "fake", "name": "routing.primary"}]

    async def call_routing_lane_target(self, _target, *, prompt, log_extra):
        assert log_extra == {"feature": "school_notice_profile"}
        self.calls.append(prompt)
        return self.response


class _SequencedProfileLLM(_CountingProfileLLM):
    def __init__(self, responses: list[str]) -> None:
        super().__init__("")
        self.responses = list(responses)

    async def call_routing_lane_target(self, target, *, prompt, log_extra):
        await super().call_routing_lane_target(
            target,
            prompt=prompt,
            log_extra=log_extra,
        )
        return self.responses[len(self.calls) - 1]


@pytest.mark.asyncio
async def test_natural_registration_requires_confirmation_and_stores_no_raw_text(
    tmp_path,
    monkeypatch,
):
    cog, bot, db = await _make_cog(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "SCHOOL_NOTICE_ENABLED", True)
    ctx = _FakeContext()
    raw_text = "비밀원문-local-parser가-모르는-학교설명"
    llm = _CountingProfileLLM(
        json.dumps(
            {
                "school_id": "jbnu",
                "degree_level": "undergraduate",
                "grade": 3,
                "preferred_topics": ["장학"],
                "delivery_time": "09:00",
            },
            ensure_ascii=False,
        )
    )
    bot.cogs["AIHandler"] = SimpleNamespace(llm_client=llm)
    bot.wait_messages = [_FakeMessage("맞아")]
    try:
        await SchoolNoticeCog.register.callback(
            cog,
            ctx,
            profile_text=raw_text,
        )

        assert len(llm.calls) == 1
        assert any("제가 이렇게 이해했어요. 맞을까요?" in str(item["content"]) for item in ctx.messages)
        async with db.execute(
            "SELECT profile_json, delivery_time FROM school_notice_profiles WHERE user_id = ?",
            (USER_ID,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert raw_text not in row[0]
        stored = json.loads(row[0])
        assert stored["school_id"] == "jbnu"
        assert stored["preferred_topics"] == ["장학"]
        assert row[1] == "09:00"
        assert USER_ID not in bot.locked_users
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_natural_registration_can_be_corrected_then_confirmed_without_llm(
    tmp_path,
    monkeypatch,
):
    cog, bot, db = await _make_cog(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "SCHOOL_NOTICE_ENABLED", True)
    ctx = _FakeContext()
    llm = _CountingProfileLLM("{}")
    bot.cogs["AIHandler"] = SimpleNamespace(llm_client=llm)
    bot.wait_messages = [
        _FakeMessage("장학 공지도 관심 있어"),
        _FakeMessage("확인"),
    ]
    try:
        await SchoolNoticeCog.register.callback(
            cog,
            ctx,
            profile_text="전북대 3학년이고 오전 9시에 알려줘",
        )

        assert llm.calls == []
        async with db.execute(
            "SELECT profile_json FROM school_notice_profiles WHERE user_id = ?",
            (USER_ID,),
        ) as cursor:
            stored = json.loads((await cursor.fetchone())[0])
        assert stored["preferred_topics"] == ["장학"]
        assert stored["grade"] == 3
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_natural_registration_applies_topic_exclusion_and_only_intent(
    tmp_path,
    monkeypatch,
):
    cog, bot, db = await _make_cog(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "SCHOOL_NOTICE_ENABLED", True)
    ctx = _FakeContext()
    llm = _CountingProfileLLM("{}")
    bot.cogs["AIHandler"] = SimpleNamespace(llm_client=llm)
    bot.wait_messages = [
        _FakeMessage("장학은 빼고 취업만 알려줘"),
        _FakeMessage("맞아"),
    ]
    try:
        await SchoolNoticeCog.register.callback(
            cog,
            ctx,
            profile_text="전북대 3학년이고 장학·인턴 공지를 오전 9시에 알려줘",
        )

        assert llm.calls == []
        async with db.execute(
            "SELECT profile_json FROM school_notice_profiles WHERE user_id = ?",
            (USER_ID,),
        ) as cursor:
            stored = json.loads((await cursor.fetchone())[0])
        assert stored["preferred_topics"] == ["취업"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_profile_session_has_hard_total_llm_call_cap(tmp_path, monkeypatch):
    cog, bot, db = await _make_cog(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "SCHOOL_NOTICE_ENABLED", True)
    ctx = _FakeContext()
    llm = _SequencedProfileLLM(
        [
            json.dumps(
                {
                    "school_id": "jbnu",
                    "degree_level": "undergraduate",
                    "grade": 3,
                    "delivery_time": "09:00",
                }
            ),
            json.dumps({"preferred_topics": ["장학"]}, ensure_ascii=False),
            json.dumps({"delivery_time": "10:00"}),
        ]
    )
    bot.cogs["AIHandler"] = SimpleNamespace(llm_client=llm)
    bot.wait_messages = [
        _FakeMessage("무언가 바꿔줘"),
        _FakeMessage("또 바꿔줘"),
        _FakeMessage("다시 바꿔줘"),
        _FakeMessage("확인"),
    ]
    try:
        await SchoolNoticeCog.register.callback(
            cog,
            ctx,
            profile_text="로컬로는 학교를 찾을 수 없는 입력",
        )
        assert len(llm.calls) == 3
        async with db.execute(
            "SELECT COUNT(*) FROM school_notice_profiles WHERE user_id = ?",
            (USER_ID,),
        ) as cursor:
            assert (await cursor.fetchone())[0] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_profile_llm_is_not_called_when_quota_reservation_fails(
    tmp_path,
    monkeypatch,
):
    cog, bot, db = await _make_cog(tmp_path, monkeypatch)
    llm = _CountingProfileLLM("{}")
    bot.cogs["AIHandler"] = SimpleNamespace(llm_client=llm)

    async def fail_reservation(*_args, **_kwargs):
        raise RuntimeError("injected reservation failure")

    monkeypatch.setattr(db, "executemany", fail_reservation)
    cog._profile_llm_calls[USER_ID] = 0
    try:
        assert await cog._call_profile_llm("provider payload", user_id=USER_ID) is None
        assert llm.calls == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_natural_registration_cancel_stores_nothing(tmp_path, monkeypatch):
    cog, bot, db = await _make_cog(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "SCHOOL_NOTICE_ENABLED", True)
    ctx = _FakeContext()
    bot.wait_messages = [_FakeMessage("취소")]
    try:
        await SchoolNoticeCog.register.callback(
            cog,
            ctx,
            profile_text="전북대 3학년이고 오전 9시에 알려줘",
        )
        async with db.execute(
            "SELECT COUNT(*) FROM school_notice_profiles"
        ) as cursor:
            assert (await cursor.fetchone())[0] == 0
        assert USER_ID not in bot.locked_users
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_no_consent_never_collects_input_or_calls_profile_llm(
    tmp_path,
    monkeypatch,
):
    cog, bot, db = await _make_cog(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "SCHOOL_NOTICE_ENABLED", True)
    ctx = _FakeContext()
    llm = _CountingProfileLLM("{}")
    bot.cogs["AIHandler"] = SimpleNamespace(llm_client=llm)
    await withdraw_consent(db, USER_ID, SCHOOL_NOTICE_SCOPE)
    try:
        await SchoolNoticeCog.register.callback(
            cog,
            ctx,
            profile_text="비밀 학교 정보",
        )
        assert llm.calls == []
        assert bot.wait_messages == []
        async with db.execute(
            "SELECT COUNT(*) FROM school_notice_profiles"
        ) as cursor:
            assert (await cursor.fetchone())[0] == 0
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


@pytest.mark.asyncio
async def test_withdrawal_blocks_feedback_delivery_and_active_profile(
    tmp_path,
    monkeypatch,
):
    cog, bot, db = await _make_cog(tmp_path, monkeypatch)
    try:
        await _register_profile(cog)
        await withdraw_consent(db, USER_ID, SCHOOL_NOTICE_SCOPE)

        with pytest.raises(ConsentRequiredError):
            await cog.record_feedback(
                user_id=USER_ID,
                source_id="jbnu_software",
                external_id="1",
                feedback_type="useful",
                interaction_id="withdrawn-interaction",
            )
        assert await cog.deliver_to_user(USER_ID, USER_KEY, TODAY) == "consent_required"
        assert bot.user_obj.messages == []
        assert await cog.active_profiles() == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_delete_removes_delivery_runs_but_preserves_consent_audit(
    tmp_path,
    monkeypatch,
):
    cog, _bot, db = await _make_cog(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "SCHOOL_NOTICE_CORE_DB", "")
    ctx = _FakeContext()
    try:
        await _register_profile(cog)
        await db.execute(
            """
            INSERT INTO school_notice_delivery_runs
                (user_key, digest_date, status, attempt_count, updated_at)
            VALUES (?, ?, 'retry', 1, ?)
            """,
            (USER_KEY, TODAY.isoformat(), f"{TODAY.isoformat()}T09:00:00+09:00"),
        )
        await db.commit()

        await SchoolNoticeCog.delete_profile.callback(cog, ctx)

        for table in (
            "school_notice_profiles",
            "school_notice_feedback",
            "school_notice_deliveries",
            "school_notice_delivery_runs",
            "school_notice_batch_runs",
        ):
            async with db.execute(f"SELECT COUNT(*) FROM {table}") as cursor:
                assert (await cursor.fetchone())[0] == 0
        async with db.execute(
            """
            SELECT COUNT(*) FROM privacy_consent_events
            WHERE user_id = ? AND scope = ?
            """,
            (USER_ID, SCHOOL_NOTICE_SCOPE),
        ) as cursor:
            assert (await cursor.fetchone())[0] >= 2
        assert any("감사 이력은 보존" in str(item["content"]) for item in ctx.messages)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_natural_school_notice_request_routes_without_general_ai(
    tmp_path,
    monkeypatch,
):
    cog, bot, db = await _make_cog(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "SCHOOL_NOTICE_ENABLED", True)
    ctx = _FakeContext()
    bot.context = ctx
    routed = []

    async def fake_begin(
        received_ctx,
        *,
        initial_text,
        prefer_existing,
    ):
        routed.append((received_ctx, initial_text, prefer_existing))

    monkeypatch.setattr(cog, "begin_profile_setup", fake_begin)
    try:
        handled = await cog.try_handle_natural_message(
            _FakeMessage(
                "전북대 소프트웨어공학과 3학년 공지를 오전 9시에 알려줘"
            )
        )

        assert handled is True
        assert routed == [
            (
                ctx,
                "전북대 소프트웨어공학과 3학년 공지를 오전 9시에 알려줘",
                False,
            )
        ]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_bare_notice_command_opens_unified_dashboard(
    tmp_path,
    monkeypatch,
):
    cog, _bot, db = await _make_cog(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "SCHOOL_NOTICE_ENABLED", True)
    ctx = _FakeContext()
    try:
        await SchoolNoticeCog.school_notice.callback(cog, ctx)

        assert len(ctx.messages) == 1
        message = ctx.messages[0]
        assert message["embed"].title == "🎓 학교 공지"
        assert "05:00" in message["embed"].description
        labels = {
            child.label
            for child in message["view"].children
            if isinstance(child, discord.ui.Button)
        }
        assert labels == {
            "설정·변경",
            "최근 공지",
            "수집 상태",
            "알림 시간",
            "내 설정",
            "알림 재개",
        }
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_dashboard_does_not_claim_profile_was_lost_before_reconsent(
    tmp_path,
    monkeypatch,
):
    cog, _bot, db = await _make_cog(tmp_path, monkeypatch)
    await _register_profile(cog)
    await withdraw_consent(db, USER_ID, SCHOOL_NOTICE_SCOPE)
    monkeypatch.setattr(config, "SCHOOL_NOTICE_ENABLED", True)
    ctx = _FakeContext()
    try:
        await cog.send_dashboard(ctx)

        embed = ctx.messages[0]["embed"]
        state = next(
            field.value for field in embed.fields if field.name == "현재 상태"
        )
        assert state == "동의 확인 필요"
        assert "설정 전" not in state
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_initial_collection_is_one_bounded_user_scoped_no_llm_run(
    tmp_path,
    monkeypatch,
):
    cog, _bot, db = await _make_cog(tmp_path, monkeypatch)
    commands_seen = []

    class FakeStatusMessage:
        def __init__(self):
            self.edits = []

        async def edit(self, **kwargs):
            self.edits.append(kwargs)

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_create_subprocess_exec(*command, **kwargs):
        commands_seen.append((command, kwargs))
        return FakeProcess()

    monkeypatch.setattr(
        school_notice_module.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(config, "PROJECT_ROOT", ROOT)
    monkeypatch.setattr(
        config,
        "SCHOOL_NOTICE_SOURCE_CONFIG",
        str(ROOT / "school_notice" / "sources.json"),
    )
    monkeypatch.setattr(config, "SCHOOL_NOTICE_INITIAL_CRAWL_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(config, "SCHOOL_NOTICE_INITIAL_CRAWL_TIMEOUT_SECONDS", 30)
    monkeypatch.setattr(config, "SCHOOL_NOTICE_INITIAL_CRAWL_RETRY_SECONDS", 5)
    cog.deliver_to_user = AsyncMock(return_value="nothing_to_send")
    status = FakeStatusMessage()
    try:
        await cog._run_initial_collection(
            user_id=USER_ID,
            status_message=status,
        )

        assert len(commands_seen) == 1
        command = list(commands_seen[0][0])
        assert command[command.index("--only-user-id") + 1] == str(USER_ID)
        assert command[command.index("--max-profiles") + 1] == "1"
        assert "--no-llm" in command
        assert "--low-resource" in command
        assert any("관련 공지가 없으면" in edit["content"] for edit in status.edits)
        cog.deliver_to_user.assert_awaited_once()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ambiguous_school_chat_and_locked_user_are_not_intercepted(
    tmp_path,
    monkeypatch,
):
    cog, bot, db = await _make_cog(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "SCHOOL_NOTICE_ENABLED", True)
    bot.context = _FakeContext()
    try:
        assert not await cog.try_handle_natural_message(
            _FakeMessage("학교 공지가 왜 이렇게 늦게 올라오지?")
        )
        bot.locked_users.add(USER_ID)
        assert not await cog.try_handle_natural_message(
            _FakeMessage("학교 공지 설정")
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_profile_is_not_read_until_consent_then_original_setup_resumes(
    tmp_path,
    monkeypatch,
):
    cog, _bot, db = await _make_cog(tmp_path, monkeypatch)
    await withdraw_consent(db, USER_ID, SCHOOL_NOTICE_SCOPE)
    ctx = _FakeContext()
    prompt = {}
    profile_reads = []
    sessions = []

    async def fake_prompt(_ctx, *, on_granted=None):
        prompt["callback"] = on_granted

    async def fake_profile_row(user_id):
        profile_reads.append(user_id)
        return None

    async def fake_session(_ctx, *, initial_text, current_profile):
        sessions.append((initial_text, current_profile))

    monkeypatch.setattr(cog, "_send_school_notice_consent_prompt", fake_prompt)
    monkeypatch.setattr(cog, "_profile_row", fake_profile_row)
    monkeypatch.setattr(cog, "_run_profile_session", fake_session)
    monkeypatch.setattr(config, "SCHOOL_NOTICE_ENABLED", True)
    try:
        await cog.begin_profile_setup(
            ctx,
            initial_text="전북대 3학년",
            prefer_existing=True,
        )

        assert profile_reads == []
        assert sessions == []
        assert prompt["callback"] is not None

        await grant_consent(db, USER_ID, SCHOOL_NOTICE_SCOPE)
        await prompt["callback"](SimpleNamespace())

        assert profile_reads == [USER_ID]
        assert sessions == [("전북대 3학년", None)]
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

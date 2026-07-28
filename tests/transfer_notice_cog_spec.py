"""편입 공지 DM 전용·동의·구독·중복 방지 명세."""

import json
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import discord
import pytest
from discord.ext import commands

import config
from cogs.transfer_notice_cog import TransferNoticeCog
from utils.privacy_consent import (
    TRANSFER_NOTICE_SCOPE,
    grant_consent,
    withdraw_consent,
)


ROOT = Path(__file__).resolve().parents[1]
USER_ID = 100000000000000001


class _FakeUser:
    def __init__(self) -> None:
        self.messages: list[tuple[object, dict]] = []

    async def send(self, content=None, **kwargs):
        self.messages.append((content, kwargs))


class _FakeBot:
    def __init__(self, db) -> None:
        self.db = db
        self.user = _FakeUser()
        self.cogs: dict[str, object] = {}

    def get_user(self, user_id):
        return self.user if int(user_id) == USER_ID else None

    async def fetch_user(self, user_id):
        assert int(user_id) == USER_ID
        return self.user

    def get_cog(self, name):
        return self.cogs.get(name)


class _Destination:
    def __init__(self, *, guild=None) -> None:
        self.guild = guild
        self.author = SimpleNamespace(id=USER_ID)
        self.messages: list[dict] = []

    async def send(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})


async def _make_cog(tmp_path, monkeypatch):
    db = await aiosqlite.connect(":memory:")
    await db.executescript(
        (ROOT / "database" / "schema.sql").read_text(encoding="utf-8")
    )
    await db.commit()
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    monkeypatch.setattr(config, "TRANSFER_NOTICE_OUTPUT_DIR", str(output_dir))
    monkeypatch.setattr(config, "TRANSFER_NOTICE_SOURCE_CONFIG", str(
        ROOT / "transfer_notice" / "sources.json"
    ))
    monkeypatch.setattr(config, "TRANSFER_NOTICE_DELIVERY_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(config, "TRANSFER_NOTICE_DELIVERY_RETRY_MINUTES", 5)
    monkeypatch.setattr(config, "TRANSFER_NOTICE_MAX_ITEMS_PER_DM", 10)
    bot = _FakeBot(db)
    return TransferNoticeCog(bot), bot, db


def _write_output(cog, *, changes):
    payload = {
        "schema_version": 1,
        "run_id": "run-1",
        "generated_at": "2099-01-01T00:00:00+00:00",
        "status": "succeeded",
        "source_count": 20,
        "healthy_count": 20,
        "changes": changes,
        "latest": changes,
    }
    cog.output_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def _change(source_id="kangwon"):
    return {
        "source_id": source_id,
        "university": "강원대학교",
        "external_id": "notice-1",
        "title": "2027학년도 편입학 전형 일정 안내",
        "url": "https://admission.kangwon.ac.kr/notice/1",
        "published_date": "2026-07-28",
        "fingerprint": "f1",
        "change_type": "new",
        "revision": 1,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command_name",
    ["transfer", "recent", "status", "unsubscribe", "resume", "delete"],
)
async def test_every_transfer_command_rejects_guild_context(command_name):
    command = getattr(TransferNoticeCog, command_name)
    guild_context = SimpleNamespace(guild=SimpleNamespace(id=1))

    with pytest.raises(commands.PrivateMessageOnly):
        await command.checks[0](guild_context)


@pytest.mark.asyncio
async def test_dashboard_has_defense_in_depth_and_never_reads_guild_profile(
    tmp_path,
    monkeypatch,
):
    cog, _bot, db = await _make_cog(tmp_path, monkeypatch)
    try:
        destination = _Destination(guild=SimpleNamespace(id=1))

        await cog.send_dashboard(destination)

        assert len(destination.messages) == 1
        assert "DM" in destination.messages[0]["content"]
        async with db.execute(
            "SELECT COUNT(*) FROM transfer_notice_subscriptions"
        ) as cursor:
            assert (await cursor.fetchone())[0] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_active_subscription_without_current_consent_is_never_selected(
    tmp_path,
    monkeypatch,
):
    cog, _bot, db = await _make_cog(tmp_path, monkeypatch)
    try:
        now = "2026-07-28T00:00:00+00:00"
        await db.execute(
            """
            INSERT INTO transfer_notice_subscriptions
                (user_id, schools_json, enabled, created_at, updated_at)
            VALUES (?, ?, 1, ?, ?)
            """,
            (USER_ID, '["kangwon"]', now, now),
        )
        await db.commit()

        rows = await cog._subscriber_rows("2099-01-01T00:00:00+00:00")

        assert rows == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_canceled_or_withdrawn_subscription_is_never_selected(
    tmp_path,
    monkeypatch,
):
    cog, _bot, db = await _make_cog(tmp_path, monkeypatch)
    try:
        await grant_consent(db, USER_ID, TRANSFER_NOTICE_SCOPE)
        await cog._save_subscription(USER_ID, {"kangwon"})
        await cog._set_enabled(USER_ID, False)
        assert await cog._subscriber_rows("2099-01-01T00:00:00+00:00") == []

        await cog._set_enabled(USER_ID, True)
        await withdraw_consent(db, USER_ID, TRANSFER_NOTICE_SCOPE)
        assert await cog._subscriber_rows("2099-01-01T00:00:00+00:00") == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_subscription_changed_after_batch_does_not_receive_old_change(
    tmp_path,
    monkeypatch,
):
    cog, _bot, db = await _make_cog(tmp_path, monkeypatch)
    try:
        await grant_consent(db, USER_ID, TRANSFER_NOTICE_SCOPE)
        await db.execute(
            """
            INSERT INTO transfer_notice_subscriptions (
                user_id, schools_json, enabled, created_at, updated_at
            ) VALUES (?, '["kangwon"]', 1, ?, ?)
            """,
            (
                USER_ID,
                "2026-07-27T00:00:00+00:00",
                "2026-07-29T00:00:00+00:00",
            ),
        )
        await db.commit()

        rows = await cog._subscriber_rows("2026-07-28T23:35:00+00:00")

        assert rows == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cancel_after_candidate_query_blocks_send(
    tmp_path,
    monkeypatch,
):
    cog, bot, db = await _make_cog(tmp_path, monkeypatch)
    try:
        await grant_consent(db, USER_ID, TRANSFER_NOTICE_SCOPE)
        await cog._save_subscription(USER_ID, {"kangwon"})
        rows = await cog._subscriber_rows("2099-01-01T00:00:00+00:00")
        assert len(rows) == 1
        await cog._set_enabled(USER_ID, False)

        result = await cog._send_user_changes(
            USER_ID,
            "run-race",
            [_change()],
            expected_subscription_updated_at=str(rows[0][2]),
        )

        assert result == "subscription_changed"
        assert bot.user.messages == []
        async with db.execute(
            "SELECT COUNT(*) FROM transfer_notice_deliveries"
        ) as cursor:
            assert (await cursor.fetchone())[0] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_new_notice_is_sent_once_only_to_active_consented_subscriber(
    tmp_path,
    monkeypatch,
):
    cog, bot, db = await _make_cog(tmp_path, monkeypatch)
    try:
        await grant_consent(db, USER_ID, TRANSFER_NOTICE_SCOPE)
        await cog._save_subscription(USER_ID, {"kangwon"})
        _write_output(cog, changes=[_change()])

        first = await cog._delivery_tick()
        second = await cog._delivery_tick()

        assert first == "sent"
        assert second == "idle"
        assert len(bot.user.messages) == 1
        assert "2027학년도 편입학 전형 일정 안내" in bot.user.messages[0][0]
        async with db.execute(
            """
            SELECT status, attempt_count
            FROM transfer_notice_deliveries
            WHERE user_id = ?
            """,
            (USER_ID,),
        ) as cursor:
            assert await cursor.fetchone() == ("sent", 1)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_long_briefing_is_paginated_without_discord_overflow_or_duplicates(
    tmp_path,
    monkeypatch,
):
    cog, bot, db = await _make_cog(tmp_path, monkeypatch)
    try:
        await grant_consent(db, USER_ID, TRANSFER_NOTICE_SCOPE)
        await cog._save_subscription(USER_ID, {"kangwon"})
        changes = []
        for index in range(10):
            item = _change()
            item.update(
                external_id=f"notice-{index}",
                title=(
                    f"2027학년도 편입학 전형 일정 안내 {index} "
                    + "공인영어 성적 제출과 모집단위별 지원 자격을 반드시 확인하세요 " * 2
                ),
                url=f"https://admission.kangwon.ac.kr/notice/{index}",
                fingerprint=f"f-{index}",
            )
            changes.append(item)
        _write_output(cog, changes=changes)

        for _ in range(5):
            await cog._delivery_tick()
            async with db.execute(
                """
                SELECT COUNT(*)
                FROM transfer_notice_deliveries
                WHERE user_id = ? AND status = 'sent'
                """,
                (USER_ID,),
            ) as cursor:
                if (await cursor.fetchone())[0] == len(changes):
                    break

        assert all(
            isinstance(content, str) and len(content) <= 1900
            for content, _kwargs in bot.user.messages
        )
        sent_text = "\n".join(content for content, _kwargs in bot.user.messages)
        for index in range(10):
            assert sent_text.count(f"일정 안내 {index} ") == 1
        async with db.execute(
            """
            SELECT COUNT(*)
            FROM transfer_notice_deliveries
            WHERE user_id = ? AND status = 'sent'
            """,
            (USER_ID,),
        ) as cursor:
            assert (await cursor.fetchone())[0] == len(changes)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_invalid_retry_payload_is_terminal_instead_of_looping_forever(
    tmp_path,
    monkeypatch,
):
    cog, _bot, db = await _make_cog(tmp_path, monkeypatch)
    try:
        await grant_consent(db, USER_ID, TRANSFER_NOTICE_SCOPE)
        await cog._save_subscription(USER_ID, {"kangwon"})
        async with db.execute(
            "SELECT updated_at FROM transfer_notice_subscriptions WHERE user_id = ?",
            (USER_ID,),
        ) as cursor:
            updated_at = (await cursor.fetchone())[0]
        await db.execute(
            """
            INSERT INTO transfer_notice_deliveries (
                user_id, run_id, source_id, external_id, revision,
                payload_json, status, attempt_count, next_attempt_at, updated_at
            ) VALUES (?, 'run-x', 'kangwon', 'broken', 1,
                      '{broken-json', 'retry', 1, NULL, ?)
            """,
            (USER_ID, updated_at),
        )
        await db.commit()

        result = await cog._retry_delivery_tick()

        assert result == "invalid"
        async with db.execute(
            """
            SELECT status, last_error
            FROM transfer_notice_deliveries
            WHERE user_id = ? AND external_id = 'broken'
            """,
            (USER_ID,),
        ) as cursor:
            assert await cursor.fetchone() == (
                "failed",
                "invalid_retry_payload",
            )
    finally:
        await db.close()

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

import cogs.fortune_cog as fortune_module
from cogs.ai_handler import AIHandler
from cogs.fortune_cog import FortuneCog
from utils.privacy_consent import (
    FORTUNE_SCOPE,
    get_consent_state,
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
    await db.execute(
        """
        INSERT INTO user_profiles (
            user_id, birth_date, birth_time, gender, birth_place,
            last_fortune_content
        ) VALUES (?, '2000-01-01', NULL, NULL, NULL, 'stored fortune')
        """,
        (USER_ID,),
    )
    await db.commit()
    return db


@pytest.mark.asyncio
async def test_fortune_check_without_consent_does_not_read_profile_or_call_ai():
    class _FailDB:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("profile DB must not be read without consent")

    bot = SimpleNamespace(db=_FailDB(), get_cog=AsyncMock())
    cog = FortuneCog.__new__(FortuneCog)
    cog.bot = bot
    cog._has_fortune_consent = AsyncMock(return_value=False)
    cog._send_fortune_consent_prompt = AsyncMock()
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=USER_ID),
        channel=object(),
    )

    await cog._check_fortune_logic(ctx)

    cog._send_fortune_consent_prompt.assert_awaited_once()
    bot.get_cog.assert_not_called()


@pytest.mark.asyncio
async def test_fortune_rechecks_consent_immediately_before_provider_and_reservation():
    class _Typing:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return None

    class _DM:
        def typing(self):
            return _Typing()

    class _AI:
        def __init__(self):
            self.calls = 0

        async def _cometapi_generate_content(self, *_args, **_kwargs):
            self.calls += 1
            return "must not run"

    class _Calculator:
        def get_comprehensive_info(self, *_args, **_kwargs):
            return "fortune data"

        def _get_astrology_chart(self, _now):
            return "chart"

    db = await _db()
    try:
        ai = _AI()
        cog = FortuneCog.__new__(FortuneCog)
        cog.bot = SimpleNamespace(
            db=db,
            get_cog=lambda name: ai if name == "AIHandler" else None,
        )
        cog.calculator = _Calculator()
        cog._has_fortune_consent = AsyncMock(side_effect=[True, False])
        cog._send_fortune_consent_prompt = AsyncMock()
        ctx = SimpleNamespace(
            author=SimpleNamespace(id=USER_ID, display_name="tester"),
            channel=_DM(),
            guild=None,
            typing=lambda: _Typing(),
            send=AsyncMock(),
        )
        status = SimpleNamespace(edit=AsyncMock())

        await cog._check_fortune_logic(ctx, status_msg=status)

        assert ai.calls == 0
        cog._send_fortune_consent_prompt.assert_awaited_once_with(
            ctx,
            status_msg=status,
        )
        async with db.execute(
            "SELECT COUNT(*) FROM api_call_log WHERE api_type = ?",
            (f"fortune_detail_{USER_ID}",),
        ) as cursor:
            assert (await cursor.fetchone())[0] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_withdrawal_during_provider_blocks_personal_response_and_context_write(
    monkeypatch,
):
    class _Typing:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return None

    class _DM:
        def typing(self):
            return _Typing()

    class _Calculator:
        def get_comprehensive_info(self, *_args, **_kwargs):
            return "fortune data"

        def _get_astrology_chart(self, _now):
            return "chart"

    db = await _db()
    try:
        await grant_consent(db, USER_ID, FORTUNE_SCOPE)

        class _AI:
            calls = 0

            async def _cometapi_generate_content(self, *_args, **_kwargs):
                self.calls += 1
                await withdraw_consent(db, USER_ID, FORTUNE_SCOPE)
                return "must not be delivered"

        monkeypatch.setattr(fortune_module.discord, "DMChannel", _DM)
        ai = _AI()
        cog = FortuneCog.__new__(FortuneCog)
        cog.bot = SimpleNamespace(
            db=db,
            get_cog=lambda name: ai if name == "AIHandler" else None,
        )
        cog.calculator = _Calculator()
        ctx = SimpleNamespace(
            author=SimpleNamespace(id=USER_ID, display_name="tester"),
            channel=_DM(),
            guild=None,
            typing=lambda: _Typing(),
            send=AsyncMock(),
        )
        status = SimpleNamespace(edit=AsyncMock())

        await cog._check_fortune_logic(
            ctx,
            "상세",
            status_msg=status,
        )

        assert ai.calls == 1
        assert "must not be delivered" not in str(status.edit.await_args_list)
        assert "철회" in str(status.edit.await_args.kwargs.get("content"))
        async with db.execute(
            "SELECT last_fortune_content FROM user_profiles WHERE user_id = ?",
            (USER_ID,),
        ) as cursor:
            assert (await cursor.fetchone())[0] == "stored fortune"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ai_fortune_context_is_available_only_while_currently_consented():
    db = await _db()
    try:
        handler = AIHandler.__new__(AIHandler)
        handler.bot = SimpleNamespace(db=db)

        assert await handler._fortune_context_with_consent(USER_ID) is None
        await grant_consent(db, USER_ID, FORTUNE_SCOPE)
        assert (
            await handler._fortune_context_with_consent(USER_ID)
            == "stored fortune"
        )
        await withdraw_consent(db, USER_ID, FORTUNE_SCOPE)
        assert await handler._fortune_context_with_consent(USER_ID) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_context_write_is_blocked_after_withdrawal():
    db = await _db()
    try:
        await grant_consent(db, USER_ID, FORTUNE_SCOPE)
        await withdraw_consent(db, USER_ID, FORTUNE_SCOPE)
        cog = FortuneCog.__new__(FortuneCog)
        cog.bot = SimpleNamespace(db=db)

        await cog._update_last_fortune_context(USER_ID, "must not be saved")

        async with db.execute(
            "SELECT last_fortune_content FROM user_profiles WHERE user_id = ?",
            (USER_ID,),
        ) as cursor:
            assert (await cursor.fetchone())[0] == "stored fortune"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_fortune_subscription_rechecks_consent_before_enabling(
    monkeypatch,
):
    db = await _db()
    try:
        monkeypatch.setattr(
            fortune_module.config,
            "FORTUNE_MORNING_BRIEFING_ENABLED",
            True,
        )
        cog = FortuneCog.__new__(FortuneCog)
        cog.bot = SimpleNamespace(db=db)
        # 애플리케이션 확인이 둘 다 stale True를 반환해도 UPDATE 자체의
        # consent 조건이 저장을 막아야 한다.
        cog._has_fortune_consent = AsyncMock(side_effect=[True, True])
        cog._send_fortune_consent_prompt = AsyncMock()
        ctx = SimpleNamespace(
            author=SimpleNamespace(id=USER_ID),
            guild=None,
            send=AsyncMock(),
            reply=AsyncMock(),
        )

        kst = fortune_module.pytz.timezone("Asia/Seoul")
        safe_time = (
            fortune_module.datetime.now(kst)
            + fortune_module.timedelta(minutes=10)
        ).strftime("%H:%M")
        await FortuneCog.fortune_subscribe.callback(cog, ctx, safe_time)

        cog._send_fortune_consent_prompt.assert_awaited_once_with(ctx)
        async with db.execute(
            """
            SELECT subscription_active, subscription_time
            FROM user_profiles
            WHERE user_id = ?
            """,
            (USER_ID,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row[0] == 0
        assert row[1] != safe_time
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_fortune_subscription_activates_with_current_consent(
    monkeypatch,
):
    db = await _db()
    try:
        await grant_consent(db, USER_ID, FORTUNE_SCOPE)
        monkeypatch.setattr(
            fortune_module.config,
            "FORTUNE_MORNING_BRIEFING_ENABLED",
            True,
        )
        cog = FortuneCog.__new__(FortuneCog)
        cog.bot = SimpleNamespace(db=db, get_cog=lambda _name: None)
        cog._send_fortune_consent_prompt = AsyncMock()
        ctx = SimpleNamespace(
            author=SimpleNamespace(id=USER_ID),
            guild=None,
            send=AsyncMock(),
            reply=AsyncMock(),
        )
        kst = fortune_module.pytz.timezone("Asia/Seoul")
        safe_time = (
            fortune_module.datetime.now(kst)
            + fortune_module.timedelta(minutes=10)
        ).strftime("%H:%M")

        await FortuneCog.fortune_subscribe.callback(cog, ctx, safe_time)
        # 같은 값을 다시 설정해도 MySQL/TiDB의 changed-row rowcount와
        # 무관하게 유효한 활성 상태로 판정해야 한다.
        await FortuneCog.fortune_subscribe.callback(cog, ctx, safe_time)

        cog._send_fortune_consent_prompt.assert_not_awaited()
        async with db.execute(
            """
            SELECT subscription_active, subscription_time
            FROM user_profiles
            WHERE user_id = ?
            """,
            (USER_ID,),
        ) as cursor:
            row = await cursor.fetchone()
        assert row == (1, safe_time)
        assert ctx.send.await_count == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_fortune_delete_removes_profile_but_keeps_general_conversation():
    db = await _db()
    try:
        await grant_consent(db, USER_ID, FORTUNE_SCOPE)
        await db.execute(
            """
            INSERT INTO conversation_history (
                message_id, guild_id, channel_id, user_id, user_name,
                content, is_bot, created_at
            ) VALUES (1, 2, 3, ?, 'user', 'ordinary conversation', 0, '2026-07-28')
            """,
            (USER_ID,),
        )
        await db.commit()

        cog = FortuneCog.__new__(FortuneCog)
        cog.bot = SimpleNamespace(db=db)
        ctx = SimpleNamespace(
            author=SimpleNamespace(id=USER_ID),
            guild=None,
            send=AsyncMock(),
            reply=AsyncMock(),
        )
        await FortuneCog.fortune_delete.callback(cog, ctx)

        async with db.execute(
            "SELECT COUNT(*) FROM user_profiles WHERE user_id = ?",
            (USER_ID,),
        ) as cursor:
            assert (await cursor.fetchone())[0] == 0
        async with db.execute(
            "SELECT content FROM conversation_history WHERE message_id = 1"
        ) as cursor:
            assert (await cursor.fetchone())[0] == "ordinary conversation"
        state = await get_consent_state(db, USER_ID, FORTUNE_SCOPE)
        assert state is not None
        assert not await has_current_consent(db, USER_ID, FORTUNE_SCOPE)
        async with db.execute(
            """
            SELECT COUNT(*) FROM privacy_consent_events
            WHERE user_id = ? AND scope = ?
            """,
            (USER_ID, FORTUNE_SCOPE),
        ) as cursor:
            assert (await cursor.fetchone())[0] == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_all_personal_modes_share_serialized_failure_counting_quota(
    monkeypatch,
):
    class _Typing:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return None

    class _DM:
        def typing(self):
            return _Typing()

    class _AI:
        def __init__(self):
            self.calls = 0
            self.max_active = 0
            self.active = 0

        async def _cometapi_generate_content(self, *_args, **_kwargs):
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.01)
                raise RuntimeError("provider failure")
            finally:
                self.active -= 1

    class _Calculator:
        def get_comprehensive_info(self, *_args, **_kwargs):
            return "fortune data"

        def _get_astrology_chart(self, _now):
            return "chart"

    monkeypatch.setattr(fortune_module.discord, "DMChannel", _DM)
    db = await _db()
    try:
        await grant_consent(db, USER_ID, FORTUNE_SCOPE)
        ai = _AI()
        cog = FortuneCog.__new__(FortuneCog)
        cog.bot = SimpleNamespace(
            db=db,
            get_cog=lambda name: ai if name == "AIHandler" else None,
        )
        cog.calculator = _Calculator()
        ctx = SimpleNamespace(
            author=SimpleNamespace(id=USER_ID, display_name="tester"),
            channel=_DM(),
            guild=None,
            typing=lambda: _Typing(),
            send=AsyncMock(),
        )
        statuses = [
            SimpleNamespace(edit=AsyncMock())
            for _ in range(4)
        ]
        requests = [
            (None, "day"),
            ("상세", "day"),
            (None, "month"),
            (None, "year"),
        ]

        await asyncio.gather(
            *(
                cog._check_fortune_logic(
                    ctx,
                    option,
                    mode,
                    status_msg=status,
                )
                for status, (option, mode) in zip(statuses, requests)
            )
        )

        assert ai.calls == 3
        assert ai.max_active == 1
        async with db.execute(
            """
            SELECT COUNT(*) FROM api_call_log
            WHERE api_type = ?
            """,
            (f"fortune_detail_{USER_ID}",),
        ) as cursor:
            assert (await cursor.fetchone())[0] == 3
        assert any(
            "남은 횟수: 2회" in str(call.kwargs.get("content"))
            for status in statuses
            for call in status.edit.await_args_list
        )
        assert any(
            "한도 초과" in str(status.edit.await_args.kwargs.get("content"))
            for status in statuses
            if status.edit.await_args is not None
        )
    finally:
        await db.close()

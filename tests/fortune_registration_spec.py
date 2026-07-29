from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cogs.fortune_cog import (
    FortuneCog,
    parse_birth_date_input,
    parse_birth_place_input,
    parse_birth_time_input,
    parse_gender_input,
)


def test_birth_date_requires_real_non_future_reasonable_calendar_date():
    today = date(2026, 7, 28)

    assert parse_birth_date_input("2000-02-29", today=today) == "2000-02-29"
    with pytest.raises(ValueError, match="달력"):
        parse_birth_date_input("2025-02-29", today=today)
    with pytest.raises(ValueError, match="미래"):
        parse_birth_date_input("2027-01-01", today=today)
    with pytest.raises(ValueError, match="1906"):
        parse_birth_date_input("1905-12-31", today=today)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        parse_birth_date_input("2000/01/01", today=today)


def test_optional_birth_fields_preserve_unknown_instead_of_guessing():
    assert parse_birth_time_input("모름") is None
    assert parse_birth_time_input("응답 안 함") is None
    assert parse_gender_input("응답 안 함") is None
    assert parse_birth_place_input("모름") is None
    assert parse_birth_time_input("09:30") == "09:30"
    assert parse_gender_input("여성") == "F"


class _Context:
    def __init__(self):
        self.author = SimpleNamespace(id=7)
        self.channel = object()
        self.guild = None
        self.send = AsyncMock()


def _message(ctx, content):
    return SimpleNamespace(
        author=ctx.author,
        channel=ctx.channel,
        content=content,
    )


def _registration_cog(ctx, answers):
    bot = SimpleNamespace(
        locked_users=set(),
        wait_for=AsyncMock(
            side_effect=[_message(ctx, answer) for answer in answers]
        ),
    )
    cog = FortuneCog.__new__(FortuneCog)
    cog.bot = bot
    cog._registration_users = set()
    cog._has_fortune_consent = AsyncMock(return_value=True)
    cog._save_user_profile = AsyncMock()
    return cog


@pytest.mark.asyncio
async def test_registration_stores_optional_unknown_as_null():
    ctx = _Context()
    cog = _registration_cog(
        ctx,
        ["2000-01-01", "모름", "응답 안 함", "모름"],
    )

    await FortuneCog.fortune_register.callback(cog, ctx)

    cog._save_user_profile.assert_awaited_once_with(
        7,
        "2000-01-01",
        None,
        None,
        None,
    )
    assert cog.bot.locked_users == set()
    assert cog._registration_users == set()


@pytest.mark.asyncio
async def test_registration_stops_after_three_invalid_inputs_without_saving():
    ctx = _Context()
    cog = _registration_cog(
        ctx,
        ["not-a-date", "2025-02-29", "3000-01-01"],
    )

    await FortuneCog.fortune_register.callback(cog, ctx)

    assert cog.bot.wait_for.await_count == 3
    cog._save_user_profile.assert_not_awaited()
    assert any(
        "3번" in str(call.args[0])
        for call in ctx.send.await_args_list
    )


@pytest.mark.asyncio
async def test_registration_cancel_stops_immediately_without_saving():
    ctx = _Context()
    cog = _registration_cog(ctx, ["취소"])

    await FortuneCog.fortune_register.callback(cog, ctx)

    assert cog.bot.wait_for.await_count == 1
    cog._save_user_profile.assert_not_awaited()

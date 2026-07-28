"""Masamo/General 및 Discord 서버 관리자 경계 명세."""

from types import SimpleNamespace

import aiosqlite
import discord
import pytest

import config
from cogs.admin_cog import AdminPanelView, _invite_url
from scripts import apply_admin_accounts_schema as migration
from utils.admin_policy import (
    is_guild_admin,
    is_instance_admin,
    is_superadmin,
    list_instance_admin_ids,
    set_instance_admin,
)


@pytest.mark.parametrize("backend", ["sqlite", "tidb"])
def test_admin_schema_is_single_additive_create(backend):
    statement = migration.schema_statement(backend)
    assert statement.upper().startswith(
        "CREATE TABLE IF NOT EXISTS BOT_ADMIN_ACCOUNTS"
    )
    for forbidden in ("DELETE ", "DROP ", "TRUNCATE ", "ALTER ", "UPDATE "):
        assert forbidden not in statement.upper()


@pytest.mark.asyncio
async def test_registered_admins_are_isolated_by_instance_and_disable_without_delete(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(config, "DB_BACKEND", "sqlite")
    path = tmp_path / "admins.db"
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(migration.schema_statement("sqlite"))
        await db.commit()

        monkeypatch.setattr(config, "INSTANCE_NAME", "masamo")
        await set_instance_admin(db, user_id=100, enabled=True, changed_by=1)
        assert await is_instance_admin(db, 100) is True

        monkeypatch.setattr(config, "INSTANCE_NAME", "general")
        assert await is_instance_admin(db, 100) is False
        await set_instance_admin(db, user_id=200, enabled=True, changed_by=2)

        monkeypatch.setattr(config, "INSTANCE_NAME", "masamo")
        assert await list_instance_admin_ids(db) == [100]
        await set_instance_admin(db, user_id=100, enabled=False, changed_by=1)
        assert await is_instance_admin(db, 100) is False
        async with db.execute(
            "SELECT COUNT(*) FROM bot_admin_accounts"
        ) as cursor:
            row_count = (await cursor.fetchone())[0]

    assert row_count == 2


def test_superadmin_and_guild_admin_are_separate(monkeypatch):
    monkeypatch.setattr(config, "SUPERADMIN_USER_IDS", frozenset({100}))
    assert is_superadmin(100) is True
    assert is_superadmin(200) is False

    guild = SimpleNamespace(id=1, owner_id=300)
    manager = SimpleNamespace(
        id=200,
        guild_permissions=SimpleNamespace(
            administrator=False,
            manage_guild=True,
        ),
    )
    ordinary = SimpleNamespace(
        id=201,
        guild_permissions=SimpleNamespace(
            administrator=False,
            manage_guild=False,
        ),
    )
    assert is_guild_admin(manager, guild) is True
    assert is_guild_admin(ordinary, guild) is False
    assert is_guild_admin(SimpleNamespace(id=300), guild) is True


def test_invite_button_is_enabled_only_for_superadmin(monkeypatch):
    monkeypatch.setattr(config, "SUPERADMIN_USER_IDS", frozenset({100}))
    bot = SimpleNamespace(
        user=SimpleNamespace(id=999),
        guilds=[],
        latency=0.01,
    )
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=100),
        guild=None,
        clean_prefix="!",
    )
    super_view = AdminPanelView(bot, ctx, "superadmin")
    invite_button = next(
        child
        for child in super_view.children
        if isinstance(child, discord.ui.Button) and "초대" in child.label
    )
    assert invite_button.disabled is False
    assert _invite_url(bot).startswith("https://discord.com/oauth2/authorize")

    registered_view = AdminPanelView(bot, ctx, "instance_admin")
    registered_invite = next(
        child
        for child in registered_view.children
        if isinstance(child, discord.ui.Button) and "초대" in child.label
    )
    assert registered_invite.disabled is True

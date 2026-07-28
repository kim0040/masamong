# -*- coding: utf-8 -*-
"""서버 관리자와 인스턴스 관리자를 분리한 Discord 관리 UX."""

from __future__ import annotations

import discord
from discord.ext import commands

import config
from logger_config import logger
from utils.admin_policy import (
    is_guild_admin,
    is_instance_admin,
    is_superadmin,
    list_instance_admin_ids,
    set_instance_admin,
)


_INVITE_PERMISSIONS = discord.Permissions(
    view_channel=True,
    send_messages=True,
    send_messages_in_threads=True,
    embed_links=True,
    attach_files=True,
    read_message_history=True,
    add_reactions=True,
    use_external_emojis=True,
)


def _invite_url(bot: commands.Bot) -> str | None:
    user = getattr(bot, "user", None)
    if user is None or not getattr(user, "id", None):
        return None
    return discord.utils.oauth_url(
        int(user.id),
        permissions=_INVITE_PERMISSIONS,
        scopes=("bot", "applications.commands"),
    )


def _admin_embed(
    bot: commands.Bot,
    ctx: commands.Context,
    access_level: str,
) -> discord.Embed:
    prefix = ctx.clean_prefix or config.COMMAND_PREFIX or "!"
    if access_level == "superadmin":
        scope_text = (
            f"**{config.INSTANCE_NAME} 인스턴스 전체**를 관리합니다. "
            "관리자 등록과 봇 초대는 최고 관리자에게만 허용됩니다."
        )
    elif access_level == "instance_admin":
        scope_text = (
            f"등록된 **{config.INSTANCE_NAME} 인스턴스 관리자**입니다. "
            "상태 확인은 가능하지만 최고 관리자 등록·초대 권한은 없습니다."
        )
    else:
        guild_id = int(ctx.guild.id) if ctx.guild else 0
        scope_text = (
            f"Discord 서버 관리자 권한은 **현재 서버({guild_id}) 설정에만** 적용됩니다. "
            "다른 서버의 말투·채널·데이터에는 접근하지 않습니다."
        )

    embed = discord.Embed(
        title="🛡️ 마사몽 관리 센터",
        description=scope_text,
        color=0x5865F2,
    )
    if ctx.guild and is_guild_admin(ctx.author, ctx.guild):
        embed.add_field(
            name="🏠 현재 서버 설정",
            value=(
                "`/config set_ai` · `/config channel` · `/config language`\n"
                "`/persona view` · `/persona set`\n"
                "모든 변경은 현재 Discord 서버 ID에만 저장됩니다."
            ),
            inline=False,
        )
    if access_level in {"superadmin", "instance_admin"}:
        embed.add_field(
            name="📊 인스턴스 상태",
            value=(
                f"프로필: `{config.PROFILE}` · 인스턴스: `{config.INSTANCE_NAME}`\n"
                f"연결 서버: `{len(getattr(bot, 'guilds', []))}` · "
                f"지연: `{float(getattr(bot, 'latency', 0.0)) * 1000:.0f} ms`\n"
                f"상세: `{prefix}관리 상태`"
            ),
            inline=False,
        )
    if access_level == "superadmin":
        embed.add_field(
            name="🔐 최고 관리자 전용",
            value=(
                f"`{prefix}관리 추가 <사용자 ID 또는 @멘션>` (DM 전용)\n"
                f"`{prefix}관리 제거 <사용자 ID 또는 @멘션>` (DM 전용)\n"
                f"`{prefix}관리 목록` (DM 전용)\n"
                f"`{prefix}초대` 또는 아래 **초대 링크** 버튼"
            ),
            inline=False,
        )
    embed.set_footer(
        text=(
            "Masamo와 General은 최고 관리자 env와 등록 관리자 DB가 서로 분리됩니다."
        )
    )
    return embed


class _InviteLinkView(discord.ui.View):
    def __init__(self, url: str) -> None:
        super().__init__(timeout=300)
        self.add_item(
            discord.ui.Button(
                label="마사몽을 서버에 초대",
                style=discord.ButtonStyle.link,
                emoji="➕",
                url=url,
            )
        )


class AdminPanelView(discord.ui.View):
    """호출자 한 명만 조작하는 인스턴스/서버 관리 패널."""

    def __init__(
        self,
        bot: commands.Bot,
        ctx: commands.Context,
        access_level: str,
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.ctx = ctx
        self.user_id = int(ctx.author.id)
        self.access_level = access_level
        self.invite.disabled = access_level != "superadmin"
        if self.invite.disabled:
            self.invite.label = "초대 · 최고 관리자 전용"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) == self.user_id:
            return True
        await interaction.response.send_message(
            "이 관리 메뉴는 호출한 사용자만 사용할 수 있습니다.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(
        label="상태 새로고침",
        style=discord.ButtonStyle.secondary,
        emoji="📊",
    )
    async def status(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_message(
            embed=_admin_embed(self.bot, self.ctx, self.access_level),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(
        label="초대 링크",
        style=discord.ButtonStyle.primary,
        emoji="➕",
    )
    async def invite(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        if not is_superadmin(interaction.user.id):
            await interaction.response.send_message(
                "봇 초대 링크는 현재 인스턴스의 최고 관리자만 만들 수 있습니다.",
                ephemeral=True,
            )
            return
        url = _invite_url(self.bot)
        if not url:
            await interaction.response.send_message(
                "Discord 앱 정보를 아직 불러오지 못했습니다. 잠시 뒤 다시 시도해 주세요.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "아래 버튼에서 초대할 서버를 선택하세요. 필요한 Discord 권한만 요청합니다.",
            view=_InviteLinkView(url),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class AdminLauncherView(discord.ui.View):
    """접두사 명령의 공개 메시지에서 호출자 전용 관리 패널을 엽니다."""

    def __init__(
        self,
        bot: commands.Bot,
        ctx: commands.Context,
        access_level: str,
    ) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.ctx = ctx
        self.user_id = int(ctx.author.id)
        self.access_level = access_level
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) == self.user_id:
            return True
        await interaction.response.send_message(
            "이 버튼은 관리 메뉴를 호출한 사용자만 사용할 수 있습니다.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(
        label="나만의 관리 센터 열기",
        style=discord.ButtonStyle.primary,
        emoji="🛡️",
    )
    async def open_panel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_message(
            embed=_admin_embed(self.bot, self.ctx, self.access_level),
            view=AdminPanelView(self.bot, self.ctx, self.access_level),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


class AdminCog(commands.Cog):
    """프로필 전체 관리자와 Discord 서버 관리자를 분리해 제공합니다."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _access_level(self, ctx: commands.Context) -> str | None:
        if is_superadmin(ctx.author.id):
            return "superadmin"
        try:
            if await is_instance_admin(self.bot.db, ctx.author.id):
                return "instance_admin"
        except Exception as exc:
            logger.error("인스턴스 관리자 조회 실패: %s", exc, exc_info=True)
        if ctx.guild and is_guild_admin(ctx.author, ctx.guild):
            return "guild_admin"
        return None

    async def _require_superadmin(self, ctx: commands.Context) -> bool:
        if is_superadmin(ctx.author.id):
            return True
        await ctx.send(
            "❌ 이 작업은 현재 인스턴스의 **최고 관리자만** 실행할 수 있습니다.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    @commands.group(
        name="관리",
        aliases=["관리자", "admin"],
        hidden=True,
        invoke_without_command=True,
    )
    async def admin(self, ctx: commands.Context) -> None:
        """현재 권한 범위에 맞는 관리 센터를 엽니다."""
        access_level = await self._access_level(ctx)
        if access_level is None:
            await ctx.send(
                "❌ 현재 서버의 관리자 권한 또는 등록된 마사몽 관리자 권한이 필요합니다.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if ctx.guild:
            launcher = AdminLauncherView(self.bot, ctx, access_level)
            embed = discord.Embed(
                title="🛡️ 마사몽 관리 센터",
                description=(
                    "아래 버튼을 누르면 권한 범위에 맞는 관리 화면이 "
                    "**호출한 사람에게만** 표시됩니다."
                ),
                color=0x5865F2,
            )
            embed.set_footer(text="버튼은 3분간 유효합니다.")
            launcher.message = await ctx.send(
                embed=embed,
                view=launcher,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await ctx.send(
            embed=_admin_embed(self.bot, ctx, access_level),
            view=AdminPanelView(self.bot, ctx, access_level),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @admin.command(name="상태", aliases=["status"])
    async def admin_status(self, ctx: commands.Context) -> None:
        """권한 범위와 인스턴스의 비식별 상태를 확인합니다."""
        access_level = await self._access_level(ctx)
        if access_level is None:
            await ctx.send("❌ 관리자 권한이 필요합니다.")
            return
        embed = _admin_embed(self.bot, ctx, access_level)
        if ctx.guild:
            try:
                await ctx.author.send(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                await ctx.send("❌ DM을 열어야 비공개 상태 정보를 받을 수 있습니다.")
                return
            await ctx.send("✅ 관리 상태를 DM으로 보냈습니다.")
            return
        await ctx.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @admin.command(name="추가", aliases=["add"])
    async def admin_add(self, ctx: commands.Context, target: discord.User) -> None:
        """최고 관리자가 현재 인스턴스 관리자를 등록합니다. DM 전용입니다."""
        if not await self._require_superadmin(ctx):
            return
        if ctx.guild:
            await ctx.send(
                "🔒 관리자 등록은 사용자 ID 노출을 줄이기 위해 마사몽 DM에서만 가능합니다."
            )
            return
        if getattr(target, "bot", False):
            await ctx.send("❌ 봇 계정은 인스턴스 관리자로 등록할 수 없습니다.")
            return
        if is_superadmin(target.id):
            await ctx.send("ℹ️ 해당 사용자는 이미 env에 고정된 최고 관리자입니다.")
            return
        await set_instance_admin(
            self.bot.db,
            user_id=int(target.id),
            enabled=True,
            changed_by=int(ctx.author.id),
        )
        await ctx.send(
            f"✅ `{config.INSTANCE_NAME}` 인스턴스 관리자 `{int(target.id)}`를 등록했습니다.\n"
            "Discord 서버 설정 권한은 별도이며 각 서버 관리자 권한을 따릅니다.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @admin.command(name="제거", aliases=["remove"])
    async def admin_remove(self, ctx: commands.Context, target: discord.User) -> None:
        """최고 관리자가 등록 관리자를 비활성화합니다. 행은 삭제하지 않습니다."""
        if not await self._require_superadmin(ctx):
            return
        if ctx.guild:
            await ctx.send("🔒 관리자 제거는 마사몽 DM에서만 가능합니다.")
            return
        if is_superadmin(target.id):
            await ctx.send(
                "❌ env에 고정된 최고 관리자는 Discord에서 제거할 수 없습니다. "
                "프로필 env와 서비스를 별도 운영 절차로 변경해야 합니다."
            )
            return
        await set_instance_admin(
            self.bot.db,
            user_id=int(target.id),
            enabled=False,
            changed_by=int(ctx.author.id),
        )
        await ctx.send(
            f"✅ `{config.INSTANCE_NAME}` 인스턴스 관리자 `{int(target.id)}`를 비활성화했습니다.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @admin.command(name="목록", aliases=["list"])
    async def admin_list(self, ctx: commands.Context) -> None:
        """최고 관리자가 현재 프로필 관리자 목록을 DM에서 봅니다."""
        if not await self._require_superadmin(ctx):
            return
        if ctx.guild:
            await ctx.send("🔒 관리자 목록은 마사몽 DM에서만 확인할 수 있습니다.")
            return
        registered = await list_instance_admin_ids(self.bot.db)
        configured = sorted(config.SUPERADMIN_USER_IDS)
        super_lines = "\n".join(f"- `{user_id}`" for user_id in configured) or "- 없음"
        admin_lines = "\n".join(f"- `{user_id}`" for user_id in registered) or "- 없음"
        embed = discord.Embed(
            title=f"🛡️ {config.INSTANCE_NAME} 관리자 목록",
            color=0x5865F2,
        )
        embed.add_field(name="최고 관리자 · env 고정", value=super_lines, inline=False)
        embed.add_field(name="등록 인스턴스 관리자", value=admin_lines, inline=False)
        embed.set_footer(text="다른 인스턴스와 다른 Discord 서버의 권한은 포함하지 않습니다.")
        await ctx.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _send_invite(self, ctx: commands.Context) -> None:
        if not await self._require_superadmin(ctx):
            return
        url = _invite_url(self.bot)
        if not url:
            await ctx.send("❌ Discord 앱 정보를 아직 불러오지 못했습니다.")
            return
        embed = discord.Embed(
            title=f"➕ {config.INSTANCE_NAME} 마사몽 초대",
            description=(
                "아래 버튼에서 서버를 선택하세요. 메시지·임베드·첨부·반응 등 "
                "현재 기능에 필요한 Discord 권한만 요청합니다."
            ),
            color=0x5865F2,
        )
        view = _InviteLinkView(url)
        if ctx.guild:
            try:
                await ctx.author.send(
                    embed=embed,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                await ctx.send("❌ DM을 열어야 초대 링크를 비공개로 받을 수 있습니다.")
                return
            await ctx.send("✅ 최고 관리자 DM으로 초대 버튼을 보냈습니다.")
            return
        await ctx.send(
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @admin.command(name="초대", aliases=["invite"])
    async def admin_invite(self, ctx: commands.Context) -> None:
        """최고 관리자 전용 초대 버튼을 보냅니다."""
        await self._send_invite(ctx)

    @commands.command(name="초대", aliases=["invitebot"], hidden=True)
    async def invite(self, ctx: commands.Context) -> None:
        """최고 관리자 전용 봇 초대 단축 명령입니다."""
        await self._send_invite(ctx)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))

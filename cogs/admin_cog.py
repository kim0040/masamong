# -*- coding: utf-8 -*-
"""환경 변수에 고정된 최고 관리자만 사용하는 안전한 Discord 관리 UI."""

from __future__ import annotations

import discord
from discord.ext import commands

import config
from logger_config import logger
from utils.admin_policy import is_superadmin
from utils.discord_interactions import ReliableView
from utils.guild_controls import GuildControl


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


def _settings_cog(bot: commands.Bot):
    return bot.get_cog("SettingsCog")


async def _control_snapshot(
    bot: commands.Bot,
    guild_id: int | None,
) -> GuildControl | None:
    if guild_id is None:
        return None
    cog = _settings_cog(bot)
    if cog is None:
        return None
    return await cog.snapshot(int(guild_id))


async def _admin_embed(
    bot: commands.Bot,
    ctx: commands.Context,
) -> discord.Embed:
    guild_id = int(ctx.guild.id) if ctx.guild else None
    channel_id = int(ctx.channel.id) if ctx.guild else None
    control = await _control_snapshot(bot, guild_id)
    embed = discord.Embed(
        title="⚙️ 마사몽 간편 설정",
        description=(
            "이 화면은 봇 관리자에게만 보여요. 모델·DB·확인 주기·말투 같은 운영 설정은 Discord에서 바꿀 수 없어요."
        ),
        color=0x66CCFF,
    )
    if guild_id is not None:
        if control is None:
            server_state = "설정 기능을 불러오지 못했습니다."
            channel_state = "확인할 수 없음"
        else:
            server_state = "사용 중" if control.ai_enabled else "잠시 꺼짐"
            if channel_id in control.disabled_channels:
                channel_state = "응답 안 함"
            elif channel_id in control.enabled_channels:
                channel_state = "응답함"
            else:
                channel_state = (
                    "기본 설정 사용"
                    if control.ai_enabled
                    else "서버 AI가 꺼져 있음"
                )
        embed.add_field(
            name="지금 보고 있는 서버",
            value=(
                f"- 서버 AI: **{server_state}**\n"
                f"- 현재 채널: **{channel_state}**\n"
                f"- 적용 범위: `{config.INSTANCE_NAME}` 봇의 지금 이 서버"
            ),
            inline=False,
        )
    embed.add_field(
        name="연결 상태",
        value=(
            f"- 프로필: `{config.PROFILE}`\n"
            f"- 연결 서버: `{len(getattr(bot, 'guilds', []))}`\n"
            f"- Discord 지연: `{float(getattr(bot, 'latency', 0.0)) * 1000:.0f} ms`"
        ),
        inline=False,
    )
    embed.add_field(
        name="바꿀 수 있는 항목",
        value=(
            "현재 서버의 AI 응답 켜기·끄기, 현재 채널 응답 켜기·끄기, 초대 링크 열기만 제공해요."
        ),
        inline=False,
    )
    embed.set_footer(
        text="일반용과 마사모용 설정은 따로 저장돼요."
    )
    return embed


class _InviteLinkView(discord.ui.View):
    def __init__(self, url: str) -> None:
        super().__init__(timeout=300)
        self.add_item(
            discord.ui.Button(
                label="마사몽 초대하기",
                style=discord.ButtonStyle.link,
                emoji="➕",
                url=url,
            )
        )


class AdminPanelView(ReliableView):
    """호출한 최고 관리자만 조작하는 최소 권한 패널."""

    def __init__(self, bot: commands.Bot, ctx: commands.Context) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.ctx = ctx
        self.user_id = int(ctx.author.id)
        unavailable = ctx.guild is None
        self.server_on.disabled = unavailable
        self.server_off.disabled = unavailable
        self.channel_on.disabled = unavailable
        self.channel_off.disabled = unavailable

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (
            int(interaction.user.id) == self.user_id
            and is_superadmin(interaction.user.id)
        ):
            return True
        await interaction.response.send_message(
            "이 설정 화면은 이 봇의 관리자만 사용할 수 있어요.",
            ephemeral=True,
        )
        return False

    async def _set_server(
        self,
        interaction: discord.Interaction,
        enabled: bool,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild_id is None:
            await interaction.edit_original_response(
                content="서버 안에서 열어야 바꿀 수 있어요."
            )
            return
        cog = _settings_cog(self.bot)
        if cog is None:
            await interaction.edit_original_response(
                content="설정 기능을 불러오지 못했어요. 잠시 뒤 다시 시도해 주세요.",
            )
            return
        try:
            await cog.set_ai_enabled(
                guild_id=int(interaction.guild_id),
                enabled=enabled,
                changed_by=int(interaction.user.id),
            )
        except Exception:
            logger.error("최고 관리자 서버 AI 설정 변경 실패", exc_info=True)
            await interaction.edit_original_response(
                content="설정을 저장하지 못했어요. 기존 설정은 그대로예요.",
            )
            return
        state = "다시 켰어요" if enabled else "잠시 껐어요"
        await interaction.edit_original_response(
            content=f"현재 서버의 AI 응답을 **{state}**. 다른 서버에는 영향이 없어요.",
        )

    async def _set_channel(
        self,
        interaction: discord.Interaction,
        enabled: bool,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.edit_original_response(
                content="서버 채널 안에서 열어야 바꿀 수 있어요."
            )
            return
        cog = _settings_cog(self.bot)
        if cog is None:
            await interaction.edit_original_response(
                content="설정 기능을 불러오지 못했어요. 잠시 뒤 다시 시도해 주세요.",
            )
            return
        try:
            await cog.set_channel_enabled(
                guild_id=int(interaction.guild_id),
                channel_id=int(interaction.channel_id),
                enabled=enabled,
                changed_by=int(interaction.user.id),
            )
        except Exception:
            logger.error("최고 관리자 채널 AI 설정 변경 실패", exc_info=True)
            await interaction.edit_original_response(
                content="설정을 저장하지 못했어요. 기존 설정은 그대로예요.",
            )
            return
        state = "응답하도록 바꿨어요" if enabled else "응답하지 않도록 바꿨어요"
        await interaction.edit_original_response(
            content=f"이 채널에서는 이제 마사몽이 **{state}**.",
        )

    @discord.ui.button(label="새로고침", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def status(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.edit_original_response(
            embed=await _admin_embed(self.bot, self.ctx),
            view=AdminPanelView(self.bot, self.ctx),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="서버 AI 켜기", style=discord.ButtonStyle.success, emoji="▶️")
    async def server_on(self, interaction, _button) -> None:
        await self._set_server(interaction, True)

    @discord.ui.button(label="서버 AI 끄기", style=discord.ButtonStyle.danger, emoji="⏸️")
    async def server_off(self, interaction, _button) -> None:
        await self._set_server(interaction, False)

    @discord.ui.button(label="이 채널 켜기", style=discord.ButtonStyle.success, emoji="💬")
    async def channel_on(self, interaction, _button) -> None:
        await self._set_channel(interaction, True)

    @discord.ui.button(label="이 채널 끄기", style=discord.ButtonStyle.secondary, emoji="🔕")
    async def channel_off(self, interaction, _button) -> None:
        await self._set_channel(interaction, False)

    @discord.ui.button(label="초대 링크", style=discord.ButtonStyle.primary, emoji="➕")
    async def invite(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        url = _invite_url(self.bot)
        if not url:
            await interaction.response.send_message(
                "Discord 앱 정보를 아직 불러오지 못했어요. 잠시 뒤 다시 시도해 주세요.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "아래 버튼에서 초대할 서버를 골라주세요.",
            view=_InviteLinkView(url),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class AdminLauncherView(ReliableView):
    """공개 채널에서 최고 관리자에게만 비공개 패널을 엽니다."""

    def __init__(self, bot: commands.Bot, ctx: commands.Context) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.ctx = ctx
        self.user_id = int(ctx.author.id)
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (
            int(interaction.user.id) == self.user_id
            and is_superadmin(interaction.user.id)
        ):
            return True
        await interaction.response.send_message(
            "이 버튼은 관리 메뉴를 연 최고 관리자만 사용할 수 있어요.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(
        label="내 설정 화면 열기",
        style=discord.ButtonStyle.primary,
        emoji="⚙️",
    )
    async def open_panel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.edit_original_response(
            embed=await _admin_embed(self.bot, self.ctx),
            view=AdminPanelView(self.bot, self.ctx),
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
    """최고 관리자만 볼 수 있는 간편 관리 센터."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def send_interaction_panel(
        self,
        interaction: discord.Interaction,
        ctx: commands.Context,
    ) -> None:
        """통합 메뉴에서 최고 관리자 전용 패널을 비공개로 엽니다."""
        if not is_superadmin(interaction.user.id):
            await interaction.response.send_message(
                "여기서는 사용할 수 없는 메뉴예요.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.edit_original_response(
            embed=await _admin_embed(self.bot, ctx),
            view=AdminPanelView(self.bot, ctx),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="관리", aliases=["관리자", "admin"], hidden=True)
    async def admin(self, ctx: commands.Context) -> None:
        if not is_superadmin(ctx.author.id):
            await ctx.send(
                "여기서는 사용할 수 없는 메뉴예요.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if ctx.guild:
            launcher = AdminLauncherView(self.bot, ctx)
            embed = discord.Embed(
                title="⚙️ 마사몽 간편 설정",
                description="아래 버튼을 누르면 설정 화면이 본인에게만 보여요.",
                color=0x66CCFF,
            )
            embed.set_footer(text="버튼은 3분 동안 사용할 수 있어요.")
            launcher.message = await ctx.send(
                embed=embed,
                view=launcher,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await ctx.send(
            embed=await _admin_embed(self.bot, ctx),
            view=AdminPanelView(self.bot, ctx),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="초대", aliases=["invitebot"], hidden=True)
    async def invite(self, ctx: commands.Context) -> None:
        if not is_superadmin(ctx.author.id):
            await ctx.send("여기서는 사용할 수 없는 메뉴예요.")
            return
        url = _invite_url(self.bot)
        if not url:
            await ctx.send("Discord 앱 정보를 아직 불러오지 못했어요.")
            return
        await ctx.send(
            "아래 버튼에서 초대할 서버를 골라주세요.",
            view=_InviteLinkView(url),
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))

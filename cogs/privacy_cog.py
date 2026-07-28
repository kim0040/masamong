# -*- coding: utf-8 -*-
"""목적별 개인정보 동의 조회·동의·철회 명령."""

from __future__ import annotations

import discord
from discord.ext import commands

from logger_config import logger
from utils.privacy_consent import (
    CONSENT_GRANTED,
    CONSENT_WITHDRAWN,
    all_policies,
    consent_command_name,
    format_policy_notice,
    get_consent_state,
    get_policy,
    grant_consent,
    is_current_consent_state,
    normalize_scope,
    withdraw_consent,
)


class ConsentDecisionView(discord.ui.View):
    """정책 고지를 읽은 본인만 명시적으로 동의할 수 있는 버튼."""

    def __init__(self, cog: "PrivacyCog", *, user_id: int, scope: str) -> None:
        super().__init__(timeout=180)
        self._cog = cog
        self._user_id = int(user_id)
        self._scope = normalize_scope(scope)

        agree = discord.ui.Button(
            label="동의합니다",
            style=discord.ButtonStyle.success,
            custom_id=f"privacy:grant:{self._scope}",
        )
        cancel = discord.ui.Button(
            label="동의하지 않습니다",
            style=discord.ButtonStyle.secondary,
            custom_id=f"privacy:cancel:{self._scope}",
        )
        agree.callback = self._grant  # type: ignore[method-assign]
        cancel.callback = self._cancel  # type: ignore[method-assign]
        self.add_item(agree)
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) == self._user_id:
            return True
        await interaction.response.send_message(
            "이 동의 버튼은 명령을 실행한 사용자만 누를 수 있습니다.",
            ephemeral=True,
        )
        return False

    def _disable(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        self.stop()

    async def _grant(self, interaction: discord.Interaction) -> None:
        policy = get_policy(self._scope)
        try:
            await grant_consent(
                self._cog.bot.db,
                self._user_id,
                policy.scope,
            )
        except Exception as exc:
            logger.error(
                "개인정보 동의 저장 실패: scope=%s user_id=%s error=%s",
                policy.scope,
                self._user_id,
                type(exc).__name__,
                exc_info=True,
            )
            await interaction.response.send_message(
                "동의 상태를 저장하지 못했습니다. 잠시 후 다시 시도해주세요.",
                ephemeral=True,
            )
            return

        self._disable()
        await interaction.response.edit_message(
            content=(
                f"✅ **{policy.display_name}** 개인정보 처리에 동의했습니다. "
                f"(정책 `{policy.version}`)\n"
                "이제 원래 사용하려던 명령을 다시 실행해주세요. "
                f"언제든 `!개인정보 철회 {consent_command_name(policy.scope)}`로 "
                "향후 이용을 중단할 수 있습니다."
            ),
            view=self,
        )

    async def _cancel(self, interaction: discord.Interaction) -> None:
        policy = get_policy(self._scope)
        self._disable()
        await interaction.response.edit_message(
            content=(
                f"동의하지 않았습니다. **{policy.display_name}** 개인정보는 "
                "새로 수집하거나 기존 프로필에서 이용하지 않습니다."
            ),
            view=self,
        )


class PrivacyCog(commands.Cog):
    """개인정보 동의를 기능별로 관리한다."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def send_consent_prompt(
        self,
        destination,
        *,
        user_id: int,
        scope: str,
        prefix: str | None = None,
    ):
        """다른 Cog도 동일한 정책 고지와 명시 동의 버튼을 사용하게 한다."""
        policy = get_policy(scope)
        content = format_policy_notice(policy.scope)
        if prefix:
            content = f"{prefix}\n\n{content}"
        return await destination.send(
            content,
            view=ConsentDecisionView(
                self,
                user_id=int(user_id),
                scope=policy.scope,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.group(name="개인정보", invoke_without_command=True)
    @commands.dm_only()
    async def privacy(self, ctx: commands.Context) -> None:
        """현재 목적별 동의 상태를 확인합니다."""
        if ctx.invoked_subcommand is not None:
            return

        lines = [
            "🔐 **개인정보 동의 현황**",
            "일반 Discord 대화와 서버가 제공하는 정보는 아래 동의 대상이 아닙니다.",
        ]
        for policy in all_policies():
            state = await get_consent_state(
                self.bot.db,
                ctx.author.id,
                policy.scope,
            )
            if is_current_consent_state(state, policy.scope):
                label = f"동의됨 (정책 {policy.version})"
            elif state and state.status == CONSENT_WITHDRAWN:
                label = "철회됨 — 기존 데이터는 보존되지만 이용은 중단됨"
            elif state and state.status == CONSENT_GRANTED:
                label = "재동의 필요 — 정책 버전 또는 고지문이 변경됨"
            else:
                label = "미동의"
            lines.append(f"- **{policy.display_name}**: {label}")

        lines.extend(
            (
                "",
                "동의: `!개인정보 동의 운세` / `!개인정보 동의 학교공지`",
                "철회: `!개인정보 철회 운세` / `!개인정보 철회 학교공지`",
                "철회는 향후 이용만 중단합니다. 저장 데이터 삭제는 "
                "`!운세 삭제` 또는 `!공지 삭제`를 별도로 실행해야 합니다. "
                "동의·철회 증빙용 감사 이력은 기능 데이터 삭제 후에도 별도 보관됩니다.",
            )
        )
        await ctx.send(
            "\n".join(lines),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @privacy.command(name="동의")
    @commands.dm_only()
    async def consent(self, ctx: commands.Context, scope: str) -> None:
        """정책 고지를 표시하고 버튼으로 명시 동의를 받습니다."""
        try:
            normalized = normalize_scope(scope)
        except ValueError as exc:
            await ctx.send(f"❌ {exc}")
            return
        await self.send_consent_prompt(
            ctx,
            user_id=ctx.author.id,
            scope=normalized,
        )

    @privacy.command(name="철회")
    @commands.dm_only()
    async def withdraw(self, ctx: commands.Context, scope: str) -> None:
        """향후 목적별 개인정보 이용을 중단하되 누적 데이터는 보존합니다."""
        try:
            policy = get_policy(scope)
        except ValueError as exc:
            await ctx.send(f"❌ {exc}")
            return

        try:
            withdrawn = await withdraw_consent(
                self.bot.db,
                ctx.author.id,
                policy.scope,
            )
        except Exception:
            logger.error(
                "개인정보 동의 철회 저장 실패: scope=%s user_id=%s",
                policy.scope,
                ctx.author.id,
                exc_info=True,
            )
            await ctx.send("철회 상태를 저장하지 못했습니다. 잠시 후 다시 시도해주세요.")
            return

        if withdrawn is None:
            await ctx.send(
                f"ℹ️ **{policy.display_name}** 개인정보에 동의한 기록이 없어 "
                "새 철회 기록을 만들지 않았습니다.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        delete_command = (
            "`!운세 삭제`"
            if policy.scope == "fortune"
            else "`!공지 삭제`"
        )
        await ctx.send(
            f"✅ **{policy.display_name}** 개인정보 동의를 철회했습니다.\n"
            "지금부터 기존 프로필 조회·개인화 처리·자동 발송을 중단합니다. "
            "기존 누적 데이터와 구독/활성 설정은 자동 삭제하지 않아 재동의 시 그대로 재개됩니다.\n"
            f"데이터도 삭제하려면 {delete_command}를 별도로 실행해주세요. "
            "동의·철회 증빙용 감사 이력은 삭제 명령 후에도 별도 보관됩니다.",
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PrivacyCog(bot))

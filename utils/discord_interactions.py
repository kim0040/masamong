# -*- coding: utf-8 -*-
"""Discord component failures are acknowledged without exposing internals."""

from __future__ import annotations

import discord

from logger_config import logger


class ReliableView(discord.ui.View):
    """View callback failures get a private user-facing terminal response.

    discord.py's default handler only logs the exception. That leaves the user
    with "the application did not respond", even when the failure is known and
    recoverable by retrying. This base keeps the traceback in the server journal
    and sends one bounded, non-sensitive response.
    """

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        logger.error(
            "Discord component 처리 실패: view=%s item=%s error=%s",
            type(self).__name__,
            type(item).__name__,
            type(error).__name__,
            exc_info=(type(error), error, error.__traceback__),
        )
        message = (
            "이 동작을 처리하지 못했습니다. 저장 여부가 불확실하면 상태 화면을 "
            "먼저 확인한 뒤 한 번만 다시 시도해주세요."
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.warning(
                "Discord component 오류 안내 전송 실패: view=%s",
                type(self).__name__,
            )


class ReliableModal(discord.ui.Modal):
    """Modal submit failures use the same private terminal response policy."""

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ) -> None:
        logger.error(
            "Discord modal 처리 실패: modal=%s error=%s",
            type(self).__name__,
            type(error).__name__,
            exc_info=(type(error), error, error.__traceback__),
        )
        message = "입력 내용을 처리하지 못했습니다. 잠시 후 한 번만 다시 시도해주세요."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.warning(
                "Discord modal 오류 안내 전송 실패: modal=%s",
                type(self).__name__,
            )


class ReliableCommandTree(discord.app_commands.CommandTree):
    """Slash-command failures always receive one private terminal response."""

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: discord.app_commands.AppCommandError,
    ) -> None:
        logger.error(
            "Discord app command 처리 실패: command=%s error=%s",
            getattr(getattr(interaction, "command", None), "qualified_name", "unknown"),
            type(error).__name__,
            exc_info=(type(error), error, error.__traceback__),
        )
        message = "명령을 처리하지 못했습니다. 잠시 후 한 번만 다시 시도해주세요."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.warning("Discord app command 오류 안내 전송 실패")

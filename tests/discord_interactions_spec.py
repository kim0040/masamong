"""Discord 상호작용 실패가 사용자에게 종결 응답을 남기는지 검증합니다."""

from types import SimpleNamespace

import discord
import pytest

from utils.discord_interactions import (
    ReliableCommandTree,
    ReliableModal,
    ReliableView,
)


class _Response:
    def __init__(self, *, done: bool = False) -> None:
        self.done = done
        self.messages = []

    def is_done(self):
        return self.done

    async def send_message(self, content, *, ephemeral):
        self.messages.append((content, ephemeral))
        self.done = True


class _Followup:
    def __init__(self) -> None:
        self.messages = []

    async def send(self, content, *, ephemeral):
        self.messages.append((content, ephemeral))


def _interaction(*, done=False):
    return SimpleNamespace(
        response=_Response(done=done),
        followup=_Followup(),
        command=SimpleNamespace(qualified_name="test"),
    )


class _TestModal(ReliableModal, title="테스트"):
    pass


@pytest.mark.asyncio
async def test_view_error_acknowledges_unanswered_interaction():
    interaction = _interaction()
    await ReliableView(timeout=1).on_error(
        interaction,
        RuntimeError("internal detail"),
        discord.ui.Button(label="test"),
    )

    assert interaction.response.messages
    assert interaction.response.messages[0][1] is True
    assert "internal detail" not in interaction.response.messages[0][0]


@pytest.mark.asyncio
async def test_view_error_uses_followup_after_defer():
    interaction = _interaction(done=True)
    await ReliableView(timeout=1).on_error(
        interaction,
        RuntimeError("internal detail"),
        discord.ui.Button(label="test"),
    )

    assert interaction.followup.messages
    assert interaction.followup.messages[0][1] is True


@pytest.mark.asyncio
async def test_modal_and_command_errors_have_terminal_responses():
    modal_interaction = _interaction()
    await _TestModal(timeout=1).on_error(
        modal_interaction,
        RuntimeError("modal detail"),
    )
    command_interaction = _interaction()
    await ReliableCommandTree.on_error(
        object(),
        command_interaction,
        discord.app_commands.AppCommandError("command detail"),
    )

    assert modal_interaction.response.messages
    assert command_interaction.response.messages
    assert "detail" not in modal_interaction.response.messages[0][0]
    assert "detail" not in command_interaction.response.messages[0][0]

"""이미지 생성 결과의 디스코드 단일 전송 회귀 테스트."""

from types import SimpleNamespace

import discord
import pytest

from cogs.ai_handler import AIHandler


class _Channel:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace()


class _Status:
    def __init__(self) -> None:
        self.deleted = 0
        self.edits: list[str] = []

    async def delete(self):
        self.deleted += 1

    async def edit(self, *, content):
        self.edits.append(content)


class _Progress:
    def __init__(self) -> None:
        self.stopped = 0

    async def stop(self):
        self.stopped += 1


@pytest.mark.asyncio
async def test_single_image_result_sends_exactly_one_attachment():
    handler = object.__new__(AIHandler)
    channel = _Channel()
    status = _Status()
    progress = _Progress()
    message = SimpleNamespace(channel=channel)

    response = await handler._deliver_single_image_result(
        message=message,
        status_msg=status,
        progress=progress,
        image_payload={
            "image_data": b"\x89PNG\r\n\x1a\nfinal",
            "mime_type": "image/png",
            "remaining": 4,
        },
        log_extra={},
    )

    assert "한 장" in response
    assert progress.stopped == 1
    assert status.deleted == 1
    assert len(channel.sent) == 1
    assert isinstance(channel.sent[0]["file"], discord.File)
    assert channel.sent[0]["file"].filename == "masamong_image.png"


@pytest.mark.asyncio
async def test_failed_image_result_edits_status_without_attachment():
    handler = object.__new__(AIHandler)
    channel = _Channel()
    status = _Status()
    progress = _Progress()

    response = await handler._deliver_single_image_result(
        message=SimpleNamespace(channel=channel),
        status_msg=status,
        progress=progress,
        image_payload={"error": "provider unavailable"},
        log_extra={},
    )

    assert "provider unavailable" in response
    assert channel.sent == []
    assert status.edits == [response]
    assert status.deleted == 0

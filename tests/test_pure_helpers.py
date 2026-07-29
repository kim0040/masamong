# -*- coding: utf-8 -*-
"""순수 헬퍼 함수(별자리 경계, 메시지 분할)에 대한 단위 테스트.

이전에 무테스트였던 결정적 로직을 커버한다. 특히 get_sign_from_date의
날짜 경계 회귀를 방지한다.
"""

import pytest

from utils.fortune import get_sign_from_date
from utils.discord_helpers import (
    DiscordProgress,
    normalize_discord_text,
    split_message_chunks,
)


class TestZodiacBoundaries:
    """표준 트로피컬 경계 검증 (경계 하루 어긋남 회귀 방지)."""

    def test_capricorn_boundaries(self):
        # 염소자리: 12/22 ~ 1/19
        assert get_sign_from_date(12, 22) == "염소자리"
        assert get_sign_from_date(12, 24) == "염소자리"
        assert get_sign_from_date(12, 31) == "염소자리"
        assert get_sign_from_date(1, 1) == "염소자리"
        assert get_sign_from_date(1, 19) == "염소자리"
        # 경계 바로 바깥
        assert get_sign_from_date(12, 21) == "사수자리"
        assert get_sign_from_date(1, 20) == "물병자리"

    def test_libra_starts_sep_23(self):
        assert get_sign_from_date(9, 22) == "처녀자리"
        assert get_sign_from_date(9, 23) == "천칭자리"
        assert get_sign_from_date(10, 22) == "천칭자리"
        assert get_sign_from_date(10, 23) == "전갈자리"

    def test_all_twelve_signs_reachable(self):
        signs = {get_sign_from_date(m, 15) for m in range(1, 13)}
        # 각 달 중순으로 최소 12개 별자리 중 다양한 값이 나와야 한다.
        assert len(signs) >= 11


class TestSplitMessageChunks:
    def test_empty_returns_empty_list(self):
        assert split_message_chunks("") == []

    def test_short_text_single_chunk(self):
        assert split_message_chunks("안녕하세요") == ["안녕하세요"]

    def test_all_chunks_within_limit(self):
        text = "가나다 " * 2000  # 길이가 큰 텍스트
        chunks = split_message_chunks(text, chunk_size=100)
        assert len(chunks) > 1
        assert all(len(c) <= 100 for c in chunks)

    def test_no_content_lost(self):
        text = "라인1\n라인2\n" + ("단어 " * 500)
        chunks = split_message_chunks(text, chunk_size=120)
        rejoined = "".join(chunks).replace(" ", "").replace("\n", "")
        original = text.replace(" ", "").replace("\n", "")
        assert rejoined == original

    def test_long_word_without_whitespace(self):
        text = "x" * 250
        chunks = split_message_chunks(text, chunk_size=100)
        assert all(len(c) <= 100 for c in chunks)
        assert "".join(chunks) == text

    def test_discord_partial_markdown_normalizes_heading_and_table(self):
        text = (
            "## 오늘 정보\n"
            "| 항목 | 값 |\n"
            "| --- | --- |\n"
            "| 기온 | 21도 |\n"
            "| 강수 | 없음 |"
        )

        normalized = normalize_discord_text(text)

        assert normalized.startswith("**오늘 정보**")
        assert "| --- |" not in normalized
        assert "• **기온** · 값: 21도" in normalized
        assert "• **강수** · 값: 없음" in normalized

    def test_code_block_is_not_rewritten_and_each_chunk_is_balanced(self):
        text = "```python\n" + ("print('# not a heading')\n" * 30) + "```"

        chunks = split_message_chunks(text, chunk_size=120)

        assert len(chunks) > 1
        assert all(len(chunk) <= 120 for chunk in chunks)
        assert all(chunk.count("```") % 2 == 0 for chunk in chunks)
        assert sum(chunk.count("# not a heading") for chunk in chunks) == 30


@pytest.mark.asyncio
async def test_discord_progress_coalesces_fast_updates_and_keeps_latest_phase():
    class FakeMessage:
        def __init__(self):
            self.edits = []

        async def edit(self, **kwargs):
            self.edits.append(kwargs)

    message = FakeMessage()
    progress = DiscordProgress(
        message,
        initial_text="시작",
        min_update_interval_seconds=0.5,
        heartbeat_seconds=30,
    )

    assert await progress.update("빠른 1단계") is False
    assert message.edits == []
    assert progress.current_text == "빠른 1단계"

    progress.last_edit_at -= 1
    assert await progress.update("시간이 걸리는 2단계") is True
    assert message.edits[-1]["content"] == "시간이 걸리는 2단계"
    await progress.stop()


@pytest.mark.asyncio
async def test_discord_progress_uses_native_typing_and_stops_it_once():
    events = []

    class FakeTyping:
        async def __aenter__(self):
            events.append("typing-start")
            return self

        async def __aexit__(self, exc_type, exc, tb):
            events.append("typing-stop")

    class FakeChannel:
        def typing(self):
            return FakeTyping()

    class FakeMessage:
        channel = FakeChannel()

        async def edit(self, **_kwargs):
            return None

    progress = DiscordProgress(
        FakeMessage(),
        initial_text="시작",
        heartbeat_seconds=30,
    )
    await progress.start()
    assert events == ["typing-start"]

    await progress.stop()
    await progress.stop()
    assert events == ["typing-start", "typing-stop"]


@pytest.mark.asyncio
async def test_discord_progress_native_typing_has_a_hard_lifetime_cap():
    events = []

    class FakeTyping:
        async def __aenter__(self):
            events.append("typing-start")

        async def __aexit__(self, exc_type, exc, tb):
            events.append("typing-stop")

    class FakeChannel:
        def typing(self):
            return FakeTyping()

    class FakeMessage:
        channel = FakeChannel()

        async def edit(self, **_kwargs):
            return None

    progress = DiscordProgress(
        FakeMessage(),
        initial_text="시작",
        heartbeat_seconds=30,
        native_typing_max_seconds=10,
    )
    await progress.start()
    assert await progress._expire_native_typing_if_needed(9.9) is False
    assert events == ["typing-start"]
    assert await progress._expire_native_typing_if_needed(10) is True
    assert events == ["typing-start", "typing-stop"]

    # 만료 뒤 start를 다시 불러도 네이티브 keepalive는 부활하지 않는다.
    await progress.start()
    await progress.stop()
    assert events == ["typing-start", "typing-stop"]


@pytest.mark.asyncio
async def test_discord_progress_keeps_working_when_native_typing_fails():
    class BrokenTyping:
        async def __aenter__(self):
            raise RuntimeError("typing unavailable")

    class FakeChannel:
        def typing(self):
            return BrokenTyping()

    class FakeMessage:
        channel = FakeChannel()

        def __init__(self):
            self.edits = []

        async def edit(self, **kwargs):
            self.edits.append(kwargs)

    message = FakeMessage()
    progress = DiscordProgress(
        message,
        initial_text="시작",
        min_update_interval_seconds=0.5,
        heartbeat_seconds=30,
    )
    await progress.start()
    progress.last_edit_at -= 1
    assert await progress.update("계속 진행") is True
    assert message.edits[-1]["content"] == "계속 진행"
    await progress.stop()

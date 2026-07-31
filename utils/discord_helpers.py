# -*- coding: utf-8 -*-
"""
Discord 메시지 처리를 위한 공통 유틸리티 함수 모듈입니다.

메시지 분할 전송, 청크 분할 등 여러 Cog에서 공통으로 사용하는
헬퍼 함수들을 제공합니다.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Union

import discord

from .constants import DISCORD_MESSAGE_LIMIT, SPLIT_MESSAGE_CHUNK_SIZE


_MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
_MARKDOWN_TABLE_SEPARATOR_RE = re.compile(r"^:?-{3,}:?$")
_HTML_BREAK_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_LAUGHTER_RUN_RE = re.compile(r"(ㅋ{5,}|ㅎ{5,})")
_LAUGHTER_MARKER_RE = re.compile(r"(?:ㅋ{2,}|ㅎ{2,})")


class DiscordProgress:
    """긴 Discord 작업의 상태를 낮은 빈도로 갱신합니다.

    단계가 빠르게 바뀌어도 매번 API를 호출하지 않고 최신 단계만 보존합니다.
    지원되는 채널에서는 Discord의 기본 입력 중 애니메이션을 함께 켜고, 작업이
    오래 걸리면 heartbeat가 일정 간격으로 경과 시간을 보여줘 사용자가 멈춘
    것으로 오해하지 않게 합니다. 상태 표시 실패는 본 작업을 실패시키지
    않습니다.
    """

    def __init__(
        self,
        message: discord.Message,
        *,
        initial_text: str,
        min_update_interval_seconds: float = 2.5,
        heartbeat_seconds: float = 12.0,
        native_typing_max_seconds: float = 300.0,
    ) -> None:
        self.message = message
        self.current_text = normalize_discord_text(initial_text)
        self.min_update_interval_seconds = max(
            0.5,
            float(min_update_interval_seconds),
        )
        self.heartbeat_seconds = max(5.0, float(heartbeat_seconds))
        # Discord.py의 native typing context는 닫을 때까지 5초 간격으로
        # 신호를 보낸다. 본 작업의 timeout/finally와 별개로 표시 신호 자체에도
        # 상한을 둬, 외부 호출이 비정상적으로 멈춰도 무한 keepalive가 되지 않는다.
        self.native_typing_max_seconds = max(
            10.0,
            min(900.0, float(native_typing_max_seconds)),
        )
        self.started_at = time.monotonic()
        self.last_edit_at = self.started_at
        self._task: asyncio.Task | None = None
        self._typing_context = None
        self._typing_expired = False
        self._stopped = False
        self._edit_lock = asyncio.Lock()

    async def start(self) -> "DiscordProgress":
        if self._stopped:
            return self
        if self._typing_context is None and not self._typing_expired:
            channel = getattr(self.message, "channel", None)
            typing_factory = getattr(channel, "typing", None)
            if callable(typing_factory):
                try:
                    typing_context = typing_factory()
                    await typing_context.__aenter__()
                except Exception:
                    # 입력 중 표시는 장식적 보조 기능이다. 권한·연결 문제로
                    # 실패해도 상태 메시지와 본 작업은 그대로 진행한다.
                    pass
                else:
                    self._typing_context = typing_context
        if self._task is None:
            self._task = asyncio.create_task(
                self._heartbeat_loop(),
                name="discord-progress-heartbeat",
            )
        return self

    async def update(self, content: str, *, force: bool = False) -> bool:
        """최신 단계를 저장하고 필요할 때만 실제 메시지를 수정합니다."""
        if self._stopped:
            return False
        normalized = normalize_discord_text(content)
        if not normalized:
            return False
        self.current_text = normalized
        elapsed = time.monotonic() - self.last_edit_at
        if not force and elapsed < self.min_update_interval_seconds:
            return False
        return await self._edit(normalized)

    async def stop(self) -> None:
        """heartbeat를 멈춥니다. 여러 번 호출해도 안전합니다."""
        self._stopped = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await self._close_native_typing()

    async def _close_native_typing(self) -> None:
        typing_context, self._typing_context = self._typing_context, None
        if typing_context is not None:
            try:
                await typing_context.__aexit__(None, None, None)
            except Exception:
                # Discord 입력 중 표시 종료 실패도 본 작업의 성공 여부와
                # 무관하며, 클라이언트 표시 자체가 짧은 TTL 뒤 사라진다.
                pass

    async def _expire_native_typing_if_needed(
        self,
        elapsed_seconds: float,
    ) -> bool:
        if elapsed_seconds < self.native_typing_max_seconds:
            return False
        self._typing_expired = True
        await self._close_native_typing()
        return True

    async def _edit(self, content: str) -> bool:
        async with self._edit_lock:
            if self._stopped:
                return False
            try:
                await self.message.edit(
                    content=content[:1900],
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception:
                return False
            self.last_edit_at = time.monotonic()
            return True

    async def _heartbeat_loop(self) -> None:
        try:
            while not self._stopped:
                await asyncio.sleep(self.heartbeat_seconds)
                if self._stopped:
                    return
                elapsed_seconds = max(1, int(time.monotonic() - self.started_at))
                await self._expire_native_typing_if_needed(elapsed_seconds)
                await self._edit(
                    f"{self.current_text}\n⏱️ {elapsed_seconds}초째 하고 있어요. 조금만 더 기다려주세요."
                )
        except asyncio.CancelledError:
            raise


def _table_cells(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _is_table_separator(line: str) -> bool:
    cells = _table_cells(line)
    return bool(cells) and all(
        _MARKDOWN_TABLE_SEPARATOR_RE.fullmatch(cell) for cell in cells
    )


def _normalize_discord_lines(lines: list[str]) -> list[str]:
    """코드블록 밖 텍스트를 Discord에서 안정적인 기본 문법으로 바꿉니다."""
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = _HTML_BREAK_RE.sub("\n", lines[index])

        if (
            index + 1 < len(lines)
            and "|" in line
            and _is_table_separator(lines[index + 1])
        ):
            headers = _table_cells(line)
            index += 2
            converted_rows: list[str] = []
            while index < len(lines) and "|" in lines[index]:
                row = _table_cells(lines[index])
                if not any(row):
                    break
                first = row[0] if row else ""
                details: list[str] = []
                for column_index, value in enumerate(row[1:], start=1):
                    if not value:
                        continue
                    label = (
                        headers[column_index]
                        if column_index < len(headers)
                        else f"항목 {column_index + 1}"
                    )
                    details.append(f"{label}: {value}")
                if first and details:
                    converted_rows.append(
                        f"• **{first}** · " + " · ".join(details)
                    )
                elif first:
                    converted_rows.append(f"• {first}")
                elif details:
                    converted_rows.append("• " + " · ".join(details))
                index += 1
            output.extend(converted_rows or [" · ".join(headers)])
            continue

        heading = _MARKDOWN_HEADING_RE.match(line)
        if heading:
            title = heading.group(1).strip("* ")
            output.append(f"**{title}**")
        else:
            output.extend(line.splitlines() or [""])
        index += 1
    return output


def normalize_discord_text(text: str) -> str:
    """Discord 일반 메시지에서 일관되게 보이는 보수적 Markdown으로 정규화합니다.

    Discord 클라이언트별 차이가 큰 표/HTML/헤더는 굵은 제목과 일반 목록으로
    바꾸되, 사용자가 요청한 코드블록 내부는 원문을 그대로 보존합니다.
    """
    if not text:
        return ""

    normalized_lines: list[str] = []
    plain_lines: list[str] = []
    in_code_fence = False
    laughter_markers = 0

    def normalize_laughter(raw_text: str) -> str:
        """생성 오류성 웃음 도배를 접되 코드블록은 호출부에서 제외합니다."""
        nonlocal laughter_markers
        collapsed = _LAUGHTER_RUN_RE.sub(
            lambda match: match.group(0)[0] * 4,
            raw_text,
        )

        def limit_markers(match: re.Match[str]) -> str:
            nonlocal laughter_markers
            laughter_markers += 1
            if laughter_markers <= 2:
                return match.group(0)
            return ""

        return _LAUGHTER_MARKER_RE.sub(limit_markers, collapsed)

    def flush_plain() -> None:
        if plain_lines:
            normalized_lines.extend(_normalize_discord_lines(plain_lines))
            plain_lines.clear()

    for raw_line in str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw_line.lstrip().startswith("```"):
            flush_plain()
            normalized_lines.append(raw_line)
            in_code_fence = not in_code_fence
        elif in_code_fence:
            normalized_lines.append(raw_line)
        else:
            plain_lines.append(normalize_laughter(raw_line))
    flush_plain()

    result = "\n".join(normalized_lines).strip()
    return re.sub(r"\n{3,}", "\n\n", result)


def clip_discord_text(text: str, limit: int, *, suffix: str = "…") -> str:
    """Discord 필드 제한에 맞춰 문자열을 자르고 생략 사실을 표시합니다."""
    normalized = normalize_discord_text(text)
    if len(normalized) <= limit:
        return normalized
    if limit <= len(suffix):
        return suffix[:limit]
    return normalized[: limit - len(suffix)].rstrip() + suffix


def _balance_code_fences(chunks: list[str], chunk_size: int) -> list[str]:
    """청크마다 코드블록을 닫고 다시 열어 렌더링 누수를 막습니다."""
    if not chunks:
        return chunks

    balanced: list[str] = []
    open_fence: str | None = None
    for original in chunks:
        chunk = original
        if open_fence:
            chunk = f"{open_fence}\n{chunk}"

        for match in re.finditer(r"(?m)^\s*(```[^\n`]*)\s*$", original):
            token = match.group(1).strip()
            if open_fence is None:
                open_fence = token[:48] or "```"
            else:
                open_fence = None

        if open_fence:
            chunk += "\n```"
        if len(chunk) > chunk_size:
            # 코드 fence 여유분을 예약해 분할하므로 정상적으로는 도달하지 않는다.
            chunk = chunk[:chunk_size]
        balanced.append(chunk)
    return balanced


def split_message_chunks(text: str, chunk_size: int = SPLIT_MESSAGE_CHUNK_SIZE) -> list[str]:
    """Discord 메시지 제한보다 작은 단위로 텍스트를 나눕니다.

    줄바꿈, 문단, 공백 등 자연스러운 위치에서 분할합니다.

    Args:
        text: 분할할 텍스트
        chunk_size: 각 청크의 최대 길이 (기본 1900자)

    Returns:
        분할된 텍스트 리스트
    """
    normalized = normalize_discord_text(text)
    if not normalized:
        return []

    has_code_fence = "```" in normalized
    content_chunk_size = (
        max(32, chunk_size - 56) if has_code_fence else chunk_size
    )
    chunks: list[str] = []
    remaining = normalized
    while remaining:
        if len(remaining) <= content_chunk_size:
            chunks.append(remaining)
            break

        if has_code_fence:
            split_at = max(
                remaining.rfind("\n\n", 0, content_chunk_size),
                remaining.rfind("\n", 0, content_chunk_size),
            )
        else:
            split_at = max(
                remaining.rfind("\n\n", 0, content_chunk_size),
                remaining.rfind("\n", 0, content_chunk_size),
                remaining.rfind(" ", 0, content_chunk_size),
            )
        if split_at <= 0 or (
            not has_code_fence
            and split_at < content_chunk_size // 2
        ):
            split_at = content_chunk_size

        chunk = remaining[:split_at].rstrip()
        if not chunk:
            chunk = remaining[:content_chunk_size]
            split_at = content_chunk_size
        chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()

    if (
        has_code_fence
        and len(chunks) > 1
        and re.fullmatch(r"```[^\n`]*", chunks[-1].strip())
        and len(chunks[-2]) + 1 + len(chunks[-1]) <= chunk_size - 4
    ):
        chunks[-2] = f"{chunks[-2]}\n{chunks[-1]}"
        chunks.pop()
    return _balance_code_fences(chunks, chunk_size) if has_code_fence else chunks


async def send_split_message(
    destination: Union[discord.Message, discord.TextChannel, discord.Thread, discord.DMChannel],
    text: str,
    chunk_size: int = SPLIT_MESSAGE_CHUNK_SIZE,
    delay: float = 0.5,
) -> list[discord.Message]:
    """Discord 메시지 길이 제한을 준수하여 텍스트를 분할 전송합니다.

    Args:
        destination: 메시지를 보낼 대상 (Message, TextChannel 등)
        text: 전송할 텍스트
        chunk_size: 각 청크의 최대 길이
        delay: 청크 간 전송 딜레이 (초)

    Returns:
        전송된 메시지 리스트
    """
    chunks = split_message_chunks(text, chunk_size)
    if not chunks:
        return []

    sent: list[discord.Message] = []
    for chunk in chunks:
        msg = await destination.send(
            chunk,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        sent.append(msg)
        if len(chunks) > 1:
            await asyncio.sleep(delay)

    return sent

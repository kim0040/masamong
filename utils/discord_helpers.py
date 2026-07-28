# -*- coding: utf-8 -*-
"""
Discord 메시지 처리를 위한 공통 유틸리티 함수 모듈입니다.

메시지 분할 전송, 청크 분할 등 여러 Cog에서 공통으로 사용하는
헬퍼 함수들을 제공합니다.
"""

from __future__ import annotations

import asyncio
import re
from typing import Union

import discord

from .constants import DISCORD_MESSAGE_LIMIT, SPLIT_MESSAGE_CHUNK_SIZE


_MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
_MARKDOWN_TABLE_SEPARATOR_RE = re.compile(r"^:?-{3,}:?$")
_HTML_BREAK_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


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
            plain_lines.append(raw_line)
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

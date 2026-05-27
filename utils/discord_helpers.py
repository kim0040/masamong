# -*- coding: utf-8 -*-
"""
Discord 메시지 처리를 위한 공통 유틸리티 함수 모듈입니다.

메시지 분할 전송, 청크 분할 등 여러 Cog에서 공통으로 사용하는
헬퍼 함수들을 제공합니다.
"""

from __future__ import annotations

import asyncio
from typing import Union

import discord

from .constants import DISCORD_MESSAGE_LIMIT, SPLIT_MESSAGE_CHUNK_SIZE


def split_message_chunks(text: str, chunk_size: int = SPLIT_MESSAGE_CHUNK_SIZE) -> list[str]:
    """Discord 메시지 제한보다 작은 단위로 텍스트를 나눕니다.

    줄바꿈, 문단, 공백 등 자연스러운 위치에서 분할합니다.

    Args:
        text: 분할할 텍스트
        chunk_size: 각 청크의 최대 길이 (기본 1900자)

    Returns:
        분할된 텍스트 리스트
    """
    if not text:
        return []

    chunks: list[str] = []
    remaining = str(text).strip()
    while remaining:
        if len(remaining) <= chunk_size:
            chunks.append(remaining)
            break

        split_at = max(
            remaining.rfind("\n\n", 0, chunk_size),
            remaining.rfind("\n", 0, chunk_size),
            remaining.rfind(" ", 0, chunk_size),
        )
        if split_at < chunk_size // 2:
            split_at = chunk_size

        chunk = remaining[:split_at].rstrip()
        if not chunk:
            chunk = remaining[:chunk_size]
            split_at = chunk_size
        chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()

    return chunks


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
        msg = await destination.send(chunk)
        sent.append(msg)
        if len(chunks) > 1:
            await asyncio.sleep(delay)

    return sent

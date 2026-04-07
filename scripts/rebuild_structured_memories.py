#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""기존 대화 로그를 구조화 메모리 테이블로 재구성한다."""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict, deque
import sqlite3
from pathlib import Path

import config
from utils.embeddings import DiscordEmbeddingStore, get_embedding
from utils.memory_units import build_structured_memory_units


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", default=config.DATABASE_FILE)
    parser.add_argument("--target-db", default=config.DISCORD_EMBEDDING_DB_PATH)
    parser.add_argument("--clear", action="store_true", help="기존 구조화 메모리를 비우고 다시 생성")
    parser.add_argument("--guild-id", type=int, default=None)
    parser.add_argument("--channel-id", type=int, default=None)
    return parser.parse_args()


def load_history_rows(source_db: Path, guild_id: int | None, channel_id: int | None) -> dict[tuple[int, int], list[dict[str, object]]]:
    conn = sqlite3.connect(source_db)
    conn.row_factory = sqlite3.Row
    try:
        clauses: list[str] = []
        params: list[object] = []
        if guild_id is not None:
            clauses.append("guild_id = ?")
            params.append(guild_id)
        if channel_id is not None:
            clauses.append("channel_id = ?")
            params.append(channel_id)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"""
            SELECT guild_id, channel_id, message_id, user_id, user_name, content, is_bot, created_at
            FROM conversation_history
            {where_sql}
            ORDER BY guild_id ASC, channel_id ASC, created_at ASC, message_id ASC
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    grouped: dict[tuple[int, int], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        key = (int(row["guild_id"]), int(row["channel_id"]))
        grouped[key].append(
            {
                "guild_id": int(row["guild_id"]),
                "channel_id": int(row["channel_id"]),
                "message_id": int(row["message_id"]),
                "user_id": int(row["user_id"]),
                "user_name": row["user_name"] or "Unknown",
                "content": row["content"] or "",
                "is_bot": bool(row["is_bot"]),
                "created_at": row["created_at"] or "",
            }
        )
    return grouped


async def rebuild_channel_memories(
    store: DiscordEmbeddingStore,
    *,
    guild_id: int,
    channel_id: int,
    rows: list[dict[str, object]],
) -> tuple[int, int]:
    window_size = max(1, getattr(config, "CONVERSATION_WINDOW_SIZE", 12))
    stride = max(1, getattr(config, "CONVERSATION_WINDOW_STRIDE", 6))
    max_chars = max(300, getattr(config, "CONVERSATION_WINDOW_MAX_CHARS", 3000))
    buffer: deque[dict[str, object]] = deque(maxlen=window_size)
    inserted_units = 0
    created_windows = 0

    for index, row in enumerate(rows, start=1):
        buffer.append(dict(row))
        total_chars = sum(len(str(item.get("content") or "")) for item in buffer)
        is_full = len(buffer) >= window_size
        is_heavy = total_chars >= max_chars
        if not is_full and not is_heavy:
            continue
        if not is_heavy and (index - window_size) % stride != 0:
            continue

        created_windows += 1
        units = build_structured_memory_units(
            list(buffer),
            channel_id=channel_id,
            max_summary_chars=getattr(config, "STRUCTURED_MEMORY_MAX_SUMMARY_CHARS", 320),
            max_context_chars=getattr(config, "STRUCTURED_MEMORY_MAX_CONTEXT_CHARS", 1200),
            user_turn_min_chars=getattr(config, "STRUCTURED_USER_MEMORY_MIN_CHARS", 12),
        )
        for unit in units:
            embedding = await get_embedding(unit.memory_text, prefix="passage: ")
            if embedding is None:
                continue
            await store.upsert_memory_entry(
                memory_id=unit.memory_id,
                anchor_message_id=unit.anchor_message_id,
                server_id=guild_id,
                channel_id=channel_id,
                owner_user_id=unit.owner_user_id,
                owner_user_name=unit.owner_user_name,
                memory_scope=unit.memory_scope,
                memory_type=unit.memory_type,
                summary_text=unit.summary_text,
                memory_text=unit.memory_text,
                raw_context=unit.raw_context,
                source_message_ids=unit.source_message_ids,
                speaker_names=unit.speaker_names,
                keywords=unit.keywords,
                timestamp_iso=unit.timestamp_iso,
                embedding=embedding,
            )
            inserted_units += 1

    return created_windows, inserted_units


async def main() -> None:
    args = parse_args()
    source_db = Path(args.source_db).resolve()
    grouped = load_history_rows(source_db, args.guild_id, args.channel_id)
    store = DiscordEmbeddingStore(str(Path(args.target_db).resolve()))
    await store.initialize()
    if args.clear:
        await store.clear_memory_entries()

    total_channels = 0
    total_windows = 0
    total_units = 0
    for (guild_id, channel_id), rows in grouped.items():
        windows, units = await rebuild_channel_memories(
            store,
            guild_id=guild_id,
            channel_id=channel_id,
            rows=rows,
        )
        total_channels += 1
        total_windows += windows
        total_units += units
        print(f"[channel] guild={guild_id} channel={channel_id} windows={windows} units={units}")

    count = await store.count_memory_entries()
    print(f"[done] channels={total_channels} windows={total_windows} units={total_units} stored={count}")


if __name__ == "__main__":
    asyncio.run(main())

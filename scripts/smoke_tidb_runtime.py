#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TiDB 런타임 스모크 테스트."""

from __future__ import annotations

import asyncio
import argparse

import numpy as np

import config
from database.compat_db import TiDBSettings, connect_main_db
from utils.coords import get_coords_from_db
from utils import db as db_utils
from utils.embeddings import DiscordEmbeddingStore, KakaoEmbeddingStore, get_embedding


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-check", action="store_true")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    db = await connect_main_db(
        config.DB_BACKEND,
        sqlite_path=config.DATABASE_FILE,
        tidb_settings=TiDBSettings(
            host=config.TIDB_HOST or "",
            port=config.TIDB_PORT,
            user=config.TIDB_USER or "",
            password=config.TIDB_PASSWORD or "",
            database=config.TIDB_NAME,
            ssl_ca=config.TIDB_SSL_CA,
            ssl_verify_identity=config.TIDB_SSL_VERIFY_IDENTITY,
        ),
    )
    try:
        cursor = await db.execute("SELECT COUNT(*) AS cnt FROM conversation_history")
        row = await cursor.fetchone()
        print("conversation_history", row[0] if row else None)

        coords = await get_coords_from_db(db, "서울")
        print("coords", coords)

        discord_store = DiscordEmbeddingStore(config.DISCORD_EMBEDDING_DB_PATH)
        discord_rows = await discord_store.fetch_recent_embeddings(
            server_id=659398210275770368,
            channel_id=659398210980151307,
            limit=3,
        )
        print("discord_rows", len(discord_rows))
        discord_memory_rows = await discord_store.fetch_recent_memory_entries(
            server_id=659398210275770368,
            channel_id=659398210980151307,
            limit=3,
        )
        print("discord_memory_rows", len(discord_memory_rows))

        query_vector = await get_embedding("운전면허", prefix="query: ")
        if query_vector is None:
            raise RuntimeError("쿼리 임베딩 생성 실패")
        kakao_store = KakaoEmbeddingStore(config.KAKAO_EMBEDDING_DB_PATH, config.KAKAO_EMBEDDING_SERVER_MAP)
        kakao_rows = await kakao_store.fetch_recent_embeddings(
            ["912210558122598450"],
            limit=3,
            query_vector=query_vector,
        )
        print("kakao_rows", len(kakao_rows), kakao_rows[0]["message_id"] if kakao_rows else None)

        if args.write_check:
            test_guild_id = 999999999999001
            test_user_id = 999999999999002

            await db_utils.set_guild_setting(db, test_guild_id, "persona_text", "tidb smoke")
            persona = await db_utils.get_guild_setting(db, test_guild_id, "persona_text")
            print("guild_setting_write", persona)

            await db.execute(
                "REPLACE INTO user_profiles (user_id, birth_date, birth_time, gender, birth_place, created_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP(6))",
                (test_user_id, "1990-01-01", "07:30", "M", "서울"),
            )
            await db.commit()
            cursor = await db.execute("SELECT birth_place FROM user_profiles WHERE user_id = ?", (test_user_id,))
            row = await cursor.fetchone()
            print("user_profile_write", row[0] if row else None)

            dummy = np.zeros(384, dtype=np.float32)
            await discord_store.upsert_message_embedding(
                message_id=990000000000001,
                server_id=test_guild_id,
                channel_id=test_guild_id,
                user_id=test_user_id,
                user_name="smoke",
                message="smoke",
                timestamp_iso="2026-04-02T00:00:00+00:00",
                embedding=dummy,
            )
            discord_test_rows = await discord_store.fetch_recent_embeddings(
                server_id=test_guild_id,
                channel_id=test_guild_id,
                limit=1,
            )
            print("discord_write", len(discord_test_rows))
            await discord_store.upsert_memory_entry(
                memory_id="smoke:memory:1",
                anchor_message_id=990000000000001,
                server_id=test_guild_id,
                channel_id=test_guild_id,
                owner_user_id=test_user_id,
                owner_user_name="smoke",
                memory_scope="user",
                memory_type="conversation",
                summary_text="smoke memory",
                memory_text="smoke memory",
                raw_context="smoke: memory context",
                source_message_ids=[990000000000001],
                speaker_names=["smoke"],
                keywords=["smoke"],
                timestamp_iso="2026-04-02T00:00:00+00:00",
                embedding=dummy,
            )
            memory_count = await discord_store.count_memory_entries(
                server_id=test_guild_id,
                channel_id=test_guild_id,
            )
            print("discord_memory_write", memory_count)

            await discord_store.delete_embeddings([990000000000001])
            await discord_store.delete_memory_entries(["smoke:memory:1"])
            await db.execute("DELETE FROM guild_settings WHERE guild_id = ?", (test_guild_id,))
            await db.execute("DELETE FROM user_profiles WHERE user_id = ?", (test_user_id,))
            await db.commit()
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())

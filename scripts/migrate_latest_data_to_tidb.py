#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""최신 운영 데이터셋을 TiDB `masamong` DB로 적재한다."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import numpy as np
import pymysql

import config
from database.compat_db import TiDBSettings, split_sql_script


SOURCE_MAIN_TABLES = [
    "guild_settings",
    "user_activity",
    "conversation_history",
    "conversation_windows",
    "system_counters",
    "api_call_log",
    "analytics_log",
    "conversation_history_archive",
    "user_preferences",
    "locations",
    "user_profiles",
    "dm_usage_logs",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", default="임시", help="최신 운영 데이터셋 루트")
    parser.add_argument("--skip-main", action="store_true")
    parser.add_argument("--skip-discord", action="store_true")
    parser.add_argument("--skip-kakao", action="store_true")
    parser.add_argument("--truncate", action="store_true", help="적재 전 대상 테이블 비우기")
    return parser.parse_args()


def connect_tidb() -> pymysql.connections.Connection:
    settings = TiDBSettings.from_env()
    conn = pymysql.connect(**settings.to_connect_kwargs())
    with conn.cursor() as cursor:
        cursor.execute("SET @@allow_auto_random_explicit_insert = true")
    conn.commit()
    return conn


def apply_schema(conn: pymysql.connections.Connection) -> None:
    schema_path = Path(__file__).resolve().parents[1] / "database" / "schema_tidb.sql"
    statements = split_sql_script(schema_path.read_text(encoding="utf-8"))
    with conn.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)
    conn.commit()


def recreate_tables(conn: pymysql.connections.Connection) -> None:
    ordered = [
        "kakao_chunks",
        "discord_memory_entries",
        "discord_chat_embeddings",
        "analytics_log",
        "api_call_log",
        "conversation_windows",
        "conversation_history_archive",
        "conversation_history",
        "user_activity",
        "guild_settings",
        "system_counters",
        "user_preferences",
        "locations",
        "user_profiles",
        "dm_usage_logs",
    ]
    with conn.cursor() as cursor:
        for table in ordered:
            cursor.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()


def migrate_sqlite_tables(source_db: Path, conn: pymysql.connections.Connection) -> None:
    src = sqlite3.connect(source_db)
    src.row_factory = sqlite3.Row
    try:
        for table in SOURCE_MAIN_TABLES:
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                print(f"[main] {table}: 0 rows")
                continue
            columns = list(rows[0].keys())
            placeholder = ", ".join(["%s"] * len(columns))
            insert_sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholder})"
            payload = [tuple(row[col] for col in columns) for row in rows]
            with conn.cursor() as cursor:
                cursor.executemany(insert_sql, payload)
            conn.commit()
            print(f"[main] {table}: {len(rows)} rows")
    finally:
        src.close()


def migrate_discord_embeddings(source_db: Path, conn: pymysql.connections.Connection) -> None:
    src = sqlite3.connect(source_db)
    src.row_factory = sqlite3.Row
    try:
        rows = src.execute(
            """
            SELECT message_id, server_id, channel_id, user_id, user_name, message, timestamp, embedding
            FROM discord_chat_embeddings
            ORDER BY id ASC
            """
        ).fetchall()
        if not rows:
            print("[discord] 0 rows")
            return
        payload = [
            (
                row["message_id"],
                row["server_id"],
                row["channel_id"],
                row["user_id"],
                row["user_name"],
                row["message"],
                row["timestamp"],
                row["embedding"],
            )
            for row in rows
        ]
        sql = """
            INSERT INTO discord_chat_embeddings (
                message_id, server_id, channel_id, user_id, user_name, message, timestamp, embedding
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                user_name = VALUES(user_name),
                message = VALUES(message),
                timestamp = VALUES(timestamp),
                embedding = VALUES(embedding)
        """
        with conn.cursor() as cursor:
            cursor.executemany(sql, payload)
        conn.commit()
        print(f"[discord] {len(rows)} rows")
    finally:
        src.close()


def migrate_discord_memory_entries(source_db: Path, conn: pymysql.connections.Connection) -> None:
    src = sqlite3.connect(source_db)
    src.row_factory = sqlite3.Row
    try:
        table_row = src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='discord_memory_entries'"
        ).fetchone()
        if not table_row:
            print("[discord_memory] table missing")
            return
        rows = src.execute(
            """
            SELECT memory_id, anchor_message_id, server_id, channel_id, owner_user_id, owner_user_name,
                   memory_scope, memory_type, summary_text, memory_text, raw_context, source_message_ids,
                   speaker_names, keyword_json, timestamp, embedding
            FROM discord_memory_entries
            ORDER BY id ASC
            """
        ).fetchall()
        if not rows:
            print("[discord_memory] 0 rows")
            return
        payload = [
            (
                row["memory_id"],
                row["anchor_message_id"],
                row["server_id"],
                row["channel_id"],
                row["owner_user_id"],
                row["owner_user_name"],
                row["memory_scope"],
                row["memory_type"],
                row["summary_text"],
                row["memory_text"],
                row["raw_context"],
                row["source_message_ids"],
                row["speaker_names"],
                row["keyword_json"],
                row["timestamp"],
                row["embedding"],
            )
            for row in rows
        ]
        sql = """
            INSERT INTO discord_memory_entries (
                memory_id, anchor_message_id, server_id, channel_id, owner_user_id, owner_user_name,
                memory_scope, memory_type, summary_text, memory_text, raw_context, source_message_ids,
                speaker_names, keyword_json, timestamp, embedding
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                owner_user_id = VALUES(owner_user_id),
                owner_user_name = VALUES(owner_user_name),
                memory_scope = VALUES(memory_scope),
                memory_type = VALUES(memory_type),
                summary_text = VALUES(summary_text),
                memory_text = VALUES(memory_text),
                raw_context = VALUES(raw_context),
                source_message_ids = VALUES(source_message_ids),
                speaker_names = VALUES(speaker_names),
                keyword_json = VALUES(keyword_json),
                timestamp = VALUES(timestamp),
                embedding = VALUES(embedding)
        """
        with conn.cursor() as cursor:
            cursor.executemany(sql, payload)
        conn.commit()
        print(f"[discord_memory] {len(rows)} rows")
    finally:
        src.close()


def _room_label(room_key: str) -> str:
    for meta in config.KAKAO_EMBEDDING_SERVER_MAP.values():
        candidate_room_key = str(meta.get("room_key") or "").strip()
        db_path = meta.get("db_path") or ""
        if candidate_room_key == room_key or Path(db_path).name == room_key:
            return meta.get("label") or room_key
    return room_key


def migrate_kakao_store(source_root: Path, conn: pymysql.connections.Connection) -> None:
    rooms_root = source_root / "kakao_store"
    room_dirs = sorted(path for path in rooms_root.iterdir() if path.is_dir())
    total = 0
    for room_dir in room_dirs:
        room_key = room_dir.name
        metadata = json.loads((room_dir / "metadata.json").read_text(encoding="utf-8"))
        vectors = np.load(room_dir / "vectors.npy", mmap_mode="r")
        label = _room_label(room_key)

        insert_sql = """
            INSERT INTO kakao_chunks (
                room_key, source_room_label, chunk_id, session_id,
                start_date, message_count, summary, text_long, embedding
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                session_id = VALUES(session_id),
                start_date = VALUES(start_date),
                message_count = VALUES(message_count),
                summary = VALUES(summary),
                text_long = VALUES(text_long),
                embedding = VALUES(embedding)
        """
        batch: list[tuple[object, ...]] = []
        with conn.cursor() as cursor:
            for idx, item in enumerate(metadata):
                vector_literal = "[" + ",".join(f"{float(v):.8f}" for v in vectors[idx].tolist()) + "]"
                batch.append(
                    (
                        room_key,
                        label,
                        int(item.get("id", idx)),
                        item.get("session_id"),
                        item.get("start_date"),
                        item.get("message_count"),
                        item.get("summary"),
                        item.get("text"),
                        vector_literal,
                    )
                )
                if len(batch) >= 250:
                    cursor.executemany(insert_sql, batch)
                    conn.commit()
                    batch.clear()
            if batch:
                cursor.executemany(insert_sql, batch)
                conn.commit()
        total += len(metadata)
        print(f"[kakao] {room_key}: {len(metadata)} rows")
    print(f"[kakao] total: {total} rows")


def main() -> None:
    args = parse_args()
    source_root = Path(args.source_root).resolve()
    main_db = source_root / "remasamong.db"
    discord_db = source_root / "discord_embeddings.db"

    conn = connect_tidb()
    try:
        if args.truncate:
            recreate_tables(conn)
        apply_schema(conn)
        if not args.skip_main:
            migrate_sqlite_tables(main_db, conn)
        if not args.skip_discord and discord_db.exists():
            migrate_discord_embeddings(discord_db, conn)
            migrate_discord_memory_entries(discord_db, conn)
        if not args.skip_kakao and (source_root / "kakao_store").exists():
            migrate_kakao_store(source_root, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""로컬 최신 데이터셋과 TiDB 적재본의 핵심 parity를 비교한다."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from pathlib import Path

import numpy as np
import pymysql

from database.compat_db import TiDBSettings
from utils.embeddings import get_embedding


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", default="임시")
    parser.add_argument("--top-k", type=int, default=5)
    return parser.parse_args()


def connect_tidb() -> pymysql.connections.Connection:
    return pymysql.connect(**TiDBSettings.from_env().to_connect_kwargs())


def cosine_top_ids(rows: list[dict], query_vector: np.ndarray, top_k: int) -> list[str]:
    ranked: list[tuple[str, float]] = []
    norm_q = np.linalg.norm(query_vector)
    for row in rows:
        emb = row.get("embedding")
        if emb is None:
            continue
        vector = np.frombuffer(emb, dtype=np.float32)
        norm_v = np.linalg.norm(vector)
        if norm_v == 0 or norm_q == 0:
            continue
        score = float(np.dot(vector, query_vector) / (norm_v * norm_q))
        ranked.append((str(row["message_id"]), score))
    ranked.sort(key=lambda item: item[1], reverse=True)
    return [item[0] for item in ranked[:top_k]]


def latest_discord_scope(source_db: Path) -> tuple[str, str]:
    conn = sqlite3.connect(source_db)
    try:
        row = conn.execute(
            """
            SELECT server_id, channel_id, COUNT(*) AS cnt
            FROM discord_chat_embeddings
            GROUP BY server_id, channel_id
            ORDER BY cnt DESC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            raise RuntimeError("discord_chat_embeddings 데이터가 없습니다.")
        return str(row[0]), str(row[1])
    finally:
        conn.close()


def fetch_local_discord_rows(source_db: Path, server_id: str, channel_id: str) -> list[dict]:
    conn = sqlite3.connect(source_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT message_id, user_id, user_name, message, timestamp, embedding
            FROM discord_chat_embeddings
            WHERE server_id = ? AND channel_id = ?
            ORDER BY timestamp DESC
            LIMIT 200
            """,
            (server_id, channel_id),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def fetch_remote_discord_rows(conn: pymysql.connections.Connection, server_id: str, channel_id: str) -> list[dict]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT message_id, user_id, user_name, message, timestamp, embedding
            FROM discord_chat_embeddings
            WHERE server_id = %s AND channel_id = %s
            ORDER BY timestamp DESC
            LIMIT 200
            """,
            (server_id, channel_id),
        )
        return list(cursor.fetchall())


def fetch_local_kakao(room_dir: Path, query_vector: np.ndarray, top_k: int) -> list[str]:
    metadata = json.loads((room_dir / "metadata.json").read_text(encoding="utf-8"))
    vectors = np.load(room_dir / "vectors.npy", mmap_mode="r")
    norm_q = np.linalg.norm(query_vector)
    norm_v = np.linalg.norm(vectors, axis=1)
    norm_v[norm_v == 0] = 1e-10
    similarities = np.dot(vectors, query_vector) / (norm_v * norm_q)
    indices = np.argsort(similarities)[::-1][:top_k]
    return [str(metadata[idx].get("id")) for idx in indices]


def fetch_remote_kakao(conn: pymysql.connections.Connection, room_key: str, query_vector: np.ndarray, top_k: int) -> list[str]:
    vector_literal = "[" + ",".join(f"{float(v):.8f}" for v in query_vector.tolist()) + "]"
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT chunk_id
            FROM kakao_chunks
            WHERE room_key = %s
            ORDER BY VEC_COSINE_DISTANCE(embedding, %s) ASC
            LIMIT %s
            """,
            (room_key, vector_literal, top_k),
        )
        rows = cursor.fetchall()
    return [str(row["chunk_id"]) for row in rows]


async def main() -> None:
    args = parse_args()
    source_root = Path(args.source_root).resolve()
    discord_db = source_root / "discord_embeddings.db"
    kakao_root = source_root / "kakao_store"

    remote = connect_tidb()
    try:
        server_id, channel_id = latest_discord_scope(discord_db)
        local_discord = fetch_local_discord_rows(discord_db, server_id, channel_id)
        remote_discord = fetch_remote_discord_rows(remote, server_id, channel_id)

        queries = ["운전면허", "병원옥상", "사진 4장", "초대했습니다"]
        print(f"[discord] scope server={server_id} channel={channel_id}")
        for query in queries:
            vector = await get_embedding(query, prefix="query: ")
            if vector is None:
                raise RuntimeError("쿼리 임베딩 생성 실패")
            local_ids = cosine_top_ids(local_discord, vector, args.top_k)
            remote_ids = cosine_top_ids(remote_discord, vector, args.top_k)
            print(f"[discord] {query} -> local={local_ids} remote={remote_ids} match={local_ids == remote_ids}")

        for room_key in ["room1", "room2"]:
            room_dir = kakao_root / room_key
            print(f"[kakao] room={room_key}")
            for query in queries:
                vector = await get_embedding(query, prefix="query: ")
                if vector is None:
                    raise RuntimeError("쿼리 임베딩 생성 실패")
                local_ids = fetch_local_kakao(room_dir, vector, args.top_k)
                remote_ids = fetch_remote_kakao(remote, room_key, vector, args.top_k)
                print(f"[kakao] {query} -> local={local_ids} remote={remote_ids} match={local_ids == remote_ids}")
    finally:
        remote.close()


if __name__ == "__main__":
    asyncio.run(main())

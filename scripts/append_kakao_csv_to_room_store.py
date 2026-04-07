#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Append a KakaoTalk CSV export into an existing room store and TiDB."""

from __future__ import annotations

import asyncio
import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import numpy as np
import pymysql

import config
from scripts.generate_kakao_embeddings_v2 import (
    DEFAULT_MODEL_NAME,
    SUMMARIZATION_MODELS,
    KakaoSessionEmbedder,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Append Kakao CSV data into an existing room store and TiDB.")
    parser.add_argument(
        "--csv",
        default="/Users/gimhyeonmin/PycharmProjects/masamong/임시/KakaoTalk_Chat_노답형제들_2026-04-02-17-20-44.csv",
        help="Path to the KakaoTalk CSV export.",
    )
    parser.add_argument(
        "--room-dir",
        default="/Users/gimhyeonmin/PycharmProjects/masamong/임시/kakao_store/room1",
        help="Existing local room store directory containing metadata.json and vectors.npy.",
    )
    parser.add_argument("--room-key", default="room1", help="TiDB room key to append into.")
    parser.add_argument("--room-label", default="room1", help="Human-friendly source room label stored in TiDB.")
    parser.add_argument(
        "--model",
        default=getattr(config, "LOCAL_EMBEDDING_MODEL_NAME", DEFAULT_MODEL_NAME),
        help="SentenceTransformer model name for embeddings.",
    )
    parser.add_argument(
        "--summary-model",
        default="1",
        choices=sorted(SUMMARIZATION_MODELS.keys()),
        help="Summary model profile key from generate_kakao_embeddings_v2.py.",
    )
    parser.add_argument(
        "--tidb-table",
        default=getattr(config, "KAKAO_TIDB_TABLE", "kakao_chunks"),
        help="TiDB table name for Kakao chunks.",
    )
    return parser.parse_args()


async def build_chunks_with_v2(args: argparse.Namespace) -> list[dict[str, Any]]:
    api_key = os.environ.get("COMETAPI_KEY") or getattr(config, "COMETAPI_KEY", None)
    if not api_key:
        raise RuntimeError("COMETAPI_KEY가 없어 기존 Kakao V2 요약 파이프라인을 실행할 수 없습니다.")

    summary_model_config = SUMMARIZATION_MODELS[str(args.summary_model)]
    embedder = KakaoSessionEmbedder(
        args.model,
        api_key,
        os.environ.get("COMETAPI_BASE_URL") or getattr(config, "COMETAPI_BASE_URL", "https://api.cometapi.com/v1"),
        summary_model_config,
    )

    df = embedder.load_csv(str(Path(args.csv).resolve()))
    if df.empty:
        return []

    sessions = embedder.group_into_sessions(df)
    if not sessions:
        return []

    semaphore = asyncio.Semaphore(5)
    request_timeout = max(15, int(getattr(config, "AI_REQUEST_TIMEOUT", 45)))

    async def _summarize(idx: int, session: dict[str, Any]) -> dict[str, Any]:
        try:
            summary = await asyncio.wait_for(
                embedder.summarize_session(session["full_text"], semaphore),
                timeout=request_timeout,
            )
        except asyncio.TimeoutError:
            summary = session["full_text"][:500].replace("\n", " ")
            print(f"[summary-timeout] session={idx} start={session['start_date']}")
        return {
            "id": idx,
            "summary": summary,
            "original_text": session["full_text"],
            "start_date": str(session["start_date"]),
            "end_date": str(session["end_date"]),
            "message_count": session["message_count"],
        }

    summarized_sessions: list[dict[str, Any]] = []
    batch_size = 10
    for offset in range(0, len(sessions), batch_size):
        batch = sessions[offset : offset + batch_size]
        summarized_sessions.extend(
            await asyncio.gather(*[_summarize(offset + idx, session) for idx, session in enumerate(batch)])
        )
        print(f"[summaries] completed={len(summarized_sessions)}/{len(sessions)}")

    summarized_sessions.sort(key=lambda item: int(item["id"]))
    all_chunks: list[dict[str, Any]] = []
    for session in summarized_sessions:
        for chunk in embedder.chunk_session(session):
            chunk["source_session_id"] = chunk.pop("session_id")
            all_chunks.append(chunk)
    return all_chunks


def fingerprint(item: dict[str, Any]) -> str:
    payload = f"{item.get('start_date', '')}\x1f{item.get('text', '')}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_existing_store(room_dir: Path) -> tuple[list[dict[str, Any]], np.ndarray]:
    metadata = json.loads((room_dir / "metadata.json").read_text(encoding="utf-8"))
    vectors = np.load(room_dir / "vectors.npy")
    return metadata, np.asarray(vectors, dtype=np.float32)


def save_store(room_dir: Path, metadata: list[dict[str, Any]], vectors: np.ndarray) -> None:
    metadata_tmp = room_dir / "metadata.json.tmp"
    vectors_tmp = room_dir / "vectors.npy.tmp"
    metadata_tmp.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(vectors_tmp, "wb") as fp:
        np.save(fp, np.asarray(vectors, dtype=np.float32))
    metadata_tmp.replace(room_dir / "metadata.json")
    vectors_tmp.replace(room_dir / "vectors.npy")


def assign_new_ids(
    existing_metadata: list[dict[str, Any]],
    new_chunks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    existing_fingerprints = {fingerprint(item) for item in existing_metadata}
    next_chunk_id = max((int(item.get("id", -1)) for item in existing_metadata), default=-1) + 1
    next_session_id = max((int(item.get("session_id", -1)) for item in existing_metadata), default=-1) + 1
    session_map: dict[int, int] = {}
    accepted: list[dict[str, Any]] = []
    accepted_embedding_texts: list[str] = []

    for item in new_chunks:
        fp = fingerprint(item)
        if fp in existing_fingerprints:
            continue
        source_session_id = int(item["source_session_id"])
        if source_session_id not in session_map:
            session_map[source_session_id] = next_session_id
            next_session_id += 1
        accepted.append(
            {
                "id": next_chunk_id,
                "session_id": session_map[source_session_id],
                "text": item["text"],
                "summary": item["summary"],
                "start_date": item["start_date"],
                "message_count": item["message_count"],
            }
        )
        accepted_embedding_texts.append(str(item["embedding_text"]))
        next_chunk_id += 1
        existing_fingerprints.add(fp)

    return accepted, accepted_embedding_texts


def build_tidb_connection() -> pymysql.connections.Connection:
    if not (config.TIDB_HOST and config.TIDB_USER):
        raise RuntimeError("TiDB connection settings are not configured.")
    ssl_value: dict[str, Any] | None = None
    if config.TIDB_SSL_CA:
        ssl_value = {"ca": config.TIDB_SSL_CA}
        if config.TIDB_SSL_VERIFY_IDENTITY:
            ssl_value["check_hostname"] = True
    return pymysql.connect(
        host=config.TIDB_HOST,
        port=config.TIDB_PORT,
        user=config.TIDB_USER,
        password=config.TIDB_PASSWORD or "",
        database=config.TIDB_NAME,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
        ssl=ssl_value,
    )


def insert_into_tidb(
    conn: pymysql.connections.Connection,
    table_name: str,
    room_key: str,
    room_label: str,
    metadata_rows: list[dict[str, Any]],
    vectors: np.ndarray,
) -> None:
    if not metadata_rows:
        return
    insert_sql = f"""
        INSERT INTO {table_name} (
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
    batch: list[tuple[Any, ...]] = []
    with conn.cursor() as cursor:
        for item, vector in zip(metadata_rows, vectors):
            vector_literal = "[" + ",".join(f"{float(v):.8f}" for v in vector.tolist()) + "]"
            batch.append(
                (
                    room_key,
                    room_label,
                    int(item["id"]),
                    int(item["session_id"]),
                    item["start_date"],
                    int(item["message_count"]),
                    item["summary"],
                    item["text"],
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


def main() -> None:
    args = parse_args()
    room_dir = Path(args.room_dir).resolve()

    existing_metadata, existing_vectors = load_existing_store(room_dir)
    raw_chunks = asyncio.run(build_chunks_with_v2(args))
    accepted_metadata, accepted_embedding_texts = assign_new_ids(existing_metadata, raw_chunks)

    if not accepted_metadata:
        print("No new Kakao chunks detected. Nothing to append.")
        return

    model = KakaoSessionEmbedder(
        args.model,
        os.environ.get("COMETAPI_KEY") or getattr(config, "COMETAPI_KEY", "dummy-key"),
        os.environ.get("COMETAPI_BASE_URL") or getattr(config, "COMETAPI_BASE_URL", "https://api.cometapi.com/v1"),
        SUMMARIZATION_MODELS[str(args.summary_model)],
    )
    model.load_model()
    new_vectors = model.embedding_model.encode(
        accepted_embedding_texts,
        normalize_embeddings=True,
        show_progress_bar=True,
    )
    new_vectors_np = np.asarray(new_vectors, dtype=np.float32)

    conn = build_tidb_connection()
    try:
        insert_into_tidb(conn, args.tidb_table, args.room_key, args.room_label, accepted_metadata, new_vectors_np)
    finally:
        conn.close()

    merged_metadata = existing_metadata + accepted_metadata
    merged_vectors = np.concatenate([existing_vectors, new_vectors_np], axis=0)
    save_store(room_dir, merged_metadata, merged_vectors)

    print(
        json.dumps(
            {
                "room_dir": str(room_dir),
                "csv": str(Path(args.csv).resolve()),
                "added_chunks": len(accepted_metadata),
                "new_total_chunks": len(merged_metadata),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

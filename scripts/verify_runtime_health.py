#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""운영 환경의 DB/RAG/적재 경로를 한 번에 검증하는 통합 헬스체크."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import re
from typing import Any, Iterable

import aiosqlite
import numpy as np
import pymysql

import config
from database.compat_db import TiDBSettings, connect_main_db
from utils import db as db_utils
from utils.coords import get_coords_from_db
from utils.embeddings import DiscordEmbeddingStore, KakaoEmbeddingStore, get_embedding
from utils.hybrid_search import HybridSearchEngine
from utils.memory_units import build_structured_memory_units, extract_keywords


_NUMERIC_ONLY_RE = re.compile(r"^\d+$")


@dataclass
class CheckResult:
    name: str
    ok: bool
    details: str
    metrics: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-check", action="store_true", help="합성 데이터 적재/검색/정리까지 검증")
    parser.add_argument("--json", action="store_true", help="JSON 결과만 출력")
    parser.add_argument("--strict", action="store_true", help="하나라도 실패하면 exit code 1")
    parser.add_argument("--discord-probes", type=int, default=4, help="Discord 검색 질의 개수")
    parser.add_argument("--kakao-probes", type=int, default=3, help="Kakao 검색 질의 개수")
    return parser.parse_args()


def _print_text(results: list[CheckResult]) -> None:
    for item in results:
        status = "PASS" if item.ok else "FAIL"
        print(f"[{status}] {item.name}: {item.details}")
        if item.metrics:
            print(" ", json.dumps(item.metrics, ensure_ascii=False, sort_keys=True))


def _json_default(value: Any) -> Any:
    if isinstance(value, CheckResult):
        return asdict(value)
    raise TypeError(f"JSON 직렬화할 수 없는 값: {type(value)!r}")


def _append(results: list[CheckResult], name: str, ok: bool, details: str, **metrics: Any) -> None:
    results.append(CheckResult(name=name, ok=ok, details=details, metrics=metrics))


async def _select_count(db: Any, table_name: str) -> int:
    async with db.execute(f"SELECT COUNT(*) AS cnt FROM {table_name}") as cursor:
        row = await cursor.fetchone()
    return int(row[0] if row else 0)


async def _discover_discord_scope(store: DiscordEmbeddingStore) -> tuple[str, str] | None:
    await store.initialize()
    if store.backend == "tidb":
        settings = store._tidb_settings
        if settings is None:
            return None
        conn = pymysql.connect(**settings.to_connect_kwargs())
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT server_id, channel_id, COUNT(*) AS cnt
                    FROM discord_memory_entries
                    GROUP BY server_id, channel_id
                    ORDER BY cnt DESC
                    LIMIT 1
                    """
                )
                row = cursor.fetchone()
                if row:
                    return str(row["server_id"]), str(row["channel_id"])
                cursor.execute(
                    """
                    SELECT server_id, channel_id, COUNT(*) AS cnt
                    FROM discord_chat_embeddings
                    GROUP BY server_id, channel_id
                    ORDER BY cnt DESC
                    LIMIT 1
                    """
                )
                row = cursor.fetchone()
                if row:
                    return str(row["server_id"]), str(row["channel_id"])
                return None
        finally:
            conn.close()

    async with aiosqlite.connect(store.db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT server_id, channel_id, COUNT(*) AS cnt
            FROM discord_memory_entries
            GROUP BY server_id, channel_id
            ORDER BY cnt DESC
            LIMIT 1
            """
        )
        row = await cursor.fetchone()
        if row:
            return str(row["server_id"]), str(row["channel_id"])
        cursor = await db.execute(
            """
            SELECT server_id, channel_id, COUNT(*) AS cnt
            FROM discord_chat_embeddings
            GROUP BY server_id, channel_id
            ORDER BY cnt DESC
            LIMIT 1
            """
        )
        row = await cursor.fetchone()
        if row:
            return str(row["server_id"]), str(row["channel_id"])
    return None


async def _fetch_recent_discord_memory_rows(
    store: DiscordEmbeddingStore,
    server_id: str,
    channel_id: str,
    *,
    limit: int = 120,
) -> list[dict[str, Any]]:
    rows = await store.fetch_recent_memory_entries(
        server_id=int(server_id),
        channel_id=int(channel_id),
        limit=limit,
    )
    return [dict(row) for row in rows]


def _rank_keywords_from_rows(rows: Iterable[dict[str, Any]], *, limit: int) -> list[str]:
    counter: Counter[str] = Counter()
    for row in rows:
        raw_keywords = row.get("keyword_json")
        if raw_keywords:
            try:
                parsed = json.loads(raw_keywords)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed = []
            if isinstance(parsed, list):
                for item in parsed:
                    token = str(item).strip()
                    if len(token) < 2 or _NUMERIC_ONLY_RE.fullmatch(token):
                        continue
                    counter[token] += 3
        base_text = " ".join(
            [
                str(row.get("summary_text") or ""),
                str(row.get("message") or ""),
                str(row.get("raw_context") or ""),
            ]
        )
        for token in extract_keywords(base_text, limit=12):
            if len(token) < 2 or _NUMERIC_ONLY_RE.fullmatch(token):
                continue
            counter[token] += 1

    return [token for token, _ in counter.most_common(limit)]


async def _discover_user_id_for_scope(
    store: DiscordEmbeddingStore,
    server_id: str,
    channel_id: str,
) -> int | None:
    rows = await _fetch_recent_discord_memory_rows(store, server_id, channel_id, limit=80)
    for row in rows:
        if row.get("memory_scope") != "user":
            continue
        raw = row.get("user_id")
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    return None


def _discover_kakao_targets() -> list[tuple[str, str]]:
    discovered: list[tuple[str, str]] = []
    seen_room_keys: set[str] = set()
    for server_id, meta in config.KAKAO_EMBEDDING_SERVER_MAP.items():
        room_key = str(meta.get("room_key") or "").strip()
        if not room_key or room_key in seen_room_keys:
            continue
        seen_room_keys.add(room_key)
        discovered.append((str(server_id), room_key))
    return discovered


async def _fetch_recent_kakao_rows(
    store: KakaoEmbeddingStore,
    server_id: str,
    *,
    limit: int = 60,
) -> list[dict[str, Any]]:
    rows = await store.fetch_recent_embeddings([server_id], limit=limit, query_vector=None)
    return [dict(row) for row in rows]


async def _run_write_pipeline_check(
    db: Any,
    discord_store: DiscordEmbeddingStore,
) -> CheckResult:
    base = int(datetime.now(timezone.utc).timestamp())
    test_guild_id = 990000000000000 + (base % 100000)
    test_channel_id = test_guild_id + 1
    test_user_id = test_guild_id + 2
    test_message_ids = [test_guild_id + 10, test_guild_id + 11]
    unique_token = f"헬스체크토큰{base}"
    inserted_memory_ids: list[str] = []

    payload = [
        {
            "message_id": test_message_ids[0],
            "guild_id": test_guild_id,
            "channel_id": test_channel_id,
            "user_id": test_user_id,
            "user_name": "health-user",
            "content": f"{unique_token} 첫 번째 저장 메시지",
            "is_bot": False,
            "created_at": "2026-04-07T15:00:00+09:00",
        },
        {
            "message_id": test_message_ids[1],
            "guild_id": test_guild_id,
            "channel_id": test_channel_id,
            "user_id": test_user_id,
            "user_name": "health-user",
            "content": f"{unique_token} 두 번째 저장 메시지",
            "is_bot": False,
            "created_at": "2026-04-07T15:00:30+09:00",
        },
    ]

    try:
        await db_utils.set_guild_setting(db, test_guild_id, "persona_text", "health-check")
        persona = await db_utils.get_guild_setting(db, test_guild_id, "persona_text")
        if persona != "health-check":
            return CheckResult(
                name="write_pipeline",
                ok=False,
                details="guild_settings 쓰기/읽기 실패",
                metrics={"guild_id": test_guild_id},
            )

        await db.execute(
            "REPLACE INTO user_profiles (user_id, birth_date, birth_time, gender, birth_place, created_at) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP(6))",
            (test_user_id, "1990-01-01", "07:30", "M", "서울"),
        )
        await db.executemany(
            """
            REPLACE INTO conversation_history
            (message_id, guild_id, channel_id, user_id, user_name, content, is_bot, created_at, embedding)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item["message_id"],
                    item["guild_id"],
                    item["channel_id"],
                    item["user_id"],
                    item["user_name"],
                    item["content"],
                    item["is_bot"],
                    item["created_at"],
                    None,
                )
                for item in payload
            ],
        )
        await db.commit()

        dummy_embedding = await get_embedding(unique_token, prefix="passage: ")
        if dummy_embedding is None:
            return CheckResult(
                name="write_pipeline",
                ok=False,
                details="합성 임베딩 생성 실패",
                metrics={},
            )

        await discord_store.upsert_message_embedding(
            message_id=test_message_ids[1],
            server_id=test_guild_id,
            channel_id=test_channel_id,
            user_id=test_user_id,
            user_name="health-user",
            message=f"{unique_token} legacy embedding",
            timestamp_iso="2026-04-07T15:00:30+09:00",
            embedding=dummy_embedding,
        )

        units = build_structured_memory_units(
            payload,
            channel_id=test_channel_id,
            max_summary_chars=getattr(config, "STRUCTURED_MEMORY_MAX_SUMMARY_CHARS", 320),
            max_context_chars=getattr(config, "STRUCTURED_MEMORY_MAX_CONTEXT_CHARS", 1200),
            user_turn_min_chars=getattr(config, "STRUCTURED_USER_MEMORY_MIN_CHARS", 12),
        )
        if not units:
            return CheckResult(
                name="write_pipeline",
                ok=False,
                details="구조화 메모리 유닛 생성 실패",
                metrics={"token": unique_token},
            )

        for unit in units:
            embedding = await get_embedding(unit.memory_text, prefix="passage: ")
            if embedding is None:
                return CheckResult(
                    name="write_pipeline",
                    ok=False,
                    details="구조화 메모리 임베딩 생성 실패",
                    metrics={"memory_id": unit.memory_id},
                )
            inserted_memory_ids.append(unit.memory_id)
            await discord_store.upsert_memory_entry(
                memory_id=unit.memory_id,
                anchor_message_id=unit.anchor_message_id,
                server_id=test_guild_id,
                channel_id=test_channel_id,
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

        engine = HybridSearchEngine(discord_store, None, None)
        result = await engine.search(
            unique_token,
            guild_id=test_guild_id,
            channel_id=test_channel_id,
            user_id=test_user_id,
            recent_messages=None,
        )
        matched = any(unique_token in str(entry.get("message") or "") for entry in result.entries)
        ok = len(result.entries) > 0 and matched
        return CheckResult(
            name="write_pipeline",
            ok=ok,
            details="합성 적재 후 검색 확인" if ok else "합성 적재는 됐지만 검색 회수 실패",
            metrics={
                "guild_id": test_guild_id,
                "channel_id": test_channel_id,
                "message_count": len(payload),
                "memory_units": len(units),
                "search_entries": len(result.entries),
                "top_score": result.top_score,
            },
        )
    finally:
        try:
            if inserted_memory_ids:
                await discord_store.delete_memory_entries(inserted_memory_ids)
            await discord_store.delete_embeddings(test_message_ids)
            await db.execute("DELETE FROM conversation_history WHERE message_id IN (?, ?)", tuple(test_message_ids))
            await db.execute("DELETE FROM guild_settings WHERE guild_id = ?", (test_guild_id,))
            await db.execute("DELETE FROM user_profiles WHERE user_id = ?", (test_user_id,))
            await db.commit()
        except Exception:
            pass


async def main() -> int:
    args = parse_args()
    results: list[CheckResult] = []

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

    discord_store = DiscordEmbeddingStore(config.DISCORD_EMBEDDING_DB_PATH)
    kakao_store = KakaoEmbeddingStore(config.KAKAO_EMBEDDING_DB_PATH, config.KAKAO_EMBEDDING_SERVER_MAP)

    try:
        table_counts: dict[str, int] = {}
        for table_name in [
            "conversation_history",
            "conversation_windows",
            "guild_settings",
            "user_profiles",
            "locations",
        ]:
            table_counts[table_name] = await _select_count(db, table_name)

        coords = await get_coords_from_db(db, "서울")
        db_ok = table_counts["conversation_history"] > 0 and table_counts["locations"] > 0 and bool(coords)
        _append(
            results,
            "main_db",
            db_ok,
            "메인 DB 테이블/좌표 조회 확인" if db_ok else "메인 DB 핵심 데이터 조회 실패",
            backend=config.DB_BACKEND,
            counts=table_counts,
            coords=coords,
        )

        discord_scope = await _discover_discord_scope(discord_store)
        if discord_scope is None:
            _append(results, "discord_scope", False, "Discord 메모리 scope를 찾지 못했습니다.", backend=discord_store.backend)
        else:
            server_id, channel_id = discord_scope
            recent_embeddings = await discord_store.fetch_recent_embeddings(
                server_id=int(server_id),
                channel_id=int(channel_id),
                limit=5,
            )
            recent_memories = await _fetch_recent_discord_memory_rows(discord_store, server_id, channel_id, limit=120)
            memory_count = await discord_store.count_memory_entries(server_id=int(server_id), channel_id=int(channel_id))
            memory_scopes = Counter(str(row.get("memory_scope") or "") for row in recent_memories)
            scope_ok = bool(recent_embeddings) and memory_count > 0
            _append(
                results,
                "discord_storage",
                scope_ok,
                "Discord 임베딩/구조화 메모리 적재 확인" if scope_ok else "Discord 저장소 데이터가 비어 있습니다.",
                backend=discord_store.backend,
                server_id=server_id,
                channel_id=channel_id,
                recent_embeddings=len(recent_embeddings),
                memory_count=memory_count,
                recent_memory_scope_mix=dict(memory_scopes),
            )

            user_id = await _discover_user_id_for_scope(discord_store, server_id, channel_id)
            discord_queries = _rank_keywords_from_rows(recent_memories, limit=max(1, args.discord_probes))
            if not discord_queries:
                discord_queries = ["운전면허", "걸어가라고", "오푸스", "성능"][: max(1, args.discord_probes)]

            engine = HybridSearchEngine(discord_store, None, None)
            discord_query_results: list[dict[str, Any]] = []
            for query in discord_queries:
                result = await engine.search(
                    query,
                    guild_id=int(server_id),
                    channel_id=int(channel_id),
                    user_id=user_id,
                    recent_messages=None,
                )
                discord_query_results.append(
                    {
                        "query": query,
                        "entries": len(result.entries),
                        "top_score": round(float(result.top_score), 4),
                    }
                )
            search_ok = all(item["entries"] > 0 for item in discord_query_results)
            _append(
                results,
                "discord_rag",
                search_ok,
                "Discord 구조화 메모리 검색 확인" if search_ok else "Discord RAG 질의 중 회수 실패가 있습니다.",
                queries=discord_query_results,
                user_id=user_id,
            )

        kakao_targets = _discover_kakao_targets()
        kakao_metrics: list[dict[str, Any]] = []
        kakao_failures = 0
        for server_id, room_key in kakao_targets:
            recent_rows = await _fetch_recent_kakao_rows(kakao_store, server_id, limit=40)
            keywords = _rank_keywords_from_rows(recent_rows, limit=max(1, args.kakao_probes))
            if not keywords:
                keywords = ["운전면허", "사진", "초대"][: max(1, args.kakao_probes)]
            query_results: list[dict[str, Any]] = []
            for query in keywords:
                vector = await get_embedding(query, prefix="query: ")
                if vector is None:
                    query_results.append(
                        {
                            "query": query,
                            "rows": 0,
                            "top_message_id": None,
                            "error": "query_embedding_failed",
                        }
                    )
                    continue
                rows = await kakao_store.fetch_recent_embeddings([server_id], limit=3, query_vector=vector)
                query_results.append(
                    {
                        "query": query,
                        "rows": len(rows),
                        "top_message_id": rows[0].get("message_id") if rows else None,
                    }
                )
            room_ok = bool(recent_rows) and all(item["rows"] > 0 for item in query_results)
            if not room_ok:
                kakao_failures += 1
            kakao_metrics.append(
                {
                    "server_id": server_id,
                    "room_key": room_key,
                    "recent_rows": len(recent_rows),
                    "queries": query_results,
                }
            )

        _append(
            results,
            "kakao_rag",
            kakao_failures == 0 and bool(kakao_metrics),
            "Kakao 벡터 검색 확인" if kakao_failures == 0 and kakao_metrics else "Kakao 검색 질의 중 실패가 있습니다.",
            rooms=kakao_metrics,
        )

        if args.write_check:
            results.append(await _run_write_pipeline_check(db, discord_store))

    finally:
        await db.close()

    summary = {
        "ok": all(item.ok for item in results),
        "failed": [item.name for item in results if not item.ok],
        "results": results,
    }

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default))
    else:
        _print_text(results)
        print(f"overall_ok={summary['ok']}")

    if args.strict and not summary["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

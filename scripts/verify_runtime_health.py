#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""운영 환경의 DB/RAG/적재 경로를 한 번에 검증하는 통합 헬스체크."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable

import aiosqlite
import numpy as np
import pymysql

import config
from cogs.ai_handler import AIHandler
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
    """헬스체크 CLI 옵션(백엔드, 임베딩 타임아웃, 검색 질의 수 등)을 파싱합니다."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-check", action="store_true", help="합성 데이터 적재/검색/정리까지 검증")
    parser.add_argument("--json", action="store_true", help="JSON 결과만 출력")
    parser.add_argument("--strict", action="store_true", help="하나라도 실패하면 exit code 1")
    parser.add_argument("--backend", choices=["auto", "sqlite", "tidb"], default="auto", help="DB 백엔드 강제 지정")
    parser.add_argument("--embedding-timeout", type=int, default=90, help="임베딩 생성 타임아웃(초)")
    parser.add_argument("--discord-probes", type=int, default=4, help="Discord 검색 질의 개수")
    parser.add_argument("--kakao-probes", type=int, default=3, help="Kakao 검색 질의 개수")
    return parser.parse_args()


def _print_text(results: list[CheckResult]) -> None:
    """CheckResult 리스트를 PASS/FAIL 형식으로 텍스트 출력합니다."""
    for item in results:
        status = "PASS" if item.ok else "FAIL"
        print(f"[{status}] {item.name}: {item.details}")
        if item.metrics:
            print(" ", json.dumps(item.metrics, ensure_ascii=False, sort_keys=True))


def _json_default(value: Any) -> Any:
    """json.dumps에서 CheckResult 직렬화를 위한 기본 변환기입니다."""
    if isinstance(value, CheckResult):
        return asdict(value)
    raise TypeError(f"JSON 직렬화할 수 없는 값: {type(value)!r}")


def _append(results: list[CheckResult], name: str, ok: bool, details: str, **metrics: Any) -> None:
    """헬스체크 결과를 results 리스트에 추가합니다."""
    results.append(CheckResult(name=name, ok=ok, details=details, metrics=metrics))


async def _select_count(db: Any, table_name: str) -> int:
    """지정된 테이블의 행 개수를 조회합니다."""
    async with db.execute(f"SELECT COUNT(*) AS cnt FROM {table_name}") as cursor:
        row = await cursor.fetchone()
    return int(row[0] if row else 0)


def _current_timestamp_sql(db: Any) -> str:
    """백엔드에 맞는 CURRENT_TIMESTAMP SQL 표현식을 반환합니다."""
    backend = getattr(db, "backend", "sqlite")
    return "CURRENT_TIMESTAMP(6)" if backend == "tidb" else "CURRENT_TIMESTAMP"


async def _discover_discord_scope(store: DiscordEmbeddingStore) -> tuple[str, str] | None:
    """데이터가 가장 많은 Discord (server_id, channel_id)를 자동 탐색합니다."""
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
    """지정된 스코프의 최근 메모리 행을 limit개 조회합니다."""
    rows = await store.fetch_recent_memory_entries(
        server_id=int(server_id),
        channel_id=int(channel_id),
        limit=limit,
    )
    return [dict(row) for row in rows]


def _rank_keywords_from_rows(rows: Iterable[dict[str, Any]], *, limit: int) -> list[str]:
    """keyword_json과 텍스트에서 키워드를 집계하여 빈도순 상위 limit개를 반환합니다."""
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
    """해당 스코프에서 user 메모리를 가진 사용자 ID를 하나 찾아 반환합니다."""
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
    """KAKAO_EMBEDDING_SERVER_MAP에서 (server_id, room_key) 쌍을 수집합니다."""
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
    """지정된 server_id의 최근 Kakao 임베딩 행을 limit개 조회합니다."""
    rows = await store.fetch_recent_embeddings([server_id], limit=limit, query_vector=None)
    return [dict(row) for row in rows]


async def _run_write_pipeline_check(
    db: Any,
    discord_store: DiscordEmbeddingStore,
) -> CheckResult:
    """합성 데이터를 적재→임베딩→하이브리드 검색하는 전체 쓰기 파이프라인을 검증합니다."""
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
            f"REPLACE INTO user_profiles (user_id, birth_date, birth_time, gender, birth_place, created_at) VALUES (?, ?, ?, ?, ?, {_current_timestamp_sql(db)})",
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


async def _run_archive_cycle_check(db: Any) -> CheckResult:
    """RAG 아카이빙 함수가 정상 동작하는지 단일 사이클로 확인합니다."""
    try:
        before_count = await _select_count(db, "conversation_history")
        await db_utils.archive_old_conversations(db)
        after_count = await _select_count(db, "conversation_history")
        return CheckResult(
            name="archive_cycle",
            ok=True,
            details="RAG 아카이빙 루프 단일 사이클 실행 성공",
            metrics={"before_count": before_count, "after_count": after_count},
        )
    except Exception as exc:
        return CheckResult(
            name="archive_cycle",
            ok=False,
            details="RAG 아카이빙 루프 실행 실패",
            metrics={"error": str(exc)},
        )


async def _run_prompt_injection_check(channel_id: int = 0) -> CheckResult:
    """AIHandler의 프롬프트 합성이 필수 섹션을 모두 포함하는지 검증합니다."""
    class _DummyBot:
        db = None

        @staticmethod
        def get_cog(_name: str):
            return None

    class _DummyChannel:
        def __init__(self, cid: int):
            self.id = cid

    class _DummyMessage:
        def __init__(self, cid: int):
            self.channel = _DummyChannel(cid)

    handler = AIHandler(_DummyBot())
    tool_block = handler._format_tool_results_for_prompt(
        [
            {
                "tool_name": "web_search",
                "result": {
                    "summary": "테스트 웹 검색 요약",
                    "source_urls": ["https://example.com"],
                },
            }
        ]
    )
    rag_blocks = [
        "[health-user][2026-04-07T15:00:00+09:00] RAG 주입 검증용 문장",
    ]
    prompt = handler._compose_main_prompt(
        _DummyMessage(channel_id),
        user_query="RAG 프롬프트 주입이 정상인지 확인해줘",
        rag_blocks=rag_blocks,
        tool_results_block=tool_block,
        recent_history=[
            {"role": "user", "parts": ["이전 메시지"]},
            {"role": "model", "parts": ["이전 응답"]},
        ],
    )

    required_markers = [
        "[현재 시간]",
        "[최근 대화 흐름 (단기 기억)]",
        "[과거 대화 기억 (관련성 검토 후 선택 사용)]",
        "[도구 실행 결과 (최우선 정보)]",
        "[현재 질문]",
        "RAG 주입 검증용 문장",
        "테스트 웹 검색 요약",
    ]
    missing = [marker for marker in required_markers if marker not in prompt]
    ok = not missing
    return CheckResult(
        name="prompt_injection",
        ok=ok,
        details="RAG/도구/질문 섹션 주입 확인" if ok else "프롬프트 주입 누락 섹션이 있습니다.",
        metrics={
            "channel_id": channel_id,
            "prompt_chars": len(prompt),
            "missing_markers": missing,
        },
    )


async def _run_embedding_preflight(timeout_seconds: int) -> CheckResult:
    """임베딩 모델이 정상 로드되어 벡터를 생성하는지 사전 검증합니다."""
    timeout_seconds = max(5, int(timeout_seconds))
    try:
        vector = await asyncio.wait_for(
            get_embedding("헬스체크 임베딩 사전검사", prefix="query: "),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        return CheckResult(
            name="embedding_preflight",
            ok=False,
            details="임베딩 모델 로드/생성 타임아웃",
            metrics={"timeout_seconds": timeout_seconds},
        )
    except Exception as exc:
        return CheckResult(
            name="embedding_preflight",
            ok=False,
            details="임베딩 모델 사전검사 실패",
            metrics={"error": str(exc)},
        )

    if vector is None:
        return CheckResult(
            name="embedding_preflight",
            ok=False,
            details="임베딩 생성 결과가 비어 있습니다.",
            metrics={},
        )

    return CheckResult(
        name="embedding_preflight",
        ok=True,
        details="임베딩 모델 사전검사 성공",
        metrics={"dimension": int(len(vector))},
    )


async def main() -> int:
    """전체 헬스체크를 실행하고 결과를 출력합니다. 실패 시 exit code 1."""
    args = parse_args()
    results: list[CheckResult] = []
    selected_backend = config.DB_BACKEND if args.backend == "auto" else args.backend
    if (
        selected_backend == "sqlite"
        and not args.write_check
        and not Path(config.DATABASE_FILE).is_file()
    ):
        raise SystemExit(
            f"read-only health check 대상 SQLite 파일이 없습니다: {config.DATABASE_FILE}"
        )

    db = await connect_main_db(
        selected_backend,
        sqlite_path=config.DATABASE_FILE,
        tidb_settings=TiDBSettings(
            host=config.TIDB_HOST or "",
            port=config.TIDB_PORT,
            user=config.TIDB_USER or "",
            password=config.TIDB_PASSWORD or "",
            database=config.TIDB_NAME,
            ssl_ca=config.TIDB_SSL_CA,
            ssl_verify_identity=config.TIDB_SSL_VERIFY_IDENTITY,
            require_tls=config.REQUIRE_DB_TLS,
        ),
    )

    discord_store = DiscordEmbeddingStore(
        config.DISCORD_EMBEDDING_DB_PATH,
        read_only=not args.write_check,
    )
    memory_enabled = bool(config.AI_MEMORY_ENABLED and config.EMBEDDING_ENABLED)
    kakao_store = (
        KakaoEmbeddingStore(
            config.KAKAO_EMBEDDING_DB_PATH,
            config.KAKAO_EMBEDDING_SERVER_MAP,
        )
        if memory_enabled and config.KAKAO_MEMORY_ENABLED
        else None
    )

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
        history_ok = (
            table_counts["conversation_history"] >= 0
            if config.PROFILE == "general"
            else table_counts["conversation_history"] > 0
        )
        db_ok = history_ok and table_counts["locations"] > 0 and bool(coords)
        _append(
            results,
            "main_db",
            db_ok,
            "메인 DB 테이블/좌표 조회 확인" if db_ok else "메인 DB 핵심 데이터 조회 실패",
            backend=selected_backend,
            empty_history_allowed=config.PROFILE == "general",
            counts=table_counts,
            coords=coords,
        )
        # 아카이빙은 live history에서 행을 이동시키는 쓰기 작업이다. 이름이
        # "health"인 기본 실행은 반드시 읽기 전용이어야 하므로 명시적 opt-in에서만 수행한다.
        if args.write_check:
            results.append(await _run_archive_cycle_check(db))
        if memory_enabled:
            embedding_preflight = await _run_embedding_preflight(
                args.embedding_timeout
            )
        else:
            embedding_preflight = CheckResult(
                name="embedding_preflight",
                ok=True,
                details="현재 프로필에서 AI memory/embedding이 비활성화되어 검사를 건너뜁니다.",
                metrics={"skipped": True},
            )
        results.append(embedding_preflight)
        embedding_ready = memory_enabled and embedding_preflight.ok

        discord_scope = (
            await _discover_discord_scope(discord_store)
            if memory_enabled
            else None
        )
        if not memory_enabled:
            _append(
                results,
                "discord_scope",
                True,
                "현재 프로필에서 Discord RAG가 비활성화되어 검사를 건너뜁니다.",
                skipped=True,
            )
        elif discord_scope is None:
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
            if embedding_ready:
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
                            "query_fingerprint": hashlib.sha256(
                                query.encode("utf-8")
                            ).hexdigest()[:12],
                            "query_chars": len(query),
                            "entries": len(result.entries),
                            "top_score": round(float(result.top_score), 4),
                        }
                    )
            else:
                discord_query_results.append(
                    {
                        "query_fingerprint": None,
                        "query_chars": 0,
                        "entries": 0,
                        "top_score": 0.0,
                        "error": "embedding_preflight_failed",
                    }
                )
            search_ok = embedding_ready and all(item["entries"] > 0 for item in discord_query_results)
            _append(
                results,
                "discord_rag",
                search_ok,
                "Discord 구조화 메모리 검색 확인" if search_ok else "Discord RAG 질의 중 회수 실패가 있습니다.",
                queries=discord_query_results,
                user_id=user_id,
            )

        kakao_targets = (
            _discover_kakao_targets()
            if memory_enabled and config.KAKAO_MEMORY_ENABLED
            else []
        )
        kakao_metrics: list[dict[str, Any]] = []
        kakao_failures = 0
        for server_id, room_key in kakao_targets:
            assert kakao_store is not None
            recent_rows = await _fetch_recent_kakao_rows(kakao_store, server_id, limit=40)
            keywords = _rank_keywords_from_rows(recent_rows, limit=max(1, args.kakao_probes))
            if not keywords:
                keywords = ["운전면허", "사진", "초대"][: max(1, args.kakao_probes)]
            query_results: list[dict[str, Any]] = []
            for query in keywords:
                if not embedding_ready:
                    query_results.append(
                        {
                            "query_fingerprint": hashlib.sha256(
                                query.encode("utf-8")
                            ).hexdigest()[:12],
                            "query_chars": len(query),
                            "rows": 0,
                            "top_message_id": None,
                            "error": "embedding_preflight_failed",
                        }
                    )
                    continue
                vector = await get_embedding(query, prefix="query: ")
                if vector is None:
                    query_results.append(
                        {
                            "query_fingerprint": hashlib.sha256(
                                query.encode("utf-8")
                            ).hexdigest()[:12],
                            "query_chars": len(query),
                            "rows": 0,
                            "top_message_id": None,
                            "error": "query_embedding_failed",
                        }
                    )
                    continue
                rows = await kakao_store.fetch_recent_embeddings([server_id], limit=3, query_vector=vector)
                query_results.append(
                    {
                        "query_fingerprint": hashlib.sha256(
                            query.encode("utf-8")
                        ).hexdigest()[:12],
                        "query_chars": len(query),
                        "rows": len(rows),
                        "top_message_id": rows[0].get("message_id") if rows else None,
                    }
                )
            room_ok = embedding_ready and bool(recent_rows) and all(item["rows"] > 0 for item in query_results)
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

        if not memory_enabled or not config.KAKAO_MEMORY_ENABLED:
            _append(
                results,
                "kakao_rag",
                True,
                "현재 프로필에서 Kakao 기억 소스가 비활성화되어 검사를 건너뜁니다.",
                skipped=True,
            )
        else:
            _append(
                results,
                "kakao_rag",
                kakao_failures == 0 and bool(kakao_metrics),
                "Kakao 벡터 검색 확인"
                if kakao_failures == 0 and kakao_metrics
                else "Kakao 검색 질의 중 실패가 있습니다.",
                rooms=kakao_metrics,
            )

        prompt_channel_id = int(discord_scope[1]) if 'discord_scope' in locals() and discord_scope else 0
        results.append(await _run_prompt_injection_check(prompt_channel_id))

        if args.write_check:
            if not memory_enabled:
                _append(
                    results,
                    "write_pipeline",
                    True,
                    "현재 프로필에서 RAG 쓰기 경로가 비활성화되어 검사를 건너뜁니다.",
                    skipped=True,
                )
            elif embedding_ready:
                results.append(await _run_write_pipeline_check(db, discord_store))
            else:
                _append(
                    results,
                    "write_pipeline",
                    False,
                    "임베딩 사전검사 실패로 write_check를 건너뜁니다.",
                    reason="embedding_preflight_failed",
                )

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

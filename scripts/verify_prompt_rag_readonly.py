#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""원격/로컬 DB 읽기 전용 프롬프트 + RAG 품질 시나리오 검증.

목표:
1. 현재 실행 설정에서 프롬프트 파일/채널 페르소나 로딩 확인
2. 실제 DB(RAG 저장소)에서 시나리오 질의가 회수되는지 확인
3. 최종 메인 프롬프트 구성 시 페르소나/필수 섹션이 주입되는지 확인

주의:
- 쓰기 작업 없음 (SELECT + 임베딩 조회만 수행)
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import config
from cogs.ai_handler import AIHandler
from database.compat_db import TiDBSettings, connect_main_db
from utils.embeddings import DiscordEmbeddingStore, KakaoEmbeddingStore
from utils.hybrid_search import HybridSearchEngine


@dataclass
class ScenarioResult:
    name: str
    query: str
    ok: bool
    reason: str
    top_score: float
    entry_count: int
    keyword_hit: bool
    top_snippet: str
    prompt_ok: bool
    prompt_len: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["auto", "sqlite", "tidb"], default="auto")
    parser.add_argument("--guild-id", type=int, default=None)
    parser.add_argument("--channel-id", type=int, default=None)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--min-score", type=float, default=0.58)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def _resolve_backend(selected: str) -> str:
    if selected == "auto":
        return config.DB_BACKEND
    return selected


async def _discover_scope(db: Any) -> tuple[int, int] | None:
    # 구조화 메모리 우선
    async with db.execute(
        """
        SELECT server_id, channel_id, COUNT(*) AS cnt
        FROM discord_memory_entries
        GROUP BY server_id, channel_id
        ORDER BY cnt DESC
        LIMIT 1
        """
    ) as cur:
        row = await cur.fetchone()
    if row:
        return int(row[0]), int(row[1])

    # 레거시 임베딩 폴백
    table = getattr(config, "DISCORD_EMBEDDING_TIDB_TABLE", "discord_chat_embeddings")
    async with db.execute(
        f"""
        SELECT server_id, channel_id, COUNT(*) AS cnt
        FROM {table}
        GROUP BY server_id, channel_id
        ORDER BY cnt DESC
        LIMIT 1
        """
    ) as cur:
        row = await cur.fetchone()
    if row:
        return int(row[0]), int(row[1])
    return None


def _make_scenarios() -> list[tuple[str, str, list[str]]]:
    return [
        ("smalltalk", "사몽아 뭐하냐", ["사몽", "뭐", "안녕"]),
        ("weather_today", "오늘 날씨 어때", ["날씨", "기온", "비", "온도"]),
        ("weather_local", "광양 날씨 어때", ["광양", "날씨", "기온"]),
        ("finance_tesla", "테슬라 왜 떡락함", ["테슬라", "주가", "전기차", "하락"]),
        ("profile_hobby", "동준이 취미가 뭐야", ["동준", "취미"]),
        ("trip_memory", "우리가 부산여행 간게 언제더라", ["부산", "여행"]),
    ]


def _build_prompt_check(channel_id: int, query: str, rag_blocks: list[str]) -> tuple[bool, int]:
    ai = AIHandler.__new__(AIHandler)
    msg = SimpleNamespace(channel=SimpleNamespace(id=channel_id))
    prompt = ai._compose_main_prompt(
        msg,
        user_query=query,
        rag_blocks=rag_blocks,
        tool_results_block=None,
        fortune_context=None,
        recent_history=None,
    )
    system_prompt = ai._get_channel_system_prompt(channel_id)
    system_anchor = system_prompt[:40]

    required_markers = ["[현재 시간]", "[현재 질문]"]
    marker_ok = all(marker in prompt for marker in required_markers)
    persona_ok = system_anchor in prompt
    rag_ok = ("[과거 대화 기억 (참고용)]" in prompt) if rag_blocks else True
    return bool(marker_ok and persona_ok and rag_ok), len(prompt)


def _entry_text(entry: dict[str, Any]) -> str:
    return (
        str(entry.get("dialogue_block") or "")
        or str(entry.get("message") or "")
        or ""
    )


async def run(args: argparse.Namespace) -> tuple[list[ScenarioResult], dict[str, Any]]:
    backend = _resolve_backend(args.backend)
    db = await connect_main_db(
        backend,
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
        if args.guild_id is not None and args.channel_id is not None:
            guild_id = int(args.guild_id)
            channel_id = int(args.channel_id)
        else:
            scope = await _discover_scope(db)
            if not scope:
                raise RuntimeError("RAG 검증 대상 스코프를 찾지 못했습니다. discord_memory_entries가 비어있습니다.")
            guild_id, channel_id = scope

        # 읽기 전용 통계
        async with db.execute("SELECT COUNT(*) FROM conversation_history") as cur:
            conversation_count = int((await cur.fetchone())[0])
        async with db.execute("SELECT COUNT(*) FROM discord_memory_entries") as cur:
            discord_memory_count = int((await cur.fetchone())[0])
        kakao_table = getattr(config, "KAKAO_TIDB_TABLE", "kakao_chunks")
        kakao_count = None
        try:
            async with db.execute(f"SELECT COUNT(*) FROM {kakao_table}") as cur:
                kakao_count = int((await cur.fetchone())[0])
        except Exception:
            kakao_count = None

        discord_store = DiscordEmbeddingStore(config.DISCORD_EMBEDDING_DB_PATH)
        kakao_store = (
            KakaoEmbeddingStore(config.KAKAO_EMBEDDING_DB_PATH, config.KAKAO_EMBEDDING_SERVER_MAP)
            if (config.KAKAO_EMBEDDING_DB_PATH or config.KAKAO_EMBEDDING_SERVER_MAP)
            else None
        )
        engine = HybridSearchEngine(discord_store, kakao_store, None, reranker=None)

        results: list[ScenarioResult] = []
        for name, query, expected_keywords in _make_scenarios():
            search_res = await engine.search(
                query,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=None,
                recent_messages=None,
            )
            entries = search_res.entries[: max(1, int(args.top_k))]
            texts = [_entry_text(entry) for entry in entries]
            merged_text = "\n".join(texts)
            keyword_hit = any(kw in merged_text for kw in expected_keywords)
            top_score = float(search_res.top_score or 0.0)
            entry_count = len(entries)

            rag_blocks = [text for text in texts if text][: max(1, int(args.top_k))]
            prompt_ok, prompt_len = _build_prompt_check(channel_id, query, rag_blocks)

            ok = bool(entry_count > 0 and prompt_ok and (keyword_hit or top_score >= float(args.min_score)))
            reason = "ok" if ok else "empty or weak retrieval/prompt check failed"
            snippet = (texts[0][:180] + "...") if texts and len(texts[0]) > 180 else (texts[0] if texts else "")
            results.append(
                ScenarioResult(
                    name=name,
                    query=query,
                    ok=ok,
                    reason=reason,
                    top_score=top_score,
                    entry_count=entry_count,
                    keyword_hit=keyword_hit,
                    top_snippet=snippet,
                    prompt_ok=prompt_ok,
                    prompt_len=prompt_len,
                )
            )

        meta = {
            "backend": backend,
            "scope": {"guild_id": guild_id, "channel_id": channel_id},
            "prompt_config_path": config.PROMPT_CONFIG_PATH,
            "prompt_exists": Path(config.PROMPT_CONFIG_PATH).exists(),
            "channel_config_exists": channel_id in config.CHANNEL_AI_CONFIG,
            "conversation_history_count": conversation_count,
            "discord_memory_count": discord_memory_count,
            "kakao_table": kakao_table,
            "kakao_count": kakao_count,
        }
        return results, meta
    finally:
        await db.close()


def _print_human(results: list[ScenarioResult], meta: dict[str, Any]) -> None:
    print("=== prompt/rag readonly verification ===")
    print(json.dumps(meta, ensure_ascii=False))
    for row in results:
        status = "PASS" if row.ok else "FAIL"
        print(
            f"[{status}] {row.name} score={row.top_score:.4f} "
            f"entries={row.entry_count} keyword_hit={row.keyword_hit} "
            f"prompt_ok={row.prompt_ok} prompt_len={row.prompt_len}"
        )
        print(f"  query={row.query}")
        if row.top_snippet:
            print(f"  top={row.top_snippet}")


def main() -> int:
    args = parse_args()
    results, meta = asyncio.run(run(args))
    if args.json:
        print(
            json.dumps(
                {
                    "meta": meta,
                    "results": [asdict(item) for item in results],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        _print_human(results, meta)

    failed = [item for item in results if not item.ok]
    overall_ok = not failed
    print(f"overall_ok={overall_ok}")
    if args.strict and not overall_ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

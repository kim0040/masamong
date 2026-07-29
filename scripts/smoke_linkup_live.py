#!/usr/bin/env python3
"""Linkup 운영 계약을 과금 상한 1회로 확인하는 제한형 smoke test."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from database.compat_db import TiDBSettings, connect_main_db
from utils import linkup_search


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Linkup provider를 최대 1회 호출하는 비용 발생 smoke. "
            "--run과 정확한 확인 문구 없이는 호출하지 않습니다."
        )
    )
    parser.add_argument(
        "--expected-profile",
        required=True,
        choices=("masamo", "general"),
    )
    parser.add_argument(
        "--query",
        default="OpenAI API 최신 공식 업데이트",
    )
    parser.add_argument("--max-cost-eur", type=float, default=0.005)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--confirm")
    return parser.parse_args()


def _settings() -> TiDBSettings:
    ssl_ca = str(config.TIDB_SSL_CA or "").strip() or None
    if ssl_ca and not Path(ssl_ca).is_file():
        try:
            import certifi
        except ImportError as exc:
            raise RuntimeError(
                f"설정된 CA 파일이 없고 certifi도 없습니다: {ssl_ca}"
            ) from exc
        ssl_ca = certifi.where()
    return TiDBSettings(
        host=config.TIDB_HOST or "",
        port=config.TIDB_PORT,
        user=config.TIDB_USER or "",
        password=config.TIDB_PASSWORD or "",
        database=config.TIDB_NAME,
        ssl_ca=ssl_ca,
        ssl_verify_identity=config.TIDB_SSL_VERIFY_IDENTITY,
        require_tls=config.REQUIRE_DB_TLS,
        connect_timeout=config.TIDB_CONNECT_TIMEOUT,
        read_timeout=config.TIDB_READ_TIMEOUT,
        write_timeout=config.TIDB_WRITE_TIMEOUT,
        conn_max_lifetime_seconds=config.TIDB_CONN_MAX_LIFETIME_SECONDS,
    )


async def run(args: argparse.Namespace) -> int:
    if config.PROFILE != args.expected_profile:
        raise SystemExit(
            f"현재 profile={config.PROFILE!r}이 "
            f"--expected-profile={args.expected_profile!r}와 다릅니다."
        )
    query = str(args.query or "").strip()
    if not query:
        raise SystemExit("query가 비어 있습니다.")
    if linkup_search._extract_first_url(query):
        raise SystemExit("비용이 달라질 수 있는 직접 URL smoke는 허용하지 않습니다.")
    depth = linkup_search.infer_linkup_depth(query)
    estimated = linkup_search._estimate_linkup_cost("search", depth=depth)
    if estimated > float(args.max_cost_eur):
        raise SystemExit(
            f"예상 비용 €{estimated:.3f}이 상한 €{args.max_cost_eur:.3f}을 넘습니다."
        )
    expected_confirmation = (
        "RUN ONE LINKUP SEARCH FOR "
        f"profile={args.expected_profile} "
        f"max_cost_eur={float(args.max_cost_eur):.3f}"
    )
    if not args.run:
        print(
            "DRY-RUN: provider를 호출하지 않았습니다. 실행하려면 "
            f"--run --confirm {expected_confirmation!r}"
        )
        return 0
    if args.confirm != expected_confirmation:
        raise SystemExit(
            "--confirm 값이 현재 profile/비용 상한과 일치하지 않습니다."
        )

    config.LINKUP_QUALITY_RETRY_ENABLED = False
    config.WEB_RAG_CACHE_TTL_SECONDS = 0
    db = await connect_main_db(
        config.DB_BACKEND,
        sqlite_path=config.DATABASE_FILE,
        tidb_settings=_settings() if config.DB_BACKEND == "tidb" else None,
    )
    try:
        result = await linkup_search.run_linkup_search_pipeline(query, db_conn=db)
    finally:
        await db.close()

    print(
        "status={status} provider={provider} kind={kind} sources={sources} "
        "context_chars={chars} max_reserved_cost_eur={cost:.3f}".format(
            status=result.get("status"),
            provider=result.get("provider"),
            kind=result.get("search_kind"),
            sources=len(result.get("source_urls") or []),
            chars=len(str(result.get("context") or "")),
            cost=estimated,
        )
    )
    if result.get("status") != "success":
        print(
            "failure_kind={kind} message={message}".format(
                kind=result.get("failure_kind"),
                message=str(result.get("message") or "")[:240],
            )
        )
        return 1
    if not result.get("context") or not result.get("source_urls"):
        print("검색 성공 계약에 context/source_urls가 없습니다.")
        return 1
    return 0


def main() -> int:
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

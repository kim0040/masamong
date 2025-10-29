# -*- coding: utf-8 -*-
"""BM25 FTS 인덱스를 초기화하거나 재구축하는 스크립트."""

from __future__ import annotations

import asyncio
from pathlib import Path

from logger_config import logger
from database.bm25_index import bulk_rebuild

DEFAULT_DB_PATH = Path("database/remasamong.db")


async def _initialize(path: Path) -> None:
    if not path.exists():
        logger.error("BM25 인덱스를 초기화할 기본 DB가 존재하지 않습니다: %s", path)
        return
    await bulk_rebuild(str(path))


def main() -> None:
    """스크립트 진입점."""
    path = DEFAULT_DB_PATH
    logger.info("BM25 인덱스 초기화를 시작합니다. 대상: %s", path)
    asyncio.run(_initialize(path))


if __name__ == "__main__":
    main()

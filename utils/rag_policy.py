# -*- coding: utf-8 -*-
"""저사양 운영 RAG 정책.

Masamo 원격 호스트는 cpu_only로 임베딩만 돌리고 BM25/FTS5와 리랭커는
쓰지 않습니다. 설정 파일이나 구 코드 경로가 켜려 해도 이 모듈이 최종
거부합니다.
"""

from __future__ import annotations

from typing import Any


def bm25_runtime_enabled() -> bool:
    """BM25 검색·인덱스·자동 재구축은 전 인스턴스에서 끕니다."""
    return False


def should_construct_bm25_manager(database_path: Any) -> bool:
    """경로가 남아 있어도 관리자 객체를 만들지 않습니다."""
    return False


def should_run_bm25_search(*, manager: Any, enabled: bool | None = None) -> bool:
    """검색 핫패스에서 FTS 조회를 건너뛸지 결정합니다."""
    if enabled is False or not bm25_runtime_enabled():
        return False
    return manager is not None

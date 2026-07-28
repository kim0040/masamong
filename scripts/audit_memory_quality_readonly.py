#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""운영 메모리/임베딩의 비식별 품질 지표를 강제 read-only로 점검한다.

대화 원문·요약문·사용자 ID·채널 ID는 출력하지 않는다. TiDB stale-read
transaction에서 고정 SELECT만 실행하며 DB를 변경하지 않는다.
"""

from __future__ import annotations

import argparse
from array import array
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from scripts.inspect_runtime_readonly import (  # noqa: E402
    InspectionError,
    _open_tidb_readonly,
    _row_value,
    _validate_expectations,
)

_EMBEDDING_TABLES = (
    "discord_chat_embeddings",
    "discord_memory_entries",
    "kakao_chunks",
)
_PROVENANCE_COLUMNS = frozenset(
    {
        "content_hash",
        "embedding_dimension",
        "embedding_model",
        "embedding_version",
        "indexed_at",
    }
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="운영 메모리/임베딩 비식별 read-only 품질 감사"
    )
    parser.add_argument("--expected-profile", required=True)
    parser.add_argument("--expected-db", required=True)
    parser.add_argument("--sample-size", type=int, default=256)
    return parser.parse_args(argv)


def _scalar(session, sql: str) -> Any:
    rows = session.read(sql)
    return _row_value(rows[0], "value") if rows else None


def _decode_vector(value: Any) -> list[float] | None:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        if len(value) % 4:
            return None
        values = array("f")
        values.frombytes(bytes(value))
        if sys.byteorder != "little":
            values.byteswap()
        return [float(item) for item in values]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, list):
            try:
                return [float(item) for item in parsed]
            except (TypeError, ValueError):
                return None
    return None


def _embedding_sample(session, table: str, sample_size: int) -> dict[str, Any]:
    rows = session.read(
        f"SELECT `embedding` FROM `{table}` "
        "WHERE `embedding` IS NOT NULL ORDER BY `id` DESC LIMIT %s",
        (sample_size,),
    )
    dimensions: Counter[int] = Counter()
    hashes: Counter[str] = Counter()
    norms: list[float] = []
    invalid = 0
    nonfinite = 0
    for row in rows:
        raw = _row_value(row, "embedding")
        vector = _decode_vector(raw)
        if vector is None:
            invalid += 1
            continue
        dimensions[len(vector)] += 1
        rendered = raw if isinstance(raw, bytes) else str(raw).encode("utf-8")
        hashes[hashlib.sha256(rendered).hexdigest()] += 1
        if not all(math.isfinite(item) for item in vector):
            nonfinite += 1
            continue
        norms.append(math.sqrt(sum(item * item for item in vector)))
    return {
        "sample_size": len(rows),
        "dimensions": dict(sorted(dimensions.items())),
        "invalid_vector_count": invalid,
        "nonfinite_vector_count": nonfinite,
        "duplicate_vector_rows": sum(
            count - 1 for count in hashes.values() if count > 1
        ),
        "norm": {
            "min": round(min(norms), 6) if norms else None,
            "median": round(statistics.median(norms), 6) if norms else None,
            "max": round(max(norms), 6) if norms else None,
        },
    }


def audit(*, expected_profile: str, expected_db: str, sample_size: int) -> dict:
    backend = _validate_expectations(expected_profile, expected_db)
    if backend != "tidb":
        raise InspectionError(
            "unsupported_backend",
            "현재 품질 감사는 운영 TiDB에만 지원됩니다.",
        )
    bounded_sample = max(16, min(1024, int(sample_size)))
    with _open_tidb_readonly() as session:
        report: dict[str, Any] = {
            "runtime": {
                "profile": config.PROFILE,
                "database": config.TIDB_NAME,
                "embedding_model": config.LOCAL_EMBEDDING_MODEL_NAME,
                "read_only": "tidb-stale-read-transaction",
            },
            "row_counts": {},
        }
        for table in (
            "conversation_history",
            "conversation_history_archive",
            *_EMBEDDING_TABLES,
        ):
            report["row_counts"][table] = int(
                _scalar(session, f"SELECT COUNT(*) AS value FROM `{table}`") or 0
            )

        quality_row = session.read(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN `summary_text` IS NULL OR TRIM(`summary_text`) = '' "
            "THEN 1 ELSE 0 END) AS blank_summary, "
            "SUM(CASE WHEN `memory_text` IS NULL OR TRIM(`memory_text`) = '' "
            "THEN 1 ELSE 0 END) AS blank_memory, "
            "SUM(CASE WHEN `raw_context` IS NULL OR TRIM(`raw_context`) = '' "
            "THEN 1 ELSE 0 END) AS blank_context, "
            "ROUND(AVG(CHAR_LENGTH(`summary_text`)), 2) AS avg_summary_chars, "
            "ROUND(AVG(CHAR_LENGTH(`memory_text`)), 2) AS avg_memory_chars, "
            "ROUND(AVG(CHAR_LENGTH(`raw_context`)), 2) AS avg_context_chars, "
            "SUM(CASE WHEN `embedding` IS NULL THEN 1 ELSE 0 END) AS null_embeddings "
            "FROM `discord_memory_entries`"
        )[0]
        report["discord_memory_quality"] = {
            key: _row_value(quality_row, key)
            for key in (
                "total",
                "blank_summary",
                "blank_memory",
                "blank_context",
                "avg_summary_chars",
                "avg_memory_chars",
                "avg_context_chars",
                "null_embeddings",
            )
        }

        distribution = session.read(
            "SELECT `memory_scope`, `memory_type`, COUNT(*) AS rows_count "
            "FROM `discord_memory_entries` "
            "GROUP BY `memory_scope`, `memory_type` ORDER BY rows_count DESC"
        )
        report["memory_distribution"] = [
            {
                "scope": str(_row_value(row, "memory_scope") or ""),
                "type": str(_row_value(row, "memory_type", 1) or ""),
                "rows": int(_row_value(row, "rows_count", 2) or 0),
            }
            for row in distribution
        ]

        report["duplicate_keys"] = {
            "discord_memory_entries.memory_id": int(
                _scalar(
                    session,
                    "SELECT COUNT(*) AS value FROM ("
                    "SELECT `memory_id` FROM `discord_memory_entries` "
                    "GROUP BY `memory_id` HAVING COUNT(*) > 1"
                    ") AS duplicates",
                )
                or 0
            ),
            "discord_chat_embeddings.message_id": int(
                _scalar(
                    session,
                    "SELECT COUNT(*) AS value FROM ("
                    "SELECT `message_id` FROM `discord_chat_embeddings` "
                    "GROUP BY `message_id` HAVING COUNT(*) > 1"
                    ") AS duplicates",
                )
                or 0
            ),
            "kakao_chunks.room_chunk": int(
                _scalar(
                    session,
                    "SELECT COUNT(*) AS value FROM ("
                    "SELECT `room_key`, `chunk_id` FROM `kakao_chunks` "
                    "GROUP BY `room_key`, `chunk_id` HAVING COUNT(*) > 1"
                    ") AS duplicates",
                )
                or 0
            ),
        }
        report["embedding_samples"] = {
            table: _embedding_sample(session, table, bounded_sample)
            for table in _EMBEDDING_TABLES
        }

        columns = session.read(
            "SELECT `TABLE_NAME`, `COLUMN_NAME` "
            "FROM `information_schema`.`COLUMNS` "
            "WHERE `TABLE_SCHEMA` = DATABASE() "
            "AND `TABLE_NAME` IN "
            "('discord_chat_embeddings','discord_memory_entries','kakao_chunks') "
            "ORDER BY `TABLE_NAME`, `ORDINAL_POSITION`"
        )
        actual: dict[str, set[str]] = {}
        for row in columns:
            actual.setdefault(str(_row_value(row, "TABLE_NAME")), set()).add(
                str(_row_value(row, "COLUMN_NAME", 1))
            )
        report["missing_provenance_columns"] = {
            table: sorted(_PROVENANCE_COLUMNS - names)
            for table, names in actual.items()
        }
    return report


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = audit(
            expected_profile=args.expected_profile,
            expected_db=args.expected_db,
            sample_size=args.sample_size,
        )
    except InspectionError as exc:
        print(
            json.dumps(
                {"ok": False, "error": exc.code, "message": exc.public_message},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return exc.exit_code
    print(json.dumps({"ok": True, **report}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

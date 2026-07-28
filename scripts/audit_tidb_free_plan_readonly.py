#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TiDB Cloud Starter 무료 한도 사용 상태를 비식별 read-only로 점검한다.

운영 데이터의 내용, 사용자 식별자, 자격증명은 읽거나 출력하지 않는다. 저장소
수치는 ``information_schema`` 집계만 사용하고, RU 시스템 테이블이 제공되지
않는 환경에서는 실패하지 않고 Cloud 콘솔 확인이 필요하다고 표시한다.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="TiDB Cloud Starter 무료 한도 비식별 read-only 감사"
    )
    parser.add_argument("--expected-profile", required=True)
    parser.add_argument("--expected-db", required=True)
    return parser.parse_args(argv)


def _as_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, Decimal):
        return int(value)
    return int(value)


def _usage_status(used: int, quota: int, warning_ratio: float) -> str:
    if used >= quota:
        return "critical"
    if used >= int(quota * warning_ratio):
        return "warning"
    return "ok"


def _storage_report(rows: list[Any]) -> dict[str, Any]:
    tables: list[dict[str, Any]] = []
    total_bytes = 0
    total_rows = 0
    for row in rows:
        data_bytes = _as_int(_row_value(row, "DATA_LENGTH", 2))
        index_bytes = _as_int(_row_value(row, "INDEX_LENGTH", 3))
        logical_bytes = max(0, data_bytes) + max(0, index_bytes)
        approximate_rows = max(0, _as_int(_row_value(row, "TABLE_ROWS", 1)))
        total_bytes += logical_bytes
        total_rows += approximate_rows
        tables.append(
            {
                "table": str(_row_value(row, "TABLE_NAME", 0) or ""),
                "approximate_rows": approximate_rows,
                "logical_bytes": logical_bytes,
            }
        )
    tables.sort(key=lambda item: (-item["logical_bytes"], item["table"]))
    quota = int(config.TIDB_STARTER_FREE_ROW_STORAGE_BYTES)
    ratio = (total_bytes / quota) if quota else 0.0
    return {
        "logical_bytes": total_bytes,
        "quota_bytes": quota,
        "usage_ratio": round(ratio, 6),
        "headroom_bytes": max(0, quota - total_bytes),
        "status": _usage_status(
            total_bytes,
            quota,
            float(config.TIDB_STARTER_USAGE_WARNING_RATIO),
        ),
        "approximate_rows": total_rows,
        "largest_tables": tables[:10],
        "measurement": "information_schema.TABLES data_length + index_length",
    }


def _monthly_ru_report(session: Any) -> dict[str, Any]:
    quota = int(config.TIDB_STARTER_FREE_MONTHLY_RU)
    try:
        rows = session.read(
            "SELECT COALESCE(SUM(`total_ru`), 0) AS total_ru, "
            "MIN(`start_time`) AS first_record, "
            "MAX(`end_time`) AS last_record "
            "FROM `mysql`.`request_unit_by_group` "
            "WHERE `start_time` >= DATE_FORMAT(CURRENT_DATE(), '%Y-%m-01')"
        )
    except Exception:
        return {
            "available": False,
            "quota_ru": quota,
            "authoritative_source": "TiDB Cloud console: Usage this month",
            "reason": "request_unit_by_group_unavailable",
        }

    row = rows[0] if rows else {}
    used = max(0, _as_int(_row_value(row, "total_ru", 0)))
    return {
        "available": True,
        "recorded_ru": used,
        "quota_ru": quota,
        "usage_ratio": round(used / quota, 6) if quota else 0.0,
        "headroom_ru": max(0, quota - used),
        "status": _usage_status(
            used,
            quota,
            float(config.TIDB_STARTER_USAGE_WARNING_RATIO),
        ),
        "first_record": str(_row_value(row, "first_record", 1) or "") or None,
        "last_record": str(_row_value(row, "last_record", 2) or "") or None,
        "scope_note": (
            "daily system-table records only; current day and Cloud network "
            "egress can be absent"
        ),
        "authoritative_source": "TiDB Cloud console: Usage this month",
    }


def _columnar_report(session: Any) -> dict[str, Any]:
    quota = int(config.TIDB_STARTER_FREE_COLUMNAR_STORAGE_BYTES)
    try:
        rows = session.read(
            "SELECT COUNT(*) AS replica_tables, "
            "COALESCE(SUM(CASE WHEN `AVAILABLE` = 1 THEN 1 ELSE 0 END), 0) "
            "AS available_tables "
            "FROM `information_schema`.`TIFLASH_REPLICA` "
            "WHERE `TABLE_SCHEMA` = DATABASE()"
        )
    except Exception:
        return {
            "replica_inventory_available": False,
            "quota_bytes": quota,
            "authoritative_source": "TiDB Cloud console: Usage this month",
        }
    row = rows[0] if rows else {}
    return {
        "replica_inventory_available": True,
        "replica_tables": _as_int(_row_value(row, "replica_tables", 0)),
        "available_tables": _as_int(_row_value(row, "available_tables", 1)),
        "quota_bytes": quota,
        "size_note": "columnar byte usage is only authoritative in TiDB Cloud console",
        "authoritative_source": "TiDB Cloud console: Usage this month",
    }


def audit(*, expected_profile: str, expected_db: str) -> dict[str, Any]:
    backend = _validate_expectations(expected_profile, expected_db)
    if backend != "tidb":
        raise InspectionError(
            "unsupported_backend",
            "TiDB 무료 플랜 감사는 TiDB 백엔드에서만 지원됩니다.",
        )

    with _open_tidb_readonly() as session:
        table_rows = session.read(
            "SELECT `TABLE_NAME`, `TABLE_ROWS`, `DATA_LENGTH`, `INDEX_LENGTH` "
            "FROM `information_schema`.`TABLES` "
            "WHERE `TABLE_SCHEMA` = DATABASE() "
            "AND `TABLE_TYPE` = 'BASE TABLE' "
            "ORDER BY `TABLE_NAME`"
        )
        return {
            "runtime": {
                "profile": config.PROFILE,
                "database": config.TIDB_NAME,
                "read_only": "tidb-stale-read-transaction",
                "free_plan_guardrails": bool(config.TIDB_STARTER_FREE_PLAN_MODE),
                "warning_ratio": float(config.TIDB_STARTER_USAGE_WARNING_RATIO),
            },
            "row_storage": _storage_report(table_rows),
            "columnar_storage": _columnar_report(session),
            "monthly_ru": _monthly_ru_report(session),
            "console_check_required": True,
        }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = audit(
            expected_profile=args.expected_profile,
            expected_db=args.expected_db,
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

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Linkup 사용량 테이블에 USD 예약·확정 계측 열을 추가한다.

기존 데이터는 삭제하거나 덮어쓰지 않는다.

* 실행 가능한 변경은 ``ADD COLUMN``과 ``ADD INDEX``뿐이다.
* 기존 ``cost_eur`` 값은 그대로 보존하며 신규 코드의 호환 미러로만 사용한다.
* 새 열은 기존 행을 백필하지 않는다. NULL인 구버전 행은 런타임에서
  ``legacy_assumed``로 해석해 기존 예산 보호가 그대로 유지된다.
* 적용 전후 행 수와 기존 ``cost_eur`` 합계를 비교해 달라지면 실패한다.

사용:
    MASAMONG_ENV_FILE=/etc/masamong/masamo.env \
      python scripts/apply_linkup_usage_usd_columns.py \
        --expected-profile masamo --expected-db masamong --apply \
        --confirm "ADD LINKUP USD COLUMNS TO masamong"
"""

from __future__ import annotations

import argparse
from decimal import Decimal
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import pymysql  # noqa: E402
from database.compat_db import TiDBSettings  # noqa: E402


TABLE = "linkup_usage_log"
NEW_COLUMNS = {
    "request_id": "VARCHAR(64) NULL",
    "cost_usd": "DOUBLE NULL",
    "billing_status": "VARCHAR(32) NULL",
    "finalized_at": "VARCHAR(64) NULL",
}
INDEX_NAME = "idx_linkup_request_id"
FORBIDDEN = re.compile(
    r"\b(delete|drop|truncate|replace|rename|update)\b",
    re.IGNORECASE,
)


class MigrationError(RuntimeError):
    pass


def _guard(sql: str) -> str:
    if FORBIDDEN.search(sql):
        raise MigrationError(f"파괴적 SQL로 판단해 중단합니다: {sql[:120]}")
    return sql


def _settings() -> TiDBSettings:
    return TiDBSettings(
        host=config.TIDB_HOST,
        port=config.TIDB_PORT,
        user=config.TIDB_USER,
        password=config.TIDB_PASSWORD,
        database=config.TIDB_NAME,
        ssl_ca=config.TIDB_SSL_CA,
        ssl_verify_identity=config.TIDB_SSL_VERIFY_IDENTITY,
        require_tls=config.REQUIRE_DB_TLS,
        connect_timeout=config.TIDB_CONNECT_TIMEOUT,
        read_timeout=config.TIDB_READ_TIMEOUT,
        write_timeout=config.TIDB_WRITE_TIMEOUT,
        conn_max_lifetime_seconds=config.TIDB_CONN_MAX_LIFETIME_SECONDS,
    )


def _columns(cursor) -> set[str]:
    cursor.execute(
        _guard(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s"
        ),
        (TABLE,),
    )
    return {str(row["COLUMN_NAME"]) for row in cursor.fetchall()}


def _index_exists(cursor) -> bool:
    cursor.execute(
        _guard(
            "SELECT COUNT(*) AS n FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "AND INDEX_NAME = %s"
        ),
        (TABLE, INDEX_NAME),
    )
    return int(cursor.fetchone()["n"]) > 0


def _legacy_fingerprint(cursor) -> tuple[int, Decimal]:
    cursor.execute(
        _guard(
            f"SELECT COUNT(*) AS n, "
            f"COALESCE(SUM(`cost_eur`), 0) AS legacy_cost FROM `{TABLE}`"
        )
    )
    row = cursor.fetchone()
    return int(row["n"]), Decimal(str(row["legacy_cost"] or 0))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-profile", required=True)
    parser.add_argument("--expected-db", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configured_profile = str(config.PROFILE).strip().lower()
    configured_instance = str(config.INSTANCE_NAME).strip().lower()
    expected_profile = args.expected_profile.strip().lower()
    expected_confirmation = f"ADD LINKUP USD COLUMNS TO {args.expected_db}"

    if str(config.DB_BACKEND).strip().lower() != "tidb":
        print("TiDB 백엔드에서만 실행할 수 있습니다.", file=sys.stderr)
        return 2
    if (
        configured_profile != expected_profile
        or configured_instance != expected_profile
    ):
        print(
            "프로필이 다릅니다: "
            f"PROFILE={configured_profile}, INSTANCE={configured_instance}",
            file=sys.stderr,
        )
        return 2
    if str(config.TIDB_NAME) != args.expected_db:
        print(
            f"대상 DB가 다릅니다: 설정={config.TIDB_NAME}, 확인={args.expected_db}",
            file=sys.stderr,
        )
        return 2
    if args.apply and args.confirm != expected_confirmation:
        print(
            f'적용 확인 문구가 필요합니다: --confirm "{expected_confirmation}"',
            file=sys.stderr,
        )
        return 2

    connection = pymysql.connect(**_settings().to_connect_kwargs())
    try:
        with connection.cursor() as cursor:
            if not args.apply:
                cursor.execute(
                    _guard(
                        "START TRANSACTION READ ONLY AS OF TIMESTAMP "
                        "NOW() - INTERVAL 5 SECOND"
                    )
                )
            cursor.execute(_guard("SELECT DATABASE() AS db"))
            actual_db = str(cursor.fetchone()["db"])
            if actual_db != args.expected_db:
                raise MigrationError(f"접속한 DB가 다릅니다: {actual_db}")

            rows_before, cost_before = _legacy_fingerprint(cursor)
            columns_before = _columns(cursor)
            missing = [
                name for name in NEW_COLUMNS
                if name not in columns_before
            ]
            index_before = _index_exists(cursor)

            print(f"대상: {actual_db}.{TABLE}")
            print(f"기존 행: {rows_before:,}")
            print(f"기존 cost_eur 합계: {cost_before}")
            print(f"추가할 열: {', '.join(missing) if missing else '없음'}")
            print(f"{INDEX_NAME}: {'있음' if index_before else '없음'}")

            if not args.apply:
                print("--apply 없이 확인만 했습니다.")
                return 0

            for name in missing:
                cursor.execute(
                    _guard(
                        f"ALTER TABLE `{TABLE}` "
                        f"ADD COLUMN `{name}` {NEW_COLUMNS[name]}"
                    )
                )
            if not index_before:
                cursor.execute(
                    _guard(
                        f"ALTER TABLE `{TABLE}` "
                        f"ADD INDEX `{INDEX_NAME}` (`request_id`)"
                    )
                )
            connection.commit()

            columns_after = _columns(cursor)
            rows_after, cost_after = _legacy_fingerprint(cursor)
            index_after = _index_exists(cursor)
            missing_after = sorted(set(NEW_COLUMNS) - columns_after)
            if missing_after:
                raise MigrationError(
                    "추가되지 않은 열이 있습니다: " + ", ".join(missing_after)
                )
            if not index_after:
                raise MigrationError(f"{INDEX_NAME} 생성이 확인되지 않습니다.")
            if rows_after != rows_before:
                raise MigrationError(
                    f"행 수가 달라졌습니다: {rows_before} -> {rows_after}"
                )
            if cost_after != cost_before:
                raise MigrationError(
                    f"기존 cost_eur 합계가 달라졌습니다: "
                    f"{cost_before} -> {cost_after}"
                )
            print("USD 예약·확정 열 추가 완료")
            print(f"행 수 보존: {rows_after:,}")
            print(f"기존 cost_eur 합계 보존: {cost_after}")
            return 0
    except Exception as exc:
        try:
            connection.rollback()
        except Exception:
            pass
        print(f"마이그레이션 실패: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())

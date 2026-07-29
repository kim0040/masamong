#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`discord_memory_entries`에 서버측 벡터 검색용 열을 추가하고 백필한다.

이 스크립트는 **기존 데이터를 절대 지우지 않는다.**

* 실행하는 SQL은 코드에 고정돼 있고, `ADD COLUMN`과 새 열만 채우는 `UPDATE`뿐이다.
* `DELETE`/`DROP`/`TRUNCATE`/`ALTER ... DROP`은 아예 만들지 않으며, 실행 직전
  모든 문장을 검사해 금지어가 있으면 중단한다.
* 기존 `embedding BLOB` 열은 건드리지 않는다. 새 열은 그 값을 변환해 복사할 뿐이라
  원본이 손상돼도 되돌릴 수 있다.
* 시작과 끝에 전체 행 수를 세어 하나라도 줄면 실패로 처리한다.
* 여러 번 실행해도 안전하다. 이미 채운 행은 건너뛴다.

임베딩 모델을 다시 돌리지는 않는다. 기존 float32를 텍스트로 직렬화하는 정도의
CPU만 쓰며, 저사양 서버를 고려해 작은 배치로 나눠 배치 사이에 잠깐 쉰다.

사용:
    MASAMONG_ENV_FILE=/etc/masamong/masamo.env \\
      python scripts/apply_memory_vector_column.py \\
        --expected-profile masamo --expected-db masamong --apply \\
        --confirm "ADD COPY VECTOR COLUMN TO masamong"
"""

from __future__ import annotations

import argparse
from array import array
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import pymysql  # noqa: E402
from database.compat_db import TiDBSettings  # noqa: E402

TABLE = "discord_memory_entries"
SOURCE_COLUMN = "embedding"
TARGET_COLUMN = "embedding_vec"
DIMENSION = 384

# 파괴적 조작을 문법 수준에서 배제한다.
_FORBIDDEN = re.compile(
    r"\b(delete|drop|truncate|replace|rename|grant|revoke)\b", re.IGNORECASE
)


class MigrationError(RuntimeError):
    pass


def _guard(sql: str) -> str:
    """실행 직전 모든 SQL을 검사한다. 금지어가 있으면 실행하지 않는다."""
    if _FORBIDDEN.search(sql):
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
        connect_timeout=config.TIDB_CONNECT_TIMEOUT,
        read_timeout=config.TIDB_READ_TIMEOUT,
        write_timeout=config.TIDB_WRITE_TIMEOUT,
    )


def _profile_identity() -> tuple[str, str]:
    """현재 설정의 프로필과 인스턴스 이름을 정규화해 반환한다."""
    return (
        str(getattr(config, "PROFILE", "")).strip().lower(),
        str(getattr(config, "INSTANCE_NAME", "")).strip().lower(),
    )


def _vector_literal(blob: Any) -> str | None:
    """BLOB(float32 배열)을 TiDB VECTOR 리터럴로 바꾼다."""
    if blob is None:
        return None
    if isinstance(blob, str):
        blob = blob.encode("latin-1")
    if len(blob) != DIMENSION * 4:
        return None
    values = array("f")
    values.frombytes(blob)
    return "[" + ",".join(f"{v:.7g}" for v in values) + "]"


def _column_exists(cursor) -> bool:
    cursor.execute(
        _guard(
            "SELECT COUNT(*) AS n FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s"
        ),
        (TABLE, TARGET_COLUMN),
    )
    return int(cursor.fetchone()["n"]) > 0


def _total_rows(cursor) -> int:
    cursor.execute(_guard(f"SELECT COUNT(*) AS n FROM `{TABLE}`"))
    return int(cursor.fetchone()["n"])


def _pending_rows(cursor) -> int:
    cursor.execute(
        _guard(
            f"SELECT COUNT(*) AS n FROM `{TABLE}` "
            f"WHERE `{TARGET_COLUMN}` IS NULL AND `{SOURCE_COLUMN}` IS NOT NULL"
        )
    )
    return int(cursor.fetchone()["n"])


def _source_stats(cursor) -> tuple[int, int, int]:
    """행 수, 기존 BLOB 총 바이트, 차원이 다른 원본 행 수를 읽는다."""
    cursor.execute(
        _guard(
            f"SELECT COUNT(*) AS n, "
            f"COALESCE(SUM(OCTET_LENGTH(`{SOURCE_COLUMN}`)), 0) AS source_bytes, "
            f"COALESCE(SUM(CASE WHEN `{SOURCE_COLUMN}` IS NOT NULL "
            f"AND OCTET_LENGTH(`{SOURCE_COLUMN}`) <> %s THEN 1 ELSE 0 END), 0) "
            f"AS invalid_source FROM `{TABLE}`"
        ),
        (DIMENSION * 4,),
    )
    row = cursor.fetchone()
    return int(row["n"]), int(row["source_bytes"]), int(row["invalid_source"])


def _apply_batch(cursor, batch: list[dict[str, Any]]) -> tuple[int, set[str]]:
    """한 배치만 새 열로 복사한다. 원본 BLOB은 읽기만 한다."""
    updated = 0
    invalid_ids: set[str] = set()
    for row in batch:
        literal = _vector_literal(row[SOURCE_COLUMN])
        if literal is None:
            invalid_ids.add(str(row["id"]))
            continue
        cursor.execute(
            _guard(
                f"UPDATE `{TABLE}` SET `{TARGET_COLUMN}` = %s "
                f"WHERE `id` = %s AND `{TARGET_COLUMN}` IS NULL"
            ),
            (literal, row["id"]),
        )
        updated += max(0, int(cursor.rowcount))
    return updated, invalid_ids


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--expected-profile", required=True, help="안전장치: masamo 또는 general"
    )
    parser.add_argument("--expected-db", required=True, help="안전장치: 대상 DB 이름")
    parser.add_argument("--apply", action="store_true", help="없으면 현황만 보고 끝낸다")
    parser.add_argument(
        "--confirm",
        default="",
        help='적용 시 "ADD COPY VECTOR COLUMN TO <DB>"를 정확히 입력',
    )
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--sleep-seconds", type=float, default=0.2)
    parser.add_argument("--max-batches", type=int, default=1000)
    parser.add_argument("--max-seconds", type=int, default=900)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if str(config.DB_BACKEND).strip().lower() != "tidb":
        print("TiDB 백엔드에서만 실행할 수 있습니다.", file=sys.stderr)
        return 2
    configured_profile, configured_instance = _profile_identity()
    if (
        args.expected_profile.strip().lower() != configured_profile
        or configured_instance != configured_profile
    ):
        print(
            "프로필이 다릅니다: "
            f"설정 PROFILE={configured_profile}, INSTANCE={configured_instance}, "
            f"확인값={args.expected_profile}",
            file=sys.stderr,
        )
        return 2
    if args.expected_db != str(config.TIDB_NAME):
        print(
            f"대상 DB가 다릅니다: 설정={config.TIDB_NAME} 확인값={args.expected_db}",
            file=sys.stderr,
        )
        return 2
    expected_confirmation = f"ADD COPY VECTOR COLUMN TO {args.expected_db}"
    if args.apply and args.confirm != expected_confirmation:
        print(
            f'적용 확인 문구가 필요합니다: --confirm "{expected_confirmation}"',
            file=sys.stderr,
        )
        return 2
    if not 1 <= args.batch_size <= 1000:
        print("--batch-size는 1~1000이어야 합니다.", file=sys.stderr)
        return 2
    if not 0 <= args.sleep_seconds <= 5:
        print("--sleep-seconds는 0~5여야 합니다.", file=sys.stderr)
        return 2
    if not 1 <= args.max_batches <= 10000:
        print("--max-batches는 1~10000이어야 합니다.", file=sys.stderr)
        return 2
    if not 30 <= args.max_seconds <= 3600:
        print("--max-seconds는 30~3600이어야 합니다.", file=sys.stderr)
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
            actual = cursor.fetchone()["db"]
            if actual != args.expected_db:
                raise MigrationError(f"접속한 DB가 다릅니다: {actual}")

            rows_before, source_bytes_before, invalid_before = _source_stats(cursor)
            has_column = _column_exists(cursor)
            print(f"대상: {actual}.{TABLE}")
            print(f"전체 행: {rows_before:,}")
            print(f"기존 BLOB: {source_bytes_before:,}바이트")
            print(f"차원이 다른 기존 BLOB: {invalid_before:,}행")
            print(f"{TARGET_COLUMN} 열: {'있음' if has_column else '없음'}")

            if not has_column:
                if not args.apply:
                    print("\n--apply 없이 실행해 아무것도 바꾸지 않았습니다.")
                    return 0
                print(f"\n{TARGET_COLUMN} 열을 추가합니다 (기존 열은 그대로 둡니다)...")
                cursor.execute(
                    _guard(
                        f"ALTER TABLE `{TABLE}` "
                        f"ADD COLUMN `{TARGET_COLUMN}` VECTOR({DIMENSION}) NULL"
                    )
                )
                connection.commit()
                print("추가 완료.")

            pending = _pending_rows(cursor)
            print(f"백필 대상: {pending:,}행")
            if not args.apply:
                print("\n--apply 없이 실행해 아무것도 바꾸지 않았습니다.")
                return 0
            if pending == 0:
                print("이미 모두 채워져 있습니다.")

            done = batches = 0
            failed_ids: set[str] = set()
            deadline = time.monotonic() + args.max_seconds
            while pending > 0:
                if batches >= args.max_batches:
                    print(f"\n--max-batches {args.max_batches} 도달, 안전 중단합니다.")
                    break
                if time.monotonic() >= deadline:
                    print(f"\n--max-seconds {args.max_seconds} 도달, 안전 중단합니다.")
                    break
                cursor.execute(
                    _guard(
                        f"SELECT `id`, `{SOURCE_COLUMN}` FROM `{TABLE}` "
                        f"WHERE `{TARGET_COLUMN}` IS NULL "
                        f"AND `{SOURCE_COLUMN}` IS NOT NULL LIMIT %s"
                    ),
                    (args.batch_size,),
                )
                batch = cursor.fetchall()
                if not batch:
                    break

                updated, invalid_ids = _apply_batch(cursor, batch)
                failed_ids.update(invalid_ids)
                connection.commit()
                batches += 1
                done += updated
                if updated == 0:
                    raise MigrationError(
                        "이번 배치에서 진행이 없습니다. 반복을 막기 위해 중단합니다. "
                        f"해석 불가 행={len(invalid_ids):,}"
                    )
                remaining = _pending_rows(cursor)
                if remaining >= pending:
                    raise MigrationError(
                        "대기 행 수가 줄지 않았습니다. 반복을 막기 위해 중단합니다."
                    )
                pending = remaining
                print(f"  {done:,}행 완료, {pending:,}행 남음")
                if args.sleep_seconds > 0 and pending > 0:
                    time.sleep(args.sleep_seconds)

            rows_after, source_bytes_after, invalid_after = _source_stats(cursor)
            if rows_after < rows_before:
                raise MigrationError(
                    f"행 수가 줄었습니다: {rows_before:,} → {rows_after:,}"
                )
            if source_bytes_after < source_bytes_before:
                raise MigrationError(
                    "기존 BLOB 바이트가 줄었습니다: "
                    f"{source_bytes_before:,} → {source_bytes_after:,}"
                )
            cursor.execute(
                _guard(
                    f"SELECT COUNT(*) AS n FROM `{TABLE}` "
                    f"WHERE `{TARGET_COLUMN}` IS NOT NULL"
                )
            )
            filled = int(cursor.fetchone()["n"])

            print(
                f"\n행 수 확인: {rows_before:,} → {rows_after:,} "
                "(감소 없음; 실행 중 새 행은 허용)"
            )
            print(
                f"기존 BLOB 확인: {source_bytes_before:,} → "
                f"{source_bytes_after:,}바이트 (감소 없음)"
            )
            print(f"벡터 열이 채워진 행: {filled:,} / {rows_after:,}")
            if failed_ids or invalid_after:
                print(
                    "건너뛴 행(차원 불일치): "
                    f"{max(len(failed_ids), invalid_after):,} — 원본은 그대로입니다."
                )
            if filled == rows_after:
                print("\n완료. 이제 STRUCTURED_MEMORY_VECTOR_SEARCH_ENABLED=true로 켤 수 있습니다.")
            else:
                print(
                    "\n아직 남은 행이 있습니다. 다시 실행하면 이어서 진행합니다. "
                    "검색 플래그는 켜지 마십시오."
                )
                return 1
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MigrationError as exc:
        print(f"중단: {exc}", file=sys.stderr)
        raise SystemExit(1)

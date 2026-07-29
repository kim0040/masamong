"""기존 기억을 보존하는 벡터 열 마이그레이션 안전장치를 확인한다."""

from array import array

import pytest

from scripts import apply_memory_vector_column as migration


class _Cursor:
    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []
        self.rowcount = 1

    def execute(self, sql, params=()):
        self.executed.append((sql, tuple(params)))


def test_guard_rejects_destructive_sql():
    for statement in (
        "DELETE FROM discord_memory_entries",
        "DROP TABLE discord_memory_entries",
        "TRUNCATE TABLE discord_memory_entries",
    ):
        with pytest.raises(migration.MigrationError):
            migration._guard(statement)


def test_vector_literal_only_accepts_exact_dimension():
    valid = array("f", [0.0] * migration.DIMENSION).tobytes()

    assert migration._vector_literal(valid).startswith("[")
    assert migration._vector_literal(valid[:-4]) is None


def test_invalid_batch_makes_zero_progress_without_touching_source():
    cursor = _Cursor()
    invalid = b"\x00" * 8

    updated, invalid_ids = migration._apply_batch(
        cursor,
        [{"id": "bad-memory", migration.SOURCE_COLUMN: invalid}],
    )

    assert updated == 0
    assert invalid_ids == {"bad-memory"}
    assert cursor.executed == []


def test_valid_batch_updates_only_copy_column():
    cursor = _Cursor()
    valid = array("f", [0.0] * migration.DIMENSION).tobytes()

    updated, invalid_ids = migration._apply_batch(
        cursor,
        [{"id": "memory-1", migration.SOURCE_COLUMN: valid}],
    )

    assert updated == 1
    assert invalid_ids == set()
    sql, params = cursor.executed[0]
    assert f"SET `{migration.TARGET_COLUMN}`" in sql
    assert f"SET `{migration.SOURCE_COLUMN}`" not in sql
    assert params[1] == "memory-1"


def test_cli_limits_are_finite_by_default():
    args = migration.parse_args(
        ["--expected-profile", "masamo", "--expected-db", "masamong"]
    )

    assert args.max_batches > 0
    assert args.max_seconds > 0


def test_profile_identity_uses_runtime_instance_name(monkeypatch):
    monkeypatch.setattr(migration.config, "PROFILE", "masamo")
    monkeypatch.setattr(migration.config, "INSTANCE_NAME", "masamo")

    assert migration._profile_identity() == ("masamo", "masamo")

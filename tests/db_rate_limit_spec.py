import asyncio
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest
import pytest_asyncio

import config
from utils import db as db_utils


@pytest_asyncio.fixture
async def api_log_db():
    db = await aiosqlite.connect(":memory:")
    await db.execute(
        """
        CREATE TABLE api_call_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_type TEXT NOT NULL,
            called_at TEXT NOT NULL
        )
        """
    )
    await db.commit()
    try:
        yield db
    finally:
        await db.close()


@pytest_asyncio.fixture
async def dm_limit_db():
    db = await aiosqlite.connect(":memory:")
    await db.executescript(
        """
        CREATE TABLE dm_usage_logs (
            user_id INTEGER PRIMARY KEY,
            usage_count INTEGER NOT NULL,
            window_start_at TEXT NOT NULL,
            reset_at TEXT NOT NULL
        );
        CREATE TABLE system_counters (
            counter_name TEXT PRIMARY KEY,
            counter_value INTEGER NOT NULL,
            last_reset_at TEXT
        );
        """
    )
    await db.commit()
    try:
        yield db
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_api_rate_limit_allows_usage_below_both_windows(
    api_log_db,
):
    now = datetime.now(timezone.utc)
    await api_log_db.executemany(
        "INSERT INTO api_call_log (api_type, called_at) VALUES (?, ?)",
        [
            ("weather", (now - timedelta(hours=2)).isoformat()),
            ("weather", (now - timedelta(seconds=20)).isoformat()),
            ("other", (now - timedelta(seconds=10)).isoformat()),
        ],
    )
    await api_log_db.commit()

    limited = await db_utils.check_api_rate_limit(
        api_log_db,
        "weather",
        rpm_limit=2,
        rpd_limit=3,
    )

    assert limited is False


@pytest.mark.asyncio
async def test_api_rate_limit_blocks_minute_limit(
    api_log_db,
):
    now = datetime.now(timezone.utc)
    await api_log_db.executemany(
        "INSERT INTO api_call_log (api_type, called_at) VALUES (?, ?)",
        [
            ("weather", (now - timedelta(seconds=30)).isoformat()),
            ("weather", (now - timedelta(seconds=10)).isoformat()),
        ],
    )
    await api_log_db.commit()

    limited = await db_utils.check_api_rate_limit(
        api_log_db,
        "weather",
        rpm_limit=2,
        rpd_limit=100,
    )

    assert limited is True


@pytest.mark.asyncio
async def test_api_rate_limit_blocks_daily_limit_even_outside_minute(
    api_log_db,
):
    now = datetime.now(timezone.utc)
    await api_log_db.executemany(
        "INSERT INTO api_call_log (api_type, called_at) VALUES (?, ?)",
        [
            ("weather", (now - timedelta(hours=3)).isoformat()),
            ("weather", (now - timedelta(hours=2)).isoformat()),
        ],
    )
    await api_log_db.commit()

    limited = await db_utils.check_api_rate_limit(
        api_log_db,
        "weather",
        rpm_limit=100,
        rpd_limit=2,
    )

    assert limited is True


@pytest.mark.asyncio
async def test_api_rate_limit_preserves_rows_older_than_daily_window(api_log_db):
    old_timestamp = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    await api_log_db.execute(
        "INSERT INTO api_call_log (api_type, called_at) VALUES (?, ?)",
        ("historical", old_timestamp),
    )
    await api_log_db.commit()

    limited = await db_utils.check_api_rate_limit(
        api_log_db,
        "weather",
        rpm_limit=100,
        rpd_limit=100,
    )
    async with api_log_db.execute(
        "SELECT COUNT(*) FROM api_call_log WHERE api_type = 'historical'"
    ) as cursor:
        preserved = int((await cursor.fetchone())[0])

    assert limited is False
    assert preserved == 1


@pytest.mark.asyncio
async def test_daily_api_counts_reads_multiple_keys_in_one_result(api_log_db):
    now = datetime.now(timezone.utc)
    await api_log_db.executemany(
        "INSERT INTO api_call_log (api_type, called_at) VALUES (?, ?)",
        [
            ("llm_user_1", now.isoformat()),
            ("llm_user_1", now.isoformat()),
            ("llm_global", now.isoformat()),
            ("unrelated", now.isoformat()),
            (
                "llm_global",
                (now - timedelta(days=2)).isoformat(),
            ),
        ],
    )
    await api_log_db.commit()

    counts = await db_utils.get_daily_api_counts(
        api_log_db,
        ("llm_user_1", "llm_global", "missing", "llm_global"),
    )

    assert counts == {
        "llm_user_1": 2,
        "llm_global": 1,
        "missing": 0,
    }


@pytest.mark.asyncio
async def test_hierarchical_llm_reservation_records_every_scope_once(
    api_log_db,
    monkeypatch,
):
    monkeypatch.setattr(config, "COMETAPI_RPM_LIMIT", 50)
    monkeypatch.setattr(config, "COMETAPI_RPD_LIMIT", 500)
    monkeypatch.setattr(config, "LLM_FEATURE_RPM_LIMIT", 40)
    monkeypatch.setattr(config, "LLM_FEATURE_RPD_LIMIT", 400)
    monkeypatch.setattr(config, "LLM_GUILD_RPM_LIMIT", 30)
    monkeypatch.setattr(config, "LLM_GUILD_RPD_LIMIT", 300)
    monkeypatch.setattr(config, "LLM_USER_RPM_LIMIT", 20)
    monkeypatch.setattr(config, "LLM_USER_RPD_LIMIT", 200)

    allowed, reason = await db_utils.reserve_llm_api_call(
        api_log_db,
        guild_id=22,
        user_id=33,
        feature="Image Prompt / unsafe punctuation",
    )
    async with api_log_db.execute(
        "SELECT api_type, COUNT(*) FROM api_call_log GROUP BY api_type"
    ) as cursor:
        rows = dict(await cursor.fetchall())

    assert allowed is True
    assert reason is None
    assert rows == {
        "llm:global": 1,
        "llm:feature:image_prompt_unsafe_punctuation": 1,
        "llm:guild:22": 1,
        "llm:user:33": 1,
    }


@pytest.mark.asyncio
async def test_concurrent_llm_reservations_cannot_cross_user_limit(
    api_log_db,
    monkeypatch,
):
    monkeypatch.setattr(config, "COMETAPI_RPM_LIMIT", 50)
    monkeypatch.setattr(config, "COMETAPI_RPD_LIMIT", 500)
    monkeypatch.setattr(config, "LLM_FEATURE_RPM_LIMIT", 50)
    monkeypatch.setattr(config, "LLM_FEATURE_RPD_LIMIT", 500)
    monkeypatch.setattr(config, "LLM_GUILD_RPM_LIMIT", 50)
    monkeypatch.setattr(config, "LLM_GUILD_RPD_LIMIT", 500)
    monkeypatch.setattr(config, "LLM_USER_RPM_LIMIT", 1)
    monkeypatch.setattr(config, "LLM_USER_RPD_LIMIT", 100)

    results = await asyncio.gather(
        *(
            db_utils.reserve_llm_api_call(
                api_log_db,
                guild_id=22,
                user_id=33,
                feature="chat",
            )
            for _ in range(2)
        )
    )

    assert sorted(allowed for allowed, _reason in results) == [False, True]
    assert any(reason == "사용자 분당 한도" for _allowed, reason in results)
    async with api_log_db.execute(
        "SELECT COUNT(*) FROM api_call_log WHERE api_type = 'llm:user:33'"
    ) as cursor:
        assert int((await cursor.fetchone())[0]) == 1


@pytest.mark.asyncio
async def test_dm_reservation_increments_user_and_global_together(dm_limit_db):
    allowed, reason, reset_time = await db_utils.reserve_dm_message(
        dm_limit_db,
        42,
    )

    async with dm_limit_db.execute(
        "SELECT usage_count FROM dm_usage_logs WHERE user_id = 42"
    ) as cursor:
        user_count = int((await cursor.fetchone())[0])
    async with dm_limit_db.execute(
        "SELECT counter_value FROM system_counters"
    ) as cursor:
        global_count = int((await cursor.fetchone())[0])

    assert (allowed, reason, reset_time) == (True, None, None)
    assert user_count == 1
    assert global_count == 1


@pytest.mark.asyncio
async def test_dm_user_limit_rejection_does_not_consume_global_slot(
    dm_limit_db,
    monkeypatch,
):
    monkeypatch.setattr(db_utils, "DM_LIMIT_COUNT", 1)
    now = datetime.now(timezone.utc)
    await dm_limit_db.execute(
        """
        INSERT INTO dm_usage_logs (
            user_id, usage_count, window_start_at, reset_at
        ) VALUES (?, 1, ?, ?)
        """,
        (
            42,
            now.isoformat(),
            (now + timedelta(hours=5)).isoformat(),
        ),
    )
    await dm_limit_db.commit()

    allowed, reason, reset_time = await db_utils.reserve_dm_message(
        dm_limit_db,
        42,
    )
    async with dm_limit_db.execute(
        "SELECT COUNT(*) FROM system_counters"
    ) as cursor:
        global_rows = int((await cursor.fetchone())[0])

    assert allowed is False
    assert reason == "user_limit"
    assert reset_time
    assert global_rows == 0


@pytest.mark.asyncio
async def test_dm_global_limit_rejection_does_not_consume_user_slot(
    dm_limit_db,
    monkeypatch,
):
    monkeypatch.setattr(db_utils, "DM_GLOBAL_LIMIT", 1)
    today_key = (
        "dm_daily_global_"
        + datetime.now(db_utils.KST).strftime("%Y-%m-%d")
    )
    await dm_limit_db.execute(
        """
        INSERT INTO system_counters (
            counter_name, counter_value, last_reset_at
        ) VALUES (?, 1, ?)
        """,
        (today_key, datetime.now(timezone.utc).isoformat()),
    )
    await dm_limit_db.commit()

    allowed, reason, reset_time = await db_utils.reserve_dm_message(
        dm_limit_db,
        42,
    )
    async with dm_limit_db.execute(
        "SELECT COUNT(*) FROM dm_usage_logs"
    ) as cursor:
        user_rows = int((await cursor.fetchone())[0])

    assert (allowed, reason, reset_time) == (
        False,
        "global_limit",
        None,
    )
    assert user_rows == 0


@pytest.mark.asyncio
async def test_dm_reservation_fails_closed_when_usage_store_is_missing():
    db = await aiosqlite.connect(":memory:")
    try:
        result = await db_utils.reserve_dm_message(db, 42)
    finally:
        await db.close()

    assert result == (False, "usage_store_error", None)

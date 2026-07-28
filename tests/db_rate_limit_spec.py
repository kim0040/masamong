from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest
import pytest_asyncio

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

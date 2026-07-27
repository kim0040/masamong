from datetime import datetime, timezone

from cogs.activity_cog import ActivityCog


def test_parse_period_key():
    assert ActivityCog._parse_period_key("오늘") == "today"
    assert ActivityCog._parse_period_key("이번주 랭킹") == "week"
    assert ActivityCog._parse_period_key("이번달") == "month"
    assert ActivityCog._parse_period_key("전체") == "all"
    assert ActivityCog._parse_period_key("아무말") == "all"


def test_period_bounds_today_has_utc_range():
    start_utc, end_utc, label = ActivityCog._period_bounds("today")
    assert start_utc is not None
    assert end_utc is not None
    assert "KST" in label

    start_dt = datetime.fromisoformat(start_utc)
    end_dt = datetime.fromisoformat(end_utc)
    assert start_dt.tzinfo is not None
    assert end_dt.tzinfo is not None
    assert start_dt <= end_dt


def test_format_kst_time_from_utc_iso():
    ts = datetime(2026, 4, 27, 0, 0, 0, tzinfo=timezone.utc).isoformat()
    rendered = ActivityCog._format_kst_time(ts)
    # UTC 00:00 -> KST 09:00
    assert rendered.endswith("09:00")


def test_grade_for_channel_is_dynamic():
    high_grade, _ = ActivityCog._grade_for_channel(50, total_msgs=100, total_users=10)
    low_grade, _ = ActivityCog._grade_for_channel(3, total_msgs=100, total_users=10)
    assert high_grade != low_grade

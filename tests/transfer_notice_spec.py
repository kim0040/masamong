"""편입 공지 수집·기준선·운영 안전성 명세."""

from dataclasses import replace
import json
from pathlib import Path

import pytest

from scripts.apply_transfer_notice_schema import TABLE_COLUMNS, schema_statements
from transfer_notice.catalog import load_transfer_sources
from transfer_notice.parsing import TransferNoticeItem, parse_transfer_list
from transfer_notice.storage import TransferNoticeStore


ROOT = Path(__file__).resolve().parents[1]


def _item(
    source_id: str,
    external_id: str,
    *,
    title: str,
    published_date: str | None,
    fingerprint: str,
) -> TransferNoticeItem:
    return TransferNoticeItem(
        source_id=source_id,
        university="테스트대학교",
        external_id=external_id,
        title=title,
        url=f"https://example.ac.kr/notice/{external_id}",
        published_date=published_date,
        fingerprint=fingerprint,
    )


def test_catalog_has_exactly_twenty_official_https_sources():
    sources = load_transfer_sources()

    assert len(sources) == 20
    assert len({source.university for source in sources.values()}) == 20
    for source in sources.values():
        assert source.list_url.startswith("https://")
        assert source.official_url.startswith("https://")
        assert source.allowed_hosts
        assert source.toeic_note


@pytest.mark.parametrize(
    ("source_id", "html", "expected_fragment"),
    [
        (
            "donga",
            """
            <table><tr><td class="subject">
              <a href="#" onclick="viewData('30166')">
                2026학년도 편입학모집 원서접수 안내
              </a><p>작성일 : 2025.12.15</p>
            </td></tr></table>
            """,
            "BOARD_IDX=30166",
        ),
        (
            "dongduk",
            """
            <ul><li><dl><dt>
              <a class="subTit" href="#none"
                 onclick="fn_goView('91907', false, '27', 'N')">
                [편입학] 2027학년도 편입학전형 시행계획 공지
              </a>
            </dt><dd>2026-06-10</dd></dl></li></ul>
            """,
            "id=91907",
        ),
        (
            "wku",
            """
            <table><tr><td>
              <a href="javascript:pop_pass_open('13589','N');">
                2026학년도 편입학 충원일정 안내
              </a><span>2026-01-27</span>
            </td></tr></table>
            """,
            "board_seq=13589",
        ),
    ],
)
def test_javascript_only_official_boards_build_canonical_detail_urls(
    source_id,
    html,
    expected_fragment,
):
    source = load_transfer_sources()[source_id]

    items, warnings = parse_transfer_list(html, source)

    assert warnings == []
    assert len(items) == 1
    assert expected_fragment in items[0].url
    assert items[0].published_date is not None


def test_mixed_board_rejects_unrelated_and_foreign_admissions_links():
    source = replace(
        load_transfer_sources()["cku"],
        allowed_hosts=("www.cku.ac.kr",),
    )
    html = """
    <a href="/bbs/iphak/1059/1/artclView.do">2026학년도 수시모집 안내</a>
    <a href="/bbs/iphak/1059/2/artclView.do">2026학년도 편입학 모집요강</a>
    <a href="https://evil.example/notice">2026학년도 편입학 원서접수 안내</a>
    """

    items, _warnings = parse_transfer_list(html, source)

    assert [item.title for item in items] == ["2026학년도 편입학 모집요강"]
    assert items[0].url.startswith("https://www.cku.ac.kr/")


def test_first_success_is_baseline_and_never_notifies_historical_rows(tmp_path):
    store = TransferNoticeStore(tmp_path / "core.db")
    try:
        first = _item(
            "s",
            "old",
            title="2026학년도 편입학 모집요강",
            published_date="2025-12-01",
            fingerprint="v1",
        )

        changes, baseline = store.upsert_source_items(
            "s",
            [first],
            observed_at="2026-07-28T10:00:00+00:00",
        )

        assert baseline is True
        assert changes == []
        assert len(store.latest_items()) == 1
    finally:
        store.close()


def test_new_current_item_notifies_once_and_older_backfill_is_suppressed(tmp_path):
    store = TransferNoticeStore(tmp_path / "core.db")
    try:
        initial = _item(
            "s",
            "initial",
            title="2026학년도 편입학 모집요강",
            published_date="2026-01-10",
            fingerprint="initial-v1",
        )
        store.upsert_source_items(
            "s",
            [initial],
            observed_at="2026-01-10T10:00:00+00:00",
        )
        current = _item(
            "s",
            "current",
            title="2027학년도 편입학 전형 일정 안내",
            published_date="2026-07-28",
            fingerprint="current-v1",
        )
        old_backfill = _item(
            "s",
            "backfill",
            title="2025학년도 편입학 모집요강",
            published_date="2025-01-01",
            fingerprint="backfill-v1",
        )

        changes, baseline = store.upsert_source_items(
            "s",
            [current, old_backfill],
            observed_at="2026-07-28T10:00:00+00:00",
        )
        second_changes, _ = store.upsert_source_items(
            "s",
            [current, old_backfill],
            observed_at="2026-07-28T11:00:00+00:00",
        )

        assert baseline is False
        assert [item["external_id"] for item in changes] == ["current"]
        assert second_changes == []
    finally:
        store.close()


def test_title_revision_has_new_revision_and_is_not_duplicated(tmp_path):
    store = TransferNoticeStore(tmp_path / "core.db")
    try:
        initial = _item(
            "s",
            "same",
            title="2027학년도 편입학 전형 일정 안내",
            published_date="2026-07-28",
            fingerprint="v1",
        )
        store.upsert_source_items(
            "s",
            [initial],
            observed_at="2026-07-28T10:00:00+00:00",
        )
        revised = replace(
            initial,
            title="2027학년도 편입학 전형 일정 변경 안내",
            fingerprint="v2",
        )

        changes, _ = store.upsert_source_items(
            "s",
            [revised],
            observed_at="2026-07-28T11:00:00+00:00",
        )

        assert len(changes) == 1
        assert changes[0]["change_type"] == "updated"
        assert changes[0]["revision"] == 2
    finally:
        store.close()


@pytest.mark.parametrize("backend", ["sqlite", "tidb"])
def test_schema_migration_is_additive_create_only(backend):
    statements = schema_statements(backend)

    assert len(statements) == 3
    assert all(statement.upper().startswith("CREATE TABLE IF NOT EXISTS") for statement in statements)
    assert all("DROP " not in statement.upper() for statement in statements)
    assert all("DELETE " not in statement.upper() for statement in statements)
    assert "payload_json" in TABLE_COLUMNS["transfer_notice_deliveries"]


def test_systemd_timer_is_daily_bounded_and_not_a_busy_loop():
    service = (
        ROOT / "deploy" / "systemd" / "masamong-transfer-notice-batch.service"
    ).read_text(encoding="utf-8")
    timer = (
        ROOT / "deploy" / "systemd" / "masamong-transfer-notice-batch.timer"
    ).read_text(encoding="utf-8")

    assert "OnCalendar=*-*-* 23:35:00 Asia/Seoul" in timer
    assert "CPUQuota=25%" in service
    assert "MemoryMax=256M" in service
    assert "TimeoutStartSec=960" in service
    assert "--timeout-seconds 900" in service
    assert "while " not in service
    assert "llm" not in service.casefold()

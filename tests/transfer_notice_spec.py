"""편입 공지 수집·기준선·운영 안전성 명세."""

from dataclasses import replace
import json
from pathlib import Path

import pytest

from scripts.apply_transfer_notice_schema import TABLE_COLUMNS, schema_statements
from transfer_notice.catalog import (
    load_manual_transfer_sources,
    load_transfer_sources,
)
from transfer_notice.parsing import (
    TransferNoticeItem,
    listing_fingerprint,
    parse_transfer_detail,
    parse_transfer_list,
)
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


def test_robots_blocked_official_sources_are_manual_and_never_mislabelled_as_auto():
    automatic = load_transfer_sources()
    manual = load_manual_transfer_sources()

    assert set(manual) == {"chungnam", "pknu"}
    assert not (set(automatic) & set(manual))
    for source in manual.values():
        assert source.official_url.startswith("https://")
        assert "자동 수집" in source.reason


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


def test_dynamic_list_metadata_does_not_change_title_or_fingerprint():
    source = load_transfer_sources()["yonsei_mirae"]
    template = """
    <a href="/mirae/admission/html/transfer/noticeView.asp?BBS_NO=42">
      [편입학] 2027학년도 편입학 모집요강
      작성일: 2026/07/29 ㅣ 조회수 : {views}
    </a>
    """

    first, first_warnings = parse_transfer_list(template.format(views="1,203"), source)
    second, second_warnings = parse_transfer_list(template.format(views="1,207"), source)

    assert first_warnings == second_warnings == []
    assert len(first) == len(second) == 1
    assert first[0].title == "[편입학] 2027학년도 편입학 모집요강"
    assert second[0].title == first[0].title
    assert second[0].fingerprint == first[0].fingerprint


def test_legacy_dynamic_title_is_normalized_without_replay(tmp_path):
    store = TransferNoticeStore(tmp_path / "core.db")
    url = "https://example.ac.kr/notice/same"
    noisy_title = (
        "2027학년도 편입학 모집요강 "
        "작성일: 2026/07/29 ㅣ 조회수 : 1203"
    )
    clean_title = "2027학년도 편입학 모집요강"
    legacy = TransferNoticeItem(
        source_id="s",
        university="테스트대학교",
        external_id="same",
        title=noisy_title,
        url=url,
        published_date="2026-07-29",
        fingerprint="legacy-noisy-fingerprint",
    )
    try:
        store.upsert_source_items(
            "s",
            [legacy],
            observed_at="2026-07-29T01:00:00+00:00",
        )
        normalized = replace(
            legacy,
            title=clean_title,
            fingerprint=listing_fingerprint(clean_title, url),
        )

        changes, _ = store.upsert_source_items(
            "s",
            [normalized],
            observed_at="2026-07-29T02:00:00+00:00",
        )
        latest = store.latest_items()[0]

        assert changes == []
        assert latest["title"] == clean_title
        assert latest["fingerprint"] == normalized.fingerprint
        assert latest["revision"] == 1
    finally:
        store.close()


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


def test_detail_page_extracts_bounded_summary_and_key_dates():
    item = _item(
        "s",
        "detail",
        title="2027학년도 편입학 전형 일정 안내",
        published_date="2026-07-28",
        fingerprint="listing-v1",
    )
    html = """
    <html><body>
      <nav>전혀 관련 없는 사이트 메뉴 2020-01-01</nav>
      <article class="board-view">
        <h1>2027학년도 편입학 전형 일정 안내</h1>
        <p>원서 접수는 2026년 12월 10일부터 진행합니다.</p>
        <p>서류 제출 마감은 2026-12-15이며 세부 전형은 모집요강을 확인하세요.</p>
        <script>const fakeDate = "2099-01-01";</script>
      </article>
    </body></html>
    """

    detailed = parse_transfer_detail(html, item)

    assert "원서 접수" in detailed.detail_summary
    assert "서류 제출" in detailed.detail_summary
    assert detailed.key_dates == ("2026-12-10", "2026-12-15")
    assert "2099-01-01" not in detailed.detail_text
    assert 0 < len(detailed.detail_text) <= 12_000
    assert len(detailed.detail_fingerprint) == 64


def test_detail_parser_prefers_title_context_and_ignores_dynamic_view_count():
    item = _item(
        "s",
        "detail",
        title="2027학년도 편입학 전형 일정 안내",
        published_date="2026-07-28",
        fingerprint="listing-v1",
    )
    template = """
    <html><body>
      <main>
        <nav>메인메뉴 바로가기 전체메뉴 {noise}</nav>
        <section class="board-view">
          <h2>2027학년도 편입학 전형 일정 안내</h2>
          <p>작성일: 2026-07-28 조회수: {views}</p>
          <p>원서 접수는 2026-12-10부터 시작합니다.</p>
          <p>제출 서류와 모집단위는 공식 모집요강을 확인하세요.</p>
        </section>
        <aside>{noise}</aside>
      </main>
    </body></html>
    """

    first = parse_transfer_detail(
        template.format(views="1,200", noise="메뉴 " * 500),
        item,
    )
    second = parse_transfer_detail(
        template.format(views="1,201", noise="다른 메뉴 " * 500),
        item,
    )

    assert "원서 접수" in first.detail_summary
    assert "메인메뉴 바로가기" not in first.detail_summary
    assert "1,200" not in first.detail_summary
    assert second.detail_fingerprint == first.detail_fingerprint


def test_detail_enrichment_and_detail_only_change_do_not_replay(tmp_path):
    store = TransferNoticeStore(tmp_path / "core.db")
    try:
        listed = _item(
            "s",
            "same",
            title="2027학년도 편입학 모집요강",
            published_date="2026-07-28",
            fingerprint="listing-v1",
        )
        store.upsert_source_items(
            "s",
            [listed],
            observed_at="2026-07-28T01:00:00+00:00",
        )
        first_detail = replace(
            listed,
            detail_summary="원서 접수는 2026-12-10입니다.",
            detail_text="공개 상세 본문",
            detail_fingerprint="detail-v1",
            key_dates=("2026-12-10",),
        )
        enrichment_changes, _ = store.upsert_source_items(
            "s",
            [first_detail],
            observed_at="2026-07-28T02:00:00+00:00",
        )
        revised_detail = replace(
            first_detail,
            detail_summary="원서 접수는 2026-12-11로 변경되었습니다.",
            detail_fingerprint="detail-v2",
            key_dates=("2026-12-11",),
        )
        detail_only_changes, _ = store.upsert_source_items(
            "s",
            [revised_detail],
            observed_at="2026-07-29T02:00:00+00:00",
        )
        latest = store.latest_items()[0]

        assert enrichment_changes == []
        assert detail_only_changes == []
        assert latest["revision"] == 1
        assert latest["detail_summary"] == revised_detail.detail_summary
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

    assert "OnCalendar=*-*-* 05:35:00 Asia/Seoul" in timer
    assert "CPUQuota=25%" in service
    assert "MemoryMax=256M" in service
    assert "TimeoutStartSec=960" in service
    assert "--timeout-seconds 900" in service
    assert "--max-details-per-source 3" in service
    assert "--min-request-interval-seconds 0.35" in service
    assert "while " not in service
    assert "llm" not in service.casefold()

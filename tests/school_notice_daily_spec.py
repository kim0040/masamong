"""학교 공지 daily 후보 선택과 공개 분류 활용 명세."""

from types import SimpleNamespace

from school_notice.daily import select_detail_candidates


def _candidate(name: str, *, pinned: bool):
    return SimpleNamespace(name=name, pinned=pinned)


def test_low_resource_selection_reserves_budget_for_recent_regular_notices():
    candidates = [
        _candidate("오래된 고정 1", pinned=True),
        _candidate("오래된 고정 2", pinned=True),
        _candidate("최신 일반 1", pinned=False),
        _candidate("최신 일반 2", pinned=False),
        _candidate("최신 일반 3", pinned=False),
    ]

    selected = select_detail_candidates(candidates, 4)

    assert [item.name for item in selected] == [
        "오래된 고정 1",
        "최신 일반 1",
        "최신 일반 2",
        "최신 일반 3",
    ]


def test_single_detail_budget_prefers_latest_regular_notice_over_pinned_notice():
    candidates = [
        _candidate("고정", pinned=True),
        _candidate("최신", pinned=False),
    ]

    selected = select_detail_candidates(candidates, 1)

    assert [item.name for item in selected] == ["최신"]

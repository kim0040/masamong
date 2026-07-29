"""학과·캠퍼스 전용 공지가 다른 사용자에게 섞이지 않는지 검증합니다."""

from datetime import date

from school_notice.personalization import score_notice


TODAY = date(2026, 7, 29)


def _profile(**overrides):
    profile = {
        "user_key": "discord-test",
        "school_id": "jbnu",
        "degree_level": "undergraduate",
        "grade": 3,
        "department": "소프트웨어공학과",
        "preferred_topics": [],
        "career_interests": [],
        "include_keywords": [],
        "exclude_keywords": [],
        "muted_topics": [],
        "double_majors": [],
        "minors": [],
    }
    profile.update(overrides)
    return profile


def _score(profile, *, source_tags=(), title="학과 공지", topics=()):
    return score_notice(
        profile=profile,
        profile_version=1,
        notice_id=1,
        notice_payload={
            "title": title,
            "body_text": "신청 안내입니다.",
            "candidate": {
                "title": title,
                "source_board": "공지사항",
                "source_tags": list(source_tags),
            },
        },
        analysis={
            "topics": list(topics),
            "audiences": ["학부생"],
            "actions": ["신청"],
            "eligibility_rules": [],
            "dates": [],
            "required": False,
            "urgency": "normal",
        },
        feedback_events=[],
        today=TODAY,
    )


def test_other_department_board_is_ineligible_and_hidden():
    result = _score(
        _profile(department="경영학과"),
        source_tags=("department:소프트웨어공학과",),
    )

    assert result["eligibility"] == "INELIGIBLE"
    assert result["band"] == "hidden"
    assert "등록한 전공과 다른 전공 전용 게시판" in result["reasons"]


def test_scoped_board_is_hidden_when_department_is_not_known():
    result = _score(
        _profile(department=None),
        source_tags=("department:소프트웨어공학과",),
    )

    assert result["score"] <= 39
    assert result["band"] == "hidden"
    assert any("전용 게시판 소속" in reason for reason in result["reasons"])


def test_matching_department_board_remains_visible():
    result = _score(
        _profile(),
        source_tags=("department:소프트웨어공학과",),
    )

    assert result["eligibility"] != "INELIGIBLE"
    assert result["band"] != "hidden"
    assert "사용자 전공과 직접 관련" in result["reasons"]


def test_general_school_board_still_uses_grade_and_interests():
    result = _score(
        _profile(preferred_topics=["장학"]),
        title="3학년 장학금 신청 안내",
        topics=("장학",),
    )

    assert result["band"] != "hidden"
    assert any("관심 분야 일치" in reason for reason in result["reasons"])

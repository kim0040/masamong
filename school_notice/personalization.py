from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .storage import NoticeRepository


ALLOWED_FEEDBACK = {
    "useful",
    "saved",
    "applied",
    "completed",
    "not_interested",
    "not_eligible",
    "already_knew",
    "dismiss_once",
    "mute_topic",
}

TOPIC_FEEDBACK = {
    "useful",
    "saved",
    "applied",
    "not_interested",
    "already_knew",
    "mute_topic",
}

NOTICE_STATE_FEEDBACK = {
    "completed",
    "not_eligible",
    "dismiss_once",
}

FEEDBACK_DELTAS = {
    "useful": 0.06,
    "saved": 0.04,
    "applied": 0.10,
    "not_interested": -0.06,
    "already_knew": -0.03,
}

MANDATORY_TOPICS = {"등록금", "수강", "학적", "졸업", "병무"}
DEGREE_LEVELS = {
    "undergraduate",
    "master",
    "doctorate",
    "integrated",
    "non_degree",
}
PROFILE_LIST_FIELDS = {
    "career_interests",
    "preferred_topics",
    "muted_topics",
    "include_keywords",
    "exclude_keywords",
    "double_majors",
    "minors",
    "completed_courses",
    "unknown_fields",
}
PROFILE_OPTIONAL_STRINGS = {
    "school",
    "department",
    "campus",
    "admission_type",
    "enrollment_status",
    "timezone",
}
NOTIFICATION_BANDS = {"action", "opportunity", "reference"}


def validate_profile(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("프로필은 JSON 객체여야 합니다.")
    required = {"user_key", "school_id", "degree_level"}
    missing = sorted(required - set(payload))
    if missing:
        raise ValueError(f"프로필 필수 필드 누락: {', '.join(missing)}")
    for field in ("user_key", "school_id", "degree_level"):
        if not isinstance(payload[field], str) or not payload[field].strip():
            raise ValueError(f"{field}는 비어 있지 않은 문자열이어야 합니다.")
    degree = str(payload["degree_level"])
    if degree not in DEGREE_LEVELS:
        raise ValueError(
            "degree_level은 " + ", ".join(sorted(DEGREE_LEVELS)) + " 중 하나여야 합니다."
        )
    grade = payload.get("grade")
    if degree == "undergraduate" and grade is None:
        raise ValueError("학부생 프로필에는 grade가 필요합니다.")
    if grade is not None and (
        isinstance(grade, bool)
        or not isinstance(grade, int)
        or not 1 <= grade <= 6
    ):
        raise ValueError("grade는 1~6 정수 또는 생략이어야 합니다.")
    for field in PROFILE_OPTIONAL_STRINGS:
        value = payload.get(field)
        if value is not None and (
            not isinstance(value, str) or not value.strip()
        ):
            raise ValueError(f"{field}는 비어 있지 않은 문자열이어야 합니다.")
    for field in PROFILE_LIST_FIELDS:
        value = payload.get(field, [])
        if not isinstance(value, list) or len(value) > 100:
            raise ValueError(f"{field}는 최대 100개의 문자열 배열이어야 합니다.")
        if any(
            not isinstance(item, str) or not item.strip() or len(item) > 100
            for item in value
        ):
            raise ValueError(f"{field}에는 비어 있지 않은 짧은 문자열만 허용됩니다.")
    numeric_contracts = {
        "student_number_year": (1900, 2100),
        "completed_semesters": (0, 30),
        "gpa_last_semester": (0, 4.5),
        "transfer_approved_credits": (0, 300),
    }
    for field, (minimum, maximum) in numeric_contracts.items():
        value = payload.get(field)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{field}는 숫자여야 합니다.")
        if not minimum <= value <= maximum:
            raise ValueError(f"{field}는 {minimum}~{maximum} 범위여야 합니다.")
    language_scores = payload.get("language_scores", {})
    if not isinstance(language_scores, dict) or len(language_scores) > 20:
        raise ValueError("language_scores는 최대 20개 항목의 객체여야 합니다.")
    if any(
        not isinstance(key, str)
        or not key.strip()
        or isinstance(value, (dict, list))
        for key, value in language_scores.items()
    ):
        raise ValueError("language_scores의 키와 값 형식이 올바르지 않습니다.")
    timezone = str(payload.get("timezone") or "Asia/Seoul")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"알 수 없는 timezone: {timezone}") from exc

    preferences = payload.get("notification_preferences", {})
    if not isinstance(preferences, dict):
        raise ValueError("notification_preferences는 객체여야 합니다.")
    unknown_preferences = set(preferences) - {
        "minimum_score",
        "include_bands",
        "max_action",
        "max_opportunity",
        "max_reference",
        "strict_campus",
    }
    if unknown_preferences:
        raise ValueError(
            "지원하지 않는 notification_preferences: "
            + ", ".join(sorted(unknown_preferences))
        )
    minimum_score = preferences.get("minimum_score", 40)
    if (
        isinstance(minimum_score, bool)
        or not isinstance(minimum_score, (int, float))
        or not 0 <= minimum_score <= 100
    ):
        raise ValueError("minimum_score는 0~100 숫자여야 합니다.")
    include_bands = preferences.get(
        "include_bands",
        ["action", "opportunity", "reference"],
    )
    if (
        not isinstance(include_bands, list)
        or not include_bands
        or not set(include_bands).issubset(NOTIFICATION_BANDS)
    ):
        raise ValueError("include_bands 값이 올바르지 않습니다.")
    for field in ("max_action", "max_opportunity", "max_reference"):
        value = preferences.get(field)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 100
        ):
            raise ValueError(f"{field}는 0~100 정수여야 합니다.")
    if not isinstance(preferences.get("strict_campus", False), bool):
        raise ValueError("strict_campus는 boolean이어야 합니다.")
    return dict(payload)


def load_profile(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_profile(payload)


def topic_weights(
    events: list[dict[str, Any]],
    *,
    today: date,
    half_life_days: float = 90.0,
) -> dict[str, float]:
    offsets: dict[str, float] = {}
    for event in events:
        topic = event.get("topic")
        delta = FEEDBACK_DELTAS.get(str(event.get("feedback_type")))
        if not topic or delta is None:
            continue
        try:
            created = datetime.fromisoformat(str(event["created_at"])).date()
        except (ValueError, TypeError):
            created = today
        age = max(0, (today - created).days)
        decay = math.pow(0.5, age / half_life_days)
        offsets[str(topic)] = offsets.get(str(topic), 0.0) + delta * decay
    return {
        topic: max(0.70, min(1.30, 1.0 + offset))
        for topic, offset in offsets.items()
    }


def _direct_feedback(
    events: list[dict[str, Any]],
    notice_id: int,
) -> set[str]:
    return {
        str(event["feedback_type"])
        for event in events
        if event.get("notice_id") == notice_id
    }


def _deadline_effect(
    analysis: dict[str, Any],
    today: date,
) -> tuple[float, str | None, bool]:
    deadline_values: list[date] = []
    for item in analysis.get("dates", []):
        if not isinstance(item, dict) or item.get("kind") != "deadline":
            continue
        try:
            deadline_values.append(date.fromisoformat(str(item["date"])))
        except (ValueError, KeyError):
            continue
    if not deadline_values:
        return 0.0, None, False
    future = sorted(value for value in deadline_values if value >= today)
    if not future:
        latest = max(deadline_values)
        return -50.0, latest.isoformat(), True
    nearest = future[0]
    days = (nearest - today).days
    if days <= 3:
        return 20.0, nearest.isoformat(), False
    if days <= 7:
        return 15.0, nearest.isoformat(), False
    if days <= 30:
        return 8.0, nearest.isoformat(), False
    return 2.0, nearest.isoformat(), False


def _next_event_date(
    analysis: dict[str, Any],
    today: date,
) -> str | None:
    values: list[date] = []
    for item in analysis.get("dates", []):
        if not isinstance(item, dict) or item.get("kind") != "event_date":
            continue
        try:
            value = date.fromisoformat(str(item["date"]))
        except (ValueError, KeyError):
            continue
        if value >= today:
            values.append(value)
    return min(values).isoformat() if values else None


def _constraint_matches(actual: float | int | str, operator: str, expected: Any) -> bool:
    if operator == "equals":
        return actual == expected
    if not isinstance(actual, (int, float)) or not isinstance(
        expected,
        (int, float),
    ):
        return False
    return {
        "lt": actual < expected,
        "lte": actual <= expected,
        "gt": actual > expected,
        "gte": actual >= expected,
    }.get(operator, False)


def _tag_values(candidate: dict[str, Any], prefix: str) -> list[str]:
    marker = f"{prefix}:"
    return [
        str(tag)[len(marker) :].strip()
        for tag in candidate.get("source_tags", [])
        if str(tag).startswith(marker) and str(tag)[len(marker) :].strip()
    ]


def _normalized(value: str) -> str:
    return "".join(value.casefold().split())


def _term_in_text(term: str, text: str) -> bool:
    normalized_term = _normalized(term)
    if len(normalized_term) < 2:
        return False
    if normalized_term.isascii() and normalized_term.isalnum():
        return bool(
            re.search(
                rf"(?<![a-z0-9]){re.escape(normalized_term)}"
                r"(?![a-z0-9])",
                text.casefold(),
            )
        )
    return normalized_term in _normalized(text)


def _department_terms(profile: dict[str, Any]) -> list[str]:
    values = [
        profile.get("department"),
        *profile.get("double_majors", []),
        *profile.get("minors", []),
    ]
    results: list[str] = []
    for raw in values:
        if not isinstance(raw, str) or not raw.strip():
            continue
        value = raw.strip()
        for candidate in (
            value,
            value.removesuffix("학과"),
            value.removesuffix("학부"),
            value.removesuffix("전공"),
        ):
            if len(_normalized(candidate)) >= 2 and candidate not in results:
                results.append(candidate)
    return results


def score_notice(
    *,
    profile: dict[str, Any],
    profile_version: int,
    notice_id: int,
    notice_payload: dict[str, Any],
    analysis: dict[str, Any],
    feedback_events: list[dict[str, Any]],
    today: date,
) -> dict[str, Any]:
    score = 20.0
    reasons: list[str] = []
    eligibility = "UNKNOWN"
    source_scope_unknown = False
    candidate = notice_payload.get("candidate", {})
    title = str(notice_payload.get("title") or candidate.get("title") or "")
    body = str(notice_payload.get("body_text") or "")
    source_board = str(candidate.get("source_board") or "")
    source_category = str(candidate.get("category") or "")
    searchable_text = "\n".join(
        (title, body, source_board, source_category)
    )
    topics = [str(item) for item in analysis.get("topics", [])]
    audiences = [str(item) for item in analysis.get("audiences", [])]
    actions = [str(item) for item in analysis.get("actions", [])]

    direct = _direct_feedback(feedback_events, notice_id)
    if direct.intersection({"dismiss_once", "completed"}):
        return {
            "score": 0.0,
            "band": "hidden",
            "eligibility": "ELIGIBLE",
            "reasons": ["사용자가 이번 공지를 숨기거나 완료함"],
            "topics": topics,
            "deadline": None,
            "next_event": None,
            "profile_version": profile_version,
            "mandatory_protected": False,
        }
    if "not_eligible" in direct:
        eligibility = "INELIGIBLE"
        score -= 45
        reasons.append("사용자가 자격 없음으로 표시")

    degree = str(profile.get("degree_level") or "")
    if degree == "undergraduate":
        if "학부생" in audiences:
            score += 20
            eligibility = "LIKELY_ELIGIBLE"
            reasons.append("학부생 대상")
        if "재학생" in audiences:
            score += 10
            reasons.append("재학생 대상")
        if "대학원생" in audiences and "학부생" not in audiences:
            score -= 40
            eligibility = "LIKELY_INELIGIBLE"
            reasons.append("대학원생 중심 공지")
    elif degree in {"master", "doctorate", "integrated"}:
        if "대학원생" in audiences:
            score += 20
            eligibility = "LIKELY_ELIGIBLE"
            reasons.append("대학원생 대상")
        if "학부생" in audiences and "대학원생" not in audiences:
            score -= 40
            eligibility = "LIKELY_INELIGIBLE"
            reasons.append("학부생 중심 공지")

    grade = profile.get("grade")
    grade_rules = [
        item
        for item in analysis.get("eligibility_rules", [])
        if isinstance(item, dict) and item.get("field") == "grade"
    ]
    for rule in grade_rules:
        values = rule.get("value")
        if (
            rule.get("operator") == "in"
            and isinstance(values, list)
            and isinstance(grade, int)
        ):
            if grade in values:
                score += 15
                eligibility = "ELIGIBLE"
                reasons.append(f"{grade}학년 조건 일치")
            else:
                score -= 35
                eligibility = "INELIGIBLE"
                reasons.append(f"{grade}학년이 명시 조건과 불일치")

    unknown_constraints: list[str] = []
    constraint_fields = {
        "student_number_year": ("학번 연도", "student_number_year"),
        "completed_semesters": ("이수 학기", "completed_semesters"),
        "gpa": ("직전학기 성적", "gpa_last_semester"),
        "admission_type": ("입학 유형", "admission_type"),
    }
    seen_constraints: set[tuple[str, str, Any]] = set()
    for rule in analysis.get("eligibility_rules", []):
        if not isinstance(rule, dict) or rule.get("field") not in constraint_fields:
            continue
        field = str(rule["field"])
        operator = str(rule.get("operator") or "")
        expected = rule.get("value")
        if field == "student_number_year" and isinstance(expected, int):
            if operator == "lt":
                constraint_key = (field, "max", expected - 1)
            elif operator == "lte":
                constraint_key = (field, "max", expected)
            else:
                constraint_key = (field, operator, expected)
        else:
            constraint_key = (field, operator, expected)
        if constraint_key in seen_constraints:
            continue
        seen_constraints.add(constraint_key)
        label, profile_key = constraint_fields[field]
        actual = profile.get(profile_key)
        if actual is None:
            if label not in unknown_constraints:
                unknown_constraints.append(label)
            if eligibility != "INELIGIBLE":
                eligibility = "UNKNOWN"
            reasons.append(f"{label} 조건은 있으나 프로필 값이 없음")
            continue
        if _constraint_matches(
            actual,
            operator,
            expected,
        ):
            if eligibility != "INELIGIBLE":
                eligibility = "ELIGIBLE"
            reasons.append(f"{label} 조건 일치")
        else:
            eligibility = "INELIGIBLE"
            score -= 45
            reasons.append(f"{label} 조건 불일치")

    department_terms = _department_terms(profile)
    source_departments = _tag_values(candidate, "department")
    department_match = any(
        _term_in_text(term, searchable_text)
        or any(_term_in_text(term, tagged) for tagged in source_departments)
        for term in department_terms
    )
    if department_match:
        score += 22
        reasons.append("사용자 전공과 직접 관련")
    elif source_departments and department_terms:
        score -= 60
        eligibility = "INELIGIBLE"
        reasons.append("등록한 전공과 다른 전공 전용 게시판")
    elif source_departments:
        source_scope_unknown = True
        reasons.append("전공 전용 게시판이나 등록한 전공 정보가 없음")

    source_degrees = set(_tag_values(candidate, "degree"))
    if source_degrees:
        if degree in source_degrees:
            score += 10
            reasons.append("학위 과정 게시판 일치")
        else:
            score -= 60
            eligibility = "INELIGIBLE"
            reasons.append("학위 과정 게시판 불일치")

    campus = str(profile.get("campus") or "")
    source_campuses = _tag_values(candidate, "campus")
    if campus and source_campuses:
        if any(_normalized(campus) == _normalized(item) for item in source_campuses):
            score += 12
            reasons.append("소속 캠퍼스 공지")
        else:
            score -= 60
            eligibility = "INELIGIBLE"
            reasons.append("등록한 캠퍼스와 다른 캠퍼스 전용 게시판")
    elif source_campuses:
        source_scope_unknown = True
        reasons.append("캠퍼스 전용 게시판이나 등록한 캠퍼스 정보가 없음")

    if (
        str(profile.get("admission_type")) == "transfer"
        and ("편입생" in searchable_text or "편입학" in searchable_text)
    ):
        score += 15
        reasons.append("편입생 직접 관련")

    enrollment_status = str(profile.get("enrollment_status") or "")
    enrollment_audiences = {
        "enrolled": ("재학생",),
        "assumed_enrolled": ("재학생",),
        "leave": ("휴학생",),
        "returning": ("복학생",),
        "expected_graduate": ("졸업예정자",),
    }
    expected_audiences = enrollment_audiences.get(enrollment_status, ())
    if expected_audiences and any(item in audiences for item in expected_audiences):
        score += 10
        reasons.append("현재 학적 상태와 대상 일치")
    if (
        enrollment_status == "leave"
        and "재학생" in audiences
        and "휴학생" not in audiences
    ):
        score -= 25
        reasons.append("재학생 전용으로 휴학생과 불일치 가능")

    interest_terms = list(
        dict.fromkeys(
            [
                *(
                    str(item)
                    for item in profile.get("preferred_topics", [])
                ),
                *(
                    str(item)
                    for item in profile.get("career_interests", [])
                ),
            ]
        )
    )
    matched_interests = [
        term
        for term in interest_terms
        if term in topics or _term_in_text(term, searchable_text)
    ]
    if matched_interests:
        score += min(18, 6 * len(matched_interests))
        reasons.append("관심 분야 일치: " + ", ".join(matched_interests[:3]))

    include_keywords = [
        str(item)
        for item in profile.get("include_keywords", [])
        if _term_in_text(str(item), searchable_text)
    ]
    if include_keywords:
        score += min(20, 10 * len(include_keywords))
        reasons.append("우선 키워드 일치: " + ", ".join(include_keywords[:3]))

    if actions:
        score += 10
        reasons.append("신청·제출 등 행동 항목 존재")
    required = bool(analysis.get("required"))
    if required:
        score += 20
        reasons.append("필수 또는 불이익 가능 문구")

    urgency = str(analysis.get("urgency") or "normal")
    urgency_bonus = {"low": 0, "normal": 3, "high": 10, "critical": 18}.get(
        urgency,
        0,
    )
    score += urgency_bonus
    if urgency_bonus:
        reasons.append(f"긴급도 {urgency}")

    deadline_bonus, deadline, expired = _deadline_effect(analysis, today)
    next_event = _next_event_date(analysis, today)
    score += deadline_bonus
    if deadline:
        reasons.append(
            f"마감 {deadline}" + (" (지남)" if expired else "")
        )

    weights = topic_weights(feedback_events, today=today)
    applicable_weights = [weights.get(topic, 1.0) for topic in topics]
    feedback_weight = (
        sum(applicable_weights) / len(applicable_weights)
        if applicable_weights
        else 1.0
    )
    if abs(feedback_weight - 1.0) > 0.001:
        reasons.append(f"관심 피드백 가중치 {feedback_weight:.2f}")
    score = max(0.0, min(100.0, score))
    score *= feedback_weight

    muted = {
        str(item)
        for item in profile.get("muted_topics", [])
        if str(item).strip()
    }
    muted.update(
        str(event["topic"])
        for event in feedback_events
        if event.get("feedback_type") == "mute_topic"
        and isinstance(event.get("topic"), str)
        and str(event["topic"]).strip()
    )
    muted_match = muted.intersection(topics)
    mandatory_protected = required and bool(MANDATORY_TOPICS.intersection(topics))
    if muted_match and not mandatory_protected:
        score = min(score, 20)
        reasons.append("사용자가 명시적으로 주제를 숨김")
    excluded_keywords = [
        str(item)
        for item in profile.get("exclude_keywords", [])
        if _term_in_text(str(item), searchable_text)
    ]
    if excluded_keywords and not mandatory_protected:
        score = min(score, 20)
        reasons.append("제외 키워드 일치: " + ", ".join(excluded_keywords[:3]))
    if mandatory_protected and eligibility != "INELIGIBLE" and score < 70:
        score = 70
        reasons.append("필수 행정 공지 보호 규칙")
    if unknown_constraints:
        score = min(score, 69)
        reasons.append("확인되지 않은 자격 조건이 있어 우선순위 상한 적용")
    if source_scope_unknown:
        # 학과·캠퍼스 전용 source는 해당 소속을 확인할 수 있을 때만 자동 DM한다.
        # 일반 학교 게시판은 이 제한을 받지 않으므로 최소 정보만 등록한 사용자도
        # 학교 공통 공지는 계속 받을 수 있다.
        score = min(score, 39)
        reasons.append("전용 게시판 소속을 확인할 수 없어 자동 알림 제외")
    if eligibility == "LIKELY_INELIGIBLE":
        # 코어 내부의 약한 audience 추론을 전달 계약의 확정 자격 판정으로
        # 내보내지 않는다. 확인이 필요한 UNKNOWN으로 낮추고 자동 DM에서는
        # 제외해 다른 학위 과정 공지가 섞이지 않게 한다.
        eligibility = "UNKNOWN"
        score = min(score, 39)
        reasons.append("다른 학위 과정 대상일 가능성이 있어 자동 알림 제외")

    score = max(0.0, min(100.0, round(score, 2)))
    if expired and not required:
        band = "hidden"
    elif score >= 80:
        band = "action"
    elif score >= 60:
        band = "opportunity"
    elif score >= 40:
        band = "reference"
    else:
        band = "hidden"
    if eligibility == "INELIGIBLE":
        band = "hidden"

    unique_reasons = list(dict.fromkeys(reasons))
    return {
        "score": score,
        "band": band,
        "eligibility": eligibility,
        "reasons": unique_reasons[:8],
        "topics": topics,
        "deadline": deadline,
        "next_event": next_event,
        "profile_version": profile_version,
        "mandatory_protected": mandatory_protected,
    }


@dataclass
class FeedbackService:
    repository: NoticeRepository

    def record(
        self,
        *,
        user_key: str,
        feedback_type: str,
        notice_id: int | None = None,
        topic: str | None = None,
        reason: str | None = None,
    ) -> list[int]:
        if feedback_type not in ALLOWED_FEEDBACK:
            raise ValueError(f"지원하지 않는 피드백: {feedback_type}")
        if feedback_type in NOTICE_STATE_FEEDBACK and notice_id is None:
            raise ValueError(f"{feedback_type}에는 공지 ID가 필요합니다.")
        if feedback_type in TOPIC_FEEDBACK and topic is None and notice_id is None:
            raise ValueError(
                f"{feedback_type}에는 topic 또는 분석된 공지 ID가 필요합니다."
            )
        topics: list[str | None] = [topic]
        if (
            topic is None
            and notice_id is not None
            and feedback_type in TOPIC_FEEDBACK
        ):
            analysis = self.repository.latest_analysis_any(notice_id)
            derived = analysis.get("topics", []) if analysis else []
            topics = [str(item) for item in derived[:3]] or [None]
        if feedback_type in TOPIC_FEEDBACK and not any(topics):
            raise ValueError(
                f"{feedback_type}에는 --topic 또는 분석된 topic이 있는 공지가 "
                "필요합니다."
            )
        return [
            self.repository.add_feedback(
                user_key=user_key,
                notice_id=notice_id,
                feedback_type=feedback_type,
                topic=item,
                reason=reason,
            )
            for item in topics
        ]

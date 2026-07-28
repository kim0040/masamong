from __future__ import annotations

import re

from .models import NoticeSignals


FULL_DATE_PATTERN = re.compile(
    r"\b(20\d{2})\s*[./년-]\s*(\d{1,2})\s*[./월-]\s*(\d{1,2})\s*일?"
    r"(?:\s*\([월화수목금토일]\))?"
)
MONTH_DAY_PATTERN = re.compile(
    r"(?<![\d.년/-])(\d{1,2})\s*[./월]\s*(\d{1,2})\s*일?"
    r"(?:\s*\([월화수목금토일]\))?"
)

ACTION_TERMS = {
    "신청": ("신청", "접수"),
    "지원": ("지원서", "지원 바랍니다", "지원자"),
    "제출": ("제출",),
    "등록": ("등록", "재등록"),
    "수강신청": ("수강신청",),
    "납부": ("납부", "등록금"),
    "신고": ("신고",),
    "참여": ("참여", "모집"),
}

AUDIENCE_TERMS = {
    "학부생": ("학부생", "학부 재학생", "학사과정", "[학부]"),
    "대학원생": ("대학원생", "대학원 재학생", "석사과정", "박사과정"),
    "재학생": ("재학생",),
    "휴학생": ("휴학생", "휴학 중"),
    "복학생": ("복학생", "복학 예정"),
    "신입생": ("신입생", "신입학"),
    "졸업예정자": ("졸업예정자", "졸업 예정자"),
    "교직원": ("교직원",),
    "외국인학생": ("외국인 학생", "외국인학생", "유학생"),
    "편입생": ("편입생", "편입학"),
}

TOPIC_TERMS = {
    "장학": ("장학", "장학생"),
    "등록금": ("등록금", "학비"),
    "수강": ("수강신청", "수강변경", "시간표"),
    "학적": ("휴학", "복학", "재입학", "학적"),
    "졸업": ("졸업", "학위취득"),
    "취업": ("취업", "진로", "인턴"),
    "기숙사": ("생활관", "기숙사"),
    "국제교류": ("교환학생", "해외파견", "국제교류"),
    "공모전": ("공모전", "경진대회", "해커톤"),
    "병무": ("예비군", "군입대", "병무"),
}


def _matched_labels(text: str, vocabulary: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    return tuple(
        label
        for label, terms in vocabulary.items()
        if any(term.casefold() in text.casefold() for term in terms)
    )


def extract_signals(title: str, body: str) -> NoticeSignals:
    text = f"{title}\n{body}"
    dates: list[str] = []
    occupied_spans: list[tuple[int, int]] = []
    for match in FULL_DATE_PATTERN.finditer(text):
        year, month, day = (int(value) for value in match.groups()[:3])
        normalized = f"{year:04d}-{month:02d}-{day:02d}"
        if normalized not in dates:
            dates.append(normalized)
        occupied_spans.append(match.span())
        if len(dates) >= 12:
            break
    if len(dates) < 12:
        for match in MONTH_DAY_PATTERN.finditer(text):
            if any(
                match.start() < end and match.end() > start
                for start, end in occupied_spans
            ):
                continue
            month, day = (int(value) for value in match.groups()[:2])
            normalized = f"{month:02d}-{day:02d}"
            if normalized not in dates:
                dates.append(normalized)
            if len(dates) >= 12:
                break
    return NoticeSignals(
        dates=tuple(dates),
        actions=_matched_labels(text, ACTION_TERMS),
        audiences=_matched_labels(text, AUDIENCE_TERMS),
        topics=_matched_labels(text, TOPIC_TERMS),
    )

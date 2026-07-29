# -*- coding: utf-8 -*-
"""
프로젝트 전반에서 공유하는 상수 정의 모듈입니다.

NSFW 필터 키워드, 공통 매직넘버 등을 한 곳에서 관리하여
중복 코드를 방지하고 일관성을 유지합니다.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# NSFW 필터 키워드 (이미지 생성, RAG 컨텍스트 검사 등에서 공통 사용)
# ---------------------------------------------------------------------------
NSFW_KEYWORDS: frozenset[str] = frozenset([
    # 영문 (선정적 키워드)
    'nude', 'naked', 'nsfw', 'explicit', 'sexual', 'porn', 'pornographic',
    'hentai', 'erotic', 'xxx', 'adult only', 'lewd',
    'topless', 'bottomless', 'genitals', 'nipple',
    'intercourse', 'orgasm', 'fetish', 'bdsm', 'bondage',
    'sexy',
    # 영문 (우회 시도)
    'n*de', 'nak3d', 'nud3', 'p0rn', 'pr0n', 's3x', 'seggs',
    'boobs', 'tits', 'titties',
    # 영문 (혐오/폭력)
    'hate', 'gore', 'suicide', 'murder', 'torture', 'kill',
    'terror', 'massacre', 'genocide', 'racist', 'racism',
    'neo-nazi', 'nazi', 'swastika', 'kkk',
    'self-harm', 'selfharm', 'cutting',
    # 한국어 (선정적)
    # "성인", "노출", "가슴", "엉덩이"처럼 정상적인 여행·사진·의학
    # 문맥에도 흔한 단어는 로컬 선차단에서 제외한다. 애매한 문맥은 이미지
    # provider의 문맥 기반 안전 필터가 최종 판단한다.
    '야한', '선정적', '음란', '에로', '야동', '포르노',
    '벗은', '알몸', '나체', '누드', '성기', '성관계',
    '섹시',
    '19금', '18금', 'r18', 'r-18',
    # 한국어 (혐오/폭력)
    '살인', '자살', '테러', '학살', '고문',
    '인종차별', '나치',
])


_ASCII_NSFW_KEYWORDS: tuple[str, ...] = tuple(
    sorted(
        (keyword for keyword in NSFW_KEYWORDS if keyword.isascii()),
        key=len,
        reverse=True,
    )
)
_NON_ASCII_NSFW_KEYWORDS: tuple[str, ...] = tuple(
    sorted(
        (keyword for keyword in NSFW_KEYWORDS if not keyword.isascii()),
        key=len,
        reverse=True,
    )
)
_ASCII_NSFW_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:"
    + "|".join(re.escape(keyword) for keyword in _ASCII_NSFW_KEYWORDS)
    + r")(?![a-z0-9])",
    re.IGNORECASE,
)


def contains_nsfw(text: str) -> bool:
    """명백한 NSFW/극단적 폭력 표현을 경량 선차단합니다.

    영문은 단어 경계를 확인해 ``skill`` 안의 ``kill`` 같은 부분 문자열
    오탐을 피한다. 한국어는 조사가 붙는 언어 특성상 명백한 표현만 부분
    문자열로 확인한다.
    """
    if not text:
        return False
    lower = text.lower()
    return bool(_ASCII_NSFW_PATTERN.search(lower)) or any(
        keyword in lower for keyword in _NON_ASCII_NSFW_KEYWORDS
    )


# ---------------------------------------------------------------------------
# 메시지 분할 관련 상수
# ---------------------------------------------------------------------------
DISCORD_MESSAGE_LIMIT = 2000
SPLIT_MESSAGE_CHUNK_SIZE = 1900


# ---------------------------------------------------------------------------
# DM 사용량 제한 상수
# ---------------------------------------------------------------------------
DM_LIMIT_WINDOW_HOURS = 5
DM_LIMIT_COUNT = 30
DM_GLOBAL_LIMIT = 100
FORTUNE_DAILY_LIMIT = 3


# ---------------------------------------------------------------------------
# 지진 관련 상수
# ---------------------------------------------------------------------------
EARTHQUAKE_MIN_MAGNITUDE = 4.0
EARTHQUAKE_EVACUATION_MAGNITUDE = 6.0


# ---------------------------------------------------------------------------
# 이모지 캐시 관련 상수
# ---------------------------------------------------------------------------
EMOJI_CACHE_TTL_SECONDS = 600
EMOJI_SAMPLE_COUNT_LARGE = 30
EMOJI_SAMPLE_COUNT_SMALL = 5
EMOJI_RANDOM_THRESHOLD = 0.2

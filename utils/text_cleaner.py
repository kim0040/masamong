# -*- coding: utf-8 -*-
"""텍스트 정제 및 욕설 필터링 유틸리티 모듈."""

import re

class ProfanityFilter:
    """욕설 및 비속어를 필터링하는 클래스."""

    # 필터링할 욕설 패턴 (정규식)
    # 텍스트 내에서 문맥을 유지하기 위해 단어 단위 삭제가 아닌 마스킹(***) 처리를 권장함.
    # 너무 광범위한 필터는 오탐을 유발할 수 있으므로, 명백한 욕설 위주로 구성.
    BAD_PATTERNS = [
        r"(씨|시|씌|쉬)(발|빨|벌|뻘)",
        r"(개|게)(새|세)끼",
        r"(병|븅)(신|쉰)",
        r"지랄",
        r"존나",
        r"졸라",
        r"씹",
        r"창녀",
        r"걸레",
        r"애미",
        r"애비",
        r"느금",
        r"니기미",
        r"니미",
        r"미친(놈|년|새|게)",
        r"닥쳐",
        r"대가리",
        r"빠가",
        r"나가뒤",
        r"나가죽",
        r"좆",
        r"좃",
    ]

    def __init__(self):
        # 모든 패턴을 하나의 정규식으로 컴파일
        pattern_str = "|".join(self.BAD_PATTERNS)
        self.regex = re.compile(pattern_str, re.IGNORECASE)

    def clean(self, text: str, replacement: str = "***") -> str:
        """텍스트에서 욕설을 찾아 replacement로 마스킹합니다."""
        if not text:
            return ""
        return self.regex.sub(replacement, text)

# 싱글톤 인스턴스
_filter = ProfanityFilter()

def clean_profanity(text: str) -> str:
    """전역 필터를 사용하여 욕설을 마스킹합니다."""
    return _filter.clean(text)

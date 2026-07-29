# -*- coding: utf-8 -*-
"""
마사몽 봇의 의도 분석 및 도구 탐지를 담당하는 모듈입니다.

의미 기반 LLM 분석으로 사용자 의도를 파악하고 적절한 도구(tool)를
선택합니다. 키워드 휴리스틱은 라우팅 provider 장애 시의 비상 경로에만
사용합니다.
"""

from __future__ import annotations

import json as _json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import config
from logger_config import logger


@dataclass(frozen=True)
class ToolRoutingDecision:
    """의미 기반 라우터가 반환하는 실행 계획과 메모리 필요 여부."""

    plan: list[dict[str, Any]]
    source: str
    needs_memory: bool
    intent: str = ""
    context_digest: str = ""
    requires_external_evidence: bool = False
    needs_fortune_context: bool = False


class IntentAnalyzer:
    """사용자 의도 분석 및 도구 탐지를 수행합니다.

    정상 경로에서는 routing LLM이 최근 대화와 도구 계약을 의미적으로
    해석합니다. 고정 키워드 목록은 provider가 비활성/실패한 경우에만
    최소 기능을 유지하는 보수적 fallback으로 사용합니다.
    """

    # ── Keyword / pattern sets ──────────────────────────────────────────

    _WEATHER_KEYWORDS = frozenset([
        '날씨', '기온', '온도', '흐림', '우산', '강수', '일기예보', '체감',
        '폭염', '한파', '태풍', '황사', '미세먼지', '자외선',
        '비가', '비온', '비내', '눈이', '눈온', '눈날',
        '덥다', '덥네', '더워', '춥다', '춥네', '추워', '따뜻해', '쌀쌀',
        '맑음', '맑다', '맑네', '흐리다', '구름', '안개',
    ])
    _STOCK_US_KEYWORDS = frozenset([
        '애플', 'apple', 'aapl', '테슬라', 'tesla', 'tsla',
        '구글', 'google', 'googl', '엔비디아', 'nvidia', 'nvda',
        '마이크로소프트', 'microsoft', 'msft', '아마존', 'amazon', 'amzn',
        '맥도날드', '스타벅스', '코카콜라', '펩시', '넷플릭스',
        '메타', '페이스북', '디즈니', '인텔', 'amd', '나이키', '코스트코', '버크셔',
    ])
    _STOCK_KR_KEYWORDS = frozenset([
        '삼성전자', '현대차', 'sk하이닉스', '네이버', '카카오',
        'lg에너지', '셀트리온', '삼성바이오', '기아', '포스코',
    ])
    _STOCK_GENERAL_KEYWORDS = frozenset(['주가', '주식', '시세', '종가', '시가', '상장'])
    _EXCHANGE_KEYWORDS = frozenset([
        '환율', '달러', '엔화', '유로', 'usd', 'jpy', 'eur', 'krw', '환전',
        '코인', '비트코인', '이더리움', 'crypto', 'bitcoin', 'eth',
    ])
    _FINANCE_INTENT_HINTS = frozenset([
        '주가', '주식', '시세', '종가', '시가', '상장', '시총', '시가총액', '배당',
        '증시', '나스닥', '뉴욕증시', '코스피', '코스닥', '투자', '실적', '매출',
        '영업이익', 'per', 'pbr', 'eps', 'etf', 'fund', 'market cap',
        '환율', '환전', '달러', '엔화', '유로', '코인', '비트코인', '이더리움',
    ])
    _STOCK_TICKER_PATTERN = re.compile(
        r"\b(aapl|tsla|googl|nvda|msft|amzn|mcd|sbux|ko|pep|nflx|meta|dis|intc|amd|nke|cost|brk\.?b)\b",
        re.IGNORECASE,
    )
    _FINANCE_KEYWORDS = _STOCK_US_KEYWORDS | _STOCK_KR_KEYWORDS | _STOCK_GENERAL_KEYWORDS | _EXCHANGE_KEYWORDS
    _DEPRECATED_FINANCE_TOOLS = frozenset([
        'get_krw_exchange_rate',
        'get_company_news',
        'get_exchange_rate',
    ])
    _ALLOWED_RUNTIME_TOOLS = frozenset([
        "web_search",
        "get_weather_forecast",
        "get_market_snapshot",
        "get_stock_price",
        "search_for_place",
        "generate_image",
    ])
    _PLACE_KEYWORDS = frozenset(['맛집', '카페', '음식점', '식당', '근처', '주변', '가볼만한', '핫플레이스'])
    _IMAGE_GEN_KEYWORDS = frozenset([
        '이미지 생성', '그림 그려', '사진 만들어', '이미지 만들어',
        '그려줘', '생성해줘', '그림 생성', '이미지 그려', '사진 생성',
        '만들어줘', '그림으로 그려', '이미지로 만들어',
        'generate image', 'create image', 'draw me', 'make an image',
    ])
    _WEB_SEARCH_KEYWORDS = frozenset([
        '웹검색', '검색', '검색해줘', '찾아줘', '조사해줘', '탐색해줘',
        '뉴스', '최신', '최근', '실시간', '속보', '이슈', '현황', '상황',
        '어떻게 됐어', '어떻게 됨', '출처', '링크', '기사',
        '공식 문서', '레퍼런스', '가이드', '튜토리얼', '사용법',
        '리뷰', '사용기', '비교', '업데이트', '버전', '변경사항', '패치노트', '릴리즈', '발표',
    ])
    _WEB_SEARCH_FOLLOWUP_KEYWORDS = frozenset([
        '자세히', '근거', '링크', '출처', '원문', '기사', '팩트체크',
    ])
    _REALTIME_WEB_QUERY_HINTS = frozenset([
        '오늘', '지금', '현재', '실시간', '최신', '최근', '속보',
        '급등', '급락', '떡상', '떡락', '코스피', '코스닥', '주가', '환율',
        '이번', '올해', '라인업', '일정', '축제', '행사',
    ])
    _FACTUAL_WEB_QUERY_HINTS = frozenset([
        '누가', '언제', '어디', '왜', '무엇', '몇', '얼마', '정의', '의미', '차이',
        '비교', '장단점', '순위', '통계', '수치', '근거', '출처', '링크',
        '공식', '문서', '가이드', '튜토리얼', '사용법',
        '호환성', '문제', '오류', '버그', '이슈', '해결', '해결법', '트러블슈팅',
        '발표', '업데이트', '버전', '릴리즈', '변경사항', '패치노트',
        '라인업', '일정', '개최', '행사', '축제',
        'latest', 'update', 'release', 'version', 'docs', 'documentation',
    ])
    _LOCAL_MEMORY_HINTS = frozenset([
        '내가 어제 말', '내가 아까 말', '내가 방금 말', '내가 전에 말',
        '우리 대화', '이전 대화', '방금 얘기', '아까 얘기', '기억나',
        '내 얘기', '우리 얘기', '저번에 말한', '앞에서 말한',
    ])
    _NO_SEARCH_PATTERNS = frozenset([
        '내 얘기', '우리 얘기', '너 얘기', '잡담만', '인사만'
    ])
    _SMALLTALK_PATTERNS = frozenset([
        '안녕', '하이', 'ㅎㅇ', 'hello', 'hey', 'hi',
        '뭐해', '뭐하냐', '뭐하네', '뭐함', '잘지내', '잘 지내',
        '반가워', '반갑다', '심심해', '놀아줘', '근황',
    ])

    def __init__(self, db: Any, llm_client: Any, tools_cog: Any):
        self.db: Any = db
        self.llm_client: Any = llm_client
        self.tools_cog: Any = tools_cog
        self._auto_web_search_last_used: dict[int, float] = {}
        self.location_cache: set[str] = set()
        # 빈 set은 falsy라 self.location_cache로는 '로드했지만 비어있음'과 '미로드'를 구분할 수 없다.
        # 별도 불린 센티넬로 1회만 로드하도록 하여, locations가 비어있을 때 매 메시지마다
        # 원격 TiDB를 재조회하는 것을 방지한다.
        self._location_cache_loaded: bool = False

    async def _load_location_cache(self) -> None:
        """DB에서 지역명 데이터를 로드하여 캐싱합니다."""
        if self._location_cache_loaded:
            return

        if not self.db:
            return

        try:
            async with self.db.execute("SELECT name FROM locations WHERE LENGTH(name) >= 2") as cursor:
                rows = await cursor.fetchall()
                self._location_cache_loaded = True
                if rows:
                    self.location_cache = {row['name'] for row in rows}
                    logger.info(f"DB에서 지역명 데이터 {len(self.location_cache)}개를 로드했습니다.")
        except Exception as e:
            logger.error(f"지역명 캐시 로드 중 오류: {e}")

    # ── Detection helpers ────────────────────────────────────────────────

    def _is_smalltalk_only_query(self, query: str) -> bool:
        """외부 도구 호출이 불필요한 인사/잡담성 질문인지 판별합니다."""
        text = (query or "").strip().lower()
        if not text:
            return False

        # 도구 키워드가 섞여 있으면 smalltalk로 보지 않습니다.
        if (
            any(kw in text for kw in self._WEATHER_KEYWORDS)
            or self._looks_like_finance_query(text)
            or any(kw in text for kw in self._PLACE_KEYWORDS)
            or any(kw in text for kw in self._WEB_SEARCH_KEYWORDS)
            or any(kw in text for kw in self._IMAGE_GEN_KEYWORDS)
        ):
            return False

        if any(token in text for token in self._SMALLTALK_PATTERNS):
            return True

        # 매우 짧은 인사 표현
        return bool(re.fullmatch(r"(안녕+|하이+|ㅎㅇ+|hello+|hey+|hi+)", text))

    def _has_explicit_web_search_intent(self, query: str) -> bool:
        """질문이 명시적으로 외부 웹 탐색을 요구하는지 판별합니다."""
        query_lower = (query or "").lower()
        explicit_terms = (
            '웹검색', '검색해줘', '검색해', '검색 좀', '찾아줘', '찾아봐', '조사해줘', '탐색해줘',
            '뉴스', '소식', '출처', '링크', '기사', '공식 문서', '레퍼런스', '가이드', '튜토리얼', '사용법',
            '리뷰', '사용기', '비교', '업데이트', '버전', '변경사항', '패치노트', '릴리즈', '발표',
        )
        return any(kw in query_lower for kw in explicit_terms)

    def _looks_like_external_fact_query(self, query: str) -> bool:
        """
        웹에서 사실 확인이 필요한 질의인지 휴리스틱으로 판별합니다.
        (명시적 웹검색 키워드가 없어도 외부 정보가 필요한 질문을 놓치지 않기 위한 보정)
        """
        text = (query or "").strip().lower()
        if not text:
            return False
        if self._is_smalltalk_only_query(text):
            return False
        if any(kw in text for kw in self._WEATHER_KEYWORDS):
            return False
        if any(kw in text for kw in self._PLACE_KEYWORDS):
            return False
        if any(kw in text for kw in self._IMAGE_GEN_KEYWORDS):
            return False
        # 로컬/이전 대화 회상성 질문은 외부 웹검색 대상으로 보지 않는다.
        if any(kw in text for kw in self._LOCAL_MEMORY_HINTS):
            return False
        if any(kw in text for kw in self._FACTUAL_WEB_QUERY_HINTS):
            return True
        return False

    def _is_realtime_web_query(self, query: str) -> bool:
        """질의에 실시간 웹 검색이 필요한지 여부를 판단합니다."""
        query_lower = (query or "").lower()
        if not query_lower:
            return False
        return any(token in query_lower for token in self._REALTIME_WEB_QUERY_HINTS)

    def _looks_like_finance_query(self, query: str) -> bool:
        """회사명 단독 언급 오탐을 줄이기 위해 금융 의도 문맥까지 함께 확인합니다."""
        query_lower = (query or "").lower().strip()
        if not query_lower:
            return False

        # 환율/코인/주가 등 강한 금융 키워드는 즉시 금융으로 분류
        unambiguous_stock_terms = self._STOCK_GENERAL_KEYWORDS - {"시가"}
        if any(kw in query_lower for kw in unambiguous_stock_terms):
            return True
        # "시가"의 단순 부분문자열 검사는 "시각 자료"까지 금융으로 오인한다.
        if (
            "시가총액" in query_lower
            or re.search(r"(?<![가-힣])시가(?![가-힣])", query_lower)
        ):
            return True
        if any(kw in query_lower for kw in self._EXCHANGE_KEYWORDS):
            return True
        if self._STOCK_TICKER_PATTERN.search(query_lower):
            return True

        # 회사명만 있는 경우에는 금융 의도 힌트가 함께 있을 때만 금융으로 본다.
        has_stock_entity = any(kw in query_lower for kw in self._STOCK_US_KEYWORDS) or any(
            kw in query_lower for kw in self._STOCK_KR_KEYWORDS
        )
        if not has_stock_entity:
            return False
        return any(hint in query_lower for hint in self._FINANCE_INTENT_HINTS)

    def _looks_like_market_brief_query(self, query: str) -> bool:
        """개별 종목이 아닌 시장 지수·시황 브리핑 요청인지 판별합니다."""
        text = (query or "").lower().strip()
        if not text or not self._looks_like_finance_query(text):
            return False

        index_or_market_terms = (
            "국장", "미장", "한국 시장", "미국 시장", "주식 시장", "증권 시장",
            "시장 흐름", "시장 동향", "시장 브리핑", "시황", "증시",
            "코스피", "코스닥", "kospi", "kosdaq",
            "나스닥", "nasdaq", "다우", "dow", "s&p", "sp500", "s&p 500",
        )
        if any(term in text for term in index_or_market_terms):
            return True

        news_terms = ("뉴스", "소식", "이슈", "동향", "브리핑", "주요")
        has_news_request = any(term in text for term in news_terms)
        has_named_stock = (
            any(term in text for term in self._STOCK_US_KEYWORDS)
            or any(term in text for term in self._STOCK_KR_KEYWORDS)
            or bool(self._STOCK_TICKER_PATTERN.search(text))
        )
        return has_news_request and not has_named_stock

    @staticmethod
    def _market_region_from_text(query: str) -> str:
        """시장 브리핑 대상 지역을 보수적으로 정규화합니다."""
        text = (query or "").lower()
        if any(
            term in text
            for term in ("글로벌", "세계 증시", "전 세계", "국내외", "global")
        ):
            return "global"
        if any(
            term in text
            for term in (
                "미국", "미장", "뉴욕증시", "나스닥", "nasdaq",
                "다우", "dow", "s&p", "sp500",
            )
        ):
            return "us"
        if any(
            term in text
            for term in (
                "한국", "국장", "국내", "코스피", "코스닥", "kospi", "kosdaq",
            )
        ):
            return "kr"
        # 한국어 봇에서 시장을 따로 지정하지 않은 "오늘 주식 소식"은 국장을
        # 기본으로 보고, 해외 전체를 임의로 섞지 않는다.
        if re.search(r"[가-힣]", text):
            return "kr"
        return "global"

    def _derive_external_evidence_requirement(
        self,
        query: str,
        *,
        intent: str = "",
        declared: Any = None,
    ) -> bool:
        """라우터 응답 누락·오판 시에도 검증이 필요한 사실 질문을 보호합니다."""
        if isinstance(declared, bool) and declared:
            return True

        semantic_text = f"{query}\n{intent}".strip()
        if self._looks_like_finance_query(semantic_text):
            return True
        if self._has_explicit_web_search_intent(query):
            return True
        if self._looks_like_external_fact_query(query):
            return True

        # ``intent``는 의미 라우터가 이미 자연어 문맥을 해석한 결과다. 사용자가
        # 제시한 외부 사건·변화의 사실 여부를 "확인"하는 의도인데 도구가 비면
        # 모델의 내장 지식으로 단정하지 않고 공개 자료를 확인한다.
        intent_lower = (intent or "").lower()
        verification_terms = (
            "사실 확인", "여부 확인", "있는지 확인", "맞는지 확인",
            "변화 확인", "관련 소식", "최신 정보", "공개 자료 확인",
        )
        return any(term in intent_lower for term in verification_terms)

    def _enforce_evidence_tool_plan(
        self,
        query: str,
        intent: str,
        plan: list[dict[str, Any]],
        *,
        requires_external_evidence: bool,
        log_extra: dict | None = None,
    ) -> list[dict[str, Any]]:
        """검증 필수 요청에서 라우터의 도구 누락을 결정적으로 보정합니다."""
        normalized = list(plan or [])
        semantic_text = f"{query}\n{intent}".strip()
        finance_query = self._looks_like_finance_query(semantic_text)
        market_brief = finance_query and self._looks_like_market_brief_query(
            semantic_text
        )
        names = {
            str(item.get("tool_to_use") or item.get("tool_name") or "")
            for item in normalized
            if isinstance(item, dict)
        }
        inferred_market_region = self._market_region_from_text(semantic_text)
        market_search_label = {
            "kr": "한국 증시(코스피·코스닥)",
            "us": "미국 증시(다우·S&P 500·나스닥)",
            "global": "글로벌 주요 증시",
        }[inferred_market_region]
        finance_search_base = (
            f"{query}\n해석된 요청: {intent}\n대상 시장: {market_search_label}"
            if market_brief
            else semantic_text
        )

        if market_brief and "get_market_snapshot" not in names:
            normalized.insert(
                0,
                {
                    "tool_to_use": "get_market_snapshot",
                    "tool_name": "get_market_snapshot",
                    "parameters": {
                        "region": inferred_market_region,
                    },
                },
            )
            names.add("get_market_snapshot")
            logger.warning(
                "[도구보정] 시장 브리핑에 검증 지수 도구를 강제 추가합니다.",
                extra=log_extra,
            )
        elif market_brief:
            # region은 비용이나 호출 수를 바꾸지 않는 실행 파라미터다. 한국어
            # 기본 질문을 라우터가 임의로 global로 넓히거나 후속 "미국은?"을
            # 이전 지역으로 남기는 일을 막는다.
            for item in normalized:
                if not isinstance(item, dict):
                    continue
                name = str(
                    item.get("tool_to_use")
                    or item.get("tool_name")
                    or ""
                )
                if name != "get_market_snapshot":
                    continue
                item["parameters"] = {"region": inferred_market_region}
                break

        # 시장 뉴스는 지수 스냅샷만으로 설명할 수 없으므로 공개 출처도 함께
        # 확인한다. 그 밖의 외부 사실 질문은 도구가 완전히 비었을 때만 검색한다.
        needs_web = market_brief or (
            requires_external_evidence
            and not names.intersection(
                {
                    "web_search",
                    "get_weather_forecast",
                    "get_market_snapshot",
                    "get_stock_price",
                    "search_for_place",
                }
            )
        )
        if needs_web and "web_search" not in names:
            search_query = (
                self._build_finance_news_query(finance_search_base)
                if finance_query
                else (query or intent).strip()
            )
            normalized.append(
                {
                    "tool_to_use": "web_search",
                    "tool_name": "web_search",
                    "parameters": {"query": search_query},
                }
            )
            logger.warning(
                "[도구보정] 검증 필수 요청에 web_search를 강제 추가합니다.",
                extra=log_extra,
            )
        elif market_brief:
            # 라우터가 검색어를 직접 만든 경우에도 현재 KST 날짜, 공식 자료
            # 우선, 커뮤니티 배제 조건을 빠뜨릴 수 없게 공통 금융 계약을 씌운다.
            for item in normalized:
                if not isinstance(item, dict):
                    continue
                name = str(
                    item.get("tool_to_use")
                    or item.get("tool_name")
                    or ""
                )
                if name != "web_search":
                    continue
                params = item.get("parameters")
                if not isinstance(params, dict):
                    params = {}
                item["parameters"] = {
                    "query": self._build_finance_news_query(
                        finance_search_base
                    )
                }
                break

        return normalized[:2]

    @staticmethod
    def _normalize_realtime_web_query(query: str) -> str:
        """실시간 질의에서 과거 연/월 오염 토큰을 제거하고 현재 날짜 앵커를 부여합니다."""
        raw = str(query or "").strip()
        if not raw:
            return raw

        cleaned = raw
        patterns = (
            r"(?:19|20)\d{2}\s*년\s*\d{1,2}\s*월\s*\d{0,2}\s*일?",
            r"(?:19|20)\d{2}\s*년\s*\d{1,2}\s*월",
            r"(?:19|20)\d{2}\s*년",
            r"(?:19|20)\d{2}[./-]\d{1,2}[./-]\d{1,2}",
            r"(?:19|20)\d{2}[./-]\d{1,2}",
        )
        for pat in patterns:
            cleaned = re.sub(pat, " ", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        if not cleaned:
            cleaned = raw

        now_kst = datetime.now(timezone(timedelta(hours=9)))
        anchor = f"{now_kst.year}년 {now_kst.month}월 {now_kst.day}일"
        lower = cleaned.lower()
        if not any(token in lower for token in ("오늘", "현재", "실시간", "최신", "최근")):
            cleaned = f"{cleaned} {anchor}".strip()
        return cleaned

    def _has_tool_keyword_signal(self, query: str) -> bool:
        """질문에 도구 호출이 필요한 명시적 신호가 있는지 판별합니다."""
        query_lower = (query or "").lower()
        if not query_lower:
            return False
        return (
            any(kw in query_lower for kw in self._WEATHER_KEYWORDS)
            or self._looks_like_finance_query(query)
            or any(kw in query_lower for kw in self._PLACE_KEYWORDS)
            or any(kw in query_lower for kw in self._IMAGE_GEN_KEYWORDS)
        )

    @staticmethod
    def _auto_web_search_scope_key(message: Any) -> int:
        """자동 웹검색 쿨다운을 적용할 스코프 키를 계산합니다."""
        if message.guild:
            return int(message.channel.id)
        # DM은 사용자 단위로 쿨다운 적용
        return -int(message.author.id)

    def _can_run_auto_web_search(self, message: Any, query: str, log_extra: dict | None = None) -> bool:
        """
        자동 웹검색(도구 계획이 없을 때의 fallback) 실행 가능 여부를 판단합니다.
        명시적 웹검색 요청은 쿨다운을 적용하지 않습니다.
        """
        if self._has_explicit_web_search_intent(query):
            return True

        cooldown_seconds = max(0, int(getattr(config, "AUTO_WEB_SEARCH_COOLDOWN_SECONDS", 90)))
        if cooldown_seconds <= 0:
            return True

        key = self._auto_web_search_scope_key(message)
        now_mono = time.monotonic()
        last_mono = self._auto_web_search_last_used.get(key)
        if last_mono is None:
            return True

        elapsed = now_mono - last_mono
        if elapsed >= cooldown_seconds:
            return True

        remaining = cooldown_seconds - elapsed
        logger.info(
            "[도구보정] 자동 web_search 쿨다운으로 생략합니다. 남은 시간=%.1fs",
            remaining,
            extra=log_extra,
        )
        return False

    def _mark_auto_web_search_used(self, message: Any) -> None:
        """자동 웹 검색 사용 시점을 기록하여 쿨다운을 관리합니다."""
        key = self._auto_web_search_scope_key(message)
        self._auto_web_search_last_used[key] = time.monotonic()
        if len(self._auto_web_search_last_used) > 2048:
            # 오래된 엔트리 절반 정리
            sorted_items = sorted(self._auto_web_search_last_used.items(), key=lambda item: item[1])
            for old_key, _ in sorted_items[:1024]:
                self._auto_web_search_last_used.pop(old_key, None)

    # ── Tool detection (keyword / LLM) ──────────────────────────────────

    def _detect_tools_by_keyword(self, query: str) -> list[dict]:
        """키워드 기반 도구 감지 (LLM 실패 시 fallback)."""
        tools = []
        query_lower = query.lower()

        # 날씨 감지
        if any(kw in query_lower for kw in self._WEATHER_KEYWORDS):
            location = self._extract_location_from_query(query) or '광양'

            day_offset = 0
            if "내일" in query:
                day_offset = 1
            elif "모레" in query:
                day_offset = 2
            elif "글피" in query:
                day_offset = 3
            elif any(kw in query for kw in ["다음주", "이번주", "주말", "일주일"]):
                day_offset = 3  # Start of mid-term forecast

            tools.append({
                'tool_to_use': 'get_weather_forecast',
                'tool_name': 'get_weather_forecast',
                'parameters': {'location': location, 'day_offset': day_offset}
            })
            return tools  # 날씨 요청은 단일 도구로 처리

        # 금융 관련 질문은 직접 시세 도구 대신 웹 검색으로 대체
        if self._looks_like_finance_query(query):
            logger.info(
                "금융 관련 질문 감지. query_chars=%d; web_search로 대체",
                len(query),
            )
            tools.append({
                'tool_to_use': 'web_search',
                'tool_name': 'web_search',
                'parameters': {'query': self._build_finance_news_query(query)}
            })
            return tools

        # 장소 관련 질문도 web_search로 통합 처리
        if any(kw in query_lower for kw in self._PLACE_KEYWORDS):
            tools.append({
                'tool_to_use': 'web_search',
                'tool_name': 'web_search',
                'parameters': {'query': query.strip()}
            })
            return tools

        # 이미지 생성 요청 감지
        if any(kw in query_lower for kw in self._IMAGE_GEN_KEYWORDS):
            tools.append({
                'tool_to_use': 'generate_image',
                'tool_name': 'generate_image',
                'parameters': {'prompt': query.strip()}
            })
            return tools

        # 도구 필요 없음 - 일반 대화 또는 RAG로 처리
        return tools

    def _emergency_routing_decision(
        self,
        query: str,
        *,
        source: str,
    ) -> ToolRoutingDecision:
        """라우팅 모델 장애 때만 사용하는 보수적 호환 경로.

        자연어 도구 선택의 주 경로로 사용하지 않는다. 외부 라우터가 완전히
        unavailable일 때 기존 명시 표현을 살려 기능 전체가 멈추는 것을 피한다.
        """
        plan = self._detect_tools_by_keyword(query)
        requires_external_evidence = self._derive_external_evidence_requirement(
            query,
            intent="fallback",
        )
        plan = self._enforce_evidence_tool_plan(
            query,
            "fallback",
            plan,
            requires_external_evidence=requires_external_evidence,
        )
        return ToolRoutingDecision(
            plan=plan,
            source=source,
            # 도구를 특정하지 못한 경우에는 기존 대화 품질을 보존하도록 RAG를
            # 허용한다. 명시 도구 fallback은 과거 기억을 불필요하게 조회하지 않는다.
            needs_memory=not bool(plan),
            intent="fallback",
            requires_external_evidence=requires_external_evidence,
        )

    @staticmethod
    def _parse_routing_json(raw: Any) -> dict[str, Any]:
        """JSON 본문 또는 모델의 짧은 부가 텍스트 뒤 JSON 객체를 파싱한다.

        일부 OpenAI 호환 모델은 JSON-only 지시에도 reasoning 태그나 한 줄
        설명을 앞에 붙인다. 첫 ``{``부터 무작정 마지막 ``}``까지 자르면 내부
        예시 중괄호에 취약하므로 JSONDecoder로 각 객체 시작점을 검증하고,
        라우팅 계약 필드가 있는 첫 객체만 채택한다.
        """
        cleaned = str(raw or "").strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(
                r"^```(?:json)?\s*",
                "",
                cleaned,
                flags=re.IGNORECASE,
            )
            cleaned = re.sub(r"\s*```$", "", cleaned)

        decoder = _json.JSONDecoder()
        candidates = [0]
        candidates.extend(
            index
            for index, char in enumerate(cleaned)
            if char == "{" and index != 0
        )
        for start in candidates:
            try:
                parsed, _end = decoder.raw_decode(cleaned[start:])
            except _json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            if "tools" in parsed and (
                "needs_memory" in parsed or "intent" in parsed
            ):
                return parsed
        raise ValueError("라우팅 응답에서 계약에 맞는 JSON 객체를 찾지 못했습니다.")

    async def route_tools(
        self,
        query: str,
        log_extra: dict,
        history: list | None = None,
    ) -> ToolRoutingDecision:
        """현재 발화의 의미와 대화 흐름으로 도구 및 장기기억 필요성을 결정한다.

        키워드 표는 정상 경로의 선택 근거로 쓰지 않는다. routing lane이 도구
        계약을 읽고 의미적으로 판단하며, 키워드 감지는 provider 장애 시의
        제한된 비상 fallback에만 남겨 둔다.
        """
        if not getattr(config, "INTENT_LLM_ENABLED", True):
            return self._emergency_routing_decision(
                query,
                source="disabled_fallback",
            )
        if not self.llm_client.use_cometapi:
            return self._emergency_routing_decision(
                query,
                source="provider_fallback",
            )

        materialized_history = [
            item for item in (history or []) if isinstance(item, dict)
        ]

        def _format_history_item(
            item: dict[str, Any],
            *,
            content_limit: int,
        ) -> str:
            role = str(item.get("role") or "")
            parts = item.get("parts") or []
            content = parts[0] if isinstance(parts, list) and parts else ""
            if not content:
                return ""
            if role == "user":
                speaker = str(item.get("speaker") or "사용자")[:80]
                current_mark = " (현재 질문자)" if item.get("is_current_user") else ""
                label = f"사용자 {speaker}{current_mark}"
            else:
                label = "마사몽"
            normalized_content = re.sub(r"\s+", " ", str(content)).strip()
            return f"{label}: {normalized_content[:content_limit]}"

        history_lines: list[str] = []
        for item in materialized_history[-config.INTENT_HISTORY_LIMIT:]:
            rendered = _format_history_item(item, content_limit=1_000)
            if rendered:
                history_lines.append(rendered)
        history_text = "\n".join(history_lines) or "(최근 대화 없음)"

        context_recent_turns = max(
            1,
            int(getattr(config, "AI_CONTEXT_RECENT_TURNS", 8)),
        )
        total_history_chars = sum(
            len(str((item.get("parts") or [""])[0] or ""))
            for item in materialized_history
            if isinstance(item.get("parts"), list) and item.get("parts")
        )
        compaction_requested = (
            len(materialized_history) > context_recent_turns
            and total_history_chars
            >= int(
                getattr(
                    config,
                    "AI_CONTEXT_COMPACTION_TRIGGER_CHARS",
                    3_500,
                )
            )
        )
        compaction_lines: list[str] = []
        if compaction_requested:
            source_budget = int(
                getattr(
                    config,
                    "AI_CONTEXT_COMPACTION_SOURCE_MAX_CHARS",
                    5_000,
                )
            )
            used_chars = 0
            # 최신 원문 바로 앞 구간부터 예산을 채운 뒤 시간순으로 되돌린다.
            for item in reversed(materialized_history[:-context_recent_turns]):
                rendered = _format_history_item(item, content_limit=800)
                if not rendered:
                    continue
                next_chars = len(rendered) + (1 if compaction_lines else 0)
                if used_chars + next_chars > source_budget:
                    continue
                compaction_lines.append(rendered)
                used_chars += next_chars
            compaction_lines.reverse()

        compaction_text = "\n".join(compaction_lines)
        digest_limit = int(
            getattr(config, "AI_CONTEXT_DIGEST_MAX_CHARS", 600)
        )

        now_kst = datetime.now(timezone(timedelta(hours=9))).isoformat(
            timespec="minutes"
        )
        digest_instruction = (
            f"context_digest에는 아래 오래된 대화에서 현재 흐름에 필요한 사실, 결정, "
            f"변경, 부정, 미정 사항을 독립적인 문장으로 최대 {digest_limit}자까지 "
            "압축한다. 잡담과 말투는 버리고 원문에 없는 사실은 만들지 않는다."
            if compaction_text
            else "context_digest는 빈 문자열로 둔다."
        )
        compaction_section = (
            f"\n압축할 오래된 대화:\n{compaction_text}\n"
            if compaction_text
            else ""
        )
        prompt = (
            "당신은 Discord 봇의 의미 기반 라우터다. 단어 포함 여부가 아니라 현재 "
            "요청의 목적과 최근 대화 흐름으로 판단한다. 답변은 쓰지 말고 JSON 객체 "
            "하나만 반환한다. 근거 없는 웹 검색이나 파라미터를 만들지 않는다.\n"
            "도구:\n"
            "- web_search(query): 공개 웹의 최신 사실·공식 자료·가격·일정·뉴스·후기·비교\n"
            "- get_weather_forecast(location, day_offset): 지역 날씨, 오늘 0~10일 뒤\n"
            "- get_market_snapshot(region): 주요 시장 지수의 검증된 최신 수치. "
            "region은 kr, us, global 중 하나\n"
            "- get_stock_price(symbol, user_query): 현재 주가. symbol은 알면 Yahoo 호환 티커\n"
            "- search_for_place(query, page_size): 음식점·카페·장소의 위치 검색\n"
            "- generate_image(prompt): 사용자가 새 이미지 생성을 요청한 경우만\n"
            "시장 시황·주요 주식 뉴스는 get_market_snapshot과 web_search를 함께 "
            "사용한다. 공개 자료로 검증해야 하는 최신 정보, 수치, 뉴스, 일정, "
            "가격, 사용자가 제시한 외부 사실의 진위는 requires_external_evidence=true로 "
            "둔다. 지역 제도·교통·시설의 유래나 변경처럼 내장 지식만으로 확신하기 "
            "어려운 틈새 사실도 true다. 이때 적절한 조회 도구가 적어도 하나 있어야 "
            "하며, 모델의 기억만으로 답하지 않는다. 의견·창작·잡담·현재 대화나 "
            "장기기억 회상, 널리 확립된 안정적인 일반 상식은 false다. "
            "도구가 불필요하면 tools=[]이며 최대 2개다. needs_memory는 장기기억 "
            "저장소를 별도로 검색할지 정하는 스위치이며, 제공된 최근 대화를 활용한다는 "
            "표시가 아니다. 최근 대화 안에 답변에 필요한 대상과 사실이 이미 있으면 "
            "반드시 false다. 그보다 오래된 Discord/Kakao 기억이 답변의 정확도나 "
            "개인화에 도움이 되면 true다. 이전 합의·결정·취향·관계·사건뿐 아니라 공개 지식으로 "
            "알 수 없는 현재 사용자나 서버 구성원·지인 등 특정 인물이 누구인지, "
            "어떤 사람인지, 무엇을 좋아하는지 묻는 요청도 별도의 '전에/기억' 표현이 "
            "없어도 true다. 최근 대화만으로 지시 대상이나 생략된 주어를 확정할 수 "
            "없을 때도 true다. 순수 인사와 과거 맥락이 전혀 필요 없는 독립적인 "
            "일반 지식·의견 질문만 false다. '그 계획을 이어서'처럼 생략이 있어도 "
            "최근 대화에 대상과 필요한 내용이 이미 드러나 있으면 false이며, 단지 "
            "대화가 이어진다는 이유로 true를 쓰지 않는다. 그 내용을 최근 대화만으로 "
            "확정할 수 없을 때에만 기억 누락보다 관련도 필터링을 우선해 true로 둔다. "
            "needs_fortune_context는 DM 일반 대화에서 동의받아 "
            "저장한 직전 운세 내용을 실제로 참고해야 할 때만 true다. 사용자가 운세 "
            "내용을 이어 묻거나 그 운세를 바탕으로 조언을 요청한 경우가 아니면 false다. "
            "저장 운세만 있으면 답할 수 있는 요청에서는 needs_fortune_context=true, "
            "needs_memory=false다. 운세 외의 오래된 Discord/Kakao 사실도 함께 필요할 "
            "때만 두 값을 모두 true로 둔다.\n"
            f"{digest_instruction}\n"
            "출력 형식: "
            '{"intent":"짧은 의도","needs_memory":false,'
            '"needs_fortune_context":false,'
            '"requires_external_evidence":false,'
            '"context_digest":"","tools":[{"tool":"도구명","params":{}}]}\n'
            f"현재 시각(KST): {now_kst}\n"
            f"{compaction_section}"
            f"최근 대화:\n{history_text}\n"
            f"현재 요청:\n{query}\n"
        )
        try:
            router_max_tokens = int(
                getattr(config, "SEMANTIC_ROUTER_MAX_TOKENS", 384)
            )
            if compaction_requested:
                router_max_tokens = max(
                    router_max_tokens,
                    int(
                        getattr(
                            config,
                            "SEMANTIC_ROUTER_COMPACTION_MAX_TOKENS",
                            768,
                        )
                    ),
                )
            raw = await self.llm_client.fast_generate_text(
                prompt,
                None,
                log_extra,
                trace_key="cometapi_fast_intent",
                max_tokens=router_max_tokens,
            )
            if not raw:
                raise ValueError("라우팅 모델 응답이 비어 있습니다.")
            parsed = self._parse_routing_json(raw)

            raw_tools = parsed.get("tools")
            if raw_tools is None:
                raw_tools = []
            if not isinstance(raw_tools, list):
                raise ValueError("tools가 배열이 아닙니다.")

            plan: list[dict[str, Any]] = []
            for item in raw_tools[:2]:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("tool") or "").strip()
                if not name or name == "none":
                    continue
                params = item.get("params")
                if not isinstance(params, dict):
                    params = {}

                if name in self._DEPRECATED_FINANCE_TOOLS:
                    name = "web_search"
                    params = {"query": str(params.get("query") or query)}
                if name not in self._ALLOWED_RUNTIME_TOOLS:
                    logger.info(
                        "[의미라우터] 비허용 도구 계획 제거: %s",
                        name,
                        extra=log_extra,
                    )
                    continue
                plan.append(
                    {
                        "tool_to_use": name,
                        "tool_name": name,
                        "parameters": params,
                    }
                )

            needs_memory_raw = parsed.get("needs_memory")
            needs_memory = (
                needs_memory_raw
                if isinstance(needs_memory_raw, bool)
                else not bool(plan)
            )
            needs_fortune_context = (
                parsed.get("needs_fortune_context")
                if isinstance(parsed.get("needs_fortune_context"), bool)
                else False
            )
            # 이미지 요청은 "아까 말한 철수", "네가 기억하는 나"처럼 짧은
            # 지시가 많다. 검색 결과는 범위·관련도 필터를 다시 통과하므로 모든
            # 이미지 요청에서 현재 DM/서버 기억 조회를 허용해도 무관한 기억을
            # 프롬프트에 억지로 넣지 않는다.
            if any(
                item["tool_to_use"] == "generate_image"
                for item in plan
            ):
                needs_memory = True
            intent = str(parsed.get("intent") or "")[:120]
            requires_external_evidence = self._derive_external_evidence_requirement(
                query,
                intent=intent,
                declared=parsed.get("requires_external_evidence"),
            )
            if any(
                item.get("tool_to_use")
                in {
                    "web_search",
                    "get_weather_forecast",
                    "get_market_snapshot",
                    "get_stock_price",
                    "search_for_place",
                }
                for item in plan
            ):
                requires_external_evidence = True
            plan = self._enforce_evidence_tool_plan(
                query,
                intent,
                plan,
                requires_external_evidence=requires_external_evidence,
                log_extra=log_extra,
            )
            # 최신 외부 사실은 최근 채팅 문맥만으로 대상을 이어가고, 장기기억의
            # 오래된 수치·요약을 사실 자료와 섞지 않는다.
            if (
                requires_external_evidence
                and not any(
                    hint in (query or "").lower()
                    for hint in self._LOCAL_MEMORY_HINTS
                )
            ):
                needs_memory = False
            context_digest = ""
            if compaction_requested:
                context_digest = re.sub(
                    r"\s+",
                    " ",
                    str(parsed.get("context_digest") or ""),
                ).strip()[:digest_limit]
            logger.info(
                "[의미라우터] intent=%s tools=%s needs_memory=%s "
                "fortune_context=%s external_evidence=%s digest_chars=%d",
                intent or "-",
                [item["tool_to_use"] for item in plan],
                needs_memory,
                needs_fortune_context,
                requires_external_evidence,
                len(context_digest),
                extra=log_extra,
            )
            return ToolRoutingDecision(
                plan=plan,
                source="llm",
                needs_memory=needs_memory,
                intent=intent,
                context_digest=context_digest,
                requires_external_evidence=requires_external_evidence,
                needs_fortune_context=needs_fortune_context,
            )
        except Exception as exc:
            logger.warning(
                "[의미라우터] 실패해 제한된 비상 fallback 사용: %s",
                exc,
                extra=log_extra,
            )
            return self._emergency_routing_decision(
                query,
                source="error_fallback",
            )

    async def _detect_tools_by_llm(
        self,
        query: str,
        log_extra: dict,
        history: list = None,
    ) -> list[dict]:
        """기존 호출자를 위한 의미 기반 라우터 호환 wrapper."""
        decision = await self.route_tools(query, log_extra, history)
        return decision.plan

    # ── Sanitize / policy ───────────────────────────────────────────────

    def _sanitize_tool_plan(
        self,
        query: str,
        tool_plan: list[dict],
        *,
        rag_top_score: float,
        log_extra: dict | None = None,
        trust_llm: bool = False,
    ) -> list[dict]:
        """LLM 도구 계획을 운영 정책 기준으로 보정합니다.

        trust_llm=True 이면 LLM의 판단을 신뢰하여 web_search를 과도하게 차단하지 않습니다.
        (휴리스틱이 판단을 유보했을 때만 True)"""
        if not tool_plan:
            finance_query = self._looks_like_finance_query(query)
            if not finance_query:
                return []
            tool_plan = self._enforce_evidence_tool_plan(
                query,
                "금융 정보 확인",
                [],
                requires_external_evidence=True,
                log_extra=log_extra,
            )

        query_lower = (query or "").lower()
        explicit_web = self._has_explicit_web_search_intent(query)
        finance_query = self._looks_like_finance_query(query)
        factual_query = self._looks_like_external_fact_query(query)
        weather_query = any(kw in query_lower for kw in self._WEATHER_KEYWORDS)
        place_query = any(kw in query_lower for kw in self._PLACE_KEYWORDS)
        rag_is_strong = rag_top_score >= config.RAG_STRONG_SIMILARITY_THRESHOLD

        normalized: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str]] = set()
        tool_counts: dict[str, int] = {}
        max_tool_calls = min(
            3,
            max(1, int(getattr(config, "AGENT_MAX_TOOL_CALLS", 3))),
        )
        # 웹 검색은 서로 다른 보완 쿼리 두 개까지 허용하되, 고비용 이미지와
        # 단일 위치 기준 날씨는 메시지당 한 번만 실행한다.
        per_tool_limits = {
            "web_search": min(2, max_tool_calls),
            "get_weather_forecast": 1,
            "get_market_snapshot": 1,
            "get_stock_price": 1,
            "search_for_place": 1,
            "generate_image": 1,
        }

        def append_candidate(candidate: dict[str, Any]) -> bool:
            """중복·전체·도구별 비용 상한을 통과한 계획만 추가합니다."""
            name = str(candidate.get("tool_to_use") or "")
            if len(normalized) >= max_tool_calls:
                return False

            per_tool_limit = per_tool_limits.get(name, 1)
            if tool_counts.get(name, 0) >= per_tool_limit:
                logger.info(
                    "[도구보정] 메시지당 %s 호출 상한(%d)으로 제거",
                    name,
                    per_tool_limit,
                    extra=log_extra,
                )
                return False

            params = candidate.get("parameters")
            key = (
                name,
                _json.dumps(
                    params,
                    sort_keys=True,
                    ensure_ascii=False,
                    default=str,
                ),
            )
            if key in seen_keys:
                return False

            seen_keys.add(key)
            tool_counts[name] = tool_counts.get(name, 0) + 1
            normalized.append(candidate)
            return True

        for raw in tool_plan:
            if len(normalized) >= max_tool_calls:
                logger.info(
                    "[도구보정] 메시지당 도구 호출 상한(%d)에 도달해 나머지 계획을 제거",
                    max_tool_calls,
                    extra=log_extra,
                )
                break

            if not isinstance(raw, dict):
                logger.info("[도구보정] 객체가 아닌 도구 계획 항목 제거", extra=log_extra)
                continue

            name = raw.get("tool_to_use") or raw.get("tool_name")
            params = raw.get("parameters")
            if not isinstance(params, dict):
                params = {}

            # 이름 정규화
            if not name:
                continue

            # 비활성화된 금융 도구는 web_search로 강제 변환
            if name in self._DEPRECATED_FINANCE_TOOLS:
                finance_query_text = (
                    params.get("query")
                    or params.get("user_query")
                    or params.get("symbol")
                    or params.get("stock_name")
                    or params.get("currency_code")
                    or query
                )
                name = "web_search"
                params = {"query": self._build_finance_news_query(finance_query_text)}

            # 실행 가능한 도구만 허용
            if name not in self._ALLOWED_RUNTIME_TOOLS:
                logger.info("[도구보정] 허용되지 않은 도구 제거: %s", name, extra=log_extra)
                continue

            # 의미 라우터의 정상 응답은 키워드 표로 다시 뒤집지 않는다. 여기서는
            # 실행 안전성에 필요한 타입·길이·범위만 검증한다.
            if trust_llm:
                if name == "web_search":
                    search_query = str(params.get("query") or query).strip()
                    if not search_query:
                        logger.info(
                            "[도구보정] 빈 web_search query 제거",
                            extra=log_extra,
                        )
                        continue
                    params = {"query": search_query[:800]}
                elif name == "get_weather_forecast":
                    location = str(params.get("location") or "").strip()
                    if not location:
                        location = (
                            self._extract_location_from_query(query)
                            or config.DEFAULT_LOCATION_NAME
                        )
                    try:
                        day_offset = int(params.get("day_offset", 0))
                    except (TypeError, ValueError, OverflowError):
                        day_offset = 0
                    params = {
                        "location": location[:80],
                        "day_offset": min(10, max(0, day_offset)),
                    }
                elif name == "get_stock_price":
                    symbol = str(params.get("symbol") or "").strip().upper()
                    if symbol and re.fullmatch(
                        r"[A-Z0-9^][A-Z0-9.^=-]{0,19}",
                        symbol,
                    ):
                        params = {"symbol": symbol}
                    else:
                        params = {"user_query": query[:300]}
                elif name == "get_market_snapshot":
                    region = str(params.get("region") or "global").strip().lower()
                    if region not in {"kr", "us", "global"}:
                        region = "global"
                    params = {"region": region}
                elif name == "search_for_place":
                    place_query_text = str(
                        params.get("query") or query
                    ).strip()
                    if not place_query_text:
                        continue
                    try:
                        page_size = int(params.get("page_size", 5))
                    except (TypeError, ValueError, OverflowError):
                        page_size = 5
                    params = {
                        "query": place_query_text[:200],
                        "page_size": min(10, max(1, page_size)),
                    }
                elif name == "generate_image":
                    prompt_text = str(
                        params.get("prompt")
                        or params.get("user_query")
                        or query
                    ).strip()
                    if not prompt_text:
                        logger.info(
                            "[도구보정] 빈 generate_image prompt 제거",
                            extra=log_extra,
                        )
                        continue
                    params = {"prompt": prompt_text[:4000]}

                append_candidate(
                    {
                        "tool_to_use": name,
                        "tool_name": name,
                        "parameters": params,
                    }
                )
                continue

            # 잡담 질문은 도구 자체를 차단
            if self._is_smalltalk_only_query(query):
                logger.info("[도구보정] 잡담성 질의로 도구 계획을 모두 무효화합니다.", extra=log_extra)
                return []

            if name == "get_stock_price":
                symbol = str(params.get("symbol") or "").strip().upper()
                params = (
                    {"symbol": symbol}
                    if symbol
                    and re.fullmatch(r"[A-Z0-9^][A-Z0-9.^=-]{0,19}", symbol)
                    else {"user_query": query[:300]}
                )
            elif name == "get_market_snapshot":
                region = str(params.get("region") or "global").strip().lower()
                if region not in {"kr", "us", "global"}:
                    region = "global"
                params = {"region": region}
            elif name == "search_for_place":
                place_query_text = str(
                    params.get("query") or query
                ).strip()
                if not place_query_text:
                    continue
                try:
                    page_size = int(params.get("page_size", 5))
                except (TypeError, ValueError, OverflowError):
                    page_size = 5
                params = {
                    "query": place_query_text[:200],
                    "page_size": min(10, max(1, page_size)),
                }

            if name == "generate_image" and not any(kw in query_lower for kw in self._IMAGE_GEN_KEYWORDS):
                logger.info("[도구보정] 이미지 생성 의도가 없어 generate_image 제거", extra=log_extra)
                continue

            if name == "web_search":
                # LLM 신뢰 모드: 휴리스틱이 판단을 유보했고 LLM이 명시적으로 제안한 경우 차단하지 않음
                if trust_llm:
                    logger.info("[도구보정] LLM 신뢰 모드로 web_search 허용", extra=log_extra)
                elif (
                    not explicit_web
                    and not finance_query
                    and not factual_query
                    and not self._has_tool_keyword_signal(query)
                ):
                    logger.info("[도구보정] 일반 대화 문맥으로 판단해 web_search 제거", extra=log_extra)
                    continue

                # 실시간형 질문은 과거 날짜 오염 토큰을 제거하고 현재 시점으로 앵커링한다.
                if self._is_realtime_web_query(query):
                    source_query = str(params.get("query") or query).strip()
                    if source_query:
                        normalized_query = self._normalize_realtime_web_query(source_query)
                        params["query"] = normalized_query
                        logger.info(
                            "[도구보정] 실시간 web_search 쿼리 정규화: '%s' -> '%s'",
                            source_query,
                            normalized_query,
                            extra=log_extra,
                        )

                # 날씨/장소는 전용 도구 우선 (웹검색 남용 방지)
                if weather_query:
                    location = self._extract_location_from_query(query) or "광양"
                    day_offset = 0
                    if "내일" in query:
                        day_offset = 1
                    elif "모레" in query:
                        day_offset = 2
                    elif "글피" in query:
                        day_offset = 3
                    elif any(token in query for token in ("다음주", "이번주", "주말", "일주일")):
                        day_offset = 3
                    candidate = {
                        "tool_to_use": "get_weather_forecast",
                        "tool_name": "get_weather_forecast",
                        "parameters": {"location": location, "day_offset": day_offset},
                    }
                    append_candidate(candidate)
                    logger.info("[도구보정] web_search -> get_weather_forecast 전환", extra=log_extra)
                    continue

                # 명시적 외부탐색 요청/금융 질문이 아니고 RAG가 강하면 웹검색 생략
                if (
                    not explicit_web
                    and not finance_query
                    and not place_query
                    and not factual_query
                    and rag_is_strong
                ):
                    logger.info(
                        "[도구보정] RAG 강한 질의에서 web_search 제거 (score=%.3f)",
                        rag_top_score,
                        extra=log_extra,
                    )
                    continue

                # 명시적 탐색 의도도 없고 금융도 아니며 짧은 일반질문이면 웹검색 차단
                if (
                    not explicit_web
                    and not finance_query
                    and not place_query
                    and not factual_query
                    and len(query.strip()) <= 16
                ):
                    logger.info("[도구보정] 명시적 탐색 의도 부족으로 web_search 제거", extra=log_extra)
                    continue

            candidate = {
                "tool_to_use": name,
                "tool_name": name,
                "parameters": params,
            }
            append_candidate(candidate)

        return normalized

    async def _should_use_web_search(self, query: str, rag_top_score: float, history: list = None) -> bool:
        """외부 정보 탐색(뉴스/웹/블로그/문서) 필요 여부를 판단합니다."""
        query_lower = query.lower()
        explicit_web = self._has_explicit_web_search_intent(query)
        finance_query = self._looks_like_finance_query(query)
        place_query = any(kw in query_lower for kw in self._PLACE_KEYWORDS)
        factual_query = self._looks_like_external_fact_query(query)

        # 인사/잡담은 항상 검색하지 않는다.
        if self._is_smalltalk_only_query(query):
            return False

        # 명시적인 검색 방지 패턴
        if any(pat in query_lower for pat in self._NO_SEARCH_PATTERNS):
            return False

        # RAG 점수가 매우 높으면 검색 생략 (이미 알고 있는 정보)
        if rag_top_score >= config.RAG_STRONG_SIMILARITY_THRESHOLD:
            # 최신/외부탐색/금융 키워드가 없으면 검색 생략
            if (
                not explicit_web
                and not finance_query
                and not place_query
                and not (factual_query and self._is_realtime_web_query(query))
            ):
                return False

        # 1. 명시 키워드 기반 판단
        if explicit_web or finance_query or place_query:
            return True

        if factual_query and self._is_realtime_web_query(query):
            return True

        # 1-1. RAG가 약하고 사실형 질의면 자동 웹검색
        if rag_top_score < config.RAG_SIMILARITY_THRESHOLD and factual_query:
            return True

        # 2. 맥락 기반 판단 (연계 질문)
        if history and rag_top_score < config.RAG_SIMILARITY_THRESHOLD:
            last_msg = history[-1]['parts'][0] if isinstance(history[-1]['parts'], list) else str(history[-1]['parts'])
            # 이전 답변이 탐색 맥락일 때만, 출처/근거/자세히 같은 명시적 후속 요청에 한해 검색 시도
            if "뉴스" in last_msg or "출처" in last_msg or "검색" in last_msg:
                if any(dw in query_lower for dw in self._WEB_SEARCH_FOLLOWUP_KEYWORDS):
                    return True
                if getattr(config, "AUTO_WEB_SEARCH_ALLOW_SHORT_FOLLOWUP", False) and len(query_lower) < 15:
                    return True

        return False

    # ── Query helpers ────────────────────────────────────────────────────

    @staticmethod
    def _build_finance_news_query(query: str) -> str:
        """금융 질문을 웹 검색 친화 쿼리로 보정합니다."""
        base = (query or "").strip()
        if not base:
            base = "국내외 금융 시장 최신 뉴스"
        base_lower = base.lower()
        has_news_hint = any(
            hint in base_lower
            for hint in ("뉴스", "소식", "헤드라인", "이슈", "동향", "시황", "news")
        )
        if not has_news_hint:
            base = f"{base} 최신 금융 뉴스"
        now_kst = datetime.now(timezone(timedelta(hours=9)))
        date_anchor = now_kst.strftime("%Y-%m-%d")
        return (
            f"{base}\n"
            f"기준일: {date_anchor} KST. 해당 시장의 최신 거래일을 명시하고, "
            "지수 수치와 등락은 거래소·공식 시세 자료로 교차 확인. "
            "뉴스는 기업 공시·거래소·중앙은행·주요 통신사/경제지 우선. "
            "커뮤니티·SNS·출처 없는 요약은 제외."
        )

    def _extract_location_from_query(self, query: str) -> str | None:
        """쿼리에서 지역명을 추출합니다 (DB 캐시 사용)."""
        if not self.location_cache:
            return None

        # 매칭된 것 중 가장 긴 것을 선택
        best_match = None
        for location in self.location_cache:
            if location in query:
                if best_match is None or len(location) > len(best_match):
                    best_match = location

        return best_match

    @staticmethod
    def _extract_us_stock_symbol(query_lower: str) -> str | None:
        """쿼리에서 미국 주식 심볼을 추출합니다."""
        symbol_map = {
            '애플': 'AAPL', 'apple': 'AAPL', 'aapl': 'AAPL',
            '테슬라': 'TSLA', 'tesla': 'TSLA', 'tsla': 'TSLA',
            '구글': 'GOOGL', 'google': 'GOOGL', 'googl': 'GOOGL',
            '엔비디아': 'NVDA', 'nvidia': 'NVDA', 'nvda': 'NVDA',
            '마이크로소프트': 'MSFT', 'microsoft': 'MSFT', 'msft': 'MSFT',
            '아마존': 'AMZN', 'amazon': 'AMZN', 'amzn': 'AMZN',
            '맥도날드': 'MCD', 'mcd': 'MCD',
            '스타벅스': 'SBUX', 'sbux': 'SBUX',
            '코카콜라': 'KO', 'coca-cola': 'KO', 'ko': 'KO',
            '펩시': 'PEP', 'pepsi': 'PEP',
            '넷플릭스': 'NFLX', 'netflix': 'NFLX',
            '메타': 'META', '페이스북': 'META', 'meta': 'META',
            '디즈니': 'DIS', 'disney': 'DIS',
            '인텔': 'INTC', 'intel': 'INTC',
            'amd': 'AMD',
            '나이키': 'NKE', 'nike': 'NKE',
            '코스트코': 'COST', 'costco': 'COST',
            '버크셔': 'BRK.B', 'berkshire': 'BRK.B'
        }
        for keyword, symbol in symbol_map.items():
            if keyword in query_lower:
                return symbol
        return None

    def _extract_kr_stock_ticker(self, query_lower: str) -> str | None:
        """쿼리에서 한국 주식 종목 코드를 추출합니다."""
        ticker_map = {
            '삼성전자': '005930', '현대차': '005380', 'sk하이닉스': '000660',
            '네이버': '035420', '카카오': '035720', 'lg에너지': '373220',
            '셀트리온': '068270', '삼성바이오': '207940', '기아': '000270', '포스코': '005490',
        }
        for keyword, ticker in ticker_map.items():
            if keyword in query_lower:
                return ticker
        return None

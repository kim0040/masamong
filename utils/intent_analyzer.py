# -*- coding: utf-8 -*-
"""
마사몽 봇의 의도 분석 및 도구 탐지를 담당하는 모듈입니다.

LLM 분석 + 키워드 기반 휴리스틱으로 사용자 의도를 파악하고,
적절한 도구(tool)를 선택하는 로직을 제공합니다.
"""

from __future__ import annotations

import json as _json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import config
from logger_config import logger


class IntentAnalyzer:
    """사용자 의도 분석 및 도구 탐지를 수행합니다.

    키워드 기반 휴리스틱과 LLM 기반 분석을 조합해
    날씨·금융·웹검색·장소·이미지 생성 등 다양한 도구 사용 의도를
    감지하고 적절한 도구 실행 계획(tool plan)을 생성합니다.
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
        'get_stock_price',
        'get_krw_exchange_rate',
        'get_company_news',
        'get_exchange_rate',
    ])
    _ALLOWED_RUNTIME_TOOLS = frozenset([
        "web_search",
        "get_weather_forecast",
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

    async def _load_location_cache(self) -> None:
        """DB에서 지역명 데이터를 로드하여 캐싱합니다."""
        if self.location_cache:
            return

        if not self.db:
            return

        try:
            async with self.db.execute("SELECT name FROM locations WHERE LENGTH(name) >= 2") as cursor:
                rows = await cursor.fetchall()
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
        if any(kw in query_lower for kw in self._STOCK_GENERAL_KEYWORDS):
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

    # ── Tool‑plan selection ─────────────────────────────────────────────

    def _select_tool_plan_without_intent_llm(
        self,
        query: str,
        *,
        rag_top_score: float,
        log_extra: dict | None = None,
    ) -> list[dict[str, Any]] | None:
        """
        의도 분석 LLM 호출 없이 처리 가능한 도구 계획을 우선 선택합니다.
        - 명확한 키워드 도구(날씨/금융/장소/이미지생성)는 즉시 라우팅
        - 강한 RAG + 도구 신호 없음이면 intent LLM 호출 자체를 생략
        """
        if self._is_smalltalk_only_query(query):
            return []

        keyword_plan = self._detect_tools_by_keyword(query)
        if keyword_plan:
            tool_name = keyword_plan[0].get("tool_to_use") or keyword_plan[0].get("tool_name")
            if tool_name and tool_name != "web_search":
                logger.info(
                    "[도구보정] 키워드 기반 라우팅으로 intent LLM 호출을 생략합니다. tool=%s",
                    tool_name,
                    extra=log_extra,
                )
                return keyword_plan
            # web_search는 명시적 탐색/금융 질문일 때만 키워드 라우팅
            if tool_name == "web_search":
                query_lower = (query or "").lower()
                finance_query = self._looks_like_finance_query(query)
                explicit_web = self._has_explicit_web_search_intent(query)
                place_query = any(kw in query_lower for kw in self._PLACE_KEYWORDS)
                if finance_query or explicit_web or place_query:
                    if self._is_realtime_web_query(query):
                        params = keyword_plan[0].setdefault("parameters", {})
                        source_query = str(params.get("query") or query).strip()
                        if source_query:
                            params["query"] = self._normalize_realtime_web_query(source_query)
                    logger.info(
                        "[도구보정] 키워드 기반 web_search 라우팅으로 intent LLM 호출을 생략합니다.",
                        extra=log_extra,
                    )
                    return keyword_plan

        # 명시적 외부 탐색 요청은 intent LLM을 거치지 않고 web_search로 직접 라우팅한다.
        if self._has_explicit_web_search_intent(query):
            normalized_query = query
            if self._is_realtime_web_query(query):
                normalized_query = self._normalize_realtime_web_query(query)
            logger.info(
                "[도구보정] 명시적 web_search 의도로 intent LLM 호출을 생략합니다.",
                extra=log_extra,
            )
            return [
                {
                    "tool_to_use": "web_search",
                    "tool_name": "web_search",
                    "parameters": {"query": normalized_query},
                }
            ]

        # 기본 대화(명시적 도구 신호 없음)는 intent LLM을 생략하고 로컬 기억 기반 응답으로 처리한다.
        if not self._has_tool_keyword_signal(query):
            if (
                self._looks_like_external_fact_query(query)
                and (
                    rag_top_score < config.RAG_SIMILARITY_THRESHOLD
                    or self._is_realtime_web_query(query)
                )
            ):
                logger.info(
                    "[도구보정] 사실형 질의로 판단해 intent LLM 없이 web_search로 직접 라우팅합니다.",
                    extra=log_extra,
                )
                normalized_query = query
                if self._is_realtime_web_query(query):
                    normalized_query = self._normalize_realtime_web_query(query)
                return [
                    {
                        "tool_to_use": "web_search",
                        "tool_name": "web_search",
                        "parameters": {"query": normalized_query},
                    }
                ]
            logger.info(
                "[도구보정] 도구 신호가 없어 intent LLM 호출을 생략합니다.",
                extra=log_extra,
            )
            return []

        if (
            getattr(config, "INTENT_LLM_RAG_STRONG_BYPASS", True)
            and rag_top_score >= config.RAG_STRONG_SIMILARITY_THRESHOLD
            and not self._has_tool_keyword_signal(query)
            and not self._has_explicit_web_search_intent(query)
        ):
            logger.info(
                "[도구보정] 강한 RAG 질의로 intent LLM 호출을 생략합니다. (score=%.3f)",
                rag_top_score,
                extra=log_extra,
            )
            return []

        return None

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
            logger.info(f"금융 관련 질문 감지: '{query}' -> web_search로 대체")
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

    async def _detect_tools_by_llm(self, query: str, log_extra: dict, history: list = None) -> list[dict]:
        """사용자의 의도와 대화 맥락을 분석하여 가장 적합한 도구와 최적화된 검색 파라미터를 결정합니다."""
        if not getattr(config, "INTENT_LLM_ENABLED", True):
            return self._detect_tools_by_keyword(query)

        # 운영 모드(INTENT_LLM_ALWAYS_RUN=false)에서는 잡담 질의를 LLM 호출 없이 단축 처리합니다.
        if self._is_smalltalk_only_query(query) and not getattr(config, "INTENT_LLM_ALWAYS_RUN", True):
            logger.info("[LLM의도분석] 잡담/인사성 질문 감지: 도구 호출 생략", extra=log_extra)
            return []

        # 히스토리 텍스트 변환
        history_text = ""
        if history:
            history_lines = []
            for h in history[-config.INTENT_HISTORY_LIMIT:]:  # 설정된 개수만큼만
                role = "User" if h['role'] == 'user' else "Masamong"
                content = h['parts'][0] if isinstance(h['parts'], list) else str(h['parts'])
                history_lines.append(f"{role}: {content}")
            history_text = "\n".join(history_lines)

        system_prompt = (
            "당신은 마사몽의 도구 플래너이자 검색 쿼리 최적화 전문가입니다. "
            "사용자의 현재 메시지와 이전 대화 맥락을 분석하여 의도를 파악하고, 작업을 수행하기 위해 가장 적절한 도구를 선택하세요.\n\n"
            "핵심 규칙:\n"
            "1. 대화 맥락 고려: 사용자가 '그거', '그때 말한 거' 등 지시 대명사를 쓰거나 주어를 생략하면 이전 대화에서 대상을 찾아 검색 쿼리에 포함하세요.\n"
            "2. 독립적 쿼리 생성: 도구 파라미터(특히 query)를 설정할 때, 이전 맥락 없이도 검색 엔진에서 정확한 결과를 얻을 수 있도록 완성된 문장/키워드로 변환하세요.\n"
            "3. 멀티 도구 선택: 여러 질문이 섞여 있다면 도구를 여러 개 선택할 수 있습니다.\n"
            "4. web_search는 '최신/실시간/뉴스/출처/웹검색 요청/금융 시황'이 분명할 때만 선택하세요.\n"
            "5. 인사/잡담/봇 상태 질문(예: '뭐해', '안녕')에는 절대 web_search를 선택하지 마세요.\n"
            "6. 날씨는 web_search 대신 get_weather_forecast를 우선 선택하세요.\n"
            "7. 사용자가 '오늘/현재/실시간/최신/최근'을 말하면, 검색 query에 과거 특정 연월(예: 2024년 5월)을 임의로 넣지 마세요.\n"
            "8. 이미지 생성/그림 요청은 web_search가 아닌 generate_image를 선택하세요.\n\n"
            "사용 가능 도구:\n"
            "1. get_weather_forecast(location, day_offset): 특정 지역/시간의 날씨.\n"
            "2. web_search(query): 외부 웹 검색(뉴스/웹/블로그/문서/커뮤니티).\n"
            "   - 최신 이슈뿐 아니라 사용법/비교/후기/공식 문서 탐색에도 사용.\n"
            "   - 맛집/장소 추천도 web_search로 처리.\n"
            "   - 주식/환율/코인 등 금융 관련 질문도 web_search로 처리.\n"
            "3. generate_image(prompt): AI 이미지 생성.\n"
            "   - '그려줘', '이미지 생성', '그림', '일러스트', '사진 만들어줘' 등의 요청에 사용.\n"
            "   - 이미지 검색(찾아줘)이 아니라 이미지 생성(만들어줘)일 때만 선택.\n\n"
            "출력 형식 (유효한 JSON만):\n"
            '{"intent": "의도", "reasoning": "선택 근거", "tools": [{"tool": "이름", "params": {"키": "값"}}]}'
        )
        try:
            if not self.llm_client.use_cometapi:
                return self._detect_tools_by_keyword(query)

            prompt = (
                f"System:\n{system_prompt}\n\n"
                "Examples:\n"
                'Context: (None)\nUser: "오늘 서울 날씨?"\n'
                'Response: {"intent": "날씨 조회", "reasoning": "서울 날씨 요청", "tools": [{"tool": "get_weather_forecast", "params": {"location": "서울", "day_offset": 0}}]}\n\n'
                'Context: User: "미국 이란 전쟁에 대해 알려줘"\\nMasamong: (전쟁 설명...)\n'
                'User: "군비는 얼마나 썼대?"\n'
                'Response: {"intent": "상세 수치 검색", "reasoning": "이전 대화인 미국-이란 전쟁의 군비 지출액을 묻는 연계 질문", "tools": [{"tool": "web_search", "params": {"query": "미국 이란 전쟁 군비 지출 및 비용"}}] }\n\n'
                'Context: User: "서울 날씨 어때?"\\nMasamong: (서울 날씨 답변...)\n'
                'User: "내일은?"\n'
                'Response: {"intent": "날씨 연계 질문", "reasoning": "이전 대화의 지역(서울) 유지, 시간만 내일로 변경", "tools": [{"tool": "get_weather_forecast", "params": {"location": "서울", "day_offset": 1}}] }\n\n'
                'Context: (None)\nUser: "사몽아 뭐하냐"\n'
                'Response: {"intent": "인사/잡담", "reasoning": "도구 불필요한 일반 대화", "tools": []}\n\n'
                'Context: (None)\nUser: "강아지 그림 그려줘"\n'
                'Response: {"intent": "이미지 생성", "reasoning": "사용자가 그림/이미지 생성을 요청", "tools": [{"tool": "generate_image", "params": {"prompt": "강아지"}}] }\n\n'
                'Context: (None)\nUser: "사이버펑크 스타일의 고양이 일러스트 만들어줘"\n'
                'Response: {"intent": "이미지 생성", "reasoning": "일러스트/이미지 생성 요청", "tools": [{"tool": "generate_image", "params": {"prompt": "사이버펑크 스타일의 고양이"}}] }\n\n'
                f"--- Current Context ---\n{history_text}\n"
                f"User Message: {query}\n\n"
                "Response:"
            )
            raw = await self.llm_client.fast_generate_text(
                prompt,
                None,
                log_extra,
                trace_key="cometapi_fast_intent",
            )
            if not raw:
                return self._detect_tools_by_keyword(query)
            if "```" in raw:
                raw = re.sub(r"```(?:json)?", "", raw).replace("```", "").strip()

            parsed = _json.loads(raw)
            logger.info(f"[LLM의도분석] Intent: {parsed.get('intent')}, Reason: {parsed.get('reasoning')}", extra=log_extra)

            tool_list = parsed.get("tools", [])
            result = []
            for t in tool_list:
                name = t.get("tool", "")
                if not name or name == "none":
                    continue
                params = t.get("params", {})
                if not isinstance(params, dict):
                    params = {}

                # 금융 도구는 실수 빈도가 높아 웹 검색 도구로 일괄 대체
                if name in self._DEPRECATED_FINANCE_TOOLS:
                    finance_query = (
                        params.get("query")
                        or params.get("user_query")
                        or params.get("symbol")
                        or params.get("stock_name")
                        or query
                    )
                    result.append(
                        {
                            "tool_to_use": "web_search",
                            "tool_name": "web_search",
                            "parameters": {"query": self._build_finance_news_query(finance_query)},
                        }
                    )
                    continue

                if name not in self._ALLOWED_RUNTIME_TOOLS:
                    logger.info("[LLM의도분석] 비허용 도구 계획 제거: %s", name, extra=log_extra)
                    continue

                result.append({"tool_to_use": name, "tool_name": name, "parameters": params})

            # LLM이 잡담에 대해 과탐지한 경우 방어적으로 무효화
            if result and self._is_smalltalk_only_query(query):
                logger.info("[LLM의도분석] 잡담 질의에 대한 도구 계획 무효화", extra=log_extra)
                return []
            return result
        except Exception as e:
            logger.warning(f"[LLM도구선택] 실패 → 키워드 fallback: {e}", extra=log_extra)
            return self._detect_tools_by_keyword(query)

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
            return []

        query_lower = (query or "").lower()
        explicit_web = self._has_explicit_web_search_intent(query)
        finance_query = self._looks_like_finance_query(query)
        factual_query = self._looks_like_external_fact_query(query)
        weather_query = any(kw in query_lower for kw in self._WEATHER_KEYWORDS)
        place_query = any(kw in query_lower for kw in self._PLACE_KEYWORDS)
        rag_is_strong = rag_top_score >= config.RAG_STRONG_SIMILARITY_THRESHOLD

        normalized: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str]] = set()

        for raw in tool_plan:
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

            # 실행 가능한 도구는 web_search / get_weather_forecast만 허용
            if name not in self._ALLOWED_RUNTIME_TOOLS:
                logger.info("[도구보정] 허용되지 않은 도구 제거: %s", name, extra=log_extra)
                continue

            # 잡담 질문은 도구 자체를 차단
            if self._is_smalltalk_only_query(query):
                logger.info("[도구보정] 잡담성 질의로 도구 계획을 모두 무효화합니다.", extra=log_extra)
                return []

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
                    key = (candidate["tool_to_use"], _json.dumps(candidate["parameters"], sort_keys=True, ensure_ascii=False))
                    if key not in seen_keys:
                        seen_keys.add(key)
                        normalized.append(candidate)
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
            key = (name, _json.dumps(params, sort_keys=True, ensure_ascii=False))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            normalized.append(candidate)

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
            return "국내외 금융 시장 최신 뉴스"
        base_lower = base.lower()
        has_news_hint = any(
            hint in base_lower
            for hint in ("뉴스", "소식", "헤드라인", "이슈", "동향", "시황", "news")
        )
        if has_news_hint:
            return base
        return f"{base} 최신 금융 뉴스"

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

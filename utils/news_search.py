# -*- coding: utf-8 -*-
"""
utils/news_search.py — 마사몽용 DuckDuckGo 범용 탐색 RAG 파이프라인

news/news_summarizer.py에서 이식, 마사몽 아키텍처에 맞게 수정:
- synthesize_final_answer() 제거 → ai_handler가 채널 페르소나로 최종 답변 생성
- call_smart_model() 제거 → 마사몽의 CometAPI 클라이언트 사용
- call_fast_model() → 마사몽의 config (COMETAPI_KEY, FAST_MODEL_NAME) 사용
- 동기 함수들을 asyncio.to_thread()로 감싸 async 호환성 확보

[모델]
- Fast: gemini-3.1-flash-lite-preview (의도 분석, 키워드 생성, 링크 선정, 기사 요약)
- Final: 마사몽의 기존 모델 (DeepSeek/Gemini) 담당 — 이 파일에서는 처리 안 함
"""
from __future__ import annotations  # Python 3.9 호환: X | Y 타입 힌트 지원

import asyncio
import re
import json
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
from typing import Any
import threading
import time
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

import requests
import trafilatura
from ddgs import DDGS
from newspaper import Article
from google import genai

import config
from database.compat_db import TiDBSettings, rewrite_sql_for_tidb
from logger_config import logger

try:
    import pymysql
except ModuleNotFoundError:  # pragma: no cover
    pymysql = None  # type: ignore

# [Security/Fix] NLTK resources check
try:
    import nltk
    # punkt, punkt_tab are needed for newspaper4k
    for res in ['punkt', 'punkt_tab']:
        try:
            nltk.data.find(f'tokenizers/{res}')
        except LookupError:
            nltk.download(res, quiet=True)
except ImportError:
    pass

# ─────────────────────────────────────────────
# Fast 모델 클라이언트 (의도 분석 / 키워드 / 기사 요약 전용)
# ─────────────────────────────────────────────
_fast_client: genai.Client | None = None
_pipeline_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_pipeline_cache_lock = threading.Lock()


class FastLLMBudget:
    """파이프라인 내 Fast 모델 호출 횟수를 제한하는 스레드 세이프 카운터."""

    def __init__(self, max_calls: int):
        self.max_calls = max(0, int(max_calls))
        self._used_calls = 0
        self._lock = threading.Lock()

    def consume(self) -> bool:
        with self._lock:
            if self._used_calls >= self.max_calls:
                return False
            self._used_calls += 1
            return True

    @property
    def used_calls(self) -> int:
        with self._lock:
            return self._used_calls


class FastLLMQuotaManager:
    """Fast 모델 호출을 중앙 api_call_log(cometapi) 기준으로 제한/기록합니다."""

    def __init__(self, db_path: str | None):
        self.db_path = db_path
        self.rpm_limit = max(1, int(getattr(config, "COMETAPI_RPM_LIMIT", 40)))
        self.rpd_limit = max(1, int(getattr(config, "COMETAPI_RPD_LIMIT", 3000)))
        self._lock = threading.Lock()
        self._db_unavailable = False
        self._tidb_settings = None
        if config.DB_BACKEND == "tidb" and config.TIDB_HOST and config.TIDB_USER:
            self._tidb_settings = TiDBSettings(
                host=config.TIDB_HOST,
                port=config.TIDB_PORT,
                user=config.TIDB_USER,
                password=config.TIDB_PASSWORD or "",
                database=config.TIDB_NAME,
                ssl_ca=config.TIDB_SSL_CA,
                ssl_verify_identity=config.TIDB_SSL_VERIFY_IDENTITY,
            )

    def try_consume(self) -> bool:
        if not self.db_path or self._db_unavailable:
            return True

        now_utc = datetime.now(timezone.utc)
        one_minute_ago = (now_utc - timedelta(minutes=1)).isoformat()
        one_day_ago = (now_utc - timedelta(days=1)).isoformat()
        now_iso = now_utc.isoformat()

        try:
            with self._lock:
                conn = self._open_connection()
                try:
                    cur = conn.cursor()
                    cur.execute(self._sql("DELETE FROM api_call_log WHERE called_at < ?"), self._params(one_day_ago))

                    cur.execute(
                        self._sql("SELECT COUNT(*) FROM api_call_log WHERE api_type = ? AND called_at >= ?"),
                        self._params("cometapi", one_day_ago),
                    )
                    if (cur.fetchone() or [0])[0] >= self.rpd_limit:
                        logger.warning("[web_search] CometAPI 일일 호출 제한 도달")
                        return False

                    cur.execute(
                        self._sql("SELECT COUNT(*) FROM api_call_log WHERE api_type = ? AND called_at >= ?"),
                        self._params("cometapi", one_minute_ago),
                    )
                    if (cur.fetchone() or [0])[0] >= self.rpm_limit:
                        logger.warning("[web_search] CometAPI 분당 호출 제한 도달")
                        return False

                    cur.execute(
                        self._sql("INSERT INTO api_call_log (api_type, called_at) VALUES (?, ?)"),
                        self._params("cometapi", now_iso),
                    )
                    cur.execute(
                        self._sql("INSERT INTO api_call_log (api_type, called_at) VALUES (?, ?)"),
                        self._params("cometapi_fast_news_search", now_iso),
                    )
                    conn.commit()
                    return True
                finally:
                    conn.close()
        except Exception as e:
            logger.warning(f"[web_search] Fast 모델 DB quota 계측 실패(우회): {e}")
            err_msg = str(e).lower()
            if "no such table" in err_msg or "unable to open database file" in err_msg:
                self._db_unavailable = True
            return True

    def _open_connection(self):
        if self._tidb_settings is not None:
            if pymysql is None:
                raise RuntimeError("PyMySQL 패키지가 필요합니다.")
            return pymysql.connect(**self._tidb_settings.to_connect_kwargs())
        return sqlite3.connect(self.db_path, timeout=2)

    def _sql(self, query: str) -> str:
        if self._tidb_settings is not None:
            return rewrite_sql_for_tidb(query)
        return query

    @staticmethod
    def _params(*items: Any):
        return tuple(items)


def _get_fast_client() -> genai.Client:
    """Fast 모델 클라이언트를 싱글턴으로 반환합니다."""
    global _fast_client
    if _fast_client is None:
        _fast_client = genai.Client(
            http_options={
                "api_version": "v1beta",
                "base_url": "https://api.cometapi.com",
            },
            api_key=config.COMETAPI_KEY,
        )
    return _fast_client


def _call_fast_model(
    prompt: str,
    *,
    budget: FastLLMBudget | None = None,
    quota_manager: FastLLMQuotaManager | None = None,
) -> str:
    """
    [동기] Fast 모델 호출 (gemini-3.1-flash-lite-preview).
    의도 분석, 키워드 생성, 개별 기사 요약에 사용합니다.
    실패 시 빈 문자열 반환.
    """
    fast_model = getattr(config, "FAST_MODEL_NAME", "gemini-3.1-flash-lite-preview")
    max_prompt_chars = int(getattr(config, "WEB_RAG_FAST_PROMPT_MAX_CHARS", 5000))
    normalized_prompt = (prompt or "")[:max_prompt_chars]
    if budget is not None and not budget.consume():
        logger.info("[web_search] Fast 모델 호출 예산 소진으로 LLM 단계를 건너뜁니다.")
        return ""
    if quota_manager is not None and not quota_manager.try_consume():
        logger.info("[web_search] CometAPI 중앙 호출 제한으로 Fast 모델 단계를 건너뜁니다.")
        return ""
    try:
        client = _get_fast_client()
        response = client.models.generate_content(
            model=fast_model,
            contents=normalized_prompt,
        )
        return response.text.strip()
    except Exception as e:
        logger.warning(f"[web_search] Fast 모델 호출 실패: {e}")
        return ""


def _build_cache_key(user_query: str) -> str:
    norm = re.sub(r"\s+", " ", (user_query or "").strip().lower())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def _load_pipeline_cache(user_query: str) -> dict[str, Any] | None:
    ttl = int(getattr(config, "WEB_RAG_CACHE_TTL_SECONDS", 300))
    if ttl <= 0:
        return None

    key = _build_cache_key(user_query)
    now = time.time()
    with _pipeline_cache_lock:
        record = _pipeline_cache.get(key)
        if not record:
            return None
        ts, payload = record
        if now - ts > ttl:
            _pipeline_cache.pop(key, None)
            return None
        return json.loads(json.dumps(payload, ensure_ascii=False))


def _save_pipeline_cache(user_query: str, payload: dict[str, Any]) -> None:
    ttl = int(getattr(config, "WEB_RAG_CACHE_TTL_SECONDS", 300))
    if ttl <= 0:
        return
    key = _build_cache_key(user_query)
    max_entries = int(getattr(config, "WEB_RAG_CACHE_MAX_ENTRIES", 128))
    now = time.time()
    with _pipeline_cache_lock:
        if len(_pipeline_cache) >= max_entries:
            oldest_key = min(_pipeline_cache.items(), key=lambda item: item[1][0])[0]
            _pipeline_cache.pop(oldest_key, None)
        _pipeline_cache[key] = (now, json.loads(json.dumps(payload, ensure_ascii=False)))


# ─────────────────────────────────────────────
# 기사 본문 추출 (3단계 fallback)
# ─────────────────────────────────────────────

_REQUEST_TIMEOUT = 15
_MIN_ARTICLE_LENGTH = 30


def _is_safe_url(url: str) -> bool:
    """
    URL의 안전성을 검사합니다.
    - 내부 네트워크(SSRF) 차단
    - 파일 크기 제한 (2MB)
    - Content-Type 제한 (HTML/TEXT)
    """
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        
        # 1. 내부/루프백 IP 및 호스트 차단
        if hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
            logger.warning(f"[web_search] 차단된 로컬 호스트: {hostname}")
            return False
        if hostname.startswith("192.168.") or hostname.startswith("10.") or hostname.startswith("172.16."):
            logger.warning(f"[web_search] 차단된 내부 IP 대역: {hostname}")
            return False

        # 2. HEAD 요청으로 메타데이터 확인 (최대 2MB)
        headers = {"User-Agent": "Mozilla/5.0"}
        # SSL 인증서 오류가 있는 사이트도 메타데이터는 확인하기 위해 verify=False 설정 고려
        response = requests.head(url, headers=headers, timeout=5, allow_redirects=True, verify=False)
        
        # Content-Type 체크
        content_type = response.headers.get("Content-Type", "").lower()
        if "text/html" not in content_type and "text/plain" not in content_type:
            logger.warning(f"[web_search] 허용되지 않는 Content-Type: {content_type} ({url})")
            return False
            
        # Content-Length 체크
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > 2 * 1024 * 1024:
            logger.warning(f"[web_search] 파일 크기 초과: {content_length} bytes ({url})")
            return False
            
        return True
    except Exception as e:
        logger.warning(f"[web_search] URL 안전성 검사 중 오류 (건너뜀): {e}")
        return True # 오류 시 본문 추출 단계에서 처리되도록 허용


def _extract_article_text(url: str) -> tuple[str | None, str]:
    """
    URL로부터 뉴스 기사 본문을 추출합니다 (3단계 fallback).

    [단계 1] Newspaper4k — 뉴스 전용 파서
    [단계 2] Trafilatura 표준 모드
    [단계 3] Trafilatura 공격 모드 (정밀도보다 재현율 우선)
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }
    try:
        response = requests.get(url, headers=headers, timeout=_REQUEST_TIMEOUT)
        response.raise_for_status()
        html_content = response.text
        text = ""

        # 단계 1: Newspaper4k
        try:
            # newspaper4k에서는 생성자에 html을 넣거나 download(html=...)를 사용
            article = Article(url, language="ko")
            # 만약 .html 속성이 있다면 직접 할당 시도해볼 수 있으나, 
            # 가장 안전한 방식은 parse() 전에 속성을 채우는 것 (버전에 따라 다름)
            if hasattr(article, 'set_html'):
                article.set_html(html_content)
            elif hasattr(article, 'download'):
                # 일부 버전은 download에 html 인자를 받음
                try:
                    article.download(input_html=html_content)
                except:
                    article.html = html_content
            else:
                article.html = html_content
                
            article.parse()
            text = article.text.strip()
        except Exception as e:
            logger.warning(f"[web_search] Newspaper4k 파싱 실패: {e}")
            text = ""

        # 단계 2: Trafilatura 표준
        if len(text) < 300:
            extracted = trafilatura.extract(
                html_content, include_comments=False, include_tables=True
            )
            if extracted and len(extracted) > len(text):
                text = extracted.strip()

        # 단계 3: Trafilatura 공격적 모드
        if len(text) < 100:
            raw_extracted = trafilatura.extract(
                html_content,
                include_comments=True,
                include_tables=True,
                no_fallback=False,
                favor_precision=False,
            )
            if raw_extracted:
                text = raw_extracted.strip()

        if len(text) < _MIN_ARTICLE_LENGTH:
            return None, "기사 내용을 추출할 수 없습니다."

        return text, "성공"

    except Exception as e:
        return None, f"추출 중 예외 발생: {e}"


# ─────────────────────────────────────────────
# 파이프라인 내부 함수들
# ─────────────────────────────────────────────

def _analyze_intent(
    user_query: str,
    *,
    budget: FastLLMBudget | None = None,
    quota_manager: FastLLMQuotaManager | None = None,
) -> dict[str, str]:
    """
    사용자 질문의 의도를 분석하여 검색 전략을 결정합니다.
    분류:
    - category: 'NEWS' (최신 사건/뉴스가 필요한 경우), 'GENERAL' (일반 지식/기관 정보/과거 사실이 필요한 경우)
    - scope: 'LOCAL', 'GLOBAL', 'BOTH' (검색 지역 범위)
    """
    prompt = (
        f"다음 질문의 의도를 분석하여 JSON 형식으로 답변하세요.\n"
        f"1. category: 최신 뉴스/사건보도가 중요하면 'NEWS', 일반 정보 탐색(문서/블로그/커뮤니티/위키 포함)이면 'GENERAL'\n"
        f"2. scope: 국내 소식 중심이면 'LOCAL', 해외 소식이 중요하면 'GLOBAL', 둘 다면 'BOTH'\n\n"
        f"질문: {user_query}\n\n"
        f"형식: {{\"category\": \"GENERAL\", \"scope\": \"BOTH\"}}"
    )
    raw = _call_fast_model(prompt, budget=budget, quota_manager=quota_manager)
    try:
        # JSON 문자열만 추출 (마크다운 코드 블록 제거 등)
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            data = json.loads(match.group())
            category = data.get("category", "NEWS").upper()
            scope = data.get("scope", "BOTH").upper()
            return {
                "category": category if category in ("NEWS", "GENERAL") else "NEWS",
                "scope": scope if scope in ("LOCAL", "GLOBAL", "BOTH") else "BOTH"
            }
    except Exception as e:
        logger.warning(f"[web_search] 의도 분석 파싱 실패: {e}")
    
    return {"category": "GENERAL", "scope": "BOTH"}


def _infer_category_fallback(user_query: str) -> str:
    query = (user_query or "").lower()
    news_keywords = (
        "최신", "최근", "요즘", "뉴스", "소식", "속보", "오늘", "이번 주", "이번주",
        "현재", "실시간", "동향", "발표", "이슈", "업데이트",
    )
    return "NEWS" if any(token in query for token in news_keywords) else "GENERAL"


def _infer_scope_fallback(user_query: str) -> str:
    query = (user_query or "").lower()
    local_keywords = ("한국", "국내", "우리나라", "전주", "서울", "광양", "부산")
    global_keywords = ("해외", "글로벌", "미국", "일본", "중국", "유럽", "world", "global")
    has_local = any(token in query for token in local_keywords)
    has_global = any(token in query for token in global_keywords)
    if has_local and has_global:
        return "BOTH"
    if has_local:
        return "LOCAL"
    if has_global:
        return "GLOBAL"
    return "BOTH"


def _plan_search_fallback(user_query: str) -> dict[str, Any]:
    category = _infer_category_fallback(user_query)
    return {
        "category": category,
        "scope": _infer_scope_fallback(user_query),
        "keywords_ko": _build_keywords_fallback(user_query, "ko", category),
        "keywords_en": _build_keywords_fallback(user_query, "en", category),
    }


def _plan_search(
    user_query: str,
    *,
    budget: FastLLMBudget | None = None,
    quota_manager: FastLLMQuotaManager | None = None,
) -> dict[str, Any]:
    """검색 전략과 다국어 키워드를 한 번에 생성합니다."""
    prompt = (
        "당신은 웹 검색 전략가입니다. 사용자 질문을 보고 검색 계획을 JSON으로만 출력하세요.\n"
        "반드시 아래 형식을 지키세요.\n"
        "{"
        "\"category\":\"GENERAL 또는 NEWS\","
        "\"scope\":\"LOCAL 또는 GLOBAL 또는 BOTH\","
        "\"keywords_ko\":[\"한국어 검색어1\",\"한국어 검색어2\"],"
        "\"keywords_en\":[\"english query 1\",\"english query 2\"]"
        "}\n\n"
        "[규칙]\n"
        "- category: 최신 뉴스/속보성 정보가 중요하면 NEWS, 일반 정보/문서/후기/비교면 GENERAL\n"
        "- scope: 국내 중심 LOCAL, 해외 중심 GLOBAL, 둘 다면 BOTH\n"
        "- keywords_ko: 핵심 검색어와 확장 검색어 2개\n"
        "- keywords_en: 영문 검색어 2개\n"
        "- 다른 설명, 코드블록, 마크다운 없이 JSON만 출력\n\n"
        f"질문: {user_query}"
    )
    raw = _call_fast_model(prompt, budget=budget, quota_manager=quota_manager)
    try:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return _plan_search_fallback(user_query)
        data = json.loads(match.group())
        category = str(data.get("category", "GENERAL")).upper()
        scope = str(data.get("scope", "BOTH")).upper()
        keywords_ko = [str(v).strip() for v in data.get("keywords_ko", []) if str(v).strip()]
        keywords_en = [str(v).strip() for v in data.get("keywords_en", []) if str(v).strip()]
        if category not in ("NEWS", "GENERAL"):
            category = _infer_category_fallback(user_query)
        if scope not in ("LOCAL", "GLOBAL", "BOTH"):
            scope = _infer_scope_fallback(user_query)
        if not keywords_ko:
            keywords_ko = _build_keywords_fallback(user_query, "ko", category)
        if not keywords_en:
            keywords_en = _build_keywords_fallback(user_query, "en", category)
        return {
            "category": category,
            "scope": scope,
            "keywords_ko": list(dict.fromkeys(keywords_ko))[:2],
            "keywords_en": list(dict.fromkeys(keywords_en))[:2],
        }
    except Exception as e:
        logger.warning(f"[web_search] 검색 계획 파싱 실패: {e}")
        return _plan_search_fallback(user_query)


def _search_web_sync(user_query: str, region: str = "kr-kr", *, keywords: list[str]) -> tuple[list | None, str]:
    """
    [동기] DuckDuckGo 일반 웹 검색을 수행합니다 (날짜 제한 없음).
    공식 홈페이지, 위키백과 등을 찾기에 유리합니다.
    """
    label = "국내/웹" if region == "kr-kr" else "해외/웹"

    logger.info(f"[web_search] [{label}] 검색어: {keywords}")

    all_raw: list[dict] = []
    seen_urls: set[str] = set()

    for kw in keywords:
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(kw, region=region, safesearch="off", max_results=10))
                for r in results:
                    url = r.get("href", "")
                    if url and url not in seen_urls:
                        all_raw.append({
                            "title": r.get("title", ""),
                            "body": r.get("body", ""),
                            "url": url,
                            "source": "Web",
                            "date": "" # 웹 검색은 날짜 정보가 부정확할 수 있음
                        })
                        seen_urls.add(url)
        except Exception as e:
            logger.warning(f"[web_search] DDGS Web 오류 ({kw[:30]}): {e}")

    if not all_raw:
        return None, f"[{label}] 검색 결과가 없습니다."

    results = [
        {
            "title": r.get("title", ""),
            "snippet": r.get("body", ""),
            "url": r.get("url", ""),
            "source": r.get("source", ""),
            "date": r.get("date", ""),
        }
        for r in all_raw
    ]
    logger.info(f"[web_search] [{label}] 총 {len(results)}개 웹 후보 확보")
    return results, "성공"


def _build_keywords_fallback(user_query: str, lang: str, category: str) -> list[str]:
    """LLM 없이도 동작하는 검색어 기본 전략."""
    base = re.sub(r"\s+", " ", (user_query or "").strip())
    if not base:
        return ["최신 이슈"] if lang == "ko" else ["latest updates"]

    if lang == "ko":
        variant = f"{base} 최신 동향" if category == "NEWS" else f"{base} 블로그 후기 정리"
    else:
        variant = f"{base} latest updates" if category == "NEWS" else f"{base} documentation blog review"
    return [base, variant]


def _build_keywords(
    user_query: str,
    lang: str,
    *,
    category: str,
    budget: FastLLMBudget | None = None,
    quota_manager: FastLLMQuotaManager | None = None,
) -> list[str]:
    """서로 다른 각도의 검색 키워드를 2개 생성합니다."""
    lang_label = "한국어" if lang == "ko" else "영어"
    prompt = (
        f"당신은 웹 검색 전문가입니다. 질문에 대한 신뢰 가능한 정보를 찾기 위한 검색어 2개를 출력하세요.\n"
        f"[규칙]\n"
        f"- 검색어 1: 핵심 키워드 중심 짧은 검색어\n"
        f"- 검색어 2: 맥락/출처 탐색용 확장 검색어\n"
        f"- category={category}\n"
        f"- 출력 언어: {lang_label}\n"
        f"- 따옴표, 번호, 설명 없이 검색어만 한 줄씩 출력\n\n"
        f"질문: {user_query}"
    )
    raw = _call_fast_model(prompt, budget=budget, quota_manager=quota_manager)
    keywords = [line.strip() for line in raw.splitlines() if line.strip()]
    if not keywords:
        return _build_keywords_fallback(user_query, lang, category)
    unique = list(dict.fromkeys(keywords))
    if len(unique) == 1:
        unique.append(_build_keywords_fallback(user_query, lang, category)[-1])
    return unique[:2]


def _domain(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""


def _rank_candidates(results: list[dict]) -> list[dict]:
    """도메인/스니펫 품질을 반영해 기본 정렬합니다."""
    ranked: list[tuple[float, dict]] = []
    for item in results:
        url = item.get("url", "")
        snippet = (item.get("snippet") or "").strip()
        dom = _domain(url)
        score = 0.0
        if snippet:
            score += min(len(snippet), 220) / 220.0
        if any(token in dom for token in (".go.kr", ".ac.kr", "wikipedia.org", "namu.wiki")):
            score += 0.35
        if any(token in dom for token in ("blog", "tistory", "velog", "brunch")):
            score += 0.15
        if any(token in dom for token in ("dcinside", "reddit", "quora")):
            score += 0.1
        ranked.append((score, item))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in ranked]


def _dedupe_by_url(results: list[dict]) -> list[dict]:
    unique: list[dict] = []
    seen: set[str] = set()
    for item in results:
        url = str(item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        unique.append(item)
    return unique


def _diversify_domains(results: list[dict], limit: int) -> list[dict]:
    """같은 도메인이 과도하게 몰리지 않도록 제한합니다."""
    per_domain_limit = 2
    domain_counts: dict[str, int] = {}
    selected: list[dict] = []
    for item in results:
        dom = _domain(item.get("url", "")) or "unknown"
        current = domain_counts.get(dom, 0)
        if current >= per_domain_limit:
            continue
        selected.append(item)
        domain_counts[dom] = current + 1
        if len(selected) >= limit:
            break
    return selected


def _format_ddgs_results_for_llm(results: list[dict]) -> str:
    """DDGS 검색 결과를 LLM이 읽기 좋은 구조화 텍스트로 변환합니다."""
    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "제목 없음")
        body = (r.get("body") or "")[:200]
        source = r.get("source", "")
        date = r.get("date", "")
        url = r.get("url", "")
        lines.append(
            f"[{i}] 제목: {title}\n"
            f"    언론사: {source}  날짜: {date}\n"
            f"    내용: {body}\n"
            f"    URL: {url}"
        )
    return "\n\n".join(lines)


def _search_news_sync(user_query: str, region: str = "kr-kr", *, keywords: list[str]) -> tuple[list | None, str]:
    """
    [동기] DuckDuckGo News에서 뉴스를 검색합니다 (API 키 불필요, 완전 무료).

    멀티키워드 전략:
    - LLM이 생성한 2가지 검색어로 각각 검색하여 결과를 합산
    - 먼저 최근 1개월(m) 기간으로 검색 → 결과 부족 시 기간 제한 없이 재검색
    """
    label = "국내" if region == "kr-kr" else "해외"

    logger.info(f"[web_search] [{label}] 검색어: {keywords}")

    all_raw: list[dict] = []
    seen_urls: set[str] = set()

    def _search(keyword: str, timelimit: str | None) -> list[dict]:
        try:
            with DDGS() as ddgs:
                return list(
                    ddgs.news(
                        keyword,
                        region=region,
                        safesearch="off",
                        timelimit=timelimit,
                        max_results=10,
                    )
                )
        except Exception as e:
            logger.warning(f"[web_search] DDGS 오류 ({keyword[:30]}): {e}")
            return []

    # 1차: 최근 1개월 검색
    for kw in keywords:
        for r in _search(kw, timelimit="m"):
            url = r.get("url", "")
            if url and url not in seen_urls:
                all_raw.append(r)
                seen_urls.add(url)

    # 2차: 결과 부족 시 기간 제한 없이 보충
    if len(all_raw) < 5:
        logger.info(f"[web_search] [{label}] 결과 부족({len(all_raw)}개) → 기간 제한 없이 재검색")
        for kw in keywords:
            for r in _search(kw, timelimit=None):
                url = r.get("url", "")
                if url and url not in seen_urls:
                    all_raw.append(r)
                    seen_urls.add(url)

    if not all_raw:
        return None, f"[{label}] 검색 결과가 없습니다."

    results = [
        {
            "title": r.get("title", ""),
            "snippet": r.get("body", ""),
            "url": r.get("url", ""),
            "source": r.get("source", ""),
            "date": r.get("date", ""),
        }
        for r in all_raw
    ]
    logger.info(f"[web_search] [{label}] 총 {len(results)}개 후보 확보")
    return results, "성공"


def _select_links_fallback(search_results: list[dict], count: int) -> list[str]:
    ranked = _rank_candidates(search_results)
    diversified = _diversify_domains(ranked, max(count * 2, count))
    return [item.get("url", "") for item in diversified if item.get("url")][:count]


def _select_best_links(
    user_query: str,
    search_results: list,
    count: int = 3,
    *,
    budget: FastLLMBudget | None = None,
    quota_manager: FastLLMQuotaManager | None = None,
) -> tuple[list, str]:
    """LLM이 검색 결과 중 질문에 가장 적합한 URL을 최대 count개 선정합니다."""
    if not search_results:
        return [], "검색 결과가 없습니다."

    candidates_text = _format_ddgs_results_for_llm(search_results)
    select_prompt = (
        f"사용자 질문: \"{user_query}\"\n\n"
        f"아래 검색 결과 중 질문에 답변하기 위해 가장 **관련성이 높고 유용한** 정보를 최대 {count}개 선정하세요.\n\n"
        f"선정 기준:\n"
        f"1. **내용 적합성**: 질문의 핵심 키워드를 가장 잘 담고 있거나 질문에 대한 답을 포함하고 있는 결과 우선\n"
        f"2. **출처의 다양성**: 공식 홈페이지(.ac.kr, .go.kr), 위키백과, 나무위키, 정규 언론사 기사는 물론, 필요한 경우 커뮤니티(디시인사이드 등)나 블로그도 정보가 풍부하다면 포함 가능\n"
        f"3. **최신성/정확성**: 뉴스인 경우 최신순, 일반 정보인 경우 정확도순으로 판단\n"
        f"4. **스팸 배제**: 질문과 전혀 관계없는 광고성 페이지는 제외\n\n"
        f"**주의**: 완벽한 공식 출처가 없더라도, 정보를 얻을 수 있는 차선책을 반드시 선정하세요.\n\n"
        f"출력 형식: 선정한 URL만 한 줄에 하나씩 출력하세요. 다른 텍스트는 출력하지 마세요.\n\n"
        f"후보 목록:\n{candidates_text}"
    )

    response_text = _call_fast_model(select_prompt, budget=budget, quota_manager=quota_manager)
    if not response_text:
        fallback_urls = _select_links_fallback(search_results, count)
        if fallback_urls:
            return fallback_urls, "LLM 선정 생략: 규칙 기반 선정 사용"
        return [], "신뢰할 수 있는 출처를 찾지 못했습니다."

    urls = re.findall(r"https?://[^\s<>\"']+", response_text)
    unique_urls = list(dict.fromkeys(urls))

    if not unique_urls:
        fallback_urls = _select_links_fallback(search_results, count)
        if fallback_urls:
            return fallback_urls, "LLM 선정 실패: 규칙 기반 선정 사용"
        return [], "신뢰할 수 있는 출처를 찾지 못했습니다."

    return unique_urls[:count], "성공"


def _snippet_fallback_summary(user_query: str, title: str, snippet: str, url: str) -> str:
    title_text = (title or "").strip()
    snippet_text = re.sub(r"\s+", " ", (snippet or "").strip())
    if not snippet_text and title_text:
        return (
            f"'{user_query}' 관련 자료를 확인했지만 본문 접근 제한이 있어 제목 기반으로 정리해.\n"
            f"- 자료 제목: {title_text}\n"
            f"- 원문 링크에서 세부 내용을 추가 확인해줘."
        )
    if snippet_text:
        return (
            f"'{user_query}' 관련 자료 요약(본문 접근 제한으로 스니펫 기반):\n"
            f"- 제목: {title_text or '제목 정보 없음'}\n"
            f"- 핵심: {snippet_text}"
        )
    return f"'{user_query}' 관련 링크를 찾았지만 본문/스니펫을 모두 확보하지 못했어. 원문 확인이 필요해: {url}"


def _local_extract_summary(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        return ""
    if len(cleaned) <= 360:
        return cleaned
    return cleaned[:357].rstrip() + "..."


def _process_single_article(
    url: str,
    user_query: str,
    snippet: str = "",
    title: str = "",
    *,
    budget: FastLLMBudget | None = None,
    quota_manager: FastLLMQuotaManager | None = None,
) -> dict | None:
    """
    단일 기사 URL에서 본문을 추출하고 Fast 모델로 요약합니다.
    ThreadPoolExecutor에서 각 워커가 독립적으로 실행하는 단위 작업입니다.
    본문 추출 실패 시 DDGS snippet을 fallback으로 활용합니다.
    """
    if not _is_safe_url(url):
        logger.warning(f"[web_search] URL 안전성 검사 실패로 건너뜀: {url}")
        if snippet or title:
            return {"url": url, "summary": _snippet_fallback_summary(user_query, title, snippet, url), "mode": "snippet"}
        return None

    text, msg = _extract_article_text(url)

    if not text:
        if snippet or title:
            logger.info(f"[web_search] 본문 추출 실패 → snippet/title 기반 폴백: {url[:50]}")
            return {"url": url, "summary": _snippet_fallback_summary(user_query, title, snippet, url), "mode": "snippet"}
        logger.warning(f"[web_search] 본문 추출 실패({url[:50]}): {msg}")
        return None

    summary_prompt = (
        f"다음 자료에서 '{user_query}'와 관련된 핵심 정보만 3~5문장으로 요약해줘.\n"
        f"반드시 한국어로 작성하고, 중요한 수치나 고유명사는 그대로 유지해줘.\n\n"
        f"기사 본문:\n{text[:3000]}"
    )
    summary = _call_fast_model(summary_prompt, budget=budget, quota_manager=quota_manager)

    if not summary:
        fallback = _local_extract_summary(text)
        if fallback:
            return {"url": url, "summary": fallback, "mode": "local"}
        return None

    return {"url": url, "summary": summary, "mode": "llm"}


# ─────────────────────────────────────────────
# async 진입점 (masamong tools_cog에서 호출)
# ─────────────────────────────────────────────

async def run_news_search_pipeline(user_query: str) -> dict[str, Any]:
    """
    마사몽용 비동기 범용 탐색(뉴스/웹/블로그/문서) RAG 파이프라인.
    """
    if not getattr(config, "DDGS_ENABLED", True):
        return {"status": "error", "message": "검색 기능이 비활성화되어 있습니다."}

    logger.info(f"[web_search] 파이프라인 시작: \"{user_query}\"")

    cached = _load_pipeline_cache(user_query)
    if cached:
        cached["cached"] = True
        logger.info("[web_search] 캐시 히트: 외부 검색/LLM 호출을 생략합니다.")
        return cached

    fast_budget = FastLLMBudget(getattr(config, "WEB_RAG_FAST_LLM_MAX_CALLS", 5))
    quota_manager = FastLLMQuotaManager(getattr(config, "DATABASE_FILE", None))

    # ── [NEW] 단계 0: 직접 URL 여부 확인 ──
    # 사용자가 직접 URL을 던진 경우 검색 과정을 건너뛰고 바로 크롤링합니다.
    url_match = re.search(r"https?://[^\s<>\"']+", user_query)
    if url_match:
        target_url = url_match.group()
        logger.info(f"[web_search] 직접 URL 감지: {target_url}")
        
        # 바로 요약 단계로 진행
        res = await asyncio.to_thread(
            _process_single_article,
            target_url,
            user_query,
            "",
            "",
            budget=fast_budget,
            quota_manager=quota_manager,
        )
        if res and res.get("summary"):
            payload = {
                "status": "success",
                "context": f"[직접 링크 분석]\n{res['summary']}",
                "source_urls": [target_url],
                "search_kind": "DIRECT_URL",
                "fast_llm_calls": fast_budget.used_calls,
            }
            _save_pipeline_cache(user_query, payload)
            return payload
        else:
            return {"status": "error", "message": "해당 URL에서 내용을 추출하지 못했습니다."}

    # ── 단계 1: 검색 계획 생성 ──
    plan = await asyncio.to_thread(
        _plan_search,
        user_query,
        budget=fast_budget,
        quota_manager=quota_manager,
    )
    category = plan["category"]
    scope = plan["scope"]
    keywords_ko = plan["keywords_ko"]
    keywords_en = plan["keywords_en"]
    logger.info(f"[web_search] 검색 계획: Category=[{category}], Scope=[{scope}]")
    logger.info(f"[web_search] 생성된 키워드(ko): {keywords_ko}")
    logger.info(f"[web_search] 생성된 키워드(en): {keywords_en}")

    # ── 단계 2: 검색 (NEWS면 뉴스 우선, GENERAL이면 일반 웹 우선) ──
    all_results: list[dict] = []

    async def _perform_search(cat: str, target_scope: str):
        results = []
        if cat == "NEWS":
            if target_scope in ("LOCAL", "BOTH"):
                res, _ = await asyncio.to_thread(_search_news_sync, user_query, "kr-kr", keywords=keywords_ko)
                if res: results.extend(res)
            if target_scope in ("GLOBAL", "BOTH"):
                res, _ = await asyncio.to_thread(_search_news_sync, user_query, "wt-wt", keywords=keywords_en)
                if res: results.extend(res)
        else: # GENERAL
            if target_scope in ("LOCAL", "BOTH"):
                res, _ = await asyncio.to_thread(_search_web_sync, user_query, "kr-kr", keywords=keywords_ko)
                if res: results.extend(res)
            if target_scope in ("GLOBAL", "BOTH"):
                res, _ = await asyncio.to_thread(_search_web_sync, user_query, "wt-wt", keywords=keywords_en)
                if res: results.extend(res)
        return results

    all_results = await _perform_search(category, scope)

    # NEWS 검색 실패 시 GENERAL로 자동 폴백
    if not all_results and category == "NEWS":
        logger.info(f"[web_search] 뉴스 결과 없음 → 일반 웹 검색으로 폴백 시도")
        all_results = await _perform_search("GENERAL", scope)
    elif not all_results and category == "GENERAL":
        logger.info(f"[web_search] 일반 웹 결과 없음 → 뉴스 소스 검색으로 폴백 시도")
        all_results = await _perform_search("NEWS", scope)

    if not all_results:
        return {"status": "error", "message": "관련 정보를 찾지 못했습니다."}

    # 후보 중복 제거 + 정렬/다양성 보강 (도메인 편향 완화)
    deduped_results = _dedupe_by_url(all_results)
    ranked = _rank_candidates(deduped_results)
    candidate_limit = int(getattr(config, "WEB_RAG_MAX_CANDIDATES", 24))
    candidates = _diversify_domains(ranked, candidate_limit)
    if not candidates:
        return {"status": "error", "message": "탐색 가능한 후보를 확보하지 못했습니다."}

    # ── 단계 3: 기사/페이지 선정 ──
    selected_count = int(getattr(config, "WEB_RAG_MAX_SELECTED_URLS", 4))
    best_urls, msg = await asyncio.to_thread(
        _select_best_links,
        user_query,
        candidates,
        selected_count,
        budget=fast_budget,
        quota_manager=quota_manager,
    )
    if not best_urls:
        return {"status": "error", "message": f"정보 선정 실패: {msg}"}

    snippet_map = {r.get("url"): r.get("snippet", "") for r in candidates if r.get("url")}
    title_map = {r.get("url"): r.get("title", "") for r in candidates if r.get("url")}

    # ── 단계 4: 병렬 요약 ──
    max_summarized = int(getattr(config, "WEB_RAG_MAX_SUMMARIZED_ARTICLES", 3))
    target_urls = best_urls[:max_summarized]
    logger.info(f"[web_search] {len(target_urls)}개 페이지 병렬 분석 중... (선정 {len(best_urls)}개 중)")

    def _parallel_summarize() -> list[dict]:
        worker_count = max(1, min(3, len(target_urls)))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [
                executor.submit(
                    _process_single_article,
                    url,
                    user_query,
                    snippet_map.get(url, ""),
                    title_map.get(url, ""),
                    budget=fast_budget,
                    quota_manager=quota_manager,
                )
                for url in target_urls
            ]
            collected: list[dict] = []
            for fut in futures:
                try:
                    result = fut.result()
                except Exception as e:
                    logger.warning(f"[web_search] 병렬 요약 워커 예외: {e}")
                    continue
                if result:
                    collected.append(result)
            return collected

    individual_results = await asyncio.to_thread(_parallel_summarize)

    if not individual_results:
        return {"status": "error", "message": "본문 추출에 모두 실패했습니다."}

    # ── 단계 5: 컨텍스트 문자열 조립 ──
    context_blocks = []
    source_urls = []
    for i, res in enumerate(individual_results, 1):
        if res and res.get("summary"):
            context_blocks.append(f"[검색 출처 {i}]\n{res['summary']}")
            source_urls.append(res["url"])

    if not context_blocks:
        return {"status": "error", "message": "유효한 요약을 생성하지 못했습니다."}

    context = "\n\n".join(context_blocks)

    payload = {
        "status": "success",
        "context": context,
        "source_urls": source_urls,
        "search_kind": category,
        "selection_note": msg,
        "fast_llm_calls": fast_budget.used_calls,
    }
    logger.info(
        "[web_search] 파이프라인 완료. 분석 페이지: %d개, Fast LLM 호출: %d회",
        len(individual_results),
        fast_budget.used_calls,
    )
    _save_pipeline_cache(user_query, payload)
    return payload

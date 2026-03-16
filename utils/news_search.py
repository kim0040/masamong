# -*- coding: utf-8 -*-
"""
utils/news_search.py — 마사몽용 DuckDuckGo 뉴스 RAG 파이프라인

news/news_summarizer.py에서 이식, 마사몽 아키텍처에 맞게 수정:
- synthesize_final_answer() 제거 → ai_handler가 채널 페르소나로 최종 답변 생성
- call_smart_model() 제거 → 마사몽의 CometAPI 클라이언트 사용
- call_fast_model() → 마사몽의 config (COMETAPI_KEY, FAST_MODEL_NAME) 사용
- 동기 함수들을 asyncio.to_thread()로 감싸 async 호환성 확보

[모델]
- Fast: gemini-3.1-flash-lite-preview (의도 분석, 키워드 생성, 기사 요약)
- Final: 마사몽의 기존 모델 (DeepSeek/Gemini) 담당 — 이 파일에서는 처리 안 함
"""
from __future__ import annotations  # Python 3.9 호환: X | Y 타입 힌트 지원

import re
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests
import trafilatura
from ddgs import DDGS
from newspaper import Article
from google import genai

import config
from logger_config import logger

# ─────────────────────────────────────────────
# Fast 모델 클라이언트 (의도 분석 / 키워드 / 기사 요약 전용)
# ─────────────────────────────────────────────
_fast_client: genai.Client | None = None

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


def _call_fast_model(prompt: str) -> str:
    """
    [동기] Fast 모델 호출 (gemini-3.1-flash-lite-preview).
    의도 분석, 키워드 생성, 개별 기사 요약에 사용합니다.
    실패 시 빈 문자열 반환.
    """
    fast_model = getattr(config, "FAST_MODEL_NAME", "gemini-3.1-flash-lite-preview")
    try:
        client = _get_fast_client()
        response = client.models.generate_content(
            model=fast_model,
            contents=prompt,
        )
        return response.text.strip()
    except Exception as e:
        logger.warning(f"[news_search] Fast 모델 호출 실패: {e}")
        return ""


# ─────────────────────────────────────────────
# 기사 본문 추출 (3단계 fallback)
# ─────────────────────────────────────────────

_REQUEST_TIMEOUT = 15
_MIN_ARTICLE_LENGTH = 30


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
        article = Article(url, language="ko")
        article.set_html(html_content)
        article.parse()
        text = article.text.strip()

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

def _analyze_intent(user_query: str) -> str:
    """
    사용자 질문이 국내(LOCAL)/해외(GLOBAL)/양쪽(BOTH) 뉴스를 필요로 하는지 판단합니다.
    """
    prompt = (
        f"다음 질문이 국내 소식에 국한된 것인지, 해외 소식이 중요하거나 함께 봐야 하는지 분석하세요.\n"
        f"반드시 'LOCAL', 'GLOBAL', 'BOTH' 중 하나로만 대답하세요. 다른 텍스트는 일절 출력하지 마세요.\n"
        f"질문: {user_query}"
    )
    result = _call_fast_model(prompt).strip().upper()
    return result if result in ("LOCAL", "GLOBAL", "BOTH") else "BOTH"


def _build_keywords(user_query: str, lang: str) -> list[str]:
    """서로 다른 각도의 검색 키워드를 2개 생성합니다."""
    lang_label = "한국어" if lang == "ko" else "영어"
    prompt = (
        f"당신은 뉴스 검색 전문가입니다. 다음 질문에 대해 최신 뉴스를 찾기 위한 "
        f"검색어 2개를 출력하세요.\n"
        f"[규칙]\n"
        f"- 검색어 1: 핵심 키워드만 추출한 짧은 검색어\n"
        f"- 검색어 2: 관련 배경·맥락을 포함한 확장 검색어\n"
        f"- 출력 언어: {lang_label}\n"
        f"- 따옴표, 번호, 설명 없이 검색어만 한 줄씩 출력\n\n"
        f"질문: {user_query}"
    )
    raw = _call_fast_model(prompt)
    keywords = [line.strip() for line in raw.splitlines() if line.strip()]
    if not keywords:
        return [user_query]
    return keywords[:2]


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


def _search_news_sync(user_query: str, region: str = "kr-kr") -> tuple[list | None, str]:
    """
    [동기] DuckDuckGo News에서 뉴스를 검색합니다 (API 키 불필요, 완전 무료).

    멀티키워드 전략:
    - LLM이 생성한 2가지 검색어로 각각 검색하여 결과를 합산
    - 먼저 최근 1개월(m) 기간으로 검색 → 결과 부족 시 기간 제한 없이 재검색
    """
    lang = "ko" if region == "kr-kr" else "en"
    label = "국내" if region == "kr-kr" else "해외"

    keywords = _build_keywords(user_query, lang)
    logger.info(f"[news_search] [{label}] 검색어: {keywords}")

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
            logger.warning(f"[news_search] DDGS 오류 ({keyword[:30]}): {e}")
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
        logger.info(f"[news_search] [{label}] 결과 부족({len(all_raw)}개) → 기간 제한 없이 재검색")
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
    logger.info(f"[news_search] [{label}] 총 {len(results)}개 후보 확보")
    return results, "성공"


def _select_best_links(user_query: str, search_results: list, count: int = 3) -> tuple[list, str]:
    """LLM이 검색 결과 중 질문에 가장 적합한 URL을 최대 count개 선정합니다."""
    if not search_results:
        return [], "검색 결과가 없습니다."

    candidates_text = _format_ddgs_results_for_llm(search_results)
    select_prompt = (
        f"사용자 질문: \"{user_query}\"\n\n"
        f"아래 뉴스 후보 중 질문에 가장 정확하고 신뢰할 수 있는 기사를 최대 {count}개 선정하세요.\n\n"
        f"선정 기준:\n"
        f"1. 언론사가 명확한 공식 보도 기관 우선 (연합뉴스, Reuters, BBC, 한경 등)\n"
        f"2. 날짜가 최신일수록 우선\n"
        f"3. 제목과 내용이 질문과 직접 관련된 기사 우선\n"
        f"4. 개인 블로그, 커뮤니티, 위키 성격의 URL은 배제\n\n"
        f"출력 형식: 선정한 URL만 한 줄에 하나씩 출력하세요. 다른 텍스트는 출력하지 마세요.\n\n"
        f"후보 목록:\n{candidates_text}"
    )

    response_text = _call_fast_model(select_prompt)
    urls = re.findall(r"https?://[^\s<>\"']+", response_text)
    unique_urls = list(dict.fromkeys(urls))

    if not unique_urls:
        return [], "신뢰할 수 있는 출처를 찾지 못했습니다."

    return unique_urls[:count], "성공"


def _process_single_article(url: str, user_query: str, snippet: str = "") -> dict | None:
    """
    단일 기사 URL에서 본문을 추출하고 Fast 모델로 요약합니다.
    ThreadPoolExecutor에서 각 워커가 독립적으로 실행하는 단위 작업입니다.
    본문 추출 실패 시 DDGS snippet을 fallback으로 활용합니다.
    """
    text, msg = _extract_article_text(url)

    if not text:
        if snippet and len(snippet) >= _MIN_ARTICLE_LENGTH:
            logger.info(f"[news_search] 본문 추출 실패 → snippet 사용: {url[:50]}")
            text = snippet
        else:
            logger.warning(f"[news_search] 본문 추출 실패({url[:50]}): {msg}")
            return None

    summary_prompt = (
        f"다음 기사에서 '{user_query}'와 관련된 핵심 정보만 3~5문장으로 요약해줘.\n"
        f"반드시 한국어로 작성하고, 중요한 수치나 고유명사는 그대로 유지해줘.\n\n"
        f"기사 본문:\n{text[:3000]}"
    )
    summary = _call_fast_model(summary_prompt)

    if not summary:
        return None

    return {"url": url, "summary": summary}


# ─────────────────────────────────────────────
# async 진입점 (masamong tools_cog에서 호출)
# ─────────────────────────────────────────────

async def run_news_search_pipeline(user_query: str) -> dict[str, Any]:
    """
    마사몽용 비동기 뉴스 검색 파이프라인.

    전체 흐름:
      1. 의도 분석 (LOCAL/GLOBAL/BOTH)
      2. DuckDuckGo 뉴스 검색
      3. LLM이 최적 기사 URL 3개 선정
      4. 병렬 본문 추출 + 요약
      5. 요약 컨텍스트 문자열 반환 (최종 답변 합성은 ai_handler 담당)

    Returns:
        {
            "status": "success",
            "context": str,      # 기사 요약들 (프롬프트에 주입될 텍스트)
            "source_urls": list  # 출처 URL 목록
        }
        또는
        {
            "status": "error",
            "message": str
        }
    """
    if not getattr(config, "DDGS_ENABLED", True):
        return {"status": "error", "message": "뉴스 검색 기능이 비활성화되어 있습니다."}

    logger.info(f"[news_search] 파이프라인 시작: \"{user_query}\"")

    # ── 단계 1: 의도 분석 (동기 → async로 감싸기) ──
    intent = await asyncio.to_thread(_analyze_intent, user_query)
    logger.info(f"[news_search] 검색 범위: [{intent}]")

    # ── 단계 2: 검색 ──
    all_results: list[dict] = []

    if intent in ("LOCAL", "BOTH"):
        results_ko, _ = await asyncio.to_thread(_search_news_sync, user_query, "kr-kr")
        if results_ko:
            all_results.extend(results_ko)

    if intent in ("GLOBAL", "BOTH"):
        results_en, _ = await asyncio.to_thread(_search_news_sync, user_query, "wt-wt")
        if results_en:
            all_results.extend(results_en)

    if not all_results:
        return {"status": "error", "message": "관련 뉴스를 찾지 못했습니다."}

    # ── 단계 3: 기사 선정 ──
    best_urls, msg = await asyncio.to_thread(_select_best_links, user_query, all_results, 3)
    if not best_urls:
        return {"status": "error", "message": f"기사 선정 실패: {msg}"}

    snippet_map = {r["url"]: r.get("snippet", "") for r in all_results}

    # ── 단계 4: 병렬 요약 ──
    logger.info(f"[news_search] {len(best_urls)}개 기사 병렬 분석 중...")

    def _parallel_summarize() -> list[dict]:
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                executor.submit(_process_single_article, url, user_query, snippet_map.get(url, ""))
                for url in best_urls
            ]
            return [f.result() for f in futures if f.result()]

    individual_results = await asyncio.to_thread(_parallel_summarize)

    if not individual_results:
        return {"status": "error", "message": "기사 본문 추출에 모두 실패했습니다."}

    # ── 단계 5: 컨텍스트 문자열 조립 (최종 합성은 ai_handler 담당) ──
    context_blocks = []
    source_urls = []
    for i, res in enumerate(individual_results, 1):
        if res and res.get("summary"):
            context_blocks.append(f"[뉴스 출처 {i}]\n{res['summary']}")
            source_urls.append(res["url"])

    if not context_blocks:
        return {"status": "error", "message": "유효한 기사 요약을 생성하지 못했습니다."}

    context = "\n\n".join(context_blocks)

    logger.info(f"[news_search] 파이프라인 완료. 요약 기사: {len(individual_results)}개")
    return {
        "status": "success",
        "context": context,
        "source_urls": source_urls,
    }

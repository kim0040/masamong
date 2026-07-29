# -*- coding: utf-8 -*-
"""
AI 에이전트가 외부 세계와 상호작용하기 위해 사용하는 '도구'들을 모아놓은 Cog입니다.

각 메서드는 특정 작업을 수행하는 도구(Tool)로, AI 핸들러에 의해 호출됩니다.
(예: 날씨 조회, 주식 검색, 카카오 기반 웹/이미지 검색 등)
"""


from __future__ import annotations

import base64
import discord
from discord.ext import commands
import re
import aiohttp
import asyncio
import importlib
import json

import config
from logger_config import logger
from utils.api_handlers import exchange_rate, finnhub, kakao, krx
from utils import db as db_utils
from utils import coords as coords_utils
from utils import weather as weather_utils
from utils.constants import contains_nsfw
from utils.tool_health import ToolHealthRegistry, ToolTemporarilyUnavailable
from .weather_cog import WeatherCog


def is_korean(text: str) -> bool:
    """텍스트에 한글이 포함되어 있는지 확인하는 유틸리티 함수입니다."""
    if not text:
        return False
    return bool(re.search("[\uac00-\ud7a3]", text))

class ToolsCog(commands.Cog):
    """AI 에이전트가 사용할 수 있는 도구(Tool)들의 모음입니다."""
    def __init__(self, bot: commands.Bot):
        """ToolsCog를 초기화하고 지연 로딩용 Lock을 설정합니다."""
        self.bot = bot
        self.weather_cog: WeatherCog = self.bot.get_cog('WeatherCog')
        self._linkup_search_pipeline = None
        self._linkup_search_loader_lock = asyncio.Lock()
        self._news_search_pipeline = None
        self._news_search_loader_lock = asyncio.Lock()
        self._yfinance_handler = None
        self._yfinance_loader_lock = asyncio.Lock()
        self._image_generation_lock = asyncio.Lock()
        self.tool_health = ToolHealthRegistry(
            failure_threshold=config.TOOL_CIRCUIT_FAILURE_THRESHOLD,
            cooldown_seconds=config.TOOL_CIRCUIT_COOLDOWN_SECONDS,
        )
        self._cleanup_tasks: set[asyncio.Task] = set()
        logger.info("ToolsCog가 성공적으로 초기화되었습니다.")

    def is_tool_available(self, tool_name: str) -> bool:
        """라우터가 cooldown 중인 provider를 계획에서 잠시 제외할 때 사용합니다."""
        return self.tool_health.is_available(tool_name)

    @staticmethod
    def _provider_result_failed(tool_name: str, result) -> bool:
        """사용자 입력 부족과 실제 provider 장애를 구분합니다."""
        if tool_name == "get_weather_forecast":
            if isinstance(result, dict):
                return bool(
                    result.get("error")
                    or (
                        result.get("current_weather") == "정보 없음"
                        and not result.get("forecast_items")
                    )
                )
            text = str(result)
            return any(
                marker in text
                for marker in ("모듈이 준비되지", "API 오류", "시간 초과")
            )
        if tool_name == "get_stock_price":
            text = str(result)
            return any(
                marker in text
                for marker in (
                    "조회가 지연되어 취소",
                    "가져오는 중 오류",
                    "API 키",
                    "API 서버",
                    "설정되지 않았",
                )
            )
        if tool_name == "get_market_snapshot":
            return not (
                isinstance(result, dict)
                and result.get("status") == "success"
                and bool(result.get("indices"))
            )
        if tool_name == "search_for_place":
            return "장소 검색 중 오류" in str(result)
        return False

    async def execute_guarded(self, tool_name: str, operation):
        """cooldown·half-open을 적용해 실제 사용자 요청에서만 자동 복구합니다."""
        if not self.tool_health.begin_attempt(tool_name):
            raise ToolTemporarilyUnavailable(tool_name)
        try:
            result = await operation()
        except Exception:
            self.tool_health.record_failure(tool_name)
            raise
        if self._provider_result_failed(tool_name, result):
            self.tool_health.record_failure(tool_name)
        else:
            self.tool_health.record_success(tool_name)
        return result

    def cog_unload(self):
        """공유 HTTP 세션 정리."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(kakao.close_kakao_session())
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    # --- 고수준 메타 도구 --- #

    async def get_current_time(self) -> str:
        """현재 시간과 날짜를 KST 기준 문자열로 반환합니다."""
        return f"현재 시간: {db_utils.get_current_time()}"

    async def get_weather_forecast(self, location: str = None, day_offset: int = 0) -> str:
        """주어진 위치의 날씨 정보를 문자열로 반환합니다."""
        if not self.weather_cog:
            return "날씨 정보 모듈이 준비되지 않았습니다."
        location_name = location or config.DEFAULT_LOCATION_NAME
        
        # Mid-term Forecast (3 ~ 10 days) - V2 (typ01)
        if day_offset >= 3:
            # [NEW] Weekly Weather Logic (Short-term + Mid-term)
            # 1. Short-term (+1, +2 days)
            coords = await coords_utils.get_coords_from_db(self.bot.db, location_name)
            nx, ny = config.DEFAULT_NX, config.DEFAULT_NY
            if coords: 
                nx, ny = str(coords["nx"]), str(coords["ny"])
                
            short_term_data, mid_term_data = await asyncio.gather(
                weather_utils.get_short_term_forecast_from_kma(self.bot.db, nx, ny),
                self.weather_cog.get_mid_term_weather(day_offset, location_name),
            )
            short_term_summary = ""
            if short_term_data and not short_term_data.get("error"):
                 tomorrow_summary = weather_utils.format_short_term_forecast(short_term_data, "내일", 1)
                 dayafter_summary = weather_utils.format_short_term_forecast(short_term_data, "모레", 2)
                 short_term_summary = f"{tomorrow_summary}\n{dayafter_summary}"
            
            return f"--- [단기 예보 (내일/모레)] ---\n{short_term_summary}\n\n--- [중기 예보 (3일 후 ~ 10일 후)] ---\n{mid_term_data}"

        coords = await coords_utils.get_coords_from_db(self.bot.db, location_name)
        if not coords:
            return f"'{location_name}' 지역의 날씨 정보는 아직 알 수 없습니다."
        
        nx, ny = str(coords["nx"]), str(coords["ny"])
        
        # [Refactor] Return Dict for AI Prompt Optimization
        # 1. Current Weather
        current_data, forecast_data = await asyncio.gather(
            weather_utils.get_current_weather_from_kma(self.bot.db, nx, ny),
            weather_utils.get_short_term_forecast_from_kma(self.bot.db, nx, ny),
        )
        current_str = weather_utils.format_current_weather(current_data) if current_data else "정보 없음"
        
        # 2. Short-term Forecast
        items_list = []
        if forecast_data and 'item' in forecast_data:
            items_list = forecast_data['item']
        
        # Return structured data
        return {
            "location": location_name,
            "current_weather": current_str,
            "forecast_items": items_list,
            # Fallback string for legacy handlers (optional, but AI handler looks for dict)
            "summary": f"{location_name} 현재: {current_str}"
        }

    async def _load_yfinance_handler(self):
        """무거운 yfinance/pandas/numpy 계열을 실제 금융 요청 때 한 번만 로드합니다."""
        if self._yfinance_handler is not None:
            return self._yfinance_handler

        async with self._yfinance_loader_lock:
            if self._yfinance_handler is not None:
                return self._yfinance_handler

            self._yfinance_handler = await asyncio.to_thread(
                importlib.import_module,
                "utils.api_handlers.yfinance_handler",
            )
            logger.info("yfinance 핸들러 지연 로딩 완료")
            return self._yfinance_handler

    async def get_stock_price(self, symbol: str = None, stock_name: str = None, user_query: str = None) -> str:
        """
        주식 시세, 기업 정보, 뉴스, 추천 트렌드를 조회합니다. 
        yfinance가 활성화된 경우 이를 우선 사용합니다.
        
        Args:
            symbol (str): (Legacy) 종목명 또는 티커 (예: "삼성전자", "AAPL", "NVDA")
            stock_name (str): (Legacy) symbol의 별칭
            user_query (str): (New) 사용자의 자연어 질문 (yfinance 모드에서 티커 추출에 사용)
        """
        # [NEW] yfinance Integration
        if getattr(config, 'USE_YFINANCE', False):
            # 1. Extract Ticker from LLM (using AIHandler)
            # AIHandler is needed. Since ToolsCog is initialized in AIHandler, we might need a reference or pass logic.
            # But ToolsCog doesn't have reference to ai_handler by default.
            # However, main.py injects ai_handler into FunCog. Let's assume we can get it via bot or pass it.
            # Actually, AIHandler calls this tool.
            
            # Since AIHandler calls this method, we can't easily call back AIHandler methods without circular dependency or injection.
            # But wait, we can just use the provided symbol/stock_name if extraction happened outside, OR 
            # if user_query is provided, we need extraction here.
            
            # Solution: We will inject `ai_handler` reference into ToolsCog during setup in main.py, similar to FunCog.
            # OR, we perform extraction here if we have access.
            
            ai_handler = self.bot.get_cog('AIHandler')
            ticker = None
            
            direct_ticker = str(symbol or "").strip().upper()
            if direct_ticker and re.fullmatch(
                r"[A-Z0-9^][A-Z0-9.^=-]{0,19}",
                direct_ticker,
            ):
                ticker = direct_ticker
            elif user_query and ai_handler:
                logger.info(
                    "yfinance 모드: 티커 추출 시도. query_chars=%d",
                    len(user_query),
                )
                ticker = await ai_handler.extract_ticker_with_llm(user_query)
            elif symbol or stock_name:
                # If direct symbol passed (legacy path), assume it might be a ticker or need extraction check
                # Ideally, extract_ticker_with_llm can handle "삼성전자" too.
                # But for safety, let's treat it as query if it's not a clear ticker.
                candidate = symbol or stock_name
                if ai_handler:
                    ticker = await ai_handler.extract_ticker_with_llm(candidate)
            
            if ticker:
                logger.info(f"yfinance 티커 확정: {ticker}")
                yfinance_handler = await self._load_yfinance_handler()
                data = await yfinance_handler.get_stock_info(ticker)
                
                if "error" in data:
                    return f"'{ticker}' 조회 실패: {data['error']}"
                
                # Format Output
                currency = data.get('currency', 'USD')
                price = data.get('price')
                change_p = data.get('change_percent')
                
                change_str = f"({change_p:+.2f}%)" if change_p is not None else ""
                price_str = f"{price:,.2f} {currency}" if price else "N/A"
                market_cap = data.get("market_cap")
                market_cap_str = f"{market_cap:,} (추정)" if isinstance(market_cap, (int, float)) else "정보 없음"
                industry = data.get("industry") or "정보 없음"
                website = data.get("website")
                
                summary = data.get('summary', '')[:300] + "..." if data.get('summary') else "정보 없음"
                
                result_str = (
                    f"## 📈 {data.get('name')} ({data.get('symbol')})\n"
                    f"- **현재가**: {price_str} {change_str}\n"
                    f"- **시가총액**: {market_cap_str}\n"
                    f"- **산업**: {industry}\n"
                    f"- **개요**: {summary}"
                )
                if website:
                    result_str += f"\n- [더 보기]({website})"
                logger.info(
                    "get_stock_price 결과 생성 완료. result_chars=%d",
                    len(result_str),
                )
                return result_str
            else:
                return "주식 정보를 찾으시는 것 같은데, 정확한 종목을 파악하지 못했어요. '삼성전자 주가 알려줘' 처럼 다시 물어봐주시겠어요?"


        # [Legacy Logic] Finnhub / KRX
        # ... (Existing implementation below)
        target_symbol = symbol or stock_name
        if not target_symbol:
            return "❌ 오류: 조회할 주식 이름이나 티커가 제공되지 않았습니다."

        symbol = target_symbol # Normalize variable name
        
        logger.info(f"주식 정보 조회 실행: '{symbol}'")

        # 1. 국내 주식 (KRX)
        if is_korean(symbol):
             logger.info(f"'{symbol}'은(는) 한글명이므로 KRX API를 호출합니다.")
             krx_result = await krx.get_stock_price(symbol)
             
             # KRX 성공 판단: 에러 메시지가 없어야 함
             # "찾을 수 없습니다", "API 키 미설정", "오류" 등이 포함되면 실패로 간주
             failure_keywords = ["찾을 수 없습니다", "API 키", "오류", "설정되지 않았습니다"]
             if not any(k in krx_result for k in failure_keywords):
                 return krx_result
             
             logger.info(f"KRX에서 '{symbol}' 조회 실패({krx_result}). 해외 주식(Finnhub) 검색으로 전환합니다.")
        
        # 2. 해외 주식 (Finnhub) - Rich Context (or Fallback from KRX)
        # [Rich Context] 4가지 정보를 병렬로 조회
        price_task = finnhub.get_stock_quote(symbol)
        profile_task = finnhub.get_company_profile(symbol)
        news_task = finnhub.get_company_news(symbol, count=3)
        reco_task = finnhub.get_recommendation_trends(symbol)
        
        results = await asyncio.gather(price_task, profile_task, news_task, reco_task, return_exceptions=True)
        price_res, profile_res, news_res, reco_res = results
        
        # Price (필수)
        if isinstance(price_res, str) and "찾을 수 없습니다" in price_res:
             # 만약 KRX에서도 실패했고 Finnhub에서도 실패했다면
             if is_korean(symbol):
                 return f"'{symbol}'에 대한 정보를 국내(KRX) 및 해외(Finnhub) 시장 모두에서 찾을 수 없습니다."
             return price_res # 시세조차 없으면 종료
        
        output_parts = [f"## 💰 시세 정보:\n{price_res}"]

        # Company Profile
        if isinstance(profile_res, dict):
            mcap = f"{profile_res.get('market_cap', 0):,.0f}" if profile_res.get('market_cap') else "N/A"
            profile_str = (f"- 기업명: {profile_res.get('name')}\n"
                           f"- 산업: {profile_res.get('industry')}\n"
                           f"- 시가총액: ${mcap} Million\n"
                           f"- 웹사이트: {profile_res.get('website')}")
            output_parts.append(f"## 🏢 기업 개요:\n{profile_str}")

        # Recommendation Trends
        if isinstance(reco_res, str) and "실패" not in reco_res:
            output_parts.append(f"## 📊 애널리스트 투자의견:\n{reco_res}")

        # News
        if isinstance(news_res, str) and "찾을 수 없습니다" not in news_res:
            output_parts.append(f"## 📰 관련 뉴스:\n{news_res}")
            
        return f"'{symbol}'에 대한 종합 주식 리포트 (Finnhub):\n\n" + "\n\n".join(output_parts)

    async def get_market_snapshot(self, region: str = "global") -> dict:
        """한국·미국 주요 지수를 검증 가능한 구조화 데이터로 반환합니다."""
        yfinance_handler = await self._load_yfinance_handler()
        return await yfinance_handler.get_market_snapshot(region)

    async def get_company_news(self, stock_name: str, count: int = 3) -> str:
        """특정 종목(Ticker Symbol)에 대한 최신 뉴스를 조회합니다."""
        return await finnhub.get_company_news(stock_name, count)

    async def get_krw_exchange_rate(self, currency_code: str = "USD") -> str:
        """특정 통화의 원화(KRW) 대비 환율을 조회합니다."""
        return await exchange_rate.get_krw_exchange_rate(currency_code)

    async def search_for_place(self, query: str, page_size: int = 5) -> str:
        """키워드로 장소를 검색합니다."""
        return await kakao.search_place_by_keyword(query, page_size=page_size)

    async def kakao_web_search(self, query: str) -> str:
        """(폴백용) Kakao API로 웹/블로그/동영상을 검색하고 결과를 요약하여 반환합니다."""
        logger.info("Kakao 통합 검색 실행. query_chars=%d", len(query))
        
        # [Rich Context] 웹, 블로그, 동영상을 병렬로 검색
        web_task = kakao.search_web(query, page_size=5) # 늘어난 limit
        blog_task = kakao.search_blog(query, page_size=3)
        vclip_task = kakao.search_vclip(query, page_size=3)
        
        results = await asyncio.gather(web_task, blog_task, vclip_task, return_exceptions=True)
        web_res, blog_res, vclip_res = results
        
        output_parts = []

        # 1. Web Results
        if isinstance(web_res, list) and web_res:
            formatted = [f"{i}. {r.get('title', '제목 없음').replace('<b>','').replace('</b>','')}\n   - {r.get('contents', '내용 없음').replace('<b>','').replace('</b>','')[:200]}..." for i, r in enumerate(web_res, 1)]
            output_parts.append(f"## 🌐 웹 검색 결과:\n" + "\n".join(formatted))
        
        # 2. Blog Results (Review/Experience)
        if isinstance(blog_res, list) and blog_res:
            formatted = [f"{i}. [블로그] {r.get('title', '').replace('<b>','').replace('</b>','')}\n   - {r.get('blogname', '')}: {r.get('contents', '').replace('<b>','').replace('</b>','')[:200]}..." for i, r in enumerate(blog_res, 1)]
            output_parts.append(f"## 📝 블로그/후기 검색 결과:\n" + "\n".join(formatted))

        # 3. Video Results
        if isinstance(vclip_res, list) and vclip_res:
            formatted = [f"{i}. [영상] {r.get('title', '').replace('<b>','').replace('</b>','')}\n   - {r.get('author', '저자')}: {r.get('url')}" for i, r in enumerate(vclip_res, 1)]
            output_parts.append(f"## 🎬 동영상 검색 결과:\n" + "\n".join(formatted))

        if not output_parts:
            return f"'{query}'에 대한 카카오 검색 결과가 없습니다."
            
        return f"'{query}'에 대한 통합 검색 결과 (Kakao):\n\n" + "\n\n".join(output_parts)

    async def web_search_rag(
        self,
        query: str,
        *,
        guild_id: int | None = None,
        user_id: int | None = None,
    ) -> dict:
        """
        범용 탐색 RAG 파이프라인을 실행합니다.
        - 기본: Linkup (`utils/linkup_search.py`)
        - 폴백: 레거시 DuckDuckGo 파이프라인 (`utils/news_search.py`)

        반환값:
            {
                "status": "success",
                "context": str,        # 탐색 요약 텍스트 (프롬프트에 주입)
                "source_footer": str,  # 출처 URL 목록 (답변 하단에 붙임)
                "source_urls": list
            }
            또는 {"status": "error", "message": str}
        """
        allowed, reason = await db_utils.reserve_web_search_call(
            self.bot.db,
            guild_id=guild_id,
            user_id=user_id,
        )
        if not allowed:
            return {
                "status": "error",
                "message": (
                    f"웹 검색 사용량 한도에 도달했습니다"
                    f" ({reason or '사용량 한도'}). 잠시 후 다시 시도해 주세요."
                ),
                "failure_kind": "rate_limited",
                "fallback_safe": False,
            }

        provider = str(getattr(config, "WEB_SEARCH_PROVIDER", "legacy") or "legacy").strip().lower()
        prefer_linkup = provider == "linkup"

        if prefer_linkup:
            try:
                run_linkup_search_pipeline = await self._load_linkup_search_pipeline()
            except Exception as e:
                # import/로더 단계에서는 provider 호출이 시작되지 않았으므로
                # 기존 레거시 경로로 안전하게 전환할 수 있다.
                logger.warning(
                    "Linkup 파이프라인 로딩 실패로 레거시 파이프라인에 폴백합니다: %s",
                    e,
                )
            else:
                try:
                    logger.info(
                        "웹 검색 RAG(Linkup) 실행. query_chars=%d",
                        len(query),
                    )
                    linkup_result = await run_linkup_search_pipeline(
                        query,
                        db_conn=self.bot.db,
                    )
                except Exception as e:
                    # 실행 계약 밖으로 예외가 새면 provider 호출 여부를 알 수
                    # 없으므로 같은 요청에서 다른 검색/LLM을 연쇄 호출하지 않는다.
                    logger.exception(
                        "Linkup 파이프라인 결과 불명 예외로 추가 외부 검색을 차단합니다."
                    )
                    return {
                        "status": "error",
                        "message": f"Linkup 검색 결과를 확인하지 못했습니다: {e}",
                        "provider": "linkup",
                        "fallback_safe": False,
                        "failure_kind": "provider_outcome_unknown",
                    }

                if linkup_result.get("status") == "success":
                    return linkup_result
                if linkup_result.get("fallback_safe") is False:
                    logger.warning(
                        "Linkup 처리/과금 여부가 불명확해 추가 외부 검색을 차단합니다: %s",
                        linkup_result.get("failure_kind"),
                    )
                    return linkup_result
                logger.info(
                    "Linkup 검색 실패로 레거시 파이프라인 폴백: %s",
                    linkup_result.get("message"),
                )

        try:
            run_news_search_pipeline = await self._load_news_search_pipeline()
            logger.info(
                "웹 검색 RAG(legacy) 실행. query_chars=%d",
                len(query),
            )
            return await run_news_search_pipeline(query)
        except Exception as e:
            logger.error(f"웹 검색 RAG 파이프라인 실행 중 오류: {e}", exc_info=True)
            return {"status": "error", "message": f"외부 검색 중 오류가 발생했습니다: {e}"}

    async def _load_linkup_search_pipeline(self):
        """Linkup 검색 파이프라인 모듈을 지연 import 합니다."""
        if self._linkup_search_pipeline is not None:
            return self._linkup_search_pipeline

        async with self._linkup_search_loader_lock:
            if self._linkup_search_pipeline is not None:
                return self._linkup_search_pipeline

            def _sync_import():
                module = importlib.import_module("utils.linkup_search")
                return module.run_linkup_search_pipeline

            self._linkup_search_pipeline = await asyncio.to_thread(_sync_import)
            logger.info("Linkup 검색 파이프라인 모듈 로딩 완료")
            return self._linkup_search_pipeline

    async def _load_news_search_pipeline(self):
        """레거시 news_search 모듈 import를 이벤트 루프 밖에서 1회 로딩합니다."""
        if self._news_search_pipeline is not None:
            return self._news_search_pipeline

        async with self._news_search_loader_lock:
            if self._news_search_pipeline is not None:
                return self._news_search_pipeline

            def _sync_import():
                module = importlib.import_module("utils.news_search")
                return module.run_news_search_pipeline

            self._news_search_pipeline = await asyncio.to_thread(_sync_import)
            logger.info("웹 검색 RAG 파이프라인 모듈 로딩 완료")
            return self._news_search_pipeline

    async def search_news_rag(self, query: str) -> dict:
        """하위 호환용 별칭. 기존 호출은 web_search_rag()로 위임합니다."""
        return await self.web_search_rag(query)

    async def web_search(
        self,
        query: str,
        *,
        guild_id: int | None = None,
        user_id: int | None = None,
    ) -> str:
        """
        웹 검색을 수행합니다.
        우선 web_search_rag()를 사용하고, 실패 시 Google/Kakao 레거시 경로로 폴백합니다.
        
        우선순위:
          1) web_search_rag() (Linkup 우선 + legacy 폴백 내장)
          2) Google Custom Search API (config.GOOGLE_API_KEY & config.GOOGLE_CX)
          3) kakao_web_search()로 폴백
        """
        logger.info("웹 검색 실행. query_chars=%d", len(query))
        try:
            rag_result = await self.web_search_rag(
                query,
                guild_id=guild_id,
                user_id=user_id,
            )
            if rag_result.get("status") == "success":
                summary = str(rag_result.get("context") or "").strip()
                urls = rag_result.get("source_urls") or []
                lines = [summary] if summary else []
                if isinstance(urls, list) and urls:
                    lines.append("\n[출처]")
                    lines.extend([f"{idx}. {url}" for idx, url in enumerate(urls[:8], 1)])
                if lines:
                    return "\n".join(lines)
            elif rag_result.get("fallback_safe") is False:
                return str(
                    rag_result.get("message")
                    or "외부 검색 처리 결과를 확인하지 못해 추가 검색을 중단했습니다."
                )

            # 1. Google Custom Search API
            if getattr(config, 'GOOGLE_API_KEY', None) and getattr(config, 'GOOGLE_CX', None):
                params = {'key': config.GOOGLE_API_KEY, 'cx': config.GOOGLE_CX, 'q': query, 'num': 3}
                async with aiohttp.ClientSession() as session:
                    async with session.get('https://www.googleapis.com/customsearch/v1', params=params, timeout=10) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            items = data.get('items', [])
                            if not items: return f"'{query}'에 대한 검색 결과가 없습니다. (Google CSE)"
                            formatted = []
                            for i, item in enumerate(items, 1):
                                title = item.get('title', '제목 없음')
                                snippet = item.get('snippet', '').replace('\n', ' ')
                                link = item.get('link')
                                formatted.append(f"{i}. {title}\n   - {snippet}\n   - {link}")
                            return f"'{query}'에 대한 웹 검색 결과 (Google CSE):\n" + "\n\n".join(formatted)
                        else:
                            error_text = await resp.text()
                            logger.warning(
                                "Google CSE API가 오류를 반환했습니다. status=%s response_chars=%d",
                                resp.status,
                                len(error_text),
                            )

            # 3. Kakao Web Search (최후의 폴백)
            logger.info("Google CSE API 실패, Kakao 웹 검색으로 폴백합니다.")
            return await self.kakao_web_search(query)

        except Exception as e:
            logger.exception("웹 검색 중 예외 발생. Kakao 웹 검색으로 폴백합니다.")
            try:
                return await self.kakao_web_search(query)
            except Exception as final_e:
                return f"모든 웹 검색 시도 중 오류가 발생했습니다: {final_e}"

    async def search_images(self, query: str, count: int = 3) -> str:
        """주어진 쿼리로 이미지를 검색하고, 결과 이미지 URL 목록을 문자열로 반환합니다."""
        logger.info("이미지 검색 실행. query_chars=%d", len(query))
        image_results = await kakao.search_image(query, page_size=count)
        if not image_results:
            return f"'{query}'에 대한 이미지를 찾을 수 없습니다."
        
        urls = [result.get('image_url') for result in image_results if result.get('image_url')]
        if not urls:
            return f"'{query}'에 대한 이미지를 찾았지만, 유효한 URL이 없습니다."
        return f"'{query}' 이미지 검색 결과:\n" + "\n".join(urls)

    # --- 이미지 생성 (CometAPI Gemini native) --- #
    
    # 안전 Negative Prompt (이미지 품질 향상용)
    _SAFETY_NEGATIVE_PROMPT = (
        "nsfw, nude, naked, sexual, explicit, "
        "ugly, deformed, blurry, low quality, watermark, signature, "
        "bad anatomy, bad hands, missing fingers, extra limbs"
    )

    def _is_prompt_safe(self, prompt: str) -> tuple[bool, str | None]:
        """프롬프트가 안전한지 확인합니다.
        
        Returns:
            (안전 여부, 감지된 금지어)
        """
        if not prompt:
            return False, "empty_prompt"

        if contains_nsfw(prompt):
            return False, "blocked_content"
        return True, None

    @staticmethod
    def _select_final_inline_image(data: dict) -> dict | None:
        """Gemini 응답에서 사용자에게 보낼 최종 이미지 한 장만 고릅니다.

        Gemini 3 이미지 모델은 한 응답에 중간 사고 이미지와 최종 렌더를
        함께 담을 수 있다. 첫 ``inlineData``를 고르면 미완성 시안이 전송될
        수 있으므로, ``thought``가 아닌 마지막 이미지를 우선하고 없으면
        전체 이미지 중 마지막 항목을 사용한다.
        """
        image_parts: list[tuple[dict, bool]] = []
        for candidate in data.get("candidates", []):
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content")
            if not isinstance(content, dict):
                continue
            for part in content.get("parts", []):
                if not isinstance(part, dict):
                    continue
                inline_data = part.get("inlineData")
                if not isinstance(inline_data, dict):
                    continue
                mime_type = str(
                    inline_data.get("mimeType") or ""
                ).casefold()
                encoded = inline_data.get("data")
                if (
                    isinstance(encoded, str)
                    and encoded
                    and mime_type
                    in {
                        "image/jpeg",
                        "image/png",
                        "image/webp",
                    }
                ):
                    image_parts.append(
                        (
                            {
                                "mime_type": mime_type,
                                "encoded": encoded,
                            },
                            bool(part.get("thought")),
                        )
                    )

        if not image_parts:
            return None

        non_thought_parts = [
            image_part for image_part, is_thought in image_parts
            if not is_thought
        ]
        selected = non_thought_parts[-1] if non_thought_parts else image_parts[-1][0]
        return {
            **selected,
            "image_part_count": len(image_parts),
            "thought_image_count": sum(
                1 for _image_part, is_thought in image_parts if is_thought
            ),
        }

    @staticmethod
    def _image_matches_mime(image_binary: bytes, mime_type: str) -> bool:
        """선언된 MIME과 실제 이미지 헤더가 일치하는지 확인합니다."""
        if mime_type == "image/png":
            return image_binary.startswith(b"\x89PNG\r\n\x1a\n")
        if mime_type == "image/jpeg":
            return image_binary.startswith(b"\xff\xd8\xff")
        if mime_type == "image/webp":
            return (
                len(image_binary) >= 12
                and image_binary.startswith(b"RIFF")
                and image_binary[8:12] == b"WEBP"
            )
        return False

    async def generate_image(
        self,
        prompt: str,
        user_id: int,
        aspect_ratio: str = None,
        guild_id: int | None = None,
    ) -> dict:
        """이미지 생성 전체를 프로세스 단위로 직렬화합니다.

        quota 확인과 실제 API 호출 전 사용량 예약 사이에 다른 요청이
        끼어들지 않아 동시 요청이 사용자/전역 제한을 초과하지 않습니다.
        """
        try:
            await asyncio.wait_for(
                self._image_generation_lock.acquire(),
                timeout=config.IMAGE_GENERATION_QUEUE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return {
                "error": (
                    "지금 다른 이미지를 그리고 있어 대기열이 가득 찼어요. "
                    "잠시 후 다시 시도해줘!"
                )
            }
        try:
            return await self._generate_image_exclusive(
                prompt=prompt,
                user_id=user_id,
                aspect_ratio=aspect_ratio,
                guild_id=guild_id,
            )
        finally:
            self._image_generation_lock.release()

    async def check_image_quota(
        self,
        user_id: int,
        guild_id: int | None = None,
    ) -> dict:
        """이미지 생성 가능 여부를 읽기 전용으로 확인합니다.

        명령 경로는 프롬프트 최적화 LLM 전에 이 메서드로 빠르게 차단하고,
        실제 생성 경로는 전역 lock 안에서 다시 검사합니다.
        """
        user_limited, user_remaining = await db_utils.check_image_user_limit(
            self.bot.db,
            user_id,
        )
        if user_limited:
            reset_hours = getattr(config, "IMAGE_USER_RESET_HOURS", 6)
            return {
                "allowed": False,
                "remaining": 0,
                "error": (
                    f"이미지 생성 제한에 도달했어요. "
                    f"{reset_hours}시간 후에 다시 시도해줘!"
                ),
            }

        global_limited, global_remaining = await db_utils.check_image_global_limit(
            self.bot.db
        )
        if global_limited:
            return {
                "allowed": False,
                "remaining": 0,
                "global_remaining": 0,
                "error": (
                    "오늘 마사몽이 생성할 수 있는 이미지가 다 끝났어... "
                    "내일 다시 불러줘!"
                ),
            }

        guild_limited, guild_remaining = await db_utils.check_image_guild_limit(
            self.bot.db,
            guild_id,
        )
        if guild_limited:
            return {
                "allowed": False,
                "remaining": 0,
                "guild_remaining": 0,
                "error": (
                    "이 서버의 오늘 이미지 생성 한도에 도달했어요. "
                    "내일 다시 시도해 주세요."
                ),
            }

        return {
            "allowed": True,
            "remaining": user_remaining,
            "global_remaining": global_remaining,
            "guild_remaining": guild_remaining,
        }

    async def _generate_image_exclusive(
        self,
        prompt: str,
        user_id: int,
        aspect_ratio: str = None,
        guild_id: int | None = None,
    ) -> dict:
        """이미지 생성 도구입니다.

        CometAPI의 Gemini native image 응답을 사용하며, 호출 전 계층형
        전역·서버·사용자 한도를 원자적으로 예약합니다.

        Args:
            prompt: 이미지 생성 프롬프트 (영문 권장)
            user_id: 요청한 유저 ID (Rate limiting용)
            aspect_ratio: 이미지 비율 (예: "1:1", "16:9" 등). None이면 config 기본값 사용.

        Returns:
            {'image_data': bytes, 'remaining': int} 또는 {'error': str}
        """
        log_extra = {
            'guild_id': guild_id,
            'user_id': user_id,
            'mode': 'image_generation',
            'prompt_chars': len(prompt or ''),
        }

        # 1. 이미지 생성 기능 활성화 확인
        if not getattr(config, 'COMETAPI_IMAGE_ENABLED', False):
            logger.warning("이미지 생성 기능이 비활성화되어 있습니다.", extra=log_extra)
            return {"error": "이미지 생성 기능이 현재 비활성화되어 있어요."}

        # 2. API 키 확인
        api_key = getattr(config, 'COMETAPI_IMAGE_API_KEY', None) or getattr(config, 'COMETAPI_KEY', None)
        if not api_key:
            logger.error("COMETAPI_IMAGE_API_KEY(COMETAPI_KEY fallback 포함)가 설정되지 않았습니다.", extra=log_extra)
            return {"error": "이미지 생성 API 키가 설정되지 않았어요."}

        # 3. 프롬프트 안전성 검사 (NSFW 차단)
        is_safe, blocked_keyword = self._is_prompt_safe(prompt)
        if not is_safe:
            logger.warning(
                "NSFW 프롬프트 차단. matched_category_present=%s",
                bool(blocked_keyword),
                extra=log_extra,
            )
            return {"error": "요청한 이미지를 생성할 수 없어요. 부적절한 내용이 포함되어 있는 것 같아요."}

        # 이 구현은 Gemini native generateContent 응답만 파싱한다. OpenAI 이미지
        # 모델명을 같은 endpoint에 넣으면 404가 나고 사용량만 예약될 수 있으므로
        # provider 호출과 DB 사용량 기록보다 먼저 계약을 검증한다.
        model_name = str(
            getattr(
                config,
                "IMAGE_MODEL",
                "gemini-3.1-flash-lite-image",
            )
        ).strip()
        if model_name != "gemini-3.1-flash-lite-image":
            logger.error(
                "이미지 모델/호출 계약 불일치. configured_model=%s",
                model_name,
                extra=log_extra,
            )
            return {
                "error": (
                    "이미지 모델 설정이 호출 방식과 맞지 않아 생성을 중단했어요. "
                    "이번 요청은 이미지 사용량에 포함되지 않습니다."
                )
            }

        # 4~5. user/global quota를 lock 내부에서 최종 확인한다.
        quota = await self.check_image_quota(user_id, guild_id)
        if not quota.get("allowed"):
            return {"error": str(quota.get("error") or "이미지 생성 제한에 도달했어요.")}
        user_remaining = int(quota.get("remaining") or 0)

        # provider가 실패하거나 빈 응답을 반환해도 실제 비용 시도는 발생한다.
        # API 직전에 먼저 예약하고, 기록 저장소 장애 시에는 호출하지 않는다.
        if not await db_utils.log_image_generation(
            self.bot.db,
            user_id,
            guild_id,
        ):
            return {
                "error": (
                    "이미지 사용량을 안전하게 예약하지 못해 생성을 중단했어요. "
                    "잠시 후 다시 시도해줘!"
                )
            }
        remaining_after_attempt = max(0, user_remaining - 1)

        base_url = str(getattr(config, 'COMETAPI_IMAGE_BASE_URL', 'https://api.cometapi.com')).rstrip("/")
        allowed_ratios = {
            "1:1",
            "2:3",
            "3:2",
            "3:4",
            "4:3",
            "4:5",
            "5:4",
            "9:16",
            "16:9",
            "21:9",
        }
        configured_ratio = str(
            getattr(config, 'IMAGE_ASPECT_RATIO', '1:1')
        ).strip()
        ratio = str(aspect_ratio or configured_ratio).strip()
        if ratio not in allowed_ratios:
            ratio = configured_ratio if configured_ratio in allowed_ratios else "1:1"

        logger.info(
            "이미지 생성 시작 (CometAPI Gemini native): "
            "user=%s, model=%s, ratio=%s, remaining=%s",
            user_id,
            model_name,
            ratio,
            user_remaining,
            extra=log_extra,
        )

        # 6. CometAPI Gemini native generateContent 호출
        try:
            endpoint = (
                f"{base_url}/v1beta/models/{model_name}:generateContent"
            )
            payload = {
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": prompt}],
                    }
                ],
                "generationConfig": {
                    "responseModalities": ["TEXT", "IMAGE"],
                    "imageConfig": {
                        "aspectRatio": ratio,
                        # Lite 모델은 현재 1K 출력 전용이다. 명시해 공급자
                        # 기본값 변경에도 요청 계약을 안정적으로 유지한다.
                        "imageSize": "1K",
                    },
                },
            }
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            }

            timeout = aiohttp.ClientTimeout(
                total=config.IMAGE_GENERATION_TIMEOUT_SECONDS
            )
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(endpoint, json=payload, headers=headers) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error(
                            "CometAPI 이미지 생성 API 오류. status=%s response_chars=%d",
                            resp.status,
                            len(error_text),
                            extra=log_extra,
                        )
                        return {"error": f"API 서버가 오류를 반환했습니다. ({resp.status})"}
                    
                    raw_response = await resp.read()
                    # 12MB 이미지의 base64와 작은 JSON metadata를 포함할
                    # 수 있는 상한이다. 비정상적으로 큰 공급자 응답이 저사양
                    # 프로세스 메모리를 고갈시키지 못하게 먼저 차단한다.
                    if len(raw_response) > 18_000_000:
                        raise ValueError("이미지 API 응답이 허용 크기를 초과했습니다.")
                    data = json.loads(raw_response)
                    # 외부 URL을 재요청하지 않고 Gemini native inlineData만
                    # 허용한다. 중간 사고 이미지가 여러 개면 최종 렌더 1장만
                    # 선택하여 디스코드에 중복 첨부하지 않는다.
                    selected_image = self._select_final_inline_image(data)
                    if not selected_image:
                        raise ValueError(
                            "응답에서 유효한 이미지 데이터를 찾을 수 없습니다."
                        )

                    mime_type = str(selected_image["mime_type"])
                    image_binary = base64.b64decode(
                        selected_image["encoded"],
                        validate=True,
                    )
                    image_part_count = int(
                        selected_image["image_part_count"]
                    )
                    thought_image_count = int(
                        selected_image["thought_image_count"]
                    )
                    if image_part_count > 1:
                        logger.info(
                            "공급자 이미지 파트 %d개 중 최종 1개 선택 "
                            "(중간 사고 이미지=%d)",
                            image_part_count,
                            thought_image_count,
                            extra=log_extra,
                        )

                    if (
                        image_binary
                        and len(image_binary) <= 12_000_000
                        and self._image_matches_mime(image_binary, mime_type)
                    ):
                        logger.info(f"이미지 생성 완료: {len(image_binary):,} bytes (Model: {model_name})", extra=log_extra)
                        return {
                            "image_data": image_binary,
                            "mime_type": mime_type,
                            "remaining": remaining_after_attempt,
                        }
                    raise ValueError(
                        "응답 이미지의 크기 또는 파일 형식이 올바르지 않습니다."
                    )

        except asyncio.TimeoutError:
            logger.error(
                "이미지 API 타임아웃 (%ss)",
                config.IMAGE_GENERATION_TIMEOUT_SECONDS,
                extra=log_extra,
            )
            return {
                "error": (
                    f"이미지 생성이 {config.IMAGE_GENERATION_TIMEOUT_SECONDS}초를 "
                    "초과해 중단되었습니다. 이번 시도는 사용량에 포함됩니다."
                )
            }
        except Exception as e:
            err_msg = str(e)
            logger.error(f"이미지 생성 중 예외: {err_msg}", exc_info=True, extra=log_extra)
            if "SAFETY" in err_msg.upper() or "sensitive" in err_msg.lower():
                return {"error": "요청한 내용을 그릴 수 없어요! 부적절한 내용이 감지되어 차단되었습니다."}
            if "429" in err_msg or "quota" in err_msg.lower():
                return {"error": "이미지 생성 요청이 너무 많아요. 잠시 후에 다시 시도해줘!"}
            return {"error": "이미지 생성 중 예상치 못한 오류가 발생했어요."}


async def setup(bot: commands.Bot):
    """Cog를 봇에 등록하는 함수입니다."""
    await bot.add_cog(ToolsCog(bot))

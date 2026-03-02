# -*- coding: utf-8 -*-
"""
AI 에이전트가 외부 세계와 상호작용하기 위해 사용하는 '도구'들을 모아놓은 Cog입니다.

각 메서드는 특정 작업을 수행하는 도구(Tool)로, AI 핸들러에 의해 호출됩니다.
(예: 날씨 조회, 주식 검색, 카카오 기반 웹/이미지 검색 등)
"""


from __future__ import annotations

import discord
from discord.ext import commands
import re
import aiohttp
import asyncio

import config
from logger_config import logger
from utils.api_handlers import exchange_rate, finnhub, kakao, krx
from utils import db as db_utils
from utils import coords as coords_utils
from utils import weather as weather_utils
from utils.api_handlers import yfinance_handler  # [NEW]
from .weather_cog import WeatherCog

def is_korean(text: str) -> bool:
    """텍스트에 한글이 포함되어 있는지 확인하는 유틸리티 함수입니다."""
    if not text:
        return False
    return bool(re.search("[\uac00-\ud7a3]", text))

class ToolsCog(commands.Cog):
    """AI 에이전트가 사용할 수 있는 도구(Tool)들의 모음입니다."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.weather_cog: WeatherCog = self.bot.get_cog('WeatherCog')
        logger.info("ToolsCog가 성공적으로 초기화되었습니다.")

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
                
            short_term_data = await weather_utils.get_short_term_forecast_from_kma(self.bot.db, nx, ny)
            short_term_summary = ""
            if short_term_data and not short_term_data.get("error"):
                 tomorrow_summary = weather_utils.format_short_term_forecast(short_term_data, "내일", 1)
                 dayafter_summary = weather_utils.format_short_term_forecast(short_term_data, "모레", 2)
                 short_term_summary = f"{tomorrow_summary}\n{dayafter_summary}"
            
            # 2. Mid-term (+3 ~ +10 days)
            mid_term_data = await self.weather_cog.get_mid_term_weather(day_offset, location_name)
            
            return f"--- [단기 예보 (내일/모레)] ---\n{short_term_summary}\n\n--- [중기 예보 (3일 후 ~ 10일 후)] ---\n{mid_term_data}"

        coords = await coords_utils.get_coords_from_db(self.bot.db, location_name)
        if not coords:
            return f"'{location_name}' 지역의 날씨 정보는 아직 알 수 없습니다."
        
        nx, ny = str(coords["nx"]), str(coords["ny"])
        
        # [Refactor] Return Dict for AI Prompt Optimization
        # 1. Current Weather
        current_data = await weather_utils.get_current_weather_from_kma(self.bot.db, nx, ny)
        current_str = weather_utils.format_current_weather(current_data) if current_data else "정보 없음"
        
        # 2. Short-term Forecast
        forecast_data = await weather_utils.get_short_term_forecast_from_kma(self.bot.db, nx, ny)
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
            
            if user_query and ai_handler:
                logger.info(f"yfinance 모드: '{user_query}'에서 티커 추출 시도...")
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
                data = await yfinance_handler.get_stock_info(ticker)
                
                if "error" in data:
                    return f"'{ticker}' 조회 실패: {data['error']}"
                
                # Format Output
                currency = data.get('currency', 'USD')
                price = data.get('price')
                change_p = data.get('change_percent')
                
                change_str = f"({change_p:+.2f}%)" if change_p is not None else ""
                price_str = f"{price:,.2f} {currency}" if price else "N/A"
                
                summary = data.get('summary', '')[:300] + "..." if data.get('summary') else "정보 없음"
                
                result_str = (
                    f"## 📈 {data.get('name')} ({data.get('symbol')})\n"
                    f"- **현재가**: {price_str} {change_str}\n"
                    f"- **시가총액**: {data.get('market_cap'):,} (추정)\n"
                    f"- **산업**: {data.get('industry')}\n"
                    f"- **개요**: {summary}\n"
                    f"- [더 보기]({data.get('website')})"
                )
                logger.info(f"get_stock_price 결과 생성 완료: {result_str[:50]}...")
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
        logger.info(f"Kakao 통합 검색 실행: '{query}'")
        
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

    async def web_search(self, query: str) -> str:
        """
        Google/SerpAPI를 사용하여 웹 검색을 수행하고, 실패 시 Kakao 검색으로 폴백합니다.
        
        우선순위:
          1) Google Custom Search API (config.GOOGLE_API_KEY & config.GOOGLE_CX)
          2) SerpAPI (config.SERPAPI_KEY)
          3) kakao_web_search()로 폴백
        """
        logger.info(f"웹 검색 실행: '{query}'")
        try:
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
                            logger.warning(f"Google CSE API가 오류를 반환했습니다 (상태 코드: {resp.status}): {await resp.text()}")

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
        logger.info(f"이미지 검색 실행: '{query}'")
        image_results = await kakao.search_image(query, page_size=count)
        if not image_results:
            return f"'{query}'에 대한 이미지를 찾을 수 없습니다."
        
        urls = [result.get('image_url') for result in image_results if result.get('image_url')]
        if not urls:
            return f"'{query}'에 대한 이미지를 찾았지만, 유효한 URL이 없습니다."
        return f"'{query}' 이미지 검색 결과:\n" + "\n".join(urls)

    # --- 이미지 생성 (CometAPI flux-2-flex) --- #
    
    # NSFW 차단 키워드 목록 (선정적 콘텐츠만 차단)
    _NSFW_BLOCKED_KEYWORDS = frozenset([
        # 영문 (핵심 선정적 키워드)
        'nude', 'naked', 'nsfw', 'explicit', 'sexual', 'porn', 'pornographic',
        'hentai', 'erotic', 'xxx', 'adult only', 'lewd',
        'topless', 'bottomless', 'genitals', 'nipple',
        'intercourse', 'orgasm', 'fetish', 'bdsm', 'bondage',
        # 영문 (우회 시도)
        'n*de', 'nak3d', 'nud3', 'p0rn', 'pr0n', 's3x', 'seggs',
        'boobs', 'tits', 'titties',
        # 한국어 (핵심)
        '야한', '선정적', '노출', '성인', '음란', '에로', '야동', '포르노',
        '벗은', '알몸', '나체', '누드', '성기', '성관계', '성적',
        '19금', '18금', 'r18', 'r-18',
    ])
    
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
        
        prompt_lower = prompt.lower()
        for keyword in self._NSFW_BLOCKED_KEYWORDS:
            if keyword in prompt_lower:
                return False, keyword
        return True, None

    async def generate_image(self, prompt: str, user_id: int) -> dict:
        """OpenAI-compatible API(CometAPI)를 사용하여 이미지를 생성합니다.
        
        Args:
            prompt: 이미지 생성 프롬프트 (영문 권장)
            user_id: 요청한 유저 ID (Rate limiting용)
            
        Returns:
            {'image_url': str, 'remaining': int} 또는 {'error': str}
        """
        log_extra = {'user_id': user_id, 'prompt_preview': prompt[:100] if prompt else ''}
        
        # 1. 이미지 생성 기능 활성화 확인
        if not getattr(config, 'COMETAPI_IMAGE_ENABLED', False):
            logger.warning("이미지 생성 기능이 비활성화되어 있습니다.", extra=log_extra)
            return {"error": "이미지 생성 기능이 현재 비활성화되어 있어요."}
        
        # 2. API 키 확인
        api_key = getattr(config, 'COMETAPI_KEY', None)
        if not api_key:
            logger.error("COMETAPI_KEY가 설정되지 않았습니다.", extra=log_extra)
            return {"error": "이미지 생성 API 키가 설정되지 않았어요."}
        
        # 3. 프롬프트 안전성 검사 (NSFW 차단) - 로컬 필터 유지
        is_safe, blocked_keyword = self._is_prompt_safe(prompt)
        if not is_safe:
            logger.warning(f"NSFW 프롬프트 차단: '{blocked_keyword}'", extra=log_extra)
            return {"error": "요청한 이미지를 생성할 수 없어요. 부적절한 내용이 포함되어 있는 것 같아요."}
        
        # 4. 유저별 제한 확인
        user_limited, user_remaining = await db_utils.check_image_user_limit(self.bot.db, user_id)
        if user_limited:
            reset_hours = getattr(config, 'IMAGE_USER_RESET_HOURS', 6)
            return {"error": f"이미지 생성 제한에 도달했어요. {reset_hours}시간 후에 다시 시도해줘!"}
        
        # 5. 전역 일일 제한 확인
        global_limited, global_remaining = await db_utils.check_image_global_limit(self.bot.db)
        if global_limited:
            return {"error": "오늘 마사몽이 생성할 수 있는 이미지가 다 끝났어... 내일 다시 불러줘!"}
        
        logger.info(f"이미지 생성 시작 (OpenAI-compatible): user={user_id}, remaining={user_remaining}", extra=log_extra)
        
        # 6. CometAPI 호출 (OpenAI-compatible Endpoint)
        try:
            model_name = getattr(config, 'IMAGE_MODEL', 'doubao-seedream-5-0-260128')
            api_url = getattr(config, 'COMETAPI_IMAGE_API_URL', 'https://api.cometapi.com/v1/images/generations')
            image_size = getattr(config, 'IMAGE_SIZE', '4K')
            response_format = getattr(config, 'IMAGE_RESPONSE_FORMAT', 'url')
            
            # 헤더 설정
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            
            # 페이로드 구성
            payload = {
                "model": model_name,
                "prompt": prompt,
                "size": image_size,
                "response_format": response_format,
                "watermark": False
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(api_url, headers=headers, json=payload, timeout=90) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error(f"이미지 생성 실패 ({resp.status}): {error_text}", extra=log_extra)
                        if resp.status == 429:
                           return {"error": "이미지 생성 요청이 너무 많아요. 잠시 후에 다시 시도해줘!"}
                        return {"error": f"이미지 생성 요청 실패: {resp.status}"}
                    
                    data = await resp.json()
                    
                    # 응답 파싱 (OpenAI API 형식)
                    images = data.get('data', [])
                    if not images:
                        logger.error(f"이미지 데이터를 찾을 수 없음: {data}", extra=log_extra)
                        return {"error": "이미지 데이터를 받지 못했어요."}
                    
                    image_data = images[0]
                    
                    # 사용량 기록
                    await db_utils.log_image_generation(self.bot.db, user_id)
                    
                    if response_format == 'b64_json' and 'b64_json' in image_data:
                        import base64
                        image_binary = base64.b64decode(image_data['b64_json'])
                        logger.info(f"이미지 디코딩 완료: {len(image_binary)} bytes", extra=log_extra)
                        return {
                            "image_data": image_binary,
                            "remaining": user_remaining - 1,
                        }
                    elif 'url' in image_data:
                        logger.info(f"이미지 URL 수신: {image_data['url'][:50]}...", extra=log_extra)
                        return {
                            "image_url": image_data['url'],
                            "remaining": user_remaining - 1,
                        }
                    else:
                        logger.error(f"지원하지 않는 이미지 응답 형식: {image_data.keys()}", extra=log_extra)
                        return {"error": "알 수 없는 이미지 형식을 받았어요."}

        except asyncio.TimeoutError:
            logger.error("이미지 API 타임아웃", extra=log_extra)
            return {"error": "이미지 생성이 너무 오래 걸려서 취소됐어. 다시 시도해줘!"}
        except Exception as e:
            logger.error(f"이미지 생성 중 예외: {e}", exc_info=True, extra=log_extra)
            return {"error": "이미지 생성 중 예상치 못한 오류가 발생했어요."}


async def setup(bot: commands.Bot):
    """Cog를 봇에 등록하는 함수입니다."""
    await bot.add_cog(ToolsCog(bot))

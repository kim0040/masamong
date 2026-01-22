# -*- coding: utf-8 -*-
"""
AI 에이전트가 외부 세계와 상호작용하기 위해 사용하는 '도구'들을 모아놓은 Cog입니다.

각 메서드는 특정 작업을 수행하는 도구(Tool)로, AI 핸들러에 의해 호출됩니다.
(예: 날씨 조회, 주식 검색, 카카오 기반 웹/이미지 검색 등)
"""

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
        coords = await coords_utils.get_coords_from_db(self.bot.db, location_name)
        if not coords:
            return f"'{location_name}' 지역의 날씨 정보는 아직 알 수 없습니다."
        weather_data, error_msg = await self.weather_cog.get_formatted_weather_string(day_offset, location_name, str(coords["nx"]), str(coords["ny"]))
        return weather_data if weather_data else error_msg

    async def get_stock_price(self, stock_name: str) -> str:
        """주식명을 기반으로 국내/해외 주식 시세를 조회합니다. 한글 포함 여부로 국내/해외를 구분합니다."""
        if is_korean(stock_name):
            logger.info(f"'{stock_name}'은(는) 한글명이므로 KRX API를 호출합니다.")
            return await krx.get_stock_price(stock_name)
        else:
            logger.info(f"'{stock_name}'은(는) Ticker이므로 Finnhub API를 호출합니다.")
            return await finnhub.get_stock_quote(stock_name)

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
        """(폴백용) Kakao API로 웹을 검색하고 결과를 요약하여 반환합니다."""
        logger.info(f"Kakao 웹 검색 실행: '{query}'")
        search_results = await kakao.search_web(query, page_size=3)
        if not search_results:
            return f"'{query}'에 대한 웹 검색 결과가 없습니다."
        
        formatted = [f"{i}. {r.get('title', '제목 없음').replace('<b>','').replace('</b>','')}\n   - {r.get('contents', '내용 없음').replace('<b>','').replace('</b>','')[:250]}..." for i, r in enumerate(search_results, 1)]
        return f"'{query}'에 대한 웹 검색 결과 요약:\n" + "\n".join(formatted)

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
        """CometAPI flux-2-flex를 사용하여 이미지를 생성합니다.
        
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
        
        # 3. 프롬프트 안전성 검사 (NSFW 차단)
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
        
        logger.info(f"이미지 생성 시작: user={user_id}, remaining={user_remaining}", extra=log_extra)
        
        # 6. CometAPI 호출 (폴링 방식)
        try:
            api_url = getattr(config, 'COMETAPI_IMAGE_API_URL', 'https://api.cometapi.com/flux/v1/flux-2-flex')
            
            # Bearer 토큰 형식으로 Authorization 헤더 설정
            headers = {
                "Authorization": f"Bearer {api_key}" if not api_key.startswith("Bearer ") else api_key,
                "Content-Type": "application/json",
                "Accept": "*/*",
            }
            
            payload = {
                "prompt": prompt,
                "prompt_upsampling": True,
                "width": getattr(config, 'IMAGE_DEFAULT_WIDTH', 768),
                "height": getattr(config, 'IMAGE_DEFAULT_HEIGHT', 768),
                "steps": getattr(config, 'IMAGE_GENERATION_STEPS', 28),
                "guidance": getattr(config, 'IMAGE_GUIDANCE_SCALE', 4.5),
                "safety_tolerance": getattr(config, 'IMAGE_SAFETY_TOLERANCE', 0),
                "output_format": "jpeg",
            }
            
            async with aiohttp.ClientSession() as session:
                # Step 1: 이미지 생성 요청 제출
                async with session.post(api_url, headers=headers, json=payload, timeout=60) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error(f"CometAPI 이미지 생성 요청 실패 ({resp.status}): {error_text}", extra=log_extra)
                        if resp.status == 402:
                            return {"error": "이미지 생성 크레딧이 부족해요. 관리자에게 문의해줘!"}
                        return {"error": "이미지 생성 요청에 실패했어요. 잠시 후 다시 시도해줘!"}
                    
                    data = await resp.json()
                    task_id = data.get('id')
                    
                    if not task_id:
                        logger.error(f"Task ID를 받지 못함: {data}", extra=log_extra)
                        return {"error": "이미지 생성 작업 ID를 받지 못했어요."}
                    
                    logger.info(f"이미지 생성 Task ID: {task_id}", extra=log_extra)
                
                # Step 2: 결과 폴링 (최대 60초 대기)
                result_url = f"https://api.cometapi.com/flux/v1/get_result?id={task_id}"
                max_polls = 30  # 2초 간격으로 30회 = 60초
                image_url = None
                
                for poll_count in range(max_polls):
                    await asyncio.sleep(2)  # 2초 대기
                    
                    async with session.get(result_url, headers=headers, timeout=30) as poll_resp:
                        if poll_resp.status != 200:
                            continue
                        
                        poll_data = await poll_resp.json()
                        status = poll_data.get('status', '')
                        
                        if status == 'Ready':
                            # 이미지 생성 완료
                            image_url = poll_data.get('result', {}).get('sample')
                            if image_url:
                                logger.info(f"이미지 폴링 완료 ({poll_count+1}회): {image_url[:80]}...", extra=log_extra)
                                break
                        elif status == 'Error':
                            error_msg = poll_data.get('result', {}).get('message', '알 수 없는 오류')
                            logger.error(f"이미지 생성 에러: {error_msg}", extra=log_extra)
                            return {"error": f"이미지 생성 중 오류: {error_msg}"}
                        # Pending 상태면 계속 폴링
                
                if not image_url:
                    logger.error("이미지 폴링 타임아웃", extra=log_extra)
                    return {"error": "이미지 생성이 너무 오래 걸려요. 다시 시도해줘!"}
                
                # Step 3: 이미지 다운로드 (Discord 업로드용)
                async with session.get(image_url, timeout=30) as img_resp:
                    if img_resp.status != 200:
                        logger.warning(f"이미지 다운로드 실패, URL 직접 반환: {img_resp.status}", extra=log_extra)
                        # 다운로드 실패 시 URL만 반환
                        await db_utils.log_image_generation(self.bot.db, user_id)
                        return {
                            "image_url": image_url,
                            "remaining": user_remaining - 1,
                        }
                    
                    image_data = await img_resp.read()
                    
                    # 사용량 기록
                    await db_utils.log_image_generation(self.bot.db, user_id)
                    
                    logger.info(f"이미지 다운로드 완료: {len(image_data)} bytes", extra=log_extra)
                    return {
                        "image_data": image_data,  # 바이너리 데이터
                        "image_url": image_url,    # 백업용 URL
                        "remaining": user_remaining - 1,
                    }
                        
        except asyncio.TimeoutError:
            logger.error("CometAPI 타임아웃", extra=log_extra)
            return {"error": "이미지 생성이 너무 오래 걸려서 취소됐어. 다시 시도해줘!"}
        except Exception as e:
            logger.error(f"이미지 생성 중 예외: {e}", exc_info=True, extra=log_extra)
            return {"error": "이미지 생성 중 예상치 못한 오류가 발생했어요."}


async def setup(bot: commands.Bot):
    """Cog를 봇에 등록하는 함수입니다."""
    await bot.add_cog(ToolsCog(bot))

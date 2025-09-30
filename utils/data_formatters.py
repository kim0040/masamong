# -*- coding: utf-8 -*-
"""
다양한 API로부터 받은 원본 데이터를 LLM이나 사용자가 이해하기 쉬운
형식의 텍스트로 가공하는 포맷터 클래스들을 제공합니다.
"""

from typing import Dict, List, Any
from logger_config import logger

class WeatherDataFormatter:
    """기상청 API의 날씨 데이터를 사람이 읽기 좋은 문자열로 변환합니다."""
    
    @staticmethod
    def format_current_weather(raw_data: Dict[str, Any]) -> str:
        """초단기실황(current weather) 데이터를 요약 문자열로 변환합니다."""
        if not raw_data or not raw_data.get('item'):
            return "현재 날씨 정보를 가져올 수 없습니다."
        
        try:
            weather_values = {item['category']: item['obsrValue'] for item in raw_data['item']}
            temp = weather_values.get('T1H', 'N/A')
            reh = weather_values.get('REH', 'N/A')
            wsd = weather_values.get('WSD', 'N/A')
            vec = weather_values.get('VEC', 'N/A')
            pty_code = weather_values.get('PTY', '0')
            rn1 = weather_values.get('RN1', '0')
            
            if 'N/A' in [temp, reh, wsd, vec]:
                return "현재 날씨 정보가 불완전합니다."
            
            pty_map = {"0": "없음", "1": "비", "2": "비/눈", "3": "눈", "5": "빗방울", "6": "빗방울/눈날림", "7": "눈날림"}
            pty = pty_map.get(pty_code, "정보 없음")
            wind_dir = WeatherDataFormatter._get_wind_direction(float(vec))
            
            result = f"🌡️ 기온: {temp}°C, 💧 습도: {reh}%"
            if pty != "없음":
                result += f", ☔ 강수: {pty} (시간당 {rn1}mm)"

            wind_speed = float(wsd)
            if wind_speed < 1: wind_desc = "바람 없음"
            elif wind_speed < 4: wind_desc = "약한 바람"
            elif wind_speed < 8: wind_desc = "보통 바람"
            else: wind_desc = "강한 바람"
            result += f", 💨 바람: {wind_dir} {wsd}m/s ({wind_desc})"
            
            return result
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"날씨 데이터 포맷팅 오류: {e}", exc_info=True)
            return "날씨 데이터 처리 중 오류가 발생했습니다."
    
    @staticmethod
    def format_forecast(raw_data: Dict[str, Any], day_name: str = "오늘") -> str:
        """단기예보 데이터를 사람이 읽기 쉬운 형태로 변환합니다."""
        if not raw_data or not raw_data.get('item'):
            return f"{day_name} 날씨 예보를 가져올 수 없습니다."
        
        try:
            items = raw_data['item']
            min_temps = [float(item['fcstValue']) for item in items if item['category'] == 'TMN']
            max_temps = [float(item['fcstValue']) for item in items if item['category'] == 'TMX']
            min_temp = min(min_temps) if min_temps else None
            max_temp = max(max_temps) if max_temps else None
            
            sky_item = next((item for item in items if item['category'] == 'SKY' and item['fcstTime'] == '1200'), None)
            if not sky_item:
                sky_item = next((item for item in items if item['category'] == 'SKY'), None)
            
            sky_map = {"1": "맑음☀️", "3": "구름많음☁️", "4": "흐림🌥️"}
            sky_condition = sky_map.get(sky_item['fcstValue'], "정보없음") if sky_item else "정보없음"
            
            pops = [int(item['fcstValue']) for item in items if item['category'] == 'POP']
            max_pop = max(pops) if pops else 0
            
            result = f"{day_name} 날씨: "
            if min_temp is not None and max_temp is not None:
                result += f"🌡️ 기온 {min_temp:.1f}°C ~ {max_temp:.1f}°C"
            result += f", 하늘: {sky_condition}, 강수확률: ~{max_pop}%"
            
            return result
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"예보 데이터 포맷팅 오류: {e}", exc_info=True)
            return f"{day_name} 날씨 예보 처리 중 오류가 발생했습니다."
    
    @staticmethod
    def _get_wind_direction(vec_value: float) -> str:
        """풍향 각도를 16방위 문자열(북, 북북동 등)로 변환하는 내부 헬퍼 함수입니다."""
        angles = ["북", "북북동", "북동", "동북동", "동", "동남동", "남동", "남남동", "남", "남남서", "남서", "서남서", "서", "서북서", "북서", "북북서"]
        index = round(vec_value / 22.5) % 16
        return angles[index]

class FinancialDataFormatter:
    """금융 관련 API(환율, 주식) 데이터를 사람이 읽기 좋은 문자열로 변환합니다."""
    
    @staticmethod
    def format_exchange_rate(raw_data: List[Dict[str, Any]], target_currency: str = "USD") -> str:
        """한국수출입은행의 환율 데이터를 요약 문자열로 변환합니다."""
        if not raw_data:
            return f"{target_currency} 환율 정보를 가져올 수 없습니다."
        
        try:
            for rate_info in raw_data:
                if rate_info.get('cur_unit') == target_currency.upper():
                    currency_name = rate_info.get('cur_nm', '알 수 없음')
                    deal_rate = float(rate_info.get('deal_bas_r', '0').replace(',', ''))
                    ttb = float(rate_info.get('ttb', '0').replace(',', ''))
                    tts = float(rate_info.get('tts', '0').replace(',', ''))
                    
                    result = f"💰 {target_currency.upper()} → KRW 환율 정보\n"
                    result += f"• 매매기준율: {deal_rate:,.2f}원 ({currency_name})\n"
                    if ttb > 0 and tts > 0:
                        result += f"• 현찰 살 때(TTB): {ttb:,.2f}원\n"
                        result += f"• 현찰 팔 때(TTS): {tts:,.2f}원\n"
                        result += f"• 스프레드: {tts-ttb:,.2f}원 ({((tts-ttb)/deal_rate*100):.2f}%)"
                    return result
            return f"❌ {target_currency} 통화를 찾을 수 없습니다."
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"환율 데이터 포맷팅 오류: {e}", exc_info=True)
            return "환율 데이터 처리 중 오류가 발생했습니다."
    
    @staticmethod
    def format_stock_data(raw_data: Dict[str, Any], stock_name: str) -> str:
        """Finnhub 또는 KRX의 주식 데이터를 요약 문자열로 변환합니다."""
        if not raw_data or 'error' in raw_data:
            return f"{stock_name} 주식 정보를 가져올 수 없습니다."
        
        try:
            if 'c' in raw_data:  # Finnhub format
                current_price = raw_data.get('c', 0)
                change = raw_data.get('d', 0)
                change_percent = raw_data.get('dp', 0)
                high = raw_data.get('h', 0)
                low = raw_data.get('l', 0)
                open_price = raw_data.get('o', 0)
                result = f"📈 {stock_name} 주식 정보\n"
                result += f"• 현재가: ${current_price:.2f}\n"
                result += f"• 변동: {change:+.2f} ({change_percent:+.2f}%)\n"
                result += f"• 고가: ${high:.2f}, 저가: ${low:.2f}\n"
                result += f"• 시가: ${open_price:.2f}"
                return result
            elif 'output' in raw_data:
                if isinstance(raw_data['output'], list) and raw_data['output']:
                    stock_info = raw_data['output'][0]
                    result = f"📈 {stock_name} 주식 정보\n"
                    result += f"• 현재가: {stock_info.get('stck_prpr', 'N/A')}원\n"
                    result += f"• 변동: {stock_info.get('prdy_vrss', 'N/A')}원\n"
                    result += f"• 변동률: {stock_info.get('prdy_ctrt', 'N/A')}%"
                    return result
            return f"{stock_name} 주식 데이터 형식을 인식할 수 없습니다."
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"주식 데이터 포맷팅 오류: {e}", exc_info=True)
            return "주식 데이터 처리 중 오류가 발생했습니다."

class GameDataFormatter:
    """게임 데이터를 LLM 친화적인 형태로 변환합니다."""
    
    @staticmethod
    def format_game_recommendation(raw_data: Dict[str, Any]) -> str:
        """게임 추천 데이터를 사람이 읽기 쉬운 형태로 변환합니다."""
        if not raw_data or 'results' not in raw_data:
            return "게임 추천 정보를 가져올 수 없습니다."
        
        try:
            games = raw_data['results'][:5]
            result = "🎮 추천 게임 목록\n\n"
            for i, game in enumerate(games, 1):
                name = game.get('name', '알 수 없음')
                released = game.get('released', 'N/A')
                rating = game.get('rating', 0)
                playtime = game.get('playtime', 0)
                metacritic = game.get('metacritic', 0)
                genres = [genre.get('name', '') for genre in game.get('genres', [])]
                genre_str = ', '.join(genres[:3]) if genres else 'N/A'
                platforms = [p.get('platform', {}).get('name', '') for p in game.get('platforms', [])]
                platform_str = ', '.join(platforms[:3]) if platforms else 'N/A'
                
                result += f"{i}. **{name}**\n"
                result += f"   • 출시일: {released}\n"
                result += f"   • 평점: {rating:.1f}/5.0"
                if metacritic > 0: result += f" (메타크리틱: {metacritic}/100)"
                result += f"\n   • 평균 플레이타임: {playtime}시간\n"
                if metacritic > 85: result += f"   • 품질: 최고 등급 🏆\n"
                elif metacritic > 70: result += f"   • 품질: 우수 등급 ⭐\n"
                result += f"   • 장르: {genre_str}\n"
                result += f"   • 플랫폼: {platform_str}\n\n"
            return result.strip()
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"게임 데이터 포맷팅 오류: {e}", exc_info=True)
            return "게임 데이터 처리 중 오류가 발생했습니다."

class TravelDataFormatter:
    """여행 데이터를 LLM 친화적인 형태로 변환합니다."""
    
    @staticmethod
    def format_places(raw_data: Dict[str, Any]) -> str:
        """장소 데이터를 사람이 읽기 쉬운 형태로 변환합니다."""
        if not raw_data or 'places' not in raw_data:
            return "장소 정보를 가져올 수 없습니다."
        
        try:
            places = raw_data['places'][:5]
            result = "📍 추천 장소\n\n"
            for i, place in enumerate(places, 1):
                name = place.get('name', '알 수 없음')
                category = place.get('categories', [{}])[0].get('name', 'N/A')
                distance = place.get('distance', 0)
                address = place.get('location', {}).get('formatted_address', 'N/A')
                result += f"{i}. **{name}**\n"
                result += f"   • 카테고리: {category}\n"
                result += f"   • 거리: {distance:.1f}m\n"
                result += f"   • 주소: {address}\n\n"
            return result.strip()
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"장소 데이터 포맷팅 오류: {e}", exc_info=True)
            return "장소 데이터 처리 중 오류가 발생했습니다."
    
    @staticmethod
    def format_events(raw_data: Dict[str, Any]) -> str:
        """이벤트 데이터를 사람이 읽기 쉬운 형태로 변환합니다."""
        if not raw_data or 'events' not in raw_data:
            return "이벤트 정보를 가져올 수 없습니다."
        
        try:
            events = raw_data['events'][:5]
            result = "🎪 주변 이벤트\n\n"
            for i, event in enumerate(events, 1):
                name = event.get('name', '알 수 없음')
                event_type = event.get('type', 'N/A')
                start_date = event.get('start_date', 'N/A')
                genre = event.get('genre', 'N/A')
                venue = event.get('venue', 'N/A')
                url = event.get('url', '')
                result += f"{i}. **{name}**\n"
                result += f"   • 유형: {event_type}\n"
                result += f"   • 날짜: {start_date}\n"
                result += f"   • 장르: {genre}\n"
                result += f"   • 장소: {venue}\n"
                if url: result += f"   • 링크: {url}\n"
                result += "\n"
            return result.strip()
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"이벤트 데이터 포맷팅 오류: {e}", exc_info=True)
            return "이벤트 데이터 처리 중 오류가 발생했습니다."
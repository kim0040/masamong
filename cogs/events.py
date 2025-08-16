# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
import re
import random

import config
from logger_config import logger, discord_log_handler
import utils
from .ai_handler import AIHandler
from .weather_cog import WeatherCog
from .fun_cog import FunCog
from .activity_cog import ActivityCog

class EventListeners(commands.Cog):
    """Discord 이벤트 리스너 (모든 상호작용의 시작점)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ai_handler: AIHandler | None = None
        self.weather_cog: WeatherCog | None = None
        self.fun_cog: FunCog | None = None
        self.activity_cog: ActivityCog | None = None

    @commands.Cog.listener()
    async def on_ready(self):
        discord_log_handler.set_bot(self.bot, config.DISCORD_LOG_CHANNEL_ID)
        logger.info(f'봇 준비 완료: {self.bot.user.name} (ID: {self.bot.user.id})')
        
        self.ai_handler = self.bot.get_cog('AIHandler')
        self.weather_cog = self.bot.get_cog('WeatherCog')
        self.fun_cog = self.bot.get_cog('FunCog')
        self.activity_cog = self.bot.get_cog('ActivityCog')

        if self.ai_handler:
            if self.fun_cog: self.fun_cog.ai_handler = self.ai_handler
            if self.activity_cog: self.activity_cog.ai_handler = self.ai_handler
            if self.weather_cog: self.weather_cog.ai_handler = self.ai_handler
            logger.info("모든 Cog에 AIHandler 의존성 주입 완료.")
        else:
            logger.error("AIHandler를 찾을 수 없어 의존성 주입 실패.")
        
        if self.weather_cog:
            self.weather_cog.setup_and_start_loops()

    async def _handle_keyword_triggers(self, message: discord.Message) -> bool:
        if self.fun_cog and config.FUN_KEYWORD_TRIGGERS.get("enabled"):
            if not self.fun_cog.is_on_cooldown(message.channel.id):
                msg_content = message.content.lower()
                for trigger_type, keywords in config.FUN_KEYWORD_TRIGGERS["triggers"].items():
                    for keyword in keywords:
                        if keyword in msg_content:
                            logger.info(f"FunCog '{trigger_type}' 키워드 감지 ('{keyword}')")
                            self.fun_cog.update_cooldown(message.channel.id)
                            if trigger_type == "summarize":
                                await self.fun_cog.execute_summarize(message.channel, message.author)
                            elif trigger_type == "fortune":
                                await self.fun_cog.execute_fortune(message.channel, message.author)
                            return True
        return False

    async def _handle_ai_interaction(self, message: discord.Message):
        logger.debug("--- AI 상호작용 처리 시작 ---")
        if not self.ai_handler or not self.ai_handler.is_ready:
            logger.debug("AI 핸들러가 준비되지 않아 처리 중단.")
            return

        channel_config = config.CHANNEL_AI_CONFIG.get(message.channel.id)
        if not (channel_config): # 'allowed' 키 대신, 설정 자체가 있는지 확인
            logger.debug(f"채널({message.channel.id})은 AI 설정이 없으므로 처리 중단.")
            return

        is_bot_mentioned = self.bot.user.mentioned_in(message)
        logger.debug(f"봇 멘션 여부: {is_bot_mentioned}")
        
        proactive_config = config.AI_PROACTIVE_RESPONSE_CONFIG
        
        proactive_keywords_found = any(keyword in message.content.lower() for keyword in proactive_config["keywords"])
        proactive_cooldown_ok = not self.ai_handler.is_proactive_on_cooldown(message.channel.id)
        proactive_probability_ok = random.random() < proactive_config["probability"]
        
        should_consider_proactive = (
            proactive_config["enabled"] and
            not is_bot_mentioned and
            proactive_keywords_found and
            proactive_cooldown_ok and
            proactive_probability_ok
        )
        logger.debug(f"자발적 응답 고려 여부: {should_consider_proactive} (활성화:{proactive_config['enabled']}, 키워드:{proactive_keywords_found}, 쿨다운:{proactive_cooldown_ok}, 확률:{proactive_probability_ok})")

        if not is_bot_mentioned and not should_consider_proactive:
            logger.debug("멘션도, 자발적 응답 조건도 아니므로 처리 중단.")
            return
            
        logger.debug("의도 분석 시작...")
        intent = await self.ai_handler.analyze_intent(message)
        logger.info(f"AI 의도 분석 결과: '{intent}' (메시지: '{message.content[:50]}...')")

        # 멘션되었거나, 자발적 응답 조건이 충족되었을 때만 아래 로직 실행
        if not (is_bot_mentioned or should_consider_proactive):
             logger.debug("AI 상호작용 조건 미충족으로 처리 종료.")
             return

        if intent == 'Weather':
            if not self.weather_cog:
                logger.error("WeatherCog가 로드되지 않아 날씨 의도를 처리할 수 없습니다.")
                return

            logger.debug("날씨 의도로 판단됨. 날씨 정보 조회를 시작합니다.")
            query_for_loc_check = message.content.lower()
            location_name = config.DEFAULT_LOCATION_NAME
            nx, ny = config.DEFAULT_NX, config.DEFAULT_NY

            sorted_locations = sorted(config.LOCATION_COORDINATES.keys(), key=len, reverse=True)
            for loc_key in sorted_locations:
                if loc_key in query_for_loc_check:
                    location_name = loc_key
                    coords = config.LOCATION_COORDINATES[location_name]
                    nx, ny = str(coords["nx"]), str(coords["ny"])
                    logger.info(f"날씨 의도 분석: 지역 감지 - {location_name} (nx: {nx}, ny: {ny})")
                    break

            day_offset = 0
            if "모레" in query_for_loc_check: day_offset = 2
            elif "내일" in query_for_loc_check: day_offset = 1

            await self.weather_cog.prepare_weather_response_for_ai(message, day_offset, location_name, nx, ny, message.content)

        elif intent in ['Chat', 'Mixed']:
            logger.debug(f"'{intent}' 의도로 판단됨. AI 채팅 응답 처리를 시작합니다.")
            if is_bot_mentioned:
                logger.debug("멘션이 확인되어 AI 응답 생성을 요청합니다.")
                await self.ai_handler.process_ai_message(message)
            elif should_consider_proactive:
                logger.debug("자발적 응답 조건 충족. 추가 판단 시작...")
                if await self.ai_handler.should_proactively_respond(message):
                    logger.debug("자발적 응답 최종 결정. AI 응답 생성을 요청합니다.")
                    self.ai_handler.update_proactive_cooldown(message.channel.id)
                    await self.ai_handler.process_ai_message(message)
                else:
                    logger.debug("자발적 응답 최종 판단 결과: 응답하지 않음.")

        elif intent == 'Command':
             logger.debug("명령어 의도로 판단되었으나, on_message에서 이미 bot.process_commands()가 처리하므로 여기서는 별도 작업을 수행하지 않습니다.")

        else:
            logger.warning(f"알 수 없는 의도('{intent}')가 분석되었습니다. 처리를 건너뜁니다.")

        logger.debug("--- AI 상호작용 처리 종료 ---")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or isinstance(message.channel, discord.DMChannel) or not message.guild:
            return

        await self.bot.process_commands(message)

        if self.activity_cog: self.activity_cog.record_message(message)
        if self.ai_handler: self.ai_handler.add_message_to_history(message)

        if not message.content.startswith(self.bot.command_prefix):
            if await self._handle_keyword_triggers(message):
                return
            await self._handle_ai_interaction(message)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.author.bot or isinstance(message.channel, discord.DMChannel) or not message.guild: return
        content_to_log = message.content if message.content else "(내용 없음)"
        if message.attachments:
            content_to_log += f" [첨부: {', '.join([f'{att.filename}({att.size}b)' for att in message.attachments])}]"
        log_msg = f"메시지 삭제됨 | 채널: #{message.channel.name} | 작성자: {message.author} | 내용: {content_to_log}"
        logger.warning(log_msg)

# [수정] Cog를 로드하기 위한 필수적인 'setup' 함수 추가
async def setup(bot: commands.Bot):
    await bot.add_cog(EventListeners(bot))

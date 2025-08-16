# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
import google.generativeai as genai
from datetime import datetime, timedelta, time
import asyncio
import pytz
from collections import deque
from typing import Dict, Any, Tuple

import config
from logger_config import logger

KST = pytz.timezone('Asia/Seoul')

class AIHandler(commands.Cog):
    """Gemini AI 상호작용 (자발적 응답, 의도 분석, DB 기반 대화 기록)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ai_user_cooldowns: Dict[int, datetime] = {}
        self.gemini_configured = False
        self.api_call_lock = asyncio.Lock()
        self.minute_request_timestamps = deque()
        self.daily_request_count = 0
        self.daily_limit_reset_time = self._get_next_kst_midnight()
        # self.conversation_histories: Dict[int, deque] = {} # DB로 대체됨
        self.last_proactive_response_times: Dict[int, datetime] = {}

        if config.GEMINI_API_KEY:
            try:
                genai.configure(api_key=config.GEMINI_API_KEY)
                self.model = genai.GenerativeModel(config.AI_MODEL_NAME)
                self.intent_model = genai.GenerativeModel(config.AI_INTENT_MODEL_NAME)
                logger.info("Gemini API 및 모델 설정 완료.")
                self.gemini_configured = True
            except Exception as e:
                logger.critical(f"Gemini API 설정 실패: {e}. AI 기능 비활성화됨.", exc_info=True)

    @property
    def is_ready(self) -> bool:
        return self.gemini_configured and self.bot.db is not None

    def _get_next_kst_midnight(self) -> datetime:
        now_kst = datetime.now(KST)
        tomorrow = now_kst.date() + timedelta(days=1)
        return KST.localize(datetime.combine(tomorrow, time(0, 0)))

    async def _check_global_rate_limit(self) -> Tuple[bool, str | None]:
        async with self.api_call_lock:
            now = datetime.now()
            one_minute_ago = now - timedelta(minutes=1)
            while self.minute_request_timestamps and self.minute_request_timestamps[0] < one_minute_ago:
                self.minute_request_timestamps.popleft()

            if len(self.minute_request_timestamps) >= config.API_RPM_LIMIT:
                logger.warning(f"분당 Gemini API 호출 제한 도달 ({len(self.minute_request_timestamps)}/{config.API_RPM_LIMIT}).")
                return True, config.MSG_AI_RATE_LIMITED

            now_kst = now.astimezone(KST)
            if now_kst >= self.daily_limit_reset_time:
                logger.info(f"KST 자정 도달. Gemini API 일일 카운트 초기화 (이전: {self.daily_request_count}).")
                self.daily_request_count = 0
                self.daily_limit_reset_time = self._get_next_kst_midnight()

            if self.daily_request_count >= config.API_RPD_LIMIT:
                logger.warning(f"일일 Gemini API 호출 제한 도달 ({self.daily_request_count}/{config.API_RPD_LIMIT}).")
                return True, config.MSG_AI_DAILY_LIMITED
            return False, None

    def _record_api_call(self):
        now = datetime.now()
        self.minute_request_timestamps.append(now)
        self.daily_request_count += 1
        logger.debug(f"Gemini API 호출 기록됨. 분당: {len(self.minute_request_timestamps)}, 일일: {self.daily_request_count}")

    def _is_on_cooldown(self, user_id: int) -> Tuple[bool, float]:
        now = datetime.now()
        if user_id in self.ai_user_cooldowns:
            time_since_last = now - self.ai_user_cooldowns[user_id]
            if time_since_last.total_seconds() < config.AI_COOLDOWN_SECONDS:
                return True, config.AI_COOLDOWN_SECONDS - time_since_last.total_seconds()
        return False, 0.0

    def _update_cooldown(self, user_id: int):
        self.ai_user_cooldowns[user_id] = datetime.now()
        cutoff_time = datetime.now() - timedelta(seconds=config.AI_COOLDOWN_SECONDS * 10)
        self.ai_user_cooldowns = {uid: t for uid, t in self.ai_user_cooldowns.items() if t >= cutoff_time}

    def is_proactive_on_cooldown(self, channel_id: int) -> bool:
        cooldown_seconds = config.AI_PROACTIVE_RESPONSE_CONFIG.get("cooldown_seconds", 90)
        last_time = self.last_proactive_response_times.get(channel_id)
        if last_time and (datetime.now() - last_time).total_seconds() < cooldown_seconds:
            logger.debug(f"채널({channel_id}) 자발적 응답 쿨다운 중.")
            return True
        return False

    def update_proactive_cooldown(self, channel_id: int):
        self.last_proactive_response_times[channel_id] = datetime.now()
        logger.info(f"채널({channel_id}) 자발적 응답 쿨다운 시작.")

    async def add_message_to_history(self, message: discord.Message):
        """메시지를 데이터베이스의 `conversation_history` 테이블에 기록합니다."""
        if not config.AI_MEMORY_ENABLED or not self.is_ready:
            return

        channel_config = config.CHANNEL_AI_CONFIG.get(message.channel.id)
        if not channel_config:
            return

        sql = """
            INSERT INTO conversation_history (message_id, guild_id, channel_id, user_id, user_name, content, is_bot, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """
        params = (
            message.id,
            message.guild.id if message.guild else 0,
            message.channel.id,
            message.author.id,
            message.author.display_name,
            message.content,
            message.author.bot,
            message.created_at.isoformat()
        )
        try:
            await self.bot.db.execute(sql, params)
            await self.bot.db.commit()
            logger.debug(f"DB에 메시지 저장됨 (채널: {message.channel.id}): {message.content[:50]}...")
        except Exception as e:
            logger.error(f"대화 기록 DB 저장 중 오류: {e}", exc_info=True)

    async def _get_history_from_db(self, channel_id: int) -> list:
        """DB에서 대화 기록을 가져와 Gemini가 요구하는 형식으로 변환합니다."""
        if not self.is_ready:
            return []

        sql = """
            SELECT user_id, user_name, is_bot, content FROM conversation_history
            WHERE channel_id = ?
            ORDER BY created_at DESC
            LIMIT ?;
        """
        try:
            async with self.bot.db.execute(sql, (channel_id, config.AI_MEMORY_MAX_MESSAGES)) as cursor:
                rows = await cursor.fetchall()

            # 시간순으로 다시 뒤집어줌 (오래된 메시지가 위로)
            rows.reverse()

            history = []
            for row in rows:
                user_id, user_name, is_bot, content = row
                role = "model" if is_bot else "user"
                user_identifier = f"User({user_id}|{user_name})"
                formatted_content = f"{user_identifier}: {content}"
                history.append({"role": role, "parts": [{"text": formatted_content}]})
            return history
        except Exception as e:
            logger.error(f"대화 기록 DB 조회 중 오류: {e}", exc_info=True)
            return []

    async def should_proactively_respond(self, message: discord.Message) -> bool:
        if not self.is_ready: return False
        history = await self._get_history_from_db(message.channel.id)
        if not history or len(history) < 2: return False

        formatted_history = "\n".join([item['parts'][0]['text'] for item in history])
        try:
            is_limited, _ = await self._check_global_rate_limit()
            if is_limited: return False
            prompt = (f"{config.AI_PROACTIVE_RESPONSE_CONFIG['gatekeeper_persona']}\n\n"
                      f"--- 최근 대화 내용 ---\n{formatted_history}\n\n"
                      "이 상황에서 챗봇이 끼어들어도 될까? (Yes/No)")
            logger.debug(f"자발적 응답 여부 판단 요청 (채널: {message.channel.id})...")
            async with self.api_call_lock:
                self._record_api_call()
                response = await self.intent_model.generate_content_async(prompt)
            decision = response.text.strip().lower()
            logger.info(f"자발적 응답 판단 결과: '{decision}'")
            return 'yes' in decision
        except Exception as e:
            logger.error(f"자발적 응답 여부 판단 중 오류: {e}", exc_info=True)
            return False

    async def analyze_intent(self, message: discord.Message) -> str:
        if not config.AI_INTENT_ANALYSIS_ENABLED or not self.is_ready: return "Chat"
        user_query = message.content.replace(f'<@!{self.bot.user.id}>', '').replace(f'<@{self.bot.user.id}>', '').strip()
        if not user_query: return "Chat"
        try:
            is_limited, _ = await self._check_global_rate_limit()
            if is_limited: return "Chat"
            prompt = f"{config.AI_INTENT_PERSONA}\n\n사용자 메시지: \"{user_query}\""
            logger.debug(f"의도 분석 요청: {user_query[:50]}...")
            async with self.api_call_lock:
                self._record_api_call()
                response = await self.intent_model.generate_content_async(prompt)
            intent = response.text.strip()
            logger.info(f"의도 분석 결과: '{intent}' (원본: '{user_query[:50]}...')")
            valid_intents = ['Weather', 'Command', 'Chat', 'Mixed']
            return intent if intent in valid_intents else 'Chat'
        except Exception as e:
            logger.error(f"AI 의도 분석 중 오류 발생: {e}", exc_info=True)
            return "Chat"

    async def generate_creative_text(self, channel: discord.TextChannel, author: discord.User, prompt_key: str, context: Dict[str, Any] | None = None) -> str | None:
        if not self.is_ready:
            logger.warning(f"창의적 텍스트 생성 불가({prompt_key}): AI 핸들러 미준비.")
            return config.MSG_AI_ERROR
        prompt_template = config.AI_CREATIVE_PROMPTS.get(prompt_key)
        if not prompt_template:
            logger.error(f"창의적 텍스트 생성 불가: config에서 프롬프트 키 '{prompt_key}'를 찾을 수 없음.")
            return config.MSG_CMD_ERROR
        task_prompt = prompt_template.format(**(context or {}))
        channel_config = config.CHANNEL_AI_CONFIG.get(channel.id, {})
        return await self._generate_gemini_response(
            channel_id=channel.id,
            user_query=task_prompt,
            author=author,
            persona_config=channel_config,
            is_task=True
        )

    async def _generate_gemini_response(
        self,
        channel_id: int,
        user_query: str,
        author: discord.User,
        persona_config: dict,
        weather_info_str: str | None = None,
        is_task: bool = False
    ) -> str | None:
        if not self.is_ready: return config.MSG_AI_ERROR

        if not is_task:
            on_cooldown, remaining_time = self._is_on_cooldown(author.id)
            if on_cooldown:
                return config.MSG_AI_COOLDOWN.format(remaining=remaining_time)
            self._update_cooldown(author.id)

        is_limited, limit_message = await self._check_global_rate_limit()
        if is_limited: return limit_message

        user_persona_override = config.USER_SPECIFIC_PERSONAS.get(author.id) if hasattr(config, 'USER_SPECIFIC_PERSONAS') else None
        persona_cfg = user_persona_override or persona_config
        
        system_instructions = [
            persona_cfg.get("persona", ""),
            persona_cfg.get("rules", "")
        ]
        if weather_info_str:
            system_instructions.append(f"참고할 날씨 정보: {weather_info_str}")

        history = await self._get_history_from_db(channel_id) if not is_task else []
        
        try:
            model = genai.GenerativeModel(
                config.AI_MODEL_NAME,
                safety_settings=config.GEMINI_SAFETY_SETTINGS,
                system_instruction="\n".join(filter(None, system_instructions))
            )
            chat_session = model.start_chat(history=history)

            user_identifier = f"User({author.id}|{author.display_name})"
            final_query = user_query if is_task else f"{user_identifier}: {user_query}"
            
            logger.debug(f"AI 처리 시작 | {final_query[:80]}...")
            
            async with self.api_call_lock:
                self._record_api_call()
                response = await chat_session.send_message_async(final_query)

            ai_response_text = response.text.strip()
            logger.info(f"AI 응답 생성 성공 (길이: {len(ai_response_text)}): {ai_response_text[:50]}...")
            return ai_response_text

        except (genai.types.BlockedPromptException, genai.types.StopCandidateException) as security_exception:
            logger.warning(f"AI 요청/응답 차단됨 | 오류: {security_exception}")
            return config.MSG_AI_BLOCKED_PROMPT
        except Exception as e:
            logger.error(f"AI 응답 생성 중 예기치 않은 오류: {e}", exc_info=True)
            return config.MSG_AI_ERROR

    async def process_ai_message(self, message: discord.Message, weather_info: str | None = None):
        if not self.is_ready: return

        channel_config = config.CHANNEL_AI_CONFIG.get(message.channel.id)
        if not channel_config:
            if weather_info: await message.reply(f"📍 {weather_info}", mention_author=False)
            return

        user_query = message.content.replace(f'<@!{self.bot.user.id}>', '').replace(f'<@{self.bot.user.id}>', '').strip()
        if not user_query and not weather_info:
            await message.reply(config.MSG_AI_NO_CONTENT.format(bot_name=self.bot.user.name), mention_author=False)
            return

        async with message.channel.typing():
            ai_response_text = await self._generate_gemini_response(
                channel_id=message.channel.id,
                user_query=user_query,
                author=message.author,
                persona_config=channel_config,
                weather_info_str=weather_info
            )

            if ai_response_text:
                # 가짜 메시지 객체를 만들어서 AI의 응답도 기록
                bot_message = discord.Object(id=discord.utils.time_snowflake(datetime.now(pytz.utc)))
                bot_message.author = self.bot.user
                bot_message.content = ai_response_text
                bot_message.channel = message.channel
                bot_message.guild = message.guild
                bot_message.created_at = datetime.now(pytz.utc)

                await self.add_message_to_history(bot_message)
                
                await message.reply(ai_response_text[:2000], mention_author=False)

    async def generate_system_alert_message(self, channel_id: int, alert_context_info: str, alert_type: str = "일반 알림") -> str | None:
        if not self.is_ready:
            logger.warning(f"시스템 알림({alert_type}) 생성 불가: AI 핸들러 미준비.")
            return None

        channel_config = config.CHANNEL_AI_CONFIG.get(channel_id, {})
        logger.info(f"시스템 {alert_type} 생성 요청: 채널={channel_id}, 내용='{alert_context_info[:100]}...'")
        
        user_query_for_alert = f"다음 상황을 너의 페르소나에 맞게 채널에 알려줘: '{alert_context_info}'"
        
        system_author = self.bot.user
        
        generated_text = await self._generate_gemini_response(
            channel_id=channel_id,
            user_query=user_query_for_alert,
            author=system_author,
            persona_config=channel_config,
            is_task=True
        )
        return generated_text

async def setup(bot: commands.Bot):
    await bot.add_cog(AIHandler(bot))

# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
import google.generativeai as genai
import google.api_core.exceptions
from datetime import datetime, timedelta, time
import asyncio
import pytz
from collections import deque
from typing import Dict, Any, Tuple
import aiosqlite
import numpy as np
import pickle
import random

import config
from logger_config import logger
import utils

KST = pytz.timezone('Asia/Seoul')

def _cosine_similarity(v1, v2):
    """코사인 유사도를 계산합니다."""
    # v1 또는 v2의 norm이 0일 경우, 0을 반환하여 ZeroDivisionError 방지
    norm_v1 = np.linalg.norm(v1)
    norm_v2 = np.linalg.norm(v2)
    if norm_v1 == 0 or norm_v2 == 0:
        return 0.0
    return np.dot(v1, v2) / (norm_v1 * norm_v2)

class AIHandler(commands.Cog):
    """Gemini AI 상호작용 (자발적 응답, 의도 분석, 사용자별 대화 기록)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ai_user_cooldowns: Dict[int, datetime] = {}
        self.gemini_configured = False
        self.api_call_lock = asyncio.Lock()
        self.minute_request_timestamps = deque()
        self.last_proactive_response_times: Dict[int, datetime] = {}

        if config.GEMINI_API_KEY:
            try:
                genai.configure(api_key=config.GEMINI_API_KEY)
                self.intent_model = genai.GenerativeModel(config.AI_INTENT_MODEL_NAME)
                self.response_model = genai.GenerativeModel(config.AI_RESPONSE_MODEL_NAME)
                self.embedding_model_name = "models/embedding-001"
                logger.info("Gemini API 및 모델 설정 완료.")
                self.gemini_configured = True
            except Exception as e:
                logger.critical(f"Gemini API 설정 실패: {e}. AI 기능 비활성화됨.", exc_info=True)

    @property
    def is_ready(self) -> bool:
        """AI 핸들러가 모든 기능을 수행할 준비가 되었는지 확인합니다."""
        return self.gemini_configured and self.bot.db is not None

    async def _check_global_rate_limit(self, counter_name: str, limit: int) -> Tuple[bool, str | None]:
        if counter_name.startswith('gemini_'):
            async with self.api_call_lock:
                now = datetime.now()
                one_minute_ago = now - timedelta(minutes=1)
                while self.minute_request_timestamps and self.minute_request_timestamps[0] < one_minute_ago:
                    self.minute_request_timestamps.popleft()

                if len(self.minute_request_timestamps) >= config.API_RPM_LIMIT:
                    logger.warning(f"분당 Gemini API 호출 제한 도달 ({len(self.minute_request_timestamps)}/{config.API_RPM_LIMIT}).")
                    return True, config.MSG_AI_RATE_LIMITED

        if await utils.is_api_limit_reached(self.bot.db, counter_name, limit):
            return True, config.MSG_AI_DAILY_LIMITED

        return False, None

    def _record_api_call(self):
        """API 호출을 기록합니다 (분당 제한용)."""
        now = datetime.now()
        self.minute_request_timestamps.append(now)
        logger.debug(f"Gemini API 호출 기록됨. (지난 1분간: {len(self.minute_request_timestamps)}회)")

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
        """대화 기록을 DB에 저장하고, 백그라운드에서 임베딩을 생성합니다."""
        if not self.is_ready or not config.AI_MEMORY_ENABLED or not message.guild:
            return

        is_guild_ai_enabled = await utils.get_guild_setting(self.bot.db, message.guild.id, 'ai_enabled', default=True)
        if not is_guild_ai_enabled:
            return

        allowed_channels = await utils.get_guild_setting(self.bot.db, message.guild.id, 'ai_allowed_channels')
        is_ai_allowed_channel = False
        if allowed_channels:
            is_ai_allowed_channel = message.channel.id in allowed_channels
        else:
            channel_config = config.CHANNEL_AI_CONFIG.get(message.channel.id, {})
            is_ai_allowed_channel = channel_config.get("allowed", False)

        if not is_ai_allowed_channel:
            return

        try:
            await self.bot.db.execute("""
                INSERT INTO conversation_history (message_id, guild_id, channel_id, user_id, user_name, content, is_bot, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                message.id,
                message.guild.id,
                message.channel.id,
                message.author.id,
                message.author.display_name,
                message.content,
                message.author.bot,
                message.created_at.isoformat()
            ))
            await self.bot.db.commit()
            logger.debug(f"[{message.guild.name}/{message.channel.name}] 메시지 ID {message.id}를 DB에 저장했습니다.", extra={'guild_id': message.guild.id})

            if not message.author.bot and len(message.content) > 1:
                asyncio.create_task(self._create_and_save_embedding(message.id, message.content, message.guild.id))

        except aiosqlite.Error as e:
            logger.error(f"대화 기록 저장 중 DB 오류: {e}", exc_info=True, extra={'guild_id': message.guild.id})
        except Exception as e:
            logger.error(f"대화 기록 저장 중 예기치 않은 오류: {e}", exc_info=True, extra={'guild_id': message.guild.id})


    async def _create_and_save_embedding(self, message_id: int, content: str, guild_id: int):
        """주어진 내용의 임베딩을 생성하고 DB에 저장합니다."""
        try:
            is_limited, _ = await self._check_global_rate_limit('gemini_embedding_calls', config.API_EMBEDDING_RPD_LIMIT)
            if is_limited:
                logger.warning("Gemini Embedding API 호출 한도 도달. 임베딩 생성 건너뜁니다.", extra={'guild_id': guild_id})
                return

            logger.debug(f"메시지 ID {message_id}의 임베딩 생성을 시작합니다.", extra={'guild_id': guild_id})
            embedding_result = await genai.embed_content_async(
                model=self.embedding_model_name,
                content=content,
                task_type="retrieval_document"
            )
            await utils.increment_api_counter(self.bot.db, 'gemini_embedding_calls')

            embedding_blob = pickle.dumps(embedding_result['embedding'])

            await self.bot.db.execute("UPDATE conversation_history SET embedding = ? WHERE message_id = ?", (embedding_blob, message_id))
            await self.bot.db.commit()
            logger.info(f"메시지 ID {message_id}의 임베딩을 DB에 저장했습니다.", extra={'guild_id': guild_id})

        except aiosqlite.Error as e:
            logger.error(f"임베딩 저장 중 DB 오류 (메시지 ID: {message_id}): {e}", exc_info=True, extra={'guild_id': guild_id})
        except Exception as e:
            logger.error(f"임베딩 생성/저장 중 오류 (메시지 ID: {message_id}): {e}", exc_info=True, extra={'guild_id': guild_id})

    async def _find_similar_conversations(self, channel_id: int, user_id: int, query: str, top_k: int = 5) -> str:
        """DB에서 유사한 대화를 검색하여 컨텍스트 문자열을 생성합니다."""
        guild_id = self.bot.get_channel(channel_id).guild.id
        try:
            is_limited, _ = await self._check_global_rate_limit('gemini_embedding_calls', config.API_EMBEDDING_RPD_LIMIT)
            if is_limited: return ""

            query_embedding_result = await genai.embed_content_async(
                model=self.embedding_model_name,
                content=query,
                task_type="retrieval_query"
            )
            await utils.increment_api_counter(self.bot.db, 'gemini_embedding_calls')
            query_embedding = np.array(query_embedding_result['embedding'])

            async with self.bot.db.execute("""
                SELECT content, embedding FROM conversation_history
                WHERE channel_id = ? AND user_id = ? AND embedding IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 100;
            """, (channel_id, user_id)) as cursor:
                rows = await cursor.fetchall()

            if not rows: return ""

            similarities = []
            for content, embedding_blob in rows:
                embedding = pickle.loads(embedding_blob)
                sim = _cosine_similarity(query_embedding, embedding)
                similarities.append((sim, content))

            similarities.sort(key=lambda x: x[0], reverse=True)
            top_conversations = [content for sim, content in similarities[:top_k]]

            if not top_conversations: return ""

            context_str = "이전 대화 중 관련 내용:\n" + "\n".join(f"- {conv}" for conv in reversed(top_conversations))
            return context_str

        except Exception as e:
            logger.error(f"유사 대화 검색 중 오류: {e}", exc_info=True, extra={'guild_id': guild_id})
            return ""

    async def _get_recent_conversation_text(self, channel_id: int, look_back: int) -> str | None:
        """DB에서 최근 대화 기록을 가져와 텍스트로 반환합니다."""
        guild_id = self.bot.get_channel(channel_id).guild.id
        try:
            async with self.bot.db.execute("""
                SELECT user_name, content FROM conversation_history
                WHERE channel_id = ?
                ORDER BY created_at DESC
                LIMIT ?;
            """, (channel_id, look_back)) as cursor:
                rows = await cursor.fetchall()

            if not rows:
                return None

            return "\n".join([f"{row[0]}: {row[1]}" for row in reversed(rows)])
        except Exception as e:
            logger.error(f"최근 대화 기록 조회 중 오류: {e}", exc_info=True, extra={'guild_id': guild_id})
            return None


    async def should_proactively_respond(self, message: discord.Message) -> bool:
        conf = config.AI_PROACTIVE_RESPONSE_CONFIG
        if not conf.get("enabled"): return False
        if not self.is_ready or message.author.bot or not message.guild: return False
        if len(message.content) < conf.get("min_message_length", 10): return False

        is_guild_ai_enabled = await utils.get_guild_setting(self.bot.db, message.guild.id, 'ai_enabled', default=True)
        if not is_guild_ai_enabled: return False

        allowed_channels = await utils.get_guild_setting(self.bot.db, message.guild.id, 'ai_allowed_channels')
        if allowed_channels:
            is_ai_allowed_channel = message.channel.id in allowed_channels
        else:
            channel_config = config.CHANNEL_AI_CONFIG.get(message.channel.id, {})
            is_ai_allowed_channel = channel_config.get("allowed", False)

        if not is_ai_allowed_channel: return False
        if self.is_proactive_on_cooldown(message.channel.id): return False

        found_keyword = any(keyword in message.content for keyword in conf.get("keywords", []))
        if found_keyword:
            logger.info(f"자발적 응답 키워드 발견. 확률 체크 건너뜁니다.", extra={'guild_id': message.guild.id})
        elif random.random() > conf.get("probability", 0.5):
            return False

        try:
            look_back = conf.get("look_back_count", 5)
            recent_conversation_text = await self._get_recent_conversation_text(message.channel.id, look_back)
            if not recent_conversation_text: return False

            is_limited, _ = await self._check_global_rate_limit('gemini_lite_daily_calls', config.API_LITE_RPD_LIMIT)
            if is_limited: return False

            gatekeeper_prompt = conf.get("gatekeeper_persona", "")
            prompt = f"{gatekeeper_prompt}\n\n[최근 대화 내용]\n{recent_conversation_text}"

            async with self.api_call_lock:
                self._record_api_call()
                response = await self.intent_model.generate_content_async(prompt)
                await utils.increment_api_counter(self.bot.db, 'gemini_lite_daily_calls')

            decision = response.text.strip().upper()
            logger.info(f"자발적 응답 AI 판단 결과: '{decision}'", extra={'guild_id': message.guild.id})

            if "YES" in decision:
                logger.info(f"모든 자발적 응답 조건을 통과하여 응답 실행. (채널: {message.channel.name})", extra={'guild_id': message.guild.id})
                return True
            return False

        except Exception as e:
            logger.error(f"자발적 응답 여부 판단 중 예기치 않은 오류: {e}", exc_info=True, extra={'guild_id': message.guild.id})
            return False

    async def analyze_intent(self, message: discord.Message) -> str:
        if not config.AI_INTENT_ANALYSIS_ENABLED or not self.is_ready: return "Chat"
        user_query = message.content.replace(f'<@!{self.bot.user.id}>', '').replace(f'<@{self.bot.user.id}>', '').strip()
        if not user_query: return "Chat"
        
        try:
            is_limited, _ = await self._check_global_rate_limit('gemini_lite_daily_calls', config.API_LITE_RPD_LIMIT)
            if is_limited: return "Chat"
            
            prompt = f"{config.AI_INTENT_PERSONA}\n\n사용자 메시지: \"{user_query}\""
            
            async with self.api_call_lock:
                self._record_api_call()
                response = await self.intent_model.generate_content_async(prompt)
                await utils.increment_api_counter(self.bot.db, 'gemini_lite_daily_calls')
            
            intent = response.text.strip()
            logger.info(f"의도 분석 결과: '{intent}'", extra={'guild_id': message.guild.id})
            
            valid_intents = ['Time', 'Weather', 'Command', 'Chat', 'Mixed']
            return intent if intent in valid_intents else 'Chat'
        except Exception as e:
            logger.error(f"AI 의도 분석 중 오류 발생: {e}", exc_info=True, extra={'guild_id': message.guild.id})
            return "Chat"

    async def generate_creative_text(self, channel: discord.TextChannel, author: discord.User, prompt_key: str, context: Dict[str, Any] | None = None) -> str | None:
        if not self.is_ready:
            logger.warning(f"창의적 텍스트 생성 불가({prompt_key}): AI 핸들러 미준비.", extra={'guild_id': channel.guild.id})
            return config.MSG_AI_ERROR
        # ... (rest of the function calls _generate_gemini_response, which is refactored)
        return await self._generate_gemini_response(
            channel_id=channel.id,
            user_query=prompt_template.format(**(context or {})),
            author=author,
            persona_config=config.CHANNEL_AI_CONFIG.get(channel.id, {}),
            is_task=True,
            intent=prompt_key
        )

    async def _generate_gemini_response(
        self, channel_id: int, user_query: str, author: discord.User, persona_config: dict,
        weather_info_str: str | None = None, time_info_str: str | None = None,
        is_task: bool = False, intent: str = "Chat"
    ) -> str | None:
        if not self.is_ready: return config.MSG_AI_ERROR
        guild_id = self.bot.get_channel(channel_id).guild.id

        if not is_task:
            on_cooldown, remaining_time = self._is_on_cooldown(author.id)
            if on_cooldown: return config.MSG_AI_COOLDOWN.format(remaining=remaining_time)
            self._update_cooldown(author.id)

        is_limited, limit_message = await self._check_global_rate_limit('gemini_flash_daily_calls', config.API_FLASH_RPD_LIMIT)
        if is_limited: return limit_message

        custom_persona_text = await utils.get_guild_setting(self.bot.db, guild_id, 'persona_text')
        user_persona_override = config.USER_SPECIFIC_PERSONAS.get(author.id)

        persona_cfg = persona_config
        if user_persona_override: persona_cfg = user_persona_override
        elif custom_persona_text: persona_cfg = {"persona": custom_persona_text, "rules": persona_config.get("rules", "")}

        system_instructions = [persona_cfg.get("persona", ""), persona_cfg.get("rules", "")]
        if weather_info_str: system_instructions.append(f"참고할 날씨 정보: {weather_info_str}")
        if time_info_str: system_instructions.append(f"참고할 현재 시간 정보: {time_info_str}")

        rag_context = await self._find_similar_conversations(channel_id, author.id, user_query)
        if rag_context: system_instructions.append(rag_context)

        try:
            model = self.response_model
            chat_session = model.start_chat(history=[])
            final_query = user_query if is_task else f"User({author.id}|{author.display_name}): {user_query}"
            
            async with self.api_call_lock:
                self._record_api_call()
                response = await chat_session.send_message_async(final_query, stream=False, system_instruction="\n".join(filter(None, system_instructions)))
                await utils.increment_api_counter(self.bot.db, 'gemini_flash_daily_calls')

            ai_response_text = response.text.strip()

            try:
                usage_metadata = response.usage_metadata
                details = {
                    "guild_id": guild_id, "user_id": author.id, "channel_id": channel_id,
                    "model_name": config.AI_RESPONSE_MODEL_NAME, "intent": intent,
                    "prompt_tokens": usage_metadata.prompt_token_count,
                    "response_tokens": usage_metadata.candidates_token_count,
                    "total_tokens": usage_metadata.total_token_count, "is_task": is_task
                }
                await utils.log_analytics(self.bot.db, "AI_INTERACTION", details)
            except Exception as e:
                logger.error(f"AI 상호작용 분석 로그 기록 중 오류: {e}", extra={'guild_id': guild_id})

            logger.info(f"AI 응답 생성 성공 (길이: {len(ai_response_text)})", extra={'guild_id': guild_id})
            return ai_response_text

        except (genai.types.BlockedPromptException, genai.types.StopCandidateException) as security_exception:
            logger.warning(f"AI 요청/응답 차단됨 | 오류: {security_exception}", extra={'guild_id': guild_id})
            return config.MSG_AI_BLOCKED_PROMPT if 'prompt' in str(security_exception).lower() else config.MSG_AI_BLOCKED_RESPONSE
        except google.api_core.exceptions.InternalServerError as e:
            logger.error(f"Gemini API 내부 서버 오류: {e}", exc_info=True, extra={'guild_id': guild_id})
            return config.MSG_AI_ERROR
        except Exception as e:
            logger.error(f"AI 응답 생성 중 예기치 않은 오류: {e}", exc_info=True, extra={'guild_id': guild_id})
            return config.MSG_AI_ERROR

    async def process_ai_message(self, message: discord.Message, weather_info: str | None = None, time_info: str | None = None, intent: str = "Chat"):
        if not self.is_ready: return
        user_query = message.content.replace(f'<@!{self.bot.user.id}>', '').replace(f'<@{self.bot.user.id}>', '').strip()
        if not user_query and not weather_info:
            await message.reply(config.MSG_AI_NO_CONTENT.format(bot_name=self.bot.user.name), mention_author=False)
            return

        async with message.channel.typing():
            ai_response_text = await self._generate_gemini_response(
                channel_id=message.channel.id, user_query=user_query, author=message.author,
                persona_config=config.CHANNEL_AI_CONFIG.get(message.channel.id, {}),
                weather_info_str=weather_info, time_info_str=time_info, intent=intent
            )
            if ai_response_text:
                bot_response_message = await message.reply(ai_response_text[:2000], mention_author=False)
                await self.add_message_to_history(bot_response_message)

    async def generate_system_alert_message(self, channel_id: int, alert_context_info: str, alert_type: str = "일반 알림") -> str | None:
        if not self.is_ready: return None
        logger.info(f"시스템 {alert_type} 생성 요청: 채널={channel_id}", extra={'guild_id': self.bot.get_channel(channel_id).guild.id})
        user_query_for_alert = f"다음 상황을 너의 페르소나에 맞게 채널에 알려줘: '{alert_context_info}'"
        
        return await self._generate_gemini_response(
            channel_id=channel_id, user_query=user_query_for_alert, author=self.bot.user,
            persona_config=config.CHANNEL_AI_CONFIG.get(channel_id, {}), is_task=True, intent=alert_type
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(AIHandler(bot))

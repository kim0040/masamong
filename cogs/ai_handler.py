# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
import google.generativeai as genai
from datetime import datetime, timedelta, time
import asyncio
import pytz
from collections import deque
from typing import Dict, Any, Tuple
import sqlite3
import numpy as np
from sentence_transformers import SentenceTransformer
import pickle

import config
from logger_config import logger
import utils

KST = pytz.timezone('Asia/Seoul')

def _cosine_similarity(v1, v2):
    """코사인 유사도를 계산합니다."""
    return np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2))

class AIHandler(commands.Cog):
    """Gemini AI 상호작용 (자발적 응답, 의도 분석, 사용자별 대화 기록)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ai_user_cooldowns: Dict[int, datetime] = {}
        self.gemini_configured = False
        self.api_call_lock = asyncio.Lock()
        self.minute_request_timestamps = deque()
        self.last_proactive_response_times: Dict[int, datetime] = {}
        self.embedding_model = None

        if config.GEMINI_API_KEY:
            try:
                genai.configure(api_key=config.GEMINI_API_KEY)
                self.model = genai.GenerativeModel(config.AI_MODEL_NAME)
                self.intent_model = genai.GenerativeModel(config.AI_INTENT_MODEL_NAME)
                logger.info("Gemini API 및 모델 설정 완료.")
                self.gemini_configured = True
            except Exception as e:
                logger.critical(f"Gemini API 설정 실패: {e}. AI 기능 비활성화됨.", exc_info=True)

        try:
            self.embedding_model = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')
            logger.info("SentenceTransformer 모델 로드 완료.")
        except Exception as e:
            logger.critical(f"SentenceTransformer 모델 로드 실패: {e}. RAG 기능 비활성화됨.", exc_info=True)

    @property
    def is_ready(self) -> bool:
        """AI 핸들러가 모든 기능을 수행할 준비가 되었는지 확인합니다."""
        return self.gemini_configured and self.embedding_model is not None

    async def _check_global_rate_limit(self) -> Tuple[bool, str | None]:
        async with self.api_call_lock:
            now = datetime.now()
            one_minute_ago = now - timedelta(minutes=1)
            while self.minute_request_timestamps and self.minute_request_timestamps[0] < one_minute_ago:
                self.minute_request_timestamps.popleft()

            if len(self.minute_request_timestamps) >= config.API_RPM_LIMIT:
                logger.warning(f"분당 Gemini API 호출 제한 도달 ({len(self.minute_request_timestamps)}/{config.API_RPM_LIMIT}).")
                return True, config.MSG_AI_RATE_LIMITED

        if await utils.is_api_limit_reached('gemini_daily_calls', config.API_RPD_LIMIT):
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

    def add_message_to_history(self, message: discord.Message):
        """대화 기록을 DB에 저장하고, 백그라운드에서 임베딩을 생성합니다."""
        if not config.AI_MEMORY_ENABLED or not message.guild:
            return

        channel_config = config.CHANNEL_AI_CONFIG.get(message.channel.id, {})
        if not channel_config.get("allowed", False):
            return

        conn = None
        try:
            conn = sqlite3.connect(f"file:{config.DATABASE_FILE}?mode=rw", uri=True)
            cursor = conn.cursor()

            cursor.execute("""
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
            conn.commit()
            logger.debug(f"[{message.guild.name}/{message.channel.name}] 메시지 ID {message.id}를 DB에 저장했습니다.")

            if not message.author.bot and self.embedding_model:
                asyncio.create_task(self._create_and_save_embedding(message.id, message.content))

        except sqlite3.Error as e:
            logger.error(f"대화 기록 저장 중 DB 오류: {e}", exc_info=True)
        finally:
            if conn:
                conn.close()

    async def _create_and_save_embedding(self, message_id: int, content: str):
        """주어진 내용의 임베딩을 생성하고 DB에 저장합니다."""
        if not self.embedding_model: return
        
        try:
            logger.debug(f"메시지 ID {message_id}의 임베딩 생성을 시작합니다.")
            embedding = await asyncio.to_thread(self.embedding_model.encode, content)
            embedding_blob = pickle.dumps(embedding)

            conn = None
            try:
                conn = sqlite3.connect(f"file:{config.DATABASE_FILE}?mode=rw", uri=True)
                cursor = conn.cursor()
                cursor.execute("UPDATE conversation_history SET embedding = ? WHERE message_id = ?", (embedding_blob, message_id))
                conn.commit()
                logger.info(f"메시지 ID {message_id}의 임베딩을 DB에 저장했습니다.")
            except sqlite3.Error as e:
                logger.error(f"임베딩 저장 중 DB 오류: {e}", exc_info=True)
            finally:
                if conn:
                    conn.close()

        except Exception as e:
            logger.error(f"임베딩 생성/저장 중 오류 (메시지 ID: {message_id}): {e}", exc_info=True)

    async def _find_similar_conversations(self, channel_id: int, user_id: int, query_embedding: np.ndarray, top_k: int = 5) -> str:
        """DB에서 유사한 대화를 검색하여 컨텍스트 문자열을 생성합니다."""
        conn = None
        try:
            conn = sqlite3.connect(f"file:{config.DATABASE_FILE}?mode=ro", uri=True)
            cursor = conn.cursor()

            cursor.execute("""
                SELECT content, embedding FROM conversation_history
                WHERE channel_id = ? AND user_id = ? AND embedding IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 100;
            """, (channel_id, user_id))
            
            rows = cursor.fetchall()
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

        except sqlite3.Error as e:
            logger.error(f"유사 대화 검색 중 DB 오류: {e}", exc_info=True)
            return ""
        finally:
            if conn:
                conn.close()

    async def should_proactively_respond(self, message: discord.Message) -> bool:
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
                await utils.increment_api_counter('gemini_daily_calls')
            
            intent = response.text.strip()
            logger.info(f"의도 분석 결과: '{intent}' (원본: '{user_query[:50]}...')")
            
            valid_intents = ['Time', 'Weather', 'Command', 'Chat', 'Mixed']
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

        user_persona_override = config.USER_SPECIFIC_PERSONAS.get(author.id)
        persona_cfg = user_persona_override or persona_config

        system_instructions = [
            persona_cfg.get("persona", ""),
            persona_cfg.get("rules", "")
        ]
        if weather_info_str:
            system_instructions.append(f"참고할 날씨 정보: {weather_info_str}")

        query_embedding = await asyncio.to_thread(self.embedding_model.encode, user_query)
        rag_context = await self._find_similar_conversations(channel_id, author.id, query_embedding)
        if rag_context:
            system_instructions.append(rag_context)

        history = []

        try:
            model = genai.GenerativeModel(
                config.AI_MODEL_NAME,
                safety_settings=config.GEMINI_SAFETY_SETTINGS,
                system_instruction="\n".join(filter(None, system_instructions))
            )
            
            chat_session = model.start_chat(history=history)
            final_query = user_query if is_task else f"User({author.id}|{author.display_name}): {user_query}"
            
            logger.debug(f"AI 처리 시작 | {final_query[:80]}...")

            async with self.api_call_lock:
                self._record_api_call()
                response = await chat_session.send_message_async(final_query, stream=False)
                await utils.increment_api_counter('gemini_daily_calls')

            ai_response_text = response.text.strip()
            logger.info(f"AI 응답 생성 성공 (길이: {len(ai_response_text)}): {ai_response_text[:50]}...")
            return ai_response_text

        except (genai.types.BlockedPromptException, genai.types.StopCandidateException) as security_exception:
            logger.warning(f"AI 요청/응답 차단됨 | 오류: {security_exception}")
            if 'prompt' in str(security_exception).lower():
                return config.MSG_AI_BLOCKED_PROMPT
            else:
                return config.MSG_AI_BLOCKED_RESPONSE
        except Exception as e:
            logger.error(f"AI 응답 생성 중 예기치 않은 오류: {e}", exc_info=True)
            return config.MSG_AI_ERROR

    async def process_ai_message(self, message: discord.Message, weather_info: str | None = None):
        if not self.is_ready: return

        channel_config = config.CHANNEL_AI_CONFIG.get(message.channel.id)
        if not channel_config or not channel_config.get("allowed", False):
            if weather_info:
                await message.reply(f"📍 {weather_info}", mention_author=False)
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
                bot_response_message = await message.reply(ai_response_text[:2000], mention_author=False)
                self.add_message_to_history(bot_response_message)

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

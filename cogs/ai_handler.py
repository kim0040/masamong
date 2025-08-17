# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
import google.generativeai as genai
import google.api_core.exceptions
from datetime import datetime, timedelta
import asyncio
import pytz
from collections import deque
import re
from typing import Dict, Any, Tuple
import aiosqlite
import numpy as np
import pickle
import random
import json

import config
from logger_config import logger
from utils import db as db_utils
from . import response_formatter

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
        self.tools_cog = bot.get_cog('ToolsCog')
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

        if await db_utils.is_api_limit_reached(self.bot.db, counter_name, limit):
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

        is_guild_ai_enabled = await db_utils.get_guild_setting(self.bot.db, message.guild.id, 'ai_enabled', default=True)
        if not is_guild_ai_enabled:
            return

        allowed_channels = await db_utils.get_guild_setting(self.bot.db, message.guild.id, 'ai_allowed_channels')
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
            await db_utils.increment_api_counter(self.bot.db, 'gemini_embedding_calls')

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
            await db_utils.increment_api_counter(self.bot.db, 'gemini_embedding_calls')
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

        is_guild_ai_enabled = await db_utils.get_guild_setting(self.bot.db, message.guild.id, 'ai_enabled', default=True)
        if not is_guild_ai_enabled: return False

        allowed_channels = await db_utils.get_guild_setting(self.bot.db, message.guild.id, 'ai_allowed_channels')
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
                await db_utils.increment_api_counter(self.bot.db, 'gemini_lite_daily_calls')

            decision = response.text.strip().upper()
            logger.info(f"자발적 응답 AI 판단 결과: '{decision}'", extra={'guild_id': message.guild.id})

            if "YES" in decision:
                logger.info(f"모든 자발적 응답 조건을 통과하여 응답 실행. (채널: {message.channel.name})", extra={'guild_id': message.guild.id})
                return True
            return False

        except Exception as e:
            logger.error(f"자발적 응답 여부 판단 중 예기치 않은 오류: {e}", exc_info=True, extra={'guild_id': message.guild.id})
            return False

    async def create_execution_plan(self, message: discord.Message) -> dict | None:
        """
        사용자의 메시지를 기반으로 '계획 및 실행'을 위한 JSON 계획을 생성합니다.
        """
        user_query = message.content.replace(f'<@!{self.bot.user.id}>', '').replace(f'<@{self.bot.user.id}>', '').strip()
        if not user_query:
            return None
        
        log_extra = {'guild_id': message.guild.id}
        logger.info(f"실행 계획 생성 시작. 사용자 쿼리: '{user_query}'", extra=log_extra)

        try:
            is_limited, _ = await self._check_global_rate_limit('gemini_lite_daily_calls', config.API_LITE_RPD_LIMIT)
            if is_limited:
                await message.reply(config.MSG_AI_DAILY_LIMITED, mention_author=False)
                return None
            
            prompt = f"{config.AGENT_PLANNER_PERSONA}\n\n# User Request:\n{user_query}"
            
            async with self.api_call_lock:
                self._record_api_call()
                response = await self.intent_model.generate_content_async(prompt)
                await db_utils.increment_api_counter(self.bot.db, 'gemini_lite_daily_calls')
            
            # 응답 텍스트에서 JSON 부분만 추출
            response_text = response.text.strip()
            json_match = re.search(r'```json\n({.*?})\n```', response_text, re.DOTALL)
            if json_match:
                json_text = json_match.group(1)
            else:
                json_text = response_text

            # JSON 파싱 및 검증
            try:
                plan_json = json.loads(json_text)
                if 'plan' not in plan_json or not isinstance(plan_json['plan'], list):
                    raise ValueError("JSON에 'plan' 키가 없거나 리스트가 아닙니다.")
                logger.info(f"실행 계획 생성 성공: {plan_json}", extra=log_extra)
                return plan_json
            except (json.JSONDecodeError, ValueError) as e:
                logger.error(f"LLM이 생성한 계획의 JSON 파싱 또는 검증 실패. 오류: {e}\n원본 텍스트: {response_text}", extra=log_extra)
                # 실패 시 일반 채팅으로 처리하는 fallback
                return {
                    "plan": [{
                        "tool_to_use": "general_chat",
                        "parameters": {"user_query": user_query}
                    }]
                }

        except Exception as e:
            logger.error(f"실행 계획 생성 중 예기치 않은 오류 발생: {e}", exc_info=True, extra=log_extra)
            await message.reply(config.MSG_AI_ERROR, mention_author=False)
            return None

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

        custom_persona_text = await db_utils.get_guild_setting(self.bot.db, guild_id, 'persona_text')
        user_persona_override = config.USER_SPECIFIC_PERSONAS.get(author.id)

        persona_cfg = persona_config
        if user_persona_override: persona_cfg = user_persona_override
        elif custom_persona_text: persona_cfg = {"persona": custom_persona_text, "rules": persona_config.get("rules", "")}

        system_instructions = [persona_cfg.get("persona", ""), persona_cfg.get("rules", "")]
        if weather_info_str: system_instructions.append(f"참고할 날씨 정보: {weather_info_str}")
        if time_info_str: system_instructions.append(f"참고할 현재 시간 정보: {time_info_str}")

        # RAG 컨텍스트 생성을 위해 최근 대화 내용을 가져와 쿼리에 추가
        rag_query_context = user_query
        recent_conv = await self._get_recent_conversation_text(channel_id, look_back=3)
        if recent_conv:
            rag_query_context = f"[최근 대화]\n{recent_conv}\n\n[현재 질문]\n{user_query}"

        rag_context = await self._find_similar_conversations(channel_id, author.id, rag_query_context)
        if rag_context: system_instructions.append(rag_context)

        try:
            model = self.response_model
            chat_session = model.start_chat(history=[])
            final_query = user_query if is_task else f"User({author.id}|{author.display_name}): {user_query}"
            
            async with self.api_call_lock:
                self._record_api_call()
                response = await chat_session.send_message_async(final_query, stream=False, system_instruction="\n".join(filter(None, system_instructions)))
                await db_utils.increment_api_counter(self.bot.db, 'gemini_flash_daily_calls')

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
                await db_utils.log_analytics(self.bot.db, "AI_INTERACTION", details)
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

    async def synthesize_final_response(self, user_query: str, execution_context: dict, author: discord.User, guild_id: int) -> str:
        """
        실행 컨텍스트를 기반으로 최종 사용자 응답을 생성합니다.
        """
        log_extra = {'guild_id': guild_id}
        context_str = json.dumps(execution_context, indent=2, ensure_ascii=False)

        prompt = (
            f"{config.AGENT_SYNTHESIZER_PERSONA}\n\n"
            f"# User's Original Query:\n{user_query}\n\n"
            f"# Data Collected from Tools:\n```json\n{context_str}\n```"
        )

        try:
            is_limited, limit_message = await self._check_global_rate_limit('gemini_flash_daily_calls', config.API_FLASH_RPD_LIMIT)
            if is_limited: return limit_message

            async with self.api_call_lock:
                self._record_api_call()
                response = await self.response_model.generate_content_async(prompt)
                await db_utils.increment_api_counter(self.bot.db, 'gemini_flash_daily_calls')

            final_answer = response.text.strip()
            logger.info("최종 응답 생성 성공.", extra=log_extra)
            return final_answer

        except Exception as e:
            logger.error(f"최종 응답 생성 중 예기치 않은 오류: {e}", exc_info=True, extra=log_extra)
            return config.MSG_AI_ERROR

    async def process_agent_message(self, message: discord.Message):
        """
        사용자 메시지에 대한 Plan-and-Execute 에이전트 워크플로우를 처리합니다.
        """
        if not self.is_ready: return

        user_query = message.content.replace(f'<@!{self.bot.user.id}>', '').replace(f'<@{self.bot.user.id}>', '').strip()
        if not user_query:
            await message.reply(config.MSG_AI_NO_CONTENT.format(bot_name=self.bot.user.name), mention_author=False)
            return

        async with message.channel.typing():
            # 1. 계획 수립 (Planner)
            plan = await self.create_execution_plan(message)
            if not plan:
                # 오류는 create_execution_plan 내부에서 이미 처리됨
                return

            # 2. 계획 실행 (Executor)
            execution_context = await self.execute_plan(plan, message.guild.id)

            # 3. 최종 응답 생성 (Synthesizer)
            first_step_result = execution_context.get("step_1", {})
            # 일반 채팅 또는 도구 사용 실패 시, 기존 RAG 기반 채팅으로 fallback
            if first_step_result.get("tool") == "general_chat" or "error" in first_step_result:
                # 에러가 있다면 context에 담겨서 Synthesizer로 전달됨
                is_error = "error" in first_step_result
                final_response = await self.synthesize_final_response(user_query, execution_context, message.author, message.guild.id)
                # 만약 에러 응답도 실패하면, 기본 에러 메시지 사용
                if not final_response and is_error:
                    final_response = config.MSG_AI_ERROR
                # 일반 챗 폴백의 경우, RAG를 사용하는 기존 _generate_gemini_response 호출
                elif not is_error:
                    final_response = await self._generate_gemini_response(
                        channel_id=message.channel.id, user_query=user_query, author=message.author,
                        persona_config=config.CHANNEL_AI_CONFIG.get(message.channel.id, {})
                    )
            else:
                # 도구 사용 성공 시, 결과를 종합하여 답변 생성
                final_response = await self.synthesize_final_response(user_query, execution_context, message.author, message.guild.id)

            if final_response:
                bot_response_message = await message.reply(final_response[:2000], mention_author=False)
                await self.add_message_to_history(bot_response_message)

    async def generate_system_alert_message(self, channel_id: int, alert_context_info: str, alert_type: str = "일반 알림") -> str | None:
        if not self.is_ready: return None
        logger.info(f"시스템 {alert_type} 생성 요청: 채널={channel_id}", extra={'guild_id': self.bot.get_channel(channel_id).guild.id})
        user_query_for_alert = f"다음 상황을 너의 페르소나에 맞게 채널에 알려줘: '{alert_context_info}'"
        
        return await self._generate_gemini_response(
            channel_id=channel_id, user_query=user_query_for_alert, author=self.bot.user,
            persona_config=config.CHANNEL_AI_CONFIG.get(channel_id, {}), is_task=True, intent=alert_type
        )

    async def synthesize_final_response(self, user_query: str, execution_context: dict, author: discord.User, guild_id: int) -> str:
        """
        실행 컨텍스트를 기반으로 최종 사용자 응답을 생성합니다.
        """
        log_extra = {'guild_id': guild_id}
        context_str = json.dumps(execution_context, indent=2, ensure_ascii=False)

        prompt = (
            f"{config.AGENT_SYNTHESIZER_PERSONA}\n\n"
            f"# User's Original Query:\n{user_query}\n\n"
            f"# Data Collected from Tools:\n```json\n{context_str}\n```"
        )

        try:
            is_limited, limit_message = await self._check_global_rate_limit('gemini_flash_daily_calls', config.API_FLASH_RPD_LIMIT)
            if is_limited: return limit_message

            async with self.api_call_lock:
                self._record_api_call()
                response = await self.response_model.generate_content_async(prompt)
                await db_utils.increment_api_counter(self.bot.db, 'gemini_flash_daily_calls')

            final_answer = response.text.strip()
            logger.info("최종 응답 생성 성공.", extra=log_extra)
            return final_answer

        except Exception as e:
            logger.error(f"최종 응답 생성 중 예기치 않은 오류: {e}", exc_info=True, extra=log_extra)
            return config.MSG_AI_ERROR

    async def process_agent_message(self, message: discord.Message):
        """
        사용자 메시지에 대한 Plan-and-Execute 에이전트 워크플로우를 처리합니다.
        """
        if not self.is_ready: return

        user_query = message.content.replace(f'<@!{self.bot.user.id}>', '').replace(f'<@{self.bot.user.id}>', '').strip()
        if not user_query:
            await message.reply(config.MSG_AI_NO_CONTENT.format(bot_name=self.bot.user.name), mention_author=False)
            return

        async with message.channel.typing():
            # 1. 계획 수립 (Planner)
            plan = await self.create_execution_plan(message)
            if not plan:
                return

            # 2. 계획 실행 (Executor)
            execution_context = await self.execute_plan(plan, message.guild.id)

            # 3. 최종 응답 생성 (Synthesizer)
            first_step_result = execution_context.get("step_1_result", {})

            if first_step_result.get("tool") == "general_chat":
                final_response_text = await self._generate_gemini_response(
                    channel_id=message.channel.id, user_query=user_query, author=message.author,
                    persona_config=config.CHANNEL_AI_CONFIG.get(message.channel.id, {})
                )
            else:
                final_response_text = await self.synthesize_final_response(user_query, execution_context, message.author, message.guild.id)

            # 4. 응답 포맷팅 및 전송
            if final_response_text:
                formatted_response = response_formatter.format_final_response(user_query, execution_context, final_response_text)

                bot_response_message = None
                if isinstance(formatted_response, discord.Embed):
                    bot_response_message = await message.reply(embed=formatted_response, mention_author=False)
                else:
                    bot_response_message = await message.reply(formatted_response[:2000], mention_author=False)

                if bot_response_message:
                    await self.add_message_to_history(bot_response_message)

    async def execute_plan(self, plan: dict, guild_id: int) -> dict:
        """
        생성된 JSON 계획을 단계별로 실행하고, 각 단계의 결과를 컨텍스트에 저장합니다.
        """
        if not self.tools_cog:
            logger.error("ToolsCog가 로드되지 않아 계획을 실행할 수 없습니다.", extra={'guild_id': guild_id})
            return {"error": "도구 실행기를 찾을 수 없습니다."}

        execution_context = {}
        step_number = 1
        log_extra = {'guild_id': guild_id}

        for step in plan.get('plan', []):
            tool_name = step.get('tool_to_use')
            parameters = step.get('parameters', {})
            step_key = f"step_{step_number}"

            if not tool_name:
                logger.warning(f"계획의 {step_number}번째 단계에 'tool_to_use'가 없습니다. 건너뜁니다.", extra=log_extra)
                step_number += 1
                continue

            # 일반 대화는 특별 처리
            if tool_name == "general_chat":
                execution_context[step_key] = {"result": parameters.get("user_query"), "tool": "general_chat"}
                logger.info("일반 대화로 계획을 종료합니다.", extra=log_extra)
                break

            try:
                tool_method = getattr(self.tools_cog, tool_name)
            except AttributeError:
                error_msg = f"계획 실행 중단: 존재하지 않는 도구 '{tool_name}'를 호출하려고 했습니다."
                logger.error(error_msg, extra=log_extra)
                execution_context[step_key] = {"error": error_msg}
                break

            try:
                logger.info(f"Executing tool: {tool_name} with params: {parameters}", extra=log_extra)
                # 이전 단계의 결과를 파라미터로 사용해야 하는 경우에 대한 처리 (고급 기능)
                # 예: parameters의 값이 "{step_1.price}" 같은 형태일 때, context에서 값을 찾아 대체
                # 현재는 직접적인 값만 전달하는 것으로 가정
                result = await tool_method(**parameters)

                if isinstance(result, dict) and result.get("error"):
                    error_msg = f"도구 '{tool_name}' 실행 중 오류 발생: {result['error']}"
                    logger.warning(error_msg, extra=log_extra)
                    execution_context[step_key] = {"error": error_msg}
                    break

                execution_context[step_key] = {"result": result, "tool": tool_name}

            except Exception as e:
                error_msg = f"도구 '{tool_name}' 실행 중 예기치 않은 오류 발생: {e}"
                logger.error(error_msg, exc_info=True, extra=log_extra)
                execution_context[step_key] = {"error": "도구 실행 중 예상치 못한 오류가 발생했습니다."}
                break

            step_number += 1

        logger.info(f"계획 실행 완료. 최종 컨텍스트: {execution_context}", extra=log_extra)
        return execution_context


async def setup(bot: commands.Bot):
    await bot.add_cog(AIHandler(bot))

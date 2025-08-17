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

KST = pytz.timezone('Asia/Seoul')

class AIHandler(commands.Cog):
    """
    Tool-Using Agent의 핵심 로직을 담당합니다.
    (계획, 실행, 응답 생성, 대화 기록 관리)
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.tools_cog = bot.get_cog('ToolsCog')
        self.ai_user_cooldowns: Dict[int, datetime] = {}
        self.gemini_configured = False
        self.api_call_lock = asyncio.Lock()
        self.minute_request_timestamps = deque()

        if config.GEMINI_API_KEY:
            try:
                genai.configure(api_key=config.GEMINI_API_KEY)
                # 이제 단일 모델을 사용합니다.
                self.model = genai.GenerativeModel(config.AI_RESPONSE_MODEL_NAME)
                self.embedding_model_name = "models/embedding-001"
                logger.info("Gemini API 및 모델 설정 완료.")
                self.gemini_configured = True
            except Exception as e:
                logger.critical(f"Gemini API 설정 실패: {e}. AI 기능 비활성화됨.", exc_info=True)

    @property
    def is_ready(self) -> bool:
        """AI 핸들러가 모든 기능을 수행할 준비가 되었는지 확인합니다."""
        return self.gemini_configured and self.bot.db is not None and self.tools_cog is not None

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

    # --- 대화 기록 및 RAG 관련 함수 ---
    async def add_message_to_history(self, message: discord.Message):
        # (이전과 동일, 변경 없음)
        if not self.is_ready or not config.AI_MEMORY_ENABLED or not message.guild: return
        is_guild_ai_enabled = await db_utils.get_guild_setting(self.bot.db, message.guild.id, 'ai_enabled', default=True)
        if not is_guild_ai_enabled: return
        allowed_channels = await db_utils.get_guild_setting(self.bot.db, message.guild.id, 'ai_allowed_channels')
        is_ai_allowed_channel = message.channel.id in allowed_channels if allowed_channels else config.CHANNEL_AI_CONFIG.get(message.channel.id, {}).get("allowed", False)
        if not is_ai_allowed_channel: return

        try:
            await self.bot.db.execute(
                "INSERT INTO conversation_history (message_id, guild_id, channel_id, user_id, user_name, content, is_bot, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (message.id, message.guild.id, message.channel.id, message.author.id, message.author.display_name, message.content, message.author.bot, message.created_at.isoformat())
            )
            await self.bot.db.commit()
            if not message.author.bot and len(message.content) > 1:
                asyncio.create_task(self._create_and_save_embedding(message.id, message.content, message.guild.id))
        except Exception as e:
            logger.error(f"대화 기록 저장 중 DB 오류: {e}", exc_info=True, extra={'guild_id': message.guild.id})

    async def _create_and_save_embedding(self, message_id: int, content: str, guild_id: int):
        # (이전과 동일, 변경 없음)
        try:
            is_limited, _ = await self._check_global_rate_limit('gemini_embedding_calls', config.API_EMBEDDING_RPD_LIMIT)
            if is_limited: return
            embedding_result = await genai.embed_content_async(model=self.embedding_model_name, content=content, task_type="retrieval_document")
            await db_utils.increment_api_counter(self.bot.db, 'gemini_embedding_calls')
            embedding_blob = pickle.dumps(embedding_result['embedding'])
            await self.bot.db.execute("UPDATE conversation_history SET embedding = ? WHERE message_id = ?", (embedding_blob, message_id))
            await self.bot.db.commit()
        except Exception as e:
            logger.error(f"임베딩 생성/저장 중 오류 (메시지 ID: {message_id}): {e}", exc_info=True, extra={'guild_id': guild_id})

    async def _get_rag_context(self, channel_id: int, user_id: int, query: str) -> str:
        """RAG를 위한 컨텍스트를 DB에서 검색합니다."""
        # (이전 _find_similar_conversations 와 거의 동일, 이름만 변경)
        try:
            is_limited, _ = await self._check_global_rate_limit('gemini_embedding_calls', config.API_EMBEDDING_RPD_LIMIT)
            if is_limited: return ""
            query_embedding_result = await genai.embed_content_async(model=self.embedding_model_name, content=query, task_type="retrieval_query")
            await db_utils.increment_api_counter(self.bot.db, 'gemini_embedding_calls')
            query_embedding = np.array(query_embedding_result['embedding'])
            async with self.bot.db.execute("SELECT content, embedding FROM conversation_history WHERE channel_id = ? AND user_id = ? AND embedding IS NOT NULL ORDER BY created_at DESC LIMIT 100;", (channel_id, user_id)) as cursor:
                rows = await cursor.fetchall()
            if not rows: return ""

            # 코사인 유사도 계산
            def _cosine_similarity(v1, v2):
                norm_v1 = np.linalg.norm(v1)
                norm_v2 = np.linalg.norm(v2)
                if norm_v1 == 0 or norm_v2 == 0: return 0.0
                return np.dot(v1, v2) / (norm_v1 * norm_v2)

            similarities = [(sim, content) for content, blob in rows if (sim := _cosine_similarity(query_embedding, pickle.loads(blob))) > 0.7]
            similarities.sort(key=lambda x: x[0], reverse=True)
            top_conversations = [content for _, content in similarities[:5]]
            if not top_conversations: return ""
            return "참고할 만한 과거 대화 내용:\n" + "\n".join(f"- {conv}" for conv in reversed(top_conversations))
        except Exception as e:
            logger.error(f"RAG 컨텍스트 검색 중 오류: {e}", exc_info=True, extra={'guild_id': self.bot.get_channel(channel_id).guild.id})
            return ""

    # --- 에이전트 핵심 로직 ---
    def _parse_tool_call(self, text: str) -> dict | None:
        """LLM의 응답에서 <tool_call> JSON 블록을 파싱합니다."""
        match = re.search(r'<tool_call>\s*({.*?})\s*</tool_call>', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                return None
        return None

    async def _execute_tool(self, tool_call: dict, guild_id: int) -> dict:
        """주어진 tool_call을 실행하고 결과를 반환합니다."""
        tool_name = tool_call.get('tool_to_use')
        parameters = tool_call.get('parameters', {})
        log_extra = {'guild_id': guild_id}

        if not tool_name:
            return {"error": "tool_to_use가 지정되지 않았습니다."}

        try:
            tool_method = getattr(self.tools_cog, tool_name)
            logger.info(f"Executing tool: {tool_name} with params: {parameters}", extra=log_extra)
            result = await tool_method(**parameters)
            return {"result": result}
        except AttributeError:
            logger.error(f"존재하지 않는 도구 호출 시도: '{tool_name}'", extra=log_extra)
            return {"error": f"'{tool_name}'이라는 도구를 찾을 수 없습니다."}
        except Exception as e:
            logger.error(f"도구 '{tool_name}' 실행 중 예기치 않은 오류: {e}", exc_info=True, extra=log_extra)
            return {"error": "도구 실행 중 예상치 못한 오류가 발생했습니다."}

    async def process_agent_message(self, message: discord.Message):
        """사용자 메시지에 대한 새로운 ReAct 스타일 에이전트 워크플로우를 처리합니다."""
        if not self.is_ready: return

        user_query = message.content.replace(f'<@!{self.bot.user.id}>', '').replace(f'<@{self.bot.user.id}>', '').strip()
        if not user_query:
            await message.reply(config.MSG_AI_NO_CONTENT.format(bot_name=self.bot.user.name), mention_author=False)
            return

        log_extra = {'guild_id': message.guild.id}

        # 1. 대화 기록 구성 (RAG + 현재 대화)
        history = []
        # RAG 컨텍스트 추가
        rag_context = await self._get_rag_context(message.channel.id, message.author.id, user_query)
        if rag_context:
            history.append({'role': 'system', 'parts': [rag_context]})

        # 최근 대화 기록 추가
        async for msg in message.channel.history(limit=5):
            role = 'model' if msg.author == self.bot.user else 'user'
            history.append({'role': role, 'parts': [msg.content]})
        history.reverse() # 시간 순서대로 정렬

        # 현재 사용자 메시지 추가
        history.append({'role': 'user', 'parts': [user_query]})

        # 시스템 프롬프트 추가
        system_prompt = config.AGENT_SYSTEM_PROMPT
        custom_persona = await db_utils.get_guild_setting(self.bot.db, message.guild.id, 'persona_text')
        if custom_persona:
            system_prompt = f"{custom_persona}\n\n{system_prompt}"

        chat_session = self.model.start_chat(history=history)

        async with message.channel.typing():
            for _ in range(5): # 최대 5번의 tool-call-result 루프
                is_limited, limit_message = await self._check_global_rate_limit('gemini_flash_daily_calls', config.API_FLASH_RPD_LIMIT)
                if is_limited:
                    await message.reply(limit_message, mention_author=False)
                    return

                # 2. LLM 호출
                response = await chat_session.send_message_async(system_instruction=system_prompt)
                await db_utils.increment_api_counter(self.bot.db, 'gemini_flash_daily_calls')
                response_text = response.text.strip()

                # 3. Tool Call 확인 및 실행
                tool_call = self._parse_tool_call(response_text)
                if tool_call:
                    logger.info(f"Tool call 감지: {tool_call}", extra=log_extra)
                    tool_result = await self._execute_tool(tool_call, message.guild.id)

                    # Tool 결과를 대화 기록에 추가하고 루프 계속
                    tool_result_str = json.dumps(tool_result, ensure_ascii=False)
                    chat_session.history.append({'role': 'model', 'parts': [response_text]}) # LLM의 tool-call 응답
                    chat_session.history.append({'role': 'user', 'parts': [f"<tool_result>\n{tool_result_str}\n</tool_result>"]}) # 시스템의 tool 실행 결과
                    continue

                # 4. Tool Call이 없으면 최종 응답으로 간주하고 종료
                else:
                    logger.info("최종 응답 생성.", extra=log_extra)
                    if response_text:
                        bot_response_message = await message.reply(response_text, mention_author=False)
                        await self.add_message_to_history(bot_response_message)
                    return

            # 루프가 5번 모두 돌았을 경우
            await message.reply("생각이 너무 많아져서 일단 멈췄어. 다시 말 걸어줄래?", mention_author=False)

    async def get_recent_conversation_text(self, channel_id: int, look_back: int) -> str | None:
        """DB에서 최근 대화 기록을 가져와 텍스트로 반환합니다. (FunCog용)"""
        try:
            async with self.bot.db.execute("SELECT user_name, content FROM conversation_history WHERE channel_id = ? ORDER BY created_at DESC LIMIT ?", (channel_id, look_back)) as cursor:
                rows = await cursor.fetchall()
            if not rows:
                return None
            return "\n".join([f"{row[0]}: {row[1]}" for row in reversed(rows)])
        except Exception as e:
            logger.error(f"최근 대화 기록 조회 중 오류(FunCog): {e}", exc_info=True, extra={'guild_id': self.bot.get_channel(channel_id).guild.id})
            return None

    async def generate_creative_text(self, channel: discord.TextChannel, author: discord.User, prompt_key: str, context: Dict[str, Any] | None = None) -> str | None:
        """
        단순한 프롬프트 기반의 텍스트를 생성합니다. (FunCog 등에서 사용)
        """
        if not self.is_ready:
            logger.warning(f"창의적 텍스트 생성 불가({prompt_key}): AI 핸들러 미준비.", extra={'guild_id': channel.guild.id})
            return config.MSG_AI_ERROR

        prompt_template = config.AI_CREATIVE_PROMPTS.get(prompt_key)
        if not prompt_template:
            logger.error(f"창의적 텍스트 생성 불가: config에서 프롬프트 키 '{prompt_key}'를 찾을 수 없음.")
            return config.MSG_CMD_ERROR

        final_prompt = prompt_template.format(**(context or {}))

        try:
            is_limited, limit_message = await self._check_global_rate_limit('gemini_flash_daily_calls', config.API_FLASH_RPD_LIMIT)
            if is_limited: return limit_message

            async with self.api_call_lock:
                self._record_api_call()
                response = await self.model.generate_content_async(final_prompt)
                await db_utils.increment_api_counter(self.bot.db, 'gemini_flash_daily_calls')

            return response.text.strip()
        except Exception as e:
            logger.error(f"창의적 텍스트 생성 중 오류({prompt_key}): {e}", exc_info=True, extra={'guild_id': channel.guild.id})
            return config.MSG_AI_ERROR


async def setup(bot: commands.Bot):
    await bot.add_cog(AIHandler(bot))

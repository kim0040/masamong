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
    (대화 관리, 도구 호출, 응답 생성)
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.tools_cog = bot.get_cog('ToolsCog')
        self.ai_user_cooldowns: Dict[int, datetime] = {}
        self.gemini_configured = False
        self.api_call_lock = asyncio.Lock()

        if config.GEMINI_API_KEY:
            try:
                genai.configure(api_key=config.GEMINI_API_KEY)
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

    # --- 대화 기록 및 RAG 관련 함수 ---
    async def add_message_to_history(self, message: discord.Message):
        if not self.is_ready or not config.AI_MEMORY_ENABLED or not message.guild: return
        try:
            is_guild_ai_enabled = await db_utils.get_guild_setting(self.bot.db, message.guild.id, 'ai_enabled', default=True)
            if not is_guild_ai_enabled: return
            allowed_channels = await db_utils.get_guild_setting(self.bot.db, message.guild.id, 'ai_allowed_channels')
            is_ai_allowed_channel = message.channel.id in allowed_channels if allowed_channels else False
            if not is_ai_allowed_channel: return

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
        try:
            # (API 호출 제한 로직은 간소화를 위해 이 예제에서는 생략)
            embedding_result = await genai.embed_content_async(model=self.embedding_model_name, content=content, task_type="retrieval_document")
            embedding_blob = pickle.dumps(embedding_result['embedding'])
            await self.bot.db.execute("UPDATE conversation_history SET embedding = ? WHERE message_id = ?", (embedding_blob, message_id))
            await self.bot.db.commit()
        except Exception as e:
            logger.error(f"임베딩 생성/저장 중 오류 (메시지 ID: {message_id}): {e}", exc_info=True, extra={'guild_id': guild_id})

    async def _get_rag_context(self, channel_id: int, user_id: int, query: str) -> Tuple[str, list[str]]:
        """RAG를 위한 컨텍스트를 DB에서 검색하고, 컨텍스트 문자열과 원본 메시지 내용 목록을 반환합니다."""
        try:
            query_embedding = np.array((await genai.embed_content_async(model=self.embedding_model_name, content=query, task_type="retrieval_query"))['embedding'])
            async with self.bot.db.execute("SELECT content, embedding FROM conversation_history WHERE channel_id = ? AND embedding IS NOT NULL ORDER BY created_at DESC LIMIT 100;", (channel_id,)) as cursor:
                rows = await cursor.fetchall()
            if not rows: return "", []

            def _cosine_similarity(v1, v2):
                norm_v1, norm_v2 = np.linalg.norm(v1), np.linalg.norm(v2)
                return np.dot(v1, v2) / (norm_v1 * norm_v2) if norm_v1 > 0 and norm_v2 > 0 else 0.0

            similarities = sorted([(sim, content) for content, blob in rows if (sim := _cosine_similarity(query_embedding, pickle.loads(blob))) > 0.75], key=lambda x: x[0], reverse=True)
            if not similarities: return "", []

            top_contents = [content for _, content in similarities[:3]]
            context_str = "참고할 만한 과거 대화 내용:\n" + "\n".join(f"- {c}" for c in top_contents)
            return context_str, top_contents
        except Exception as e:
            logger.error(f"RAG 컨텍스트 검색 중 오류: {e}", exc_info=True)
            return "", []

    # --- 에이전트 핵심 로직 ---
    def _parse_tool_call(self, text: str) -> dict | None:
        match = re.search(r'<tool_call>\s*({.*?})\s*</tool_call>', text, re.DOTALL)
        if match:
            try: return json.loads(match.group(1))
            except json.JSONDecodeError: return None
        return None

    async def _execute_tool(self, tool_call: dict, guild_id: int) -> dict:
        tool_name = tool_call.get('tool_to_use')
        parameters = tool_call.get('parameters', {})
        if not tool_name: return {"error": "tool_to_use가 지정되지 않았습니다."}

        try:
            tool_method = getattr(self.tools_cog, tool_name)
            logger.info(f"Executing tool: {tool_name} with params: {parameters}", extra={'guild_id': guild_id})
            return await tool_method(**parameters)
        except Exception as e:
            logger.error(f"도구 '{tool_name}' 실행 중 예기치 않은 오류: {e}", exc_info=True, extra={'guild_id': guild_id})
            return {"error": "도구 실행 중 예상치 못한 오류가 발생했습니다."}

    async def process_agent_message(self, message: discord.Message):
        if not self.is_ready: return
        user_query = message.content.replace(f'<@!{self.bot.user.id}>', '').replace(f'<@{self.bot.user.id}>', '').strip()
        if not user_query: return

        log_extra = {'guild_id': message.guild.id}

        # 1. RAG 컨텍스트와 중복 제거용 콘텐츠 목록 가져오기
        rag_prompt_addition, rag_contents = await self._get_rag_context(message.channel.id, message.author.id, user_query)
        rag_content_set = set(rag_contents)

        # 2. 중복을 피해 대화 기록 구성 (시간순: 오래된 -> 최신)
        history = []
        async for msg in message.channel.history(limit=15):
            if msg.content in rag_content_set:
                continue
            role = 'model' if msg.author.id == self.bot.user.id else 'user'
            history.append({'role': role, 'parts': [msg.content]})
            if len(history) >= 8:
                break
        history.reverse()

        # 3. 시스템 프롬프트 구성 (페르소나 + RAG 컨텍스트)
        system_prompt = config.AGENT_SYSTEM_PROMPT
        custom_persona = await db_utils.get_guild_setting(self.bot.db, message.guild.id, 'persona_text')
        if custom_persona:
            system_prompt = f"{custom_persona}\n\n{system_prompt}"
        if rag_prompt_addition:
            system_prompt = f"{rag_prompt_addition}\n\n{system_prompt}"

        # 4. Gemini API 호출 및 응답 처리 (상태 비저장 방식)
        async with message.channel.typing():
            try:
                full_conversation = history
                for i in range(5): # 최대 5번의 tool-call-result 루프
                    response = await self.model.generate_content_async(
                        full_conversation,
                        generation_config=genai.types.GenerationConfig(),
                        safety_settings=config.GEMINI_SAFETY_SETTINGS,
                        system_instruction=system_prompt,
                    )

                    response_text = ""
                    if response.parts:
                        response_text = "".join(part.text for part in response.parts).strip()

                    if not response_text and response.candidates[0].finish_reason.name != "STOP":
                         # 일부 모델은 response.text 대신 content.parts에 텍스트를 포함할 수 있습니다.
                         # 혹은 안전 설정, 길이 제한 등으로 인해 빈 응답이 올 수 있습니다.
                         logger.warning(f"Gemini로부터 빈 응답 수신. 종료 사유: {response.candidates[0].finish_reason.name}", extra=log_extra)

                    tool_call = self._parse_tool_call(response_text)

                    if tool_call:
                        logger.info(f"Tool call 감지 (시도 {i+1}): {tool_call}", extra=log_extra)
                        full_conversation.append({'role': 'model', 'parts': [response_text]})

                        tool_result = await self._execute_tool(tool_call, message.guild.id)
                        tool_result_str = json.dumps(tool_result, ensure_ascii=False, indent=2)

                        full_conversation.append({
                            'role': 'user',
                            'parts': [f"<tool_result>\n{tool_result_str}\n</tool_result>"]
                        })
                    else:
                        break

                logger.info("최종 응답 생성.", extra=log_extra)
                if response_text:
                    bot_response_message = await message.reply(response_text, mention_author=False)
                    await self.add_message_to_history(bot_response_message)
            except Exception as e:
                logger.error(f"에이전트 처리 중 오류: {e}", exc_info=True, extra=log_extra)
                await message.reply(config.MSG_AI_ERROR, mention_author=False)

async def setup(bot: commands.Bot):
    await bot.add_cog(AIHandler(bot))

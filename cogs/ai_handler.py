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

    async def _get_rag_context(self, channel_id: int, user_id: int, query: str) -> str:
        """RAG를 위한 컨텍스트를 DB에서 검색합니다."""
        try:
            query_embedding = np.array((await genai.embed_content_async(model=self.embedding_model_name, content=query, task_type="retrieval_query"))['embedding'])
            async with self.bot.db.execute("SELECT content, embedding FROM conversation_history WHERE channel_id = ? AND embedding IS NOT NULL ORDER BY created_at DESC LIMIT 100;", (channel_id,)) as cursor:
                rows = await cursor.fetchall()
            if not rows: return ""

            def _cosine_similarity(v1, v2):
                norm_v1, norm_v2 = np.linalg.norm(v1), np.linalg.norm(v2)
                return np.dot(v1, v2) / (norm_v1 * norm_v2) if norm_v1 > 0 and norm_v2 > 0 else 0.0

            similarities = sorted([(sim, content) for content, blob in rows if (sim := _cosine_similarity(query_embedding, pickle.loads(blob))) > 0.75], key=lambda x: x[0], reverse=True)
            if not similarities: return ""
            return "참고할 만한 과거 대화 내용:\n" + "\n".join(f"- {content}" for _, content in similarities[:3])
        except Exception as e:
            logger.error(f"RAG 컨텍스트 검색 중 오류: {e}", exc_info=True)
            return ""

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

        # 1. 대화 기록 구성 (RAG + 현재 대화)
        history = []
        rag_context = await self._get_rag_context(message.channel.id, message.author.id, user_query)
        if rag_context:
            history.append({'role': 'system', 'parts': [rag_context]})

        async for msg in message.channel.history(limit=8):
            role = 'model' if msg.author.id == self.bot.user.id else 'user'
            history.append({'role': role, 'parts': [msg.content]})
        history.reverse()

        system_prompt = config.AGENT_SYSTEM_PROMPT
        custom_persona = await db_utils.get_guild_setting(self.bot.db, message.guild.id, 'persona_text')
        if custom_persona:
            system_prompt = f"{custom_persona}\n\n{system_prompt}"

        chat_session = self.model.start_chat(history=history)

        async with message.channel.typing():
            try:
                for _ in range(5): # 최대 5번의 tool-call-result 루프
                    response = await chat_session.send_message_async(system_instruction=system_prompt)
                    response_text = response.text.strip()

                    tool_call = self._parse_tool_call(response_text)
                    if tool_call:
                        logger.info(f"Tool call 감지: {tool_call}", extra=log_extra)
                        tool_result = await self._execute_tool(tool_call, message.guild.id)
                        tool_result_str = json.dumps(tool_result, ensure_ascii=False)

                        # Gemini 1.5는 Tool-Call 응답을 history에 추가하지 않고 다음 프롬프트에 parts로 전달
                        response = await chat_session.send_message_async(
                            [f"<tool_result>\n{tool_result_str}\n</tool_result>"]
                        )
                        response_text = response.text.strip()
                        # Tool 재호출을 방지하기 위해, tool call이 없는지 한번 더 확인
                        if not self._parse_tool_call(response_text):
                            break
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

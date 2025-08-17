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
import time
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
        self.proactive_cooldowns: Dict[int, float] = {}
        self.gemini_configured = False
        self.api_call_lock = asyncio.Lock()

        if config.GEMINI_API_KEY:
            try:
                genai.configure(api_key=config.GEMINI_API_KEY)
                # We create model instances dynamically now, so we don't need a default one here.
                # self.model = genai.GenerativeModel(config.AI_RESPONSE_MODEL_NAME)
                self.embedding_model_name = "models/embedding-001"
                logger.info("Gemini API 설정 완료.")
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
            # This logic seems duplicated with `_handle_ai_interaction`.
            # However, we need to save history regardless of whether the bot responds.
            # We'll keep it for now but it could be refactored.
            is_guild_ai_enabled = await db_utils.get_guild_setting(self.bot.db, message.guild.id, 'ai_enabled', default=True)
            if not is_guild_ai_enabled: return

            allowed_channels = await db_utils.get_guild_setting(self.bot.db, message.guild.id, 'ai_allowed_channels')
            is_ai_allowed_channel = False
            if allowed_channels:
                 is_ai_allowed_channel = message.channel.id in allowed_channels
            else:
                 channel_config = config.CHANNEL_AI_CONFIG.get(message.channel.id, {})
                 is_ai_allowed_channel = channel_config.get("allowed", False)

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
            embedding_result = await genai.embed_content_async(model=self.embedding_model_name, content=content, task_type="retrieval_document")
            embedding_blob = pickle.dumps(embedding_result['embedding'])
            await self.bot.db.execute("UPDATE conversation_history SET embedding = ? WHERE message_id = ?", (embedding_blob, message_id))
            await self.bot.db.commit()
        except Exception as e:
            logger.error(f"임베딩 생성/저장 중 오류 (메시지 ID: {message_id}): {e}", exc_info=True, extra={'guild_id': guild_id})

    async def _get_rag_context(self, channel_id: int, user_id: int, query: str) -> Tuple[str, list[str]]:
        """RAG를 위한 컨텍스트를 DB에서 검색하고, 컨텍스트 문자열과 원본 메시지 내용 목록을 반환합니다."""
        log_extra = {'channel_id': channel_id, 'user_id': user_id}
        logger.info(f"RAG 컨텍스트 검색 시작. Query: '{query}'", extra=log_extra)
        try:
            query_embedding = np.array((await genai.embed_content_async(model=self.embedding_model_name, content=query, task_type="retrieval_query"))['embedding'])
            async with self.bot.db.execute("SELECT content, embedding FROM conversation_history WHERE channel_id = ? AND embedding IS NOT NULL ORDER BY created_at DESC LIMIT 100;", (channel_id,)) as cursor:
                rows = await cursor.fetchall()
            if not rows:
                logger.info("RAG: 검색할 임베딩 데이터가 없습니다.", extra=log_extra)
                return "", []

            def _cosine_similarity(v1, v2):
                norm_v1, norm_v2 = np.linalg.norm(v1), np.linalg.norm(v2)
                return np.dot(v1, v2) / (norm_v1 * norm_v2) if norm_v1 > 0 and norm_v2 > 0 else 0.0

            similarities = sorted([(sim, content) for content, blob in rows if (sim := _cosine_similarity(query_embedding, pickle.loads(blob))) > 0.75], key=lambda x: x[0], reverse=True)
            if not similarities:
                logger.info("RAG: 유사도 0.75 이상인 문서를 찾지 못했습니다.", extra=log_extra)
                return "", []

            top_contents = [content for _, content in similarities[:3]]
            context_str = "참고할 만한 과거 대화 내용:\n" + "\n".join(f"- {c}" for c in top_contents)
            logger.info(f"RAG: {len(top_contents)}개의 유사한 대화 내용을 찾았습니다.", extra=log_extra)
            logger.debug(f"RAG 결과: {context_str}", extra=log_extra)
            return context_str, top_contents
        except Exception as e:
            logger.error(f"RAG 컨텍스트 검색 중 오류: {e}", exc_info=True, extra=log_extra)
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

        log_extra = {
            'guild_id': message.guild.id,
            'channel_id': message.channel.id,
            'user_id': message.author.id
        }
        logger.info(f"에이전트 처리 시작. Query: '{user_query}'", extra=log_extra)

        try:
            # 1. RAG 및 대화 기록 컨텍스트 구성
            rag_prompt_addition, rag_contents = await self._get_rag_context(message.channel.id, message.author.id, user_query)
            rag_content_set = set(rag_contents)

            history = []
            async for msg in message.channel.history(limit=15):
                if msg.id == message.id: continue # 현재 메시지는 제외
                if msg.content in rag_content_set:
                    logger.debug(f"중복된 RAG 컨텍스트 메시지 건너뛰기: '{msg.content[:50]}...'", extra=log_extra)
                    continue
                role = 'model' if msg.author.id == self.bot.user.id else 'user'
                history.append({'role': role, 'parts': [msg.content]})
                if len(history) >= 8:
                    break
            history.reverse()
            logger.info(f"{len(history)}개의 최근 대화 기록과 {len(rag_contents)}개의 RAG 컨텍스트를 조합.", extra=log_extra)

            # 2. 시스템 프롬프트 구성
            system_prompt_str = config.AGENT_SYSTEM_PROMPT
            custom_persona = await db_utils.get_guild_setting(self.bot.db, message.guild.id, 'persona_text')
            if custom_persona:
                system_prompt_str = f"{custom_persona}\n\n{system_prompt_str}"
            if rag_prompt_addition:
                system_prompt_str = f"{rag_prompt_addition}\n\n{system_prompt_str}"
            logger.debug(f"최종 시스템 프롬프트: \n---\n{system_prompt_str}\n---", extra=log_extra)

            async with message.channel.typing():
                # 3. 모델 및 대화 생성
                model_with_dynamic_prompt = genai.GenerativeModel(
                    config.AI_RESPONSE_MODEL_NAME,
                    system_instruction=system_prompt_str
                )

                # 첫 번째 `generate_content_async` 호출을 위한 대화 목록
                # 시스템 프롬프트는 모델에 설정되었으므로, 여기에는 사용자 쿼리만 포함된 기록을 전달
                full_conversation = history + [{'role': 'user', 'parts': [user_query]}]

                # 4. 도구 사용 루프
                for i in range(5): # 최대 5번의 도구 호출 허용
                    logger.info(f"모델 생성 시도 #{i+1}", extra=log_extra)
                    logger.debug(f"모델 전달 대화 내용: {json.dumps(full_conversation, ensure_ascii=False, indent=2)}", extra=log_extra)

                    response = await model_with_dynamic_prompt.generate_content_async(
                        full_conversation,
                        generation_config=genai.types.GenerationConfig(),
                        safety_settings=config.GEMINI_SAFETY_SETTINGS
                    )

                    response_text = "".join(part.text for part in response.parts).strip() if response.parts else ""
                    logger.debug(f"모델 응답 수신: '{response_text}'", extra=log_extra)

                    if not response_text and response.candidates[0].finish_reason.name != "STOP":
                        logger.warning(f"Gemini로부터 빈 응답 수신. 종료 사유: {response.candidates[0].finish_reason.name}", extra=log_extra)

                    tool_call = self._parse_tool_call(response_text)

                    if tool_call:
                        logger.info(f"Tool call 감지: {tool_call}", extra=log_extra)
                        full_conversation.append({'role': 'model', 'parts': [response_text]}) # 모델의 응답(도구호출)을 대화에 추가

                        tool_result = await self._execute_tool(tool_call, message.guild.id)
                        tool_result_str = json.dumps(tool_result, ensure_ascii=False, indent=2)
                        logger.info(f"Tool 실행 결과: {tool_result_str}", extra=log_extra)

                        # 도구 실행 결과를 대화에 추가하여 다음 생성에 사용
                        full_conversation.append({
                            'role': 'user', # Gemini에서는 tool role이 별도로 없고, user role로 결과를 전달
                            'parts': [f"<tool_result>\n{tool_result_str}\n</tool_result>"]
                        })
                    else:
                        # 도구 호출이 없으면 루프 종료
                        break

                # 5. 최종 답변 전송
                if response_text:
                    logger.info(f"최종 응답 생성: '{response_text}'", extra=log_extra)
                    bot_response_message = await message.reply(response_text, mention_author=False)
                    await self.add_message_to_history(bot_response_message)
                else:
                    logger.warning("모델이 최종적으로 빈 응답을 반환했습니다.", extra=log_extra)
                    await message.reply(config.MSG_AI_ERROR, mention_author=False) # 빈 응답도 에러 처리

        except Exception as e:
            logger.error(f"에이전트 처리 중 오류: {e}", exc_info=True, extra=log_extra)
            await message.reply(config.MSG_AI_ERROR, mention_author=False)

    async def should_proactively_respond(self, message: discord.Message) -> bool:
        """봇이 대화에 자발적으로 참여해야 할지 여부를 결정합니다."""
        conf = config.AI_PROACTIVE_RESPONSE_CONFIG
        if not conf.get("enabled"): return False

        now = time.time()
        cooldown_seconds = conf.get("cooldown_seconds", 90)
        last_proactive_time = self.proactive_cooldowns.get(message.channel.id, 0)
        if (now - last_proactive_time) < cooldown_seconds: return False

        if len(message.content) < conf.get("min_message_length", 10): return False

        msg_lower = message.content.lower()
        if not any(keyword in msg_lower for keyword in conf.get("keywords", [])): return False

        if random.random() > conf.get("probability", 0.1): return False

        try:
            look_back = conf.get("look_back_count", 5)
            history_msgs = [f"User({msg.author.display_name}): {msg.content}" async for msg in message.channel.history(limit=look_back)]
            history_msgs.reverse()
            conversation_context = "\n".join(history_msgs)

            gatekeeper_prompt = f"""{conf['gatekeeper_persona']}

--- 최근 대화 내용 ---
{conversation_context}
---
사용자의 마지막 메시지: "{message.content}"
---

자, 판단해. Yes or No?"""

            lite_model = genai.GenerativeModel(config.AI_INTENT_MODEL_NAME)
            response = await lite_model.generate_content_async(
                gatekeeper_prompt,
                generation_config=genai.types.GenerationConfig(temperature=0.0),
                safety_settings=config.GEMINI_SAFETY_SETTINGS,
            )

            decision = response.text.strip().upper()
            logger.info(f"게이트키퍼 AI 결정: '{decision}'", extra={'guild_id': message.guild.id})

            if "YES" in decision:
                self.proactive_cooldowns[message.channel.id] = now
                return True
        except Exception as e:
            logger.error(f"게이트키퍼 AI 실행 중 오류: {e}", exc_info=True, extra={'guild_id': message.guild.id})

        return False

    async def get_recent_conversation_text(self, channel_id: int, look_back: int = 20) -> str:
        """지정된 채널의 최근 대화 기록을 텍스트로 가져옵니다."""
        if not self.bot.db: return ""

        query = "SELECT user_name, content FROM conversation_history WHERE channel_id = ? AND is_bot = 0 ORDER BY created_at DESC LIMIT ?"
        try:
            async with self.bot.db.execute(query, (channel_id, look_back)) as cursor:
                rows = await cursor.fetchall()
            if not rows: return ""

            rows.reverse()
            return "\n".join([f"User({row['user_name']}): {row['content']}" for row in rows])
        except Exception as e:
            logger.error(f"최근 대화 기록 조회 중 DB 오류: {e}", exc_info=True)
            return ""

    async def generate_creative_text(self, channel: discord.TextChannel, author: discord.User, prompt_key: str, context: dict) -> str:
        """창의적인 텍스트 생성을 위한 전용 AI 호출 함수입니다."""
        if not self.is_ready: return config.MSG_AI_ERROR

        try:
            prompt_template = config.AI_CREATIVE_PROMPTS.get(prompt_key)
            if not prompt_template:
                logger.error(f"'{prompt_key}'에 해당하는 크리에이티브 프롬프트를 찾을 수 없습니다.")
                return config.MSG_CMD_ERROR

            user_prompt = prompt_template.format(**context)

            channel_config = config.CHANNEL_AI_CONFIG.get(channel.id, {})
            persona = channel_config.get("persona", "")
            rules = channel_config.get("rules", "")
            system_prompt = f"{persona}\n\n{rules}"

            model_with_prompt = genai.GenerativeModel(
                model_name=config.AI_RESPONSE_MODEL_NAME,
                system_instruction=system_prompt
            )

            response = await model_with_prompt.generate_content_async(
                user_prompt,
                safety_settings=config.GEMINI_SAFETY_SETTINGS
            )

            return response.text.strip()
        except KeyError as e:
            logger.error(f"프롬프트 포맷팅 중 키 오류: '{prompt_key}' 프롬프트에 필요한 컨텍스트({e})가 없습니다.", extra={'guild_id': channel.guild.id})
            return config.MSG_CMD_ERROR
        except Exception as e:
            logger.error(f"Creative text 생성 중 오류: {e}", exc_info=True, extra={'guild_id': channel.guild.id})
            return config.MSG_AI_ERROR

async def setup(bot: commands.Bot):
    await bot.add_cog(AIHandler(bot))

# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
import google.generativeai as genai
from google.generativeai.types import GenerationConfig, FunctionCallingConfig
from google.generativeai.types.content_types import Tool

from datetime import datetime, timedelta, time
import asyncio
import pytz
from collections import deque
from typing import Dict, Any, Tuple, List
import numpy as np
from sentence_transformers import SentenceTransformer
import json

import config
from logger_config import logger
from .weather_cog import WeatherCog
from .fun_cog import FunCog
from .poll_cog import PollCog

KST = pytz.timezone('Asia/Seoul')

class AIHandler(commands.Cog):
    """Gemini AI 상호작용 (RAG, 함수 호출 라우터)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ai_user_cooldowns: Dict[int, datetime] = {}
        self.gemini_configured = False
        self.st_model = None
        self.api_call_lock = asyncio.Lock()
        self.minute_request_timestamps = deque()
        self.daily_request_count = 0
        self.daily_limit_reset_time = self._get_next_kst_midnight()
        self.last_proactive_response_times: Dict[int, datetime] = {}
        self.tools = []
        self.tool_map = {}

        if config.GEMINI_API_KEY:
            try:
                genai.configure(api_key=config.GEMINI_API_KEY)
                self.model = genai.GenerativeModel(config.AI_MODEL_NAME)
                self.intent_model = genai.GenerativeModel(config.AI_INTENT_MODEL_NAME)
                logger.info("Gemini API 및 모델 설정 완료.")
                self.gemini_configured = True
            except Exception as e:
                logger.critical(f"Gemini API 설정 실패: {e}", exc_info=True)

        try:
            model_name = 'jhgan/ko-sroberta-multilingual-v1'
            self.st_model = SentenceTransformer(model_name)
            self.embedding_dim = self.st_model.get_sentence_embedding_dimension()
            logger.info(f"SentenceTransformer 모델 로드 성공: {model_name} (차원: {self.embedding_dim})")
        except Exception as e:
            logger.error(f"SentenceTransformer 모델 로드 실패: {e}", exc_info=True)

    @commands.Cog.listener()
    async def on_ready(self):
        """봇이 준비되면 도구를 등록합니다."""
        self._register_tools()

    def _register_tools(self):
        """Cogs에서 도구 함수를 가져와 등록합니다."""
        weather_cog = self.bot.get_cog('WeatherCog')
        fun_cog = self.bot.get_cog('FunCog')
        poll_cog = self.bot.get_cog('PollCog')

        if weather_cog:
            self.tool_map[weather_cog.get_weather_forecast.__name__] = weather_cog.get_weather_forecast
        if fun_cog:
            # `get_daily_fortune`은 제거되었으므로 등록하지 않음
            self.tool_map[fun_cog.get_conversation_for_summary.__name__] = fun_cog.get_conversation_for_summary
        if poll_cog:
            self.tool_map[poll_cog.create_poll.__name__] = poll_cog.create_poll

        if self.tool_map:
            self.tools = [Tool.from_function(func) for func in self.tool_map.values()]
        logger.info(f"AI 도구 등록 완료: {list(self.tool_map.keys())}")


    @property
    def is_ready(self) -> bool:
        return self.gemini_configured and self.st_model is not None and self.bot.db is not None

    def _get_next_kst_midnight(self) -> datetime:
        now_kst = datetime.now(KST)
        tomorrow = now_kst.date() + timedelta(days=1)
        return KST.localize(datetime.combine(tomorrow, time(0, 0)))
    async def _check_global_rate_limit(self) -> Tuple[bool, str | None]:
        async with self.api_call_lock:
            now = datetime.now(); one_minute_ago = now - timedelta(minutes=1)
            while self.minute_request_timestamps and self.minute_request_timestamps[0] < one_minute_ago: self.minute_request_timestamps.popleft()
            if len(self.minute_request_timestamps) >= config.API_RPM_LIMIT: return True, config.MSG_AI_RATE_LIMITED
            now_kst = now.astimezone(KST)
            if now_kst >= self.daily_limit_reset_time:
                self.daily_request_count = 0; self.daily_limit_reset_time = self._get_next_kst_midnight()
            if self.daily_request_count >= config.API_RPD_LIMIT: return True, config.MSG_AI_DAILY_LIMITED
            return False, None
    def _record_api_call(self):
        now = datetime.now(); self.minute_request_timestamps.append(now); self.daily_request_count += 1
    def _is_on_cooldown(self, user_id: int) -> Tuple[bool, float]:
        now = datetime.now()
        if user_id in self.ai_user_cooldowns:
            remaining = (self.ai_user_cooldowns[user_id] + timedelta(seconds=config.AI_COOLDOWN_SECONDS)) - now
            if remaining.total_seconds() > 0: return True, remaining.total_seconds()
        return False, 0.0
    def _update_cooldown(self, user_id: int):
        self.ai_user_cooldowns[user_id] = datetime.now()
    async def _create_and_save_embedding(self, message_id: int, content: str):
        if not self.is_ready or not content: return
        try:
            loop = asyncio.get_running_loop()
            embedding = await loop.run_in_executor(None, self.st_model.encode, content)
            embedding_bytes = embedding.astype(np.float32).tobytes()
            row_id_cursor = await self.bot.db.execute("SELECT rowid FROM conversation_history WHERE message_id = ?", (message_id,))
            row_id_result = await row_id_cursor.fetchone()
            if not row_id_result: return
            rowid = row_id_result[0]
            await self.bot.db.execute("UPDATE conversation_history SET embedding = ? WHERE rowid = ?", (embedding_bytes, rowid))
            await self.bot.db.execute("INSERT INTO vss_conversations(rowid, embedding) VALUES (?, ?)", (rowid, embedding_bytes))
            await self.bot.db.commit()
        except Exception as e: logger.error(f"임베딩 생성/저장 중 오류: {e}", exc_info=True)
    async def add_message_to_history(self, message: discord.Message):
        if not config.AI_MEMORY_ENABLED or not self.is_ready: return
        if not config.CHANNEL_AI_CONFIG.get(message.channel.id): return
        sql = "INSERT INTO conversation_history (message_id, guild_id, channel_id, user_id, user_name, content, is_bot, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?);"
        params = (message.id, message.guild.id if message.guild else 0, message.channel.id, message.author.id, message.author.display_name, message.content, message.author.bot, message.created_at.isoformat())
        try:
            await self.bot.db.execute(sql, params)
            await self.bot.db.commit()
            if not message.author.bot and len(message.content) > 10: asyncio.create_task(self._create_and_save_embedding(message.id, message.content))
        except Exception as e:
            if "UNIQUE constraint failed" not in str(e): logger.error(f"대화 기록 DB 저장 중 오류: {e}", exc_info=True)
    async def _find_similar_history(self, query_embedding: np.ndarray, limit: int = 3) -> str:
        if not self.is_ready: return ""
        try:
            query_bytes = query_embedding.astype(np.float32).tobytes()
            vss_sql = "SELECT rowid, distance FROM vss_conversations WHERE vss_search(embedding, ?) AND distance < 0.5 LIMIT ?"
            async with self.bot.db.execute(vss_sql, (query_bytes, limit)) as cursor: similar_rows = await cursor.fetchall()
            if not similar_rows: return ""
            rowids = [row[0] for row in similar_rows]
            placeholders = ','.join('?' for _ in rowids)
            history_sql = f"SELECT user_name, content FROM conversation_history WHERE rowid IN ({placeholders}) ORDER BY created_at"
            async with self.bot.db.execute(history_sql, rowids) as cursor: results = await cursor.fetchall()
            return "\n".join([f"- {row[0]}: {row[1]}" for row in results])
        except Exception as e:
            logger.error(f"유사도 검색 중 오류: {e}", exc_info=True)
            return ""
    async def _get_history_from_db(self, channel_id: int) -> list:
        if not self.is_ready: return []
        sql = "SELECT user_id, user_name, is_bot, content FROM conversation_history WHERE channel_id = ? ORDER BY created_at DESC LIMIT ?"
        try:
            async with self.bot.db.execute(sql, (channel_id, config.AI_MEMORY_MAX_MESSAGES)) as cursor: rows = await cursor.fetchall()
            rows.reverse()
            history = []
            for row in rows:
                role = "model" if row[2] else "user"; user_identifier = f"User({row[0]}|{row[1]})"
                history.append({"role": role, "parts": [{"text": f"{user_identifier}: {row[3]}"}]})
            return history
        except Exception as e: logger.error(f"대화 기록 DB 조회 중 오류: {e}", exc_info=True); return []

    async def generate_system_message(self, text_to_rephrase: str, channel_id: int) -> str:
        """주어진 텍스트를 특정 채널의 페르소나에 맞게 변환합니다. (RAG/도구/히스토리 사용 안 함)"""
        if not self.is_ready: return text_to_rephrase
        channel_config = config.CHANNEL_AI_CONFIG.get(channel_id, {})
        persona = channel_config.get("persona")
        if not persona: return text_to_rephrase

        try:
            is_limited, _ = await self._check_global_rate_limit()
            if is_limited: return text_to_rephrase

            model = genai.GenerativeModel(config.AI_MODEL_NAME, system_instruction=persona)
            prompt = f"다음 문장을 너의 말투로 자연스럽게 바꿔서 말해줘: \"{text_to_rephrase}\""

            async with self.api_call_lock:
                self._record_api_call()
                response = await model.generate_content_async(prompt)

            return response.text.strip()
        except Exception as e:
            logger.error(f"시스템 메시지 생성 중 오류: {e}", exc_info=True)
            return text_to_rephrase

    async def process_ai_message(self, message: discord.Message):
        if not self.is_ready: return
        channel_config = config.CHANNEL_AI_CONFIG.get(message.channel.id)
        if not channel_config: return
        user_query = message.content.replace(f'<@!{self.bot.user.id}>', '').replace(f'<@{self.bot.user.id}>', '').strip()
        if not user_query:
            await message.reply(config.MSG_AI_NO_CONTENT.format(bot_name=self.bot.user.name), mention_author=False); return
        on_cooldown, remaining_time = self._is_on_cooldown(message.author.id)
        if on_cooldown:
            await message.reply(config.MSG_AI_COOLDOWN.format(remaining=remaining_time), mention_author=False); return
        self._update_cooldown(message.author.id)
        is_limited, limit_message = await self._check_global_rate_limit()
        if is_limited:
            await message.reply(limit_message, mention_author=False); return

        async with message.channel.typing():
            try:
                loop = asyncio.get_running_loop()
                query_embedding = await loop.run_in_executor(None, self.st_model.encode, user_query)
                rag_context = await self._find_similar_history(query_embedding)
                system_instructions = [channel_config.get("persona", ""), channel_config.get("rules", "")]
                if rag_context: system_instructions.append(f"다음은 관련된 과거 대화 내용이야. 참고해서 답변해줘.\n---\n{rag_context}\n---")
                model_with_tools = genai.GenerativeModel(config.AI_MODEL_NAME, safety_settings=config.GEMINI_SAFETY_SETTINGS, system_instruction="\n".join(filter(None, system_instructions)), tools=self.tools)
                history = await self._get_history_from_db(message.channel.id)
                chat_session = model_with_tools.start_chat(history=history)
                user_identifier = f"User({message.author.id}|{message.author.display_name})"
                response = await chat_session.send_message_async(f"{user_identifier}: {user_query}")

                function_call = response.candidates[0].content.parts[0].function_call
                if function_call:
                    tool_name = function_call.name
                    tool_args = {key: value for key, value in function_call.args.items()}
                    logger.info(f"AI가 도구 호출을 요청했습니다: {tool_name}({tool_args})")
                    tool_function = self.tool_map.get(tool_name)
                    if tool_function:
                        if 'channel_id' in tool_function.__code__.co_varnames: tool_args['channel_id'] = message.channel.id
                        if 'author_name' in tool_function.__code__.co_varnames: tool_args['author_name'] = message.author.display_name
                        if 'user_name' in tool_function.__code__.co_varnames: tool_args['user_name'] = message.author.display_name
                        function_result = await tool_function(**tool_args)
                        response = await chat_session.send_message_async(genai.Part.from_function_response(name=tool_name, response={"result": function_result}))

                final_text = response.text.strip()
                await message.reply(final_text, mention_author=False)

                bot_message = discord.Object(id=discord.utils.time_snowflake(datetime.now(pytz.utc)))
                bot_message.author = self.bot.user; bot_message.content = final_text; bot_message.channel = message.channel
                bot_message.guild = message.guild; bot_message.created_at = datetime.now(pytz.utc)
                await self.add_message_to_history(bot_message)
            except Exception as e:
                logger.error(f"AI 메시지 처리 중 예기치 않은 오류: {e}", exc_info=True)
                await message.reply(config.MSG_AI_ERROR, mention_author=False)

async def setup(bot: commands.Bot):
    await bot.add_cog(AIHandler(bot))

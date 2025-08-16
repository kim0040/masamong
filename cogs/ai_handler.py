# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
import google.generativeai as genai

from datetime import datetime, timedelta, time
import asyncio
import pytz
from collections import deque
from typing import Dict, Any, Tuple, List
import numpy as np
import importlib

import config
from logger_config import logger

KST = pytz.timezone('Asia/Seoul')

class AIHandler(commands.Cog):
    """Gemini AI 상호작용 (RAG with Gemini Embedding, 함수 호출, 비용 관리)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ai_user_cooldowns: Dict[int, datetime] = {}
        self.gemini_configured = False
        self.api_call_lock = asyncio.Lock()
        self.minute_request_timestamps = deque()
        self.minute_token_timestamps = deque()
        self.daily_request_count = 0
        self.daily_limit_reset_time = self._get_next_kst_midnight()
        self.last_proactive_response_times: Dict[int, datetime] = {}
        self.tools: List[content_types.Tool] = []
        self.tool_map: Dict[str, callable] = {}

        if config.GEMINI_API_KEY:
            try:
                genai.configure(api_key=config.GEMINI_API_KEY)
                self.model = genai.GenerativeModel(config.AI_MODEL_NAME)
                logger.info("Gemini API 및 모델 설정 완료.")
                self.gemini_configured = True
            except Exception as e:
                logger.critical(f"Gemini API 설정 실패: {e}", exc_info=True)

    @commands.Cog.listener()
    async def on_ready(self):
        self._register_tools()

    def _register_tools(self):
        self.tool_map = {}
        if not hasattr(config, 'AI_TOOLS'): return
        for tool_path in config.AI_TOOLS:
            try:
                parts = tool_path.split('.'); cog_name_str, func_name = parts[1], parts[2]
                cog_name_pascal = ''.join([p.capitalize() for p in cog_name_str.split('_')])
                cog_instance = self.bot.get_cog(cog_name_pascal)
                if cog_instance: self.tool_map[func_name] = getattr(cog_instance, func_name)
            except Exception as e: logger.error(f"도구 등록 실패: '{tool_path}'. 오류: {e}")
        if self.tool_map:
            # The google-generativeai SDK can directly use the function objects
            self.tools = list(self.tool_map.values())
        logger.info(f"AI 도구 등록 완료: {list(self.tool_map.keys())}")

    @property
    def is_ready(self) -> bool:
        return self.gemini_configured and self.bot.db is not None

    def _get_next_kst_midnight(self):
        return KST.localize(datetime.combine(datetime.now(KST).date() + timedelta(days=1), time(0, 0)))

    async def _check_global_rate_limit(self, tokens_to_be_used: int = 0) -> Tuple[bool, str | None]:
        async with self.api_call_lock:
            now = datetime.now()
            one_minute_ago = now - timedelta(minutes=1)

            while self.minute_request_timestamps and self.minute_request_timestamps[0] < one_minute_ago:
                self.minute_request_timestamps.popleft()
            if len(self.minute_request_timestamps) >= config.API_RPM_LIMIT:
                logger.warning(f"RPM 제한 도달.")
                return True, config.MSG_AI_RATE_LIMITED

            current_tpm = 0
            while self.minute_token_timestamps and self.minute_token_timestamps[0][0] < one_minute_ago:
                self.minute_token_timestamps.popleft()
            for _, tokens in self.minute_token_timestamps:
                current_tpm += tokens

            if current_tpm + tokens_to_be_used > config.API_TPM_LIMIT:
                logger.warning(f"TPM 제한 도달 (현재: {current_tpm}, 요청: {tokens_to_be_used}, 한도: {config.API_TPM_LIMIT}).")
                return True, config.MSG_AI_RATE_LIMITED

            if now.astimezone(KST) >= self.daily_limit_reset_time:
                self.daily_request_count = 0; self.daily_limit_reset_time = self._get_next_kst_midnight()
            if self.daily_request_count >= config.API_RPD_LIMIT:
                return True, config.MSG_AI_DAILY_LIMITED

            return False, None

    def _record_api_call(self, tokens_used: int):
        now = datetime.now()
        self.minute_request_timestamps.append(now)
        self.minute_token_timestamps.append((now, tokens_used))
        self.daily_request_count += 1
        logger.debug(f"Gemini API 호출 기록. 토큰: {tokens_used}, 분당 요청: {len(self.minute_request_timestamps)}, 분당 토큰: {sum(t[1] for t in self.minute_token_timestamps)}, 일일 요청: {self.daily_request_count}")

    def _is_on_cooldown(self, user_id: int):
        if user_id in self.ai_user_cooldowns:
            remaining = (self.ai_user_cooldowns[user_id] + timedelta(seconds=config.AI_COOLDOWN_SECONDS)) - datetime.now()
            if remaining.total_seconds() > 0: return True, remaining.total_seconds()
        return False, 0.0
    def _update_cooldown(self, user_id: int): self.ai_user_cooldowns[user_id] = datetime.now()

    async def _create_and_save_embedding(self, message_id: int, content: str):
        if not self.is_ready or not content: return
        try:
            # Gemini Embedding API 사용
            result = await genai.embed_content_async(
                model=config.AI_EMBEDDING_MODEL_NAME,
                content=content,
                task_type="retrieval_document", # 저장되는 문서는 'document'로 지정
                output_dimensionality=768 # 차원 축소
            )
            embedding_bytes = np.array(result['embedding'], dtype=np.float32).tobytes()

            cursor = await self.bot.db.execute("SELECT rowid FROM conversation_history WHERE message_id = ?", (message_id,))
            row = await cursor.fetchone()
            if row:
                await self.bot.db.execute("UPDATE conversation_history SET embedding = ? WHERE rowid = ?", (embedding_bytes, row[0]))
                await self.bot.db.execute("INSERT INTO vss_conversations(rowid, embedding) VALUES (?, ?)", (row[0], embedding_bytes))
                await self.bot.db.commit()
        except Exception as e: logger.error(f"임베딩 생성/저장 오류: {e}", exc_info=True)

    async def add_message_to_history(self, message: discord.Message):
        if not config.AI_MEMORY_ENABLED or not self.is_ready or not config.CHANNEL_AI_CONFIG.get(message.channel.id): return
        sql = "INSERT INTO conversation_history (message_id, guild_id, channel_id, user_id, user_name, content, is_bot, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?);"
        params = (message.id, message.guild.id, message.channel.id, message.author.id, message.author.display_name, message.content, message.author.bot, message.created_at.isoformat())
        try:
            await self.bot.db.execute(sql, params); await self.bot.db.commit()
            if not message.author.bot and len(message.content) > 10:
                asyncio.create_task(self._create_and_save_embedding(message.id, message.content))
        except Exception as e:
            if "UNIQUE" not in str(e): logger.error(f"대화기록 저장 오류: {e}", exc_info=True)

    async def _find_similar_history(self, user_query: str, limit: int = 3) -> str:
        if not self.is_ready: return ""
        try:
            # 쿼리 임베딩 생성
            result = await genai.embed_content_async(
                model=config.AI_EMBEDDING_MODEL_NAME,
                content=user_query,
                task_type="retrieval_query", # 검색용 쿼리는 'query'로 지정
                output_dimensionality=768
            )
            query_embedding_bytes = np.array(result['embedding'], dtype=np.float32).tobytes()

            sql = "SELECT rowid, distance FROM vss_conversations WHERE vss_search(embedding, ?) AND distance < 0.5 LIMIT ?"
            cursor = await self.bot.db.execute(sql, (query_embedding_bytes, limit)); similar_rows = await cursor.fetchall()
            if not similar_rows: return ""

            rowids = [row[0] for row in similar_rows]; placeholders = ','.join('?' for _ in rowids)
            sql = f"SELECT user_name, content FROM conversation_history WHERE rowid IN ({placeholders}) ORDER BY created_at"
            cursor = await self.bot.db.execute(sql, rowids); results = await cursor.fetchall()
            return "\n".join([f"- {r[0]}: {r[1]}" for r in results])
        except Exception as e: logger.error(f"유사도 검색 오류: {e}", exc_info=True); return ""

    async def _get_history_from_db(self, channel_id: int) -> list:
        sql = "SELECT user_id, user_name, is_bot, content FROM conversation_history WHERE channel_id = ? ORDER BY created_at DESC LIMIT ?"
        cursor = await self.bot.db.execute(sql, (channel_id, config.AI_MEMORY_MAX_MESSAGES)); rows = await cursor.fetchall(); rows.reverse()
        return [{"role": "model" if row[2] else "user", "parts": [{"text": f"User({r[0]}|{r[1]}): {r[3]}"}]} for r in rows]

    async def _get_persona_for_channel(self, guild_id, channel_id):
        cursor = await self.bot.db.execute("SELECT value FROM guild_settings WHERE guild_id = ? AND setting_name = 'persona'", (guild_id,)); row = await cursor.fetchone()
        return row[0] if row and row[0] else config.CHANNEL_AI_CONFIG.get(channel_id, {}).get("persona", "")

    async def generate_system_message(self, text_to_rephrase, channel_id, guild_id):
        if not self.is_ready: return text_to_rephrase
        persona = await self._get_persona_for_channel(guild_id, channel_id)
        if not persona: return text_to_rephrase
        try:
            is_limited, msg = await self._check_global_rate_limit();
            if is_limited: return text_to_rephrase
            model = genai.GenerativeModel(config.AI_MODEL_NAME, system_instruction=persona)
            prompt = f"다음 문장을 너의 말투로 자연스럽게 바꿔서 말해줘: \"{text_to_rephrase}\""
            self._record_api_call(await model.count_tokens_async(prompt))
            response = await model.generate_content_async(prompt)
            return response.text.strip()
        except Exception as e: logger.error(f"시스템 메시지 생성 오류: {e}", exc_info=True); return text_to_rephrase

    async def process_direct_prompt_task(self, prompt, author, channel):
        if not self.is_ready: return config.MSG_AI_ERROR
        on_cooldown, rem = self._is_on_cooldown(author.id)
        if on_cooldown: return config.MSG_AI_COOLDOWN.format(remaining=rem)
        self._update_cooldown(author.id)
        persona = await self._get_persona_for_channel(channel.guild.id, channel.id)
        model = genai.GenerativeModel(config.AI_MODEL_NAME, system_instruction=persona)
        prompt_tokens = await model.count_tokens_async(prompt)
        is_limited, msg = await self._check_global_rate_limit(prompt_tokens)
        if is_limited: return msg
        try:
            self._record_api_call(prompt_tokens) # 응답 토큰이 없어 정확하지 않음
            response = await model.generate_content_async(prompt)
            return response.text.strip()
        except Exception as e: logger.error(f"직접 프롬프트 처리 오류: {e}", exc_info=True); return config.MSG_AI_ERROR

    async def process_ai_message(self, message: discord.Message):
        if not self.is_ready or not config.CHANNEL_AI_CONFIG.get(message.channel.id): return
        user_query = message.content.replace(f'<@!{self.bot.user.id}>', '').replace(f'<@{self.bot.user.id}>', '').strip()
        if not user_query: await message.reply(config.MSG_AI_NO_CONTENT.format(bot_name=self.bot.user.name), mention_author=False); return
        on_cooldown, rem = self._is_on_cooldown(message.author.id)
        if on_cooldown: await message.reply(config.MSG_AI_COOLDOWN.format(remaining=rem), mention_author=False); return
        self._update_cooldown(message.author.id)

        async with message.channel.typing():
            try:
                rag_context = await self._find_similar_history(user_query)
                persona = await self._get_persona_for_channel(message.guild.id, message.channel.id)
                rules = config.CHANNEL_AI_CONFIG.get(message.channel.id, {}).get("rules", "")
                system_instructions = [persona, rules]
                if rag_context: system_instructions.append(f"다음은 관련된 과거 대화 내용이야. 참고해서 답변해줘.\n---\n{rag_context}\n---")

                model_with_tools = genai.GenerativeModel(config.AI_MODEL_NAME, system_instruction="\n".join(filter(None, system_instructions)), tools=self.tools)
                history = await self._get_history_from_db(message.channel.id)
                chat_session = model_with_tools.start_chat(history=history)
                final_query = f"User({message.author.id}|{message.author.display_name}): {user_query}"

                prompt_tokens = await model_with_tools.count_tokens_async(chat_session.history + [content_types.to_content(final_query)])
                is_limited, msg = await self._check_global_rate_limit(prompt_tokens)
                if is_limited: await message.reply(msg, mention_author=False); return

                response = await chat_session.send_message_async(final_query)

                if (function_call := response.candidates[0].content.parts[0].function_call):
                    tool_name = function_call.name; tool_args = {key: value for key, value in function_call.args.items()}
                    tool_function = self.tool_map.get(tool_name)
                    if tool_function:
                        if 'channel_id' in tool_function.__code__.co_varnames: tool_args['channel_id'] = message.channel.id
                        if 'author_name' in tool_function.__code__.co_varnames: tool_args['author_name'] = message.author.display_name
                        function_result = await tool_function(**tool_args)
                        response = await chat_session.send_message_async(content_types.Part.from_function_response(name=tool_name, response={"result": function_result}))

                self._record_api_call(response.usage_metadata.total_token_count)
                final_text = response.text.strip()
                await message.reply(final_text, mention_author=False)

                bot_message = discord.Object(id=discord.utils.time_snowflake(datetime.now(pytz.utc)))
                bot_message.author = self.bot.user; bot_message.content = final_text; bot_message.channel = message.channel
                bot_message.guild = message.guild; bot_message.created_at = datetime.now(pytz.utc)
                await self.add_message_to_history(bot_message)
            except Exception as e:
                logger.error(f"AI 메시지 처리 오류: {e}", exc_info=True)
                await message.reply(config.MSG_AI_ERROR, mention_author=False)

async def setup(bot: commands.Bot):
    await bot.add_cog(AIHandler(bot))

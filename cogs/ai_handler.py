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
from google.generativeai.types import GoogleSearch
import aiosqlite
import numpy as np
import pickle
import random
import time
import json
import uuid

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
                self.embedding_model_name = config.AI_EMBEDDING_MODEL_NAME
                logger.info("Gemini API 설정 완료.")
                self.gemini_configured = True
            except Exception as e:
                logger.critical(f"Gemini API 설정 실패: {e}. AI 기능 비활성화됨.", exc_info=True)

    @property
    def is_ready(self) -> bool:
        """AI 핸들러가 모든 기능을 수행할 준비가 되었는지 확인합니다."""
        return self.gemini_configured and self.bot.db is not None and self.tools_cog is not None

    async def _safe_generate_content(self, model: genai.GenerativeModel, prompt: Any, log_extra: dict, generation_config: genai.types.GenerationConfig = None) -> genai.types.GenerateContentResponse | None:
        """
        generate_content_async 호출을 위한 안전한 래퍼.
        Google API 관련 특정 예외를 처리하고 로깅하며, 실패 시 None을 반환합니다.
        """
        if generation_config is None:
            generation_config = genai.types.GenerationConfig(temperature=0.0)

        try:
            limit_key = 'gemini_intent' if config.AI_INTENT_MODEL_NAME in model.model_name else 'gemini_response'
            if getattr(generation_config, 'tools', None) and any(t.google_search for t in generation_config.tools):
                limit_key = 'gemini_grounding'
                rpm, rpd = 60, 500
            else:
                rpm = config.RPM_LIMIT_INTENT if limit_key == 'gemini_intent' else config.RPM_LIMIT_RESPONSE
                rpd = config.RPD_LIMIT_INTENT if limit_key == 'gemini_intent' else config.RPD_LIMIT_RESPONSE

            if await db_utils.check_api_rate_limit(self.bot.db, limit_key, rpm, rpd):
                logger.warning(f"Gemini API 호출 속도/횟수 제한에 도달했습니다 ({limit_key}).", extra=log_extra)
                if limit_key == 'gemini_grounding':
                    return genai.types.GenerateContentResponse.from_response(
                        dict(candidates=[dict(content=dict(parts=[dict(text=config.MSG_AI_GOOGLE_LIMIT_REACHED)]))])
                    )
                return None

            response = await model.generate_content_async(
                prompt,
                generation_config=generation_config,
                safety_settings=config.GEMINI_SAFETY_SETTINGS,
            )
            return response
        except Exception as e:
            logger.error(f"Gemini 응답 생성 중 예기치 않은 오류: {e}", extra=log_extra, exc_info=True)
            return None

    async def _safe_embed_content(self, model_name: str, content: str, task_type: str, log_extra: dict) -> dict | None:
        """
        embed_content_async 호출을 위한 안전한 래퍼.
        실패 시 None을 반환합니다.
        """
        try:
            if await db_utils.check_api_rate_limit(self.bot.db, 'gemini_embedding', config.RPM_LIMIT_EMBEDDING, config.RPD_LIMIT_EMBEDDING):
                logger.warning("Gemini Embedding API 호출 속도 제한에 도달했습니다.", extra=log_extra)
                return None
            return await genai.embed_content_async(model=model_name, content=content, task_type=task_type)
        except Exception as e:
            logger.error(f"Gemini 임베딩 생성 중 오류: {e}", extra=log_extra, exc_info=True)
            return None

    async def add_message_to_history(self, message: discord.Message):
        if not self.is_ready or not config.AI_MEMORY_ENABLED or not message.guild: return
        try:
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
            if not message.author.bot and len(message.content) > 25:
                asyncio.create_task(self._create_and_save_embedding(message.id, message.content, message.guild.id))
        except Exception as e:
            logger.error(f"대화 기록 저장 중 DB 오류: {e}", exc_info=True, extra={'guild_id': message.guild.id})

    async def _create_and_save_embedding(self, message_id: int, content: str, guild_id: int):
        log_extra = {'guild_id': guild_id, 'message_id': message_id}
        embedding_result = await self._safe_embed_content(
            model_name=self.embedding_model_name,
            content=content,
            task_type="retrieval_document",
            log_extra=log_extra
        )
        if not embedding_result:
            return

        try:
            embedding_blob = pickle.dumps(embedding_result['embedding'])
            await self.bot.db.execute("UPDATE conversation_history SET embedding = ? WHERE message_id = ?", (embedding_blob, message_id))
            await self.bot.db.commit()
        except (pickle.PicklingError, aiosqlite.Error) as e:
            logger.error(f"임베딩 DB 저장/직렬화 중 오류: {e}", extra=log_extra, exc_info=True)

    async def _get_rag_context(self, guild_id: int, channel_id: int, user_id: int, query: str) -> Tuple[str, list[str]]:
        log_extra = {'guild_id': guild_id, 'channel_id': channel_id, 'user_id': user_id}
        logger.info(f"RAG 컨텍스트 검색 시작. Query: '{query}'", extra=log_extra)

        query_embedding_result = await self._safe_embed_content(
            model_name=self.embedding_model_name,
            content=query,
            task_type="retrieval_query",
            log_extra=log_extra
        )
        if not query_embedding_result:
            return "", []

        try:
            query_embedding = np.array(query_embedding_result['embedding'])
            async with self.bot.db.execute("SELECT content, embedding FROM conversation_history WHERE guild_id = ? AND channel_id = ? AND embedding IS NOT NULL ORDER BY created_at DESC LIMIT 100;", (guild_id, channel_id,)) as cursor:
                rows = await cursor.fetchall()
            if not rows:
                logger.info("RAG: 검색할 임베딩 데이터가 없습니다.", extra=log_extra)
                return "", []

            def _cosine_similarity(v1, v2):
                norm_v1, norm_v2 = np.linalg.norm(v1), np.linalg.norm(v2)
                return np.dot(v1, v2) / (norm_v1 * norm_v2) if norm_v1 > 0 and norm_v2 > 0 else 0.0

            similarities = sorted([(sim, content) for content, blob in rows if (sim := _cosine_similarity(query_embedding, pickle.loads(blob))) > 0.70], key=lambda x: x[0], reverse=True)
            if not similarities:
                logger.info("RAG: 유사도 0.70 이상인 문서를 찾지 못했습니다.", extra=log_extra)
                return "", []

            top_contents = [content for _, content in similarities[:3]]
            context_str = "참고할 만한 과거 대화 내용:\n" + "\n".join(f"- {c}" for c in top_contents)
            logger.info(f"RAG: {len(top_contents)}개의 유사한 대화 내용을 찾았습니다.", extra=log_extra)
            logger.debug(f"RAG 결과: {context_str}", extra=log_extra)
            return context_str, top_contents
        except (pickle.UnpicklingError, aiosqlite.Error, np.linalg.LinAlgError) as e:
            logger.error(f"RAG 컨텍스트 처리(DB, 유사도 계산) 중 오류: {e}", exc_info=True, extra=log_extra)
            return "", []

    def _parse_tool_calls(self, text: str) -> list[dict]:
        plan_match = re.search(r'<tool_plan>\s*(\[.*?\])\s*</tool_plan>', text, re.DOTALL)
        if plan_match:
            try:
                calls = json.loads(plan_match.group(1))
                if isinstance(calls, list):
                    logger.info(f"도구 계획(plan)을 파싱했습니다: {len(calls)} 단계")
                    return calls
            except json.JSONDecodeError as e:
                logger.warning(f"tool_plan JSON 디코딩 실패: {e}. 원본: {plan_match.group(1)}")
                return []

        call_match = re.search(r'<tool_call>\s*({.*?})\s*</tool_call>', text, re.DOTALL)
        if call_match:
            try:
                call = json.loads(call_match.group(1))
                if isinstance(call, dict):
                    logger.info("단일 도구 호출(call)을 파싱했습니다.")
                    return [call]
            except json.JSONDecodeError as e:
                logger.warning(f"tool_call JSON 디코딩 실패: {e}. 원본: {call_match.group(1)}")

        return []

    async def _execute_tool(self, tool_call: dict, guild_id: int, user_query: str) -> dict:
        tool_name = tool_call.get('tool_to_use')
        parameters = tool_call.get('parameters', {})
        log_extra = {'guild_id': guild_id, 'tool_name': tool_name, 'parameters': parameters}

        if not tool_name: 
            return {"error": "tool_to_use가 지정되지 않았습니다."}

        if tool_name == 'web_search':
            logger.info("Executing special tool: web_search (Google Grounding)", extra=log_extra)
            query = parameters.get('query', user_query)
            try:
                grounding_tool = genai.types.Tool(google_search=GoogleSearch())
                grounding_model = genai.GenerativeModel(config.AI_RESPONSE_MODEL_NAME, tools=[grounding_tool])
                
                if await db_utils.check_api_rate_limit(self.bot.db, 'gemini_grounding', 60, 500):
                    logger.warning("Google Search API 호출 속도/횟수 제한에 도달했습니다.", extra=log_extra)
                    return {"error": config.MSG_AI_GOOGLE_LIMIT_REACHED}

                grounded_response = await grounding_model.generate_content_async(query)

                if grounded_response and grounded_response.text:
                    return {"result": grounded_response.text}
                else:
                    logger.error("Google Grounding 실행에 실패했으나 오류가 없습니다.", extra=log_extra)
                    return {"error": "Google 검색을 통해 정보를 찾는 데 실패했습니다."}
            except Exception as e:
                logger.error(f"Google Grounding 실행 중 예기치 않은 오류: {e}", exc_info=True, extra=log_extra)
                return {"error": f"Google 검색 중 오류가 발생했습니다: {e}"}

        try:
            tool_method = getattr(self.tools_cog, tool_name)
            logger.info(f"Executing tool: {tool_name} with params: {parameters}", extra=log_extra)
            result = await tool_method(**parameters)
            if not isinstance(result, dict):
                return {"result": str(result)}
            return result
        except AttributeError:
            logger.error(f"도구 '{tool_name}'을(를) 찾을 수 없습니다.", extra=log_extra)
            return {"error": f"'{tool_name}'이라는 도구는 존재하지 않습니다."}
        except Exception as e:
            logger.error(f"도구 '{tool_name}' 실행 중 예기치 않은 오류: {e}", exc_info=True, extra=log_extra)
            return {"error": "도구 실행 중 예상치 못한 오류가 발생했습니다."}

    async def process_agent_message(self, message: discord.Message):
        if not self.is_ready: return
        user_query = message.content.replace(f'<@!{self.bot.user.id}>', '').replace(f'<@{self.bot.user.id}>', '').strip()
        if not user_query: return

        trace_id = uuid.uuid4().hex[:8]
        log_extra = {
            'guild_id': message.guild.id,
            'channel_id': message.channel.id,
            'user_id': message.author.id,
            'trace_id': trace_id
        }
        logger.info(f"에이전트 처리 시작 (PM v5.2). Query: '{user_query}'", extra=log_extra)

        async with message.channel.typing():
            try:
                rag_prompt, _ = await self._get_rag_context(message.guild.id, message.channel.id, message.author.id, user_query)
                history = await self._get_recent_history(message, rag_prompt)

                lite_system_prompt = config.LITE_MODEL_SYSTEM_PROMPT
                if rag_prompt:
                    lite_system_prompt = f"{rag_prompt}\n\n{lite_system_prompt}"

                lite_model = genai.GenerativeModel(config.AI_INTENT_MODEL_NAME, system_instruction=lite_system_prompt)
                lite_conversation = history + [{'role': 'user', 'parts': [user_query]}]

                logger.info("1단계: Lite 모델 (PM) 호출 시작...", extra=log_extra)
                lite_response = await self._safe_generate_content(lite_model, lite_conversation, log_extra)

                if not lite_response or not lite_response.text:
                    logger.error("Lite 모델(PM)이 응답을 생성하지 못했습니다.", extra=log_extra)
                    await message.reply(config.MSG_AI_ERROR, mention_author=False)
                    return

                lite_response_text = lite_response.text.strip()
                logger.info(f"Lite 모델(PM) 응답: '{lite_response_text[:150]}...'", extra=log_extra)
                
                tool_plan = self._parse_tool_calls(lite_response_text)

                if "<conversation_response>" in lite_response_text:
                    logger.info("분기: 간단한 대화로 판단, Main 모델을 호출하여 페르소나 기반으로 응답합니다.", extra=log_extra)

                    channel_config = config.CHANNEL_AI_CONFIG.get(message.channel.id, {})
                    persona = channel_config.get('persona')
                    rules = channel_config.get('rules')

                    if not persona or not rules:
                        system_prompt = config.AGENT_SYSTEM_PROMPT.format(user_query=user_query, tool_result="N/A")
                    else:
                        system_prompt = f"{persona}\n\n{rules}"
                    
                    main_model = genai.GenerativeModel(config.AI_RESPONSE_MODEL_NAME, system_instruction=system_prompt)
                    main_response = await self._safe_generate_content(main_model, user_query, log_extra)

                    if main_response and main_response.text:
                        final_response_text = main_response.text.strip()
                        await message.reply(final_response_text, mention_author=False)
                        await db_utils.log_analytics(self.bot.db, "AI_INTERACTION", {
                            "guild_id": message.guild.id,
                            "user_id": message.author.id,
                            "channel_id": message.channel.id,
                            "trace_id": trace_id,
                            "user_query": user_query,
                            "tool_plan": [],
                            "final_response": final_response_text,
                            "is_fallback": False,
                        })
                    else:
                        logger.error("간단한 대화에 대해 Main 모델이 응답을 생성하지 못했습니다.", extra=log_extra)
                        await message.reply(config.MSG_AI_ERROR, mention_author=False)
                    return

                if not tool_plan:
                    logger.info("분기: 도구 계획이 없으며, Lite 모델의 답변으로 바로 응답합니다.", extra=log_extra)
                    await message.reply(lite_response_text, mention_author=False)
                    await db_utils.log_analytics(self.bot.db, "AI_INTERACTION", {
                        "guild_id": message.guild.id,
                        "user_id": message.author.id,
                        "channel_id": message.channel.id,
                        "trace_id": trace_id,
                        "user_query": user_query,
                        "tool_plan": [],
                        "final_response": lite_response_text,
                        "is_fallback": False,
                    })
                    return

                logger.info(f"분기: 도구 사용 계획 발견. 총 {len(tool_plan)}단계.", extra=log_extra)
                
                tool_results = []
                for i, tool_call in enumerate(tool_plan):
                    step_num = i + 1
                    logger.info(f"계획 실행 ({step_num}/{len(tool_plan)}): {tool_call.get('tool_to_use')}", extra=log_extra)
                    
                    result = await self._execute_tool(tool_call, message.guild.id, user_query)
                    
                    if result is None:
                        error_msg = f"도구 실행 결과가 None입니다: {tool_call.get('tool_to_use')}"
                        logger.error(error_msg, extra=log_extra)
                        tool_results.append({f"error_step_{step_num}": error_msg})
                        continue

                    tool_results.append({
                        "step": step_num,
                        "tool_name": tool_call.get('tool_to_use'),
                        "parameters": tool_call.get('parameters'),
                        "result": result
                    })

                def is_tool_failed(result):
                    if result is None: return True
                    res_str = str(result).lower()
                    fail_keywords = ["error", "오류", "실패", "없습니다", "알 수 없는", "찾을 수"]
                    return any(keyword in res_str for keyword in fail_keywords)

                any_failed = any(is_tool_failed(res.get("result")) for res in tool_results)
                use_fallback_prompt = False

                if tool_plan and any_failed and not any(tc.get('tool_to_use') == 'web_search' for tc in tool_plan):
                    logger.info("하나 이상의 도구 실행에 실패하여 웹 검색으로 대체합니다.", extra=log_extra)
                    
                    web_search_tool_call = {
                        "tool_to_use": "web_search",
                        "parameters": {"query": user_query}
                    }
                    web_search_result_dict = await self._execute_tool(web_search_tool_call, message.guild.id, user_query)
                    web_search_result = web_search_result_dict.get("result", "웹 검색에 실패했습니다.")

                    tool_results = [{
                        "step": 1,
                        "tool_name": "web_search",
                        "parameters": {"query": user_query},
                        "result": web_search_result
                    }]
                    use_fallback_prompt = True

                tool_results_str = json.dumps(tool_results, ensure_ascii=False, indent=2)

                logger.info("3단계: Main 모델 호출 시작...", extra=log_extra)

                is_travel_tool_used = any(tc.get('tool_to_use') == 'get_travel_recommendation' for tc in tool_plan)

                if use_fallback_prompt:
                    main_system_prompt = config.WEB_FALLBACK_PROMPT.format(
                        user_query=user_query,
                        tool_result=tool_results_str
                    )
                elif is_travel_tool_used:
                    main_system_prompt = config.SPECIALIZED_PROMPTS["travel_assistant"].format(
                        user_query=user_query,
                        tool_result=tool_results_str
                    )
                else:
                    main_system_prompt = config.AGENT_SYSTEM_PROMPT.format(
                        user_query=user_query, 
                        tool_result=tool_results_str
                    )
                main_prompt = user_query

                channel_config = config.CHANNEL_AI_CONFIG.get(message.channel.id, {})
                persona = channel_config.get('persona')
                rules = channel_config.get('rules')

                if persona and rules:
                    main_system_prompt = f"{persona}\n\n{rules}\n\n{main_system_prompt}"
                else:
                    custom_persona = await db_utils.get_guild_setting(self.bot.db, message.guild.id, 'persona_text')
                    if custom_persona:
                        main_system_prompt = f"{custom_persona}\n\n{main_system_prompt}"

                main_model = genai.GenerativeModel(config.AI_RESPONSE_MODEL_NAME, system_instruction=main_system_prompt)
                main_response = await self._safe_generate_content(main_model, main_prompt, log_extra)

                if main_response and main_response.text:
                    logger.info("Main 모델이 최종 답변을 생성했습니다.", extra=log_extra)
                    final_response_text = main_response.text.strip()
                    await message.reply(final_response_text, mention_author=False)
                    await db_utils.log_analytics(self.bot.db, "AI_INTERACTION", {
                        "guild_id": message.guild.id,
                        "user_id": message.author.id,
                        "channel_id": message.channel.id,
                        "trace_id": trace_id,
                        "user_query": user_query,
                        "tool_plan": tool_plan,
                        "final_response": final_response_text,
                        "is_fallback": use_fallback_prompt,
                    })
                else:
                    logger.error("Main 모델이 최종 답변을 생성하지 못했습니다.", extra=log_extra)
                    await message.reply(f"모든 도구 실행을 마쳤지만, 최종 답변을 만드는 데 실패했어요. 여기 실행 결과라도 확인해보세요.\n```json\n{tool_results_str}\n```", mention_author=False)
                    await db_utils.log_analytics(self.bot.db, "AI_INTERACTION_FAILED", {
                        "guild_id": message.guild.id,
                        "user_id": message.author.id,
                        "channel_id": message.channel.id,
                        "trace_id": trace_id,
                        "user_query": user_query,
                        "tool_plan": tool_plan,
                        "tool_results": tool_results,
                    })

            except Exception as e:
                logger.error(f"에이전트 처리 중 최상위 오류: {e}", exc_info=True, extra=log_extra)
                await message.reply(config.MSG_AI_ERROR, mention_author=False)

    async def _get_recent_history(self, message: discord.Message, rag_prompt: str) -> list:
        history_limit = 6 if rag_prompt else 12
        history = []
        
        async for msg in message.channel.history(limit=history_limit + 1):
            if msg.id == message.id:
                continue

            role = 'model' if msg.author.id == self.bot.user.id else 'user'
            content = msg.content[:2000] if len(msg.content) > 2000 else msg.content
            history.append({'role': role, 'parts': [content]})

        history.reverse()
        return history

    async def should_proactively_respond(self, message: discord.Message) -> bool:
        conf = config.AI_PROACTIVE_RESPONSE_CONFIG
        if not conf.get("enabled"): return False

        now = time.time()
        if (now - self.proactive_cooldowns.get(message.channel.id, 0)) < conf.get("cooldown_seconds", 90): return False
        if len(message.content) < conf.get("min_message_length", 10): return False
        if not any(keyword in message.content.lower() for keyword in conf.get("keywords", [])): return False
        if random.random() > conf.get("probability", 0.1): return False

        log_extra = {'guild_id': message.guild.id, 'channel_id': message.channel.id}
        try:
            history_msgs = [f"User({msg.author.display_name}): {msg.content}" async for msg in message.channel.history(limit=conf.get("look_back_count", 5))]
            history_msgs.reverse()
            conversation_context = "\n".join(history_msgs)
            gatekeeper_prompt = f"{conf['gatekeeper_persona']}\n\n--- 최근 대화 내용 ---\n{conversation_context}\n---\n사용자의 마지막 메시지: \"{message.content}\"\n---\n\n자, 판단해. Yes or No?"

            lite_model = genai.GenerativeModel(config.AI_INTENT_MODEL_NAME)
            response = await self._safe_generate_content(lite_model, gatekeeper_prompt, log_extra)

            if response and "YES" in response.text.strip().upper():
                self.proactive_cooldowns[message.channel.id] = now
                return True
        except Exception as e:
            logger.error(f"게이트키퍼 AI 실행 중 오류: {e}", exc_info=True, extra=log_extra)

        return False

    async def get_recent_conversation_text(self, guild_id: int, channel_id: int, look_back: int = 20) -> str:
        if not self.bot.db: return ""
        query = "SELECT user_name, content FROM conversation_history WHERE guild_id = ? AND channel_id = ? AND is_bot = 0 ORDER BY created_at DESC LIMIT ?"
        try:
            async with self.bot.db.execute(query, (guild_id, channel_id, look_back)) as cursor:
                rows = await cursor.fetchall()
            if not rows: return ""
            rows.reverse()
            return "\n".join([f"User({row['user_name']}): {row['content']}" for row in rows])
        except Exception as e:
            logger.error(f"최근 대화 기록 조회 중 DB 오류: {e}", exc_info=True)
            return ""

    async def generate_creative_text(self, channel: discord.TextChannel, author: discord.User, prompt_key: str, context: dict) -> str:
        if not self.is_ready: return config.MSG_AI_ERROR
        log_extra = {'guild_id': channel.guild.id, 'user_id': author.id, 'prompt_key': prompt_key}

        try:
            prompt_template = config.AI_CREATIVE_PROMPTS.get(prompt_key)
            if not prompt_template:
                logger.error(f"'{prompt_key}'에 해당하는 크리에이티브 프롬프트를 찾을 수 없습니다.", extra=log_extra)
                return config.MSG_CMD_ERROR

            user_prompt = prompt_template.format(**context)
            system_prompt = f"{config.CHANNEL_AI_CONFIG.get(channel.id, {}).get('persona', '')}\n\n{config.CHANNEL_AI_CONFIG.get(channel.id, {}).get('rules', '')}"

            model = genai.GenerativeModel(model_name=config.AI_RESPONSE_MODEL_NAME, system_instruction=system_prompt)
            response = await self._safe_generate_content(model, user_prompt, log_extra)

            return response.text.strip() if response and response.text else config.MSG_AI_ERROR
        except KeyError as e:
            logger.error(f"프롬프트 포맷팅 중 키 오류: '{prompt_key}' 프롬프트에 필요한 컨텍스트({e})가 없습니다.", extra=log_extra)
            return config.MSG_CMD_ERROR
        except Exception as e:
            logger.error(f"Creative text 생성 중 최상위 오류: {e}", exc_info=True, extra=log_extra)
            return config.MSG_AI_ERROR

async def setup(bot: commands.Bot):
    await bot.add_cog(AIHandler(bot))
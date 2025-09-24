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
                # We create model instances dynamically now, so we don't need a default one here.
                # self.model = genai.GenerativeModel(config.AI_RESPONSE_MODEL_NAME)
                self.embedding_model_name = config.AI_EMBEDDING_MODEL_NAME
                logger.info("Gemini API 설정 완료.")
                self.gemini_configured = True
            except Exception as e:
                logger.critical(f"Gemini API 설정 실패: {e}. AI 기능 비활성화됨.", exc_info=True)

    @property
    def is_ready(self) -> bool:
        """AI 핸들러가 모든 기능을 수행할 준비가 되었는지 확인합니다."""
        return self.gemini_configured and self.bot.db is not None and self.tools_cog is not None

    # --- Gemini API 안정성 강화를 위한 래퍼 함수 ---
    async def _safe_generate_content(self, model: genai.GenerativeModel, prompt: Any, log_extra: dict) -> genai.types.GenerateContentResponse | None:
        """
        generate_content_async 호출을 위한 안전한 래퍼.
        Google API 관련 특정 예외를 처리하고 로깅하며, 실패 시 None을 반환합니다.
        """
        try:
            # API 속도 제한 확인 (모델에 따라 다른 카운터 사용)
            limit_key = 'gemini_intent' if config.AI_INTENT_MODEL_NAME in model.model_name else 'gemini_response'
            rpm = config.RPM_LIMIT_INTENT if limit_key == 'gemini_intent' else config.RPM_LIMIT_RESPONSE
            rpd = config.RPD_LIMIT_INTENT if limit_key == 'gemini_intent' else config.RPD_LIMIT_RESPONSE

            if await db_utils.check_api_rate_limit(self.bot.db, limit_key, rpm, rpd):
                logger.warning(f"Gemini API 호출 속도 제한에 도달했습니다 ({limit_key}).", extra=log_extra)
                return None

            response = await model.generate_content_async(
                prompt,
                generation_config=genai.types.GenerationConfig(temperature=0.0),
                safety_settings=config.GEMINI_SAFETY_SETTINGS,
            )
            return response
        except google.api_core.exceptions.ResourceExhausted as e:
            logger.error(f"Gemini API 할당량 초과: {e}", extra=log_extra, exc_info=True)
            return None
        except google.api_core.exceptions.GoogleAPICallError as e:
            logger.error(f"Gemini API 호출 오류: {e}", extra=log_extra, exc_info=True)
            return None
        except google.api_core.exceptions.InvalidArgument as e:
            logger.error(f"Gemini API 잘못된 인수: {e}", extra=log_extra, exc_info=True)
            return None
        except google.api_core.exceptions.PermissionDenied as e:
            logger.error(f"Gemini API 권한 거부: {e}", extra=log_extra, exc_info=True)
            return None
        except google.api_core.exceptions.DeadlineExceeded as e:
            logger.error(f"Gemini API 타임아웃: {e}", extra=log_extra, exc_info=True)
            return None
        except google.api_core.exceptions.ServiceUnavailable as e:
            logger.error(f"Gemini API 서비스 불가: {e}", extra=log_extra, exc_info=True)
            return None
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
        """RAG를 위한 컨텍스트를 DB에서 검색하고, 컨텍스트 문자열과 원본 메시지 내용 목록을 반환합니다."""
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

    # --- 에이전트 핵심 로직 ---
    def _parse_tool_calls(self, text: str) -> list[dict]:
        """
        <tool_call> 또는 <tool_plan> 태그에서 도구 호출 목록을 추출합니다.
        단일 도구 호출(dict)과 도구 계획(list of dicts)을 모두 처리하여 항상 list[dict]를 반환합니다.
        """
        # 1. <tool_plan> (JSON 배열) 우선 검색
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

        # 2. <tool_call> (단일 JSON 객체) 검색
        call_match = re.search(r'<tool_call>\s*({.*?})\s*</tool_call>', text, re.DOTALL)
        if call_match:
            try:
                call = json.loads(call_match.group(1))
                if isinstance(call, dict):
                    logger.info("단일 도구 호출(call)을 파싱했습니다.")
                    return [call] # 단일 호출도 리스트에 담아 반환
            except json.JSONDecodeError as e:
                logger.warning(f"tool_call JSON 디코딩 실패: {e}. 원본: {call_match.group(1)}")

        return []

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
                # --- 1단계: Lite 모델로 작업 계획 수립 ---
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
                
                # PM v5.2: 단일 호출이 아닌 '계획'을 파싱
                tool_plan = self._parse_tool_calls(lite_response_text)

                # --- 2단계: 분기 처리 ---
                if not tool_plan:
                    # Case 1: 간단한 대화 - Lite 모델의 답변을 그대로 사용
                    logger.info("분기: 간단한 대화로 판단, Lite 모델의 답변으로 바로 응답합니다.", extra=log_extra)
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

                # Case 2: 도구 사용 계획 존재 - 계획을 순차적으로 실행
                logger.info(f"분기: 도구 사용 계획 발견. 총 {len(tool_plan)}단계.", extra=log_extra)
                
                tool_results = []
                for i, tool_call in enumerate(tool_plan):
                    step_num = i + 1
                    logger.info(f"계획 실행 ({step_num}/{len(tool_plan)}): {tool_call.get('tool_to_use')}", extra=log_extra)
                    
                    # 현재는 이전 단계 결과를 다음 단계에 넘기지 않음. 추후 확장 가능.
                    result = await self._execute_tool(tool_call, message.guild.id)
                    
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

                # --- 2.5단계: 도구 실패 시 웹 검색으로 대체 (Fallback) ---
                def is_tool_failed(result):
                    """도구 결과가 실패했는지 여부를 간단히 확인합니다."""
                    if result is None: return True
                    res_str = str(result).lower()
                    # 실패를 나타내는 키워드 목록
                    fail_keywords = ["error", "오류", "실패", "없습니다", "알 수 없는", "찾을 수"]
                    return any(keyword in res_str for keyword in fail_keywords)

                any_failed = any(is_tool_failed(res.get("result")) for res in tool_results)
                use_fallback_prompt = False

                if tool_plan and any_failed and not any(tc.get('tool_to_use') == 'web_search' for tc in tool_plan):
                    logger.info("하나 이상의 도구 실행에 실패하여 웹 검색으로 대체합니다.", extra=log_extra)
                    web_search_result = await self.tools_cog.web_search(user_query)
                    # 기존의 실패한 결과 대신 웹 검색 결과를 사용
                    tool_results = [{
                        "step": 1,
                        "tool_name": "web_search",
                        "parameters": {"query": user_query},
                        "result": web_search_result
                    }]
                    use_fallback_prompt = True

                # 모든 도구의 실행 결과를 하나의 문자열로 통합
                tool_results_str = json.dumps(tool_results, ensure_ascii=False, indent=2)

                # --- 3단계: Main 모델로 최종 답변 생성 ---
                logger.info("3단계: Main 모델 호출 시작...", extra=log_extra)

                # 도구 사용 여부에 따라 적절한 시스템 프롬프트 선택
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

                # 페르소나 적용
                # 1. 채널별 페르소나 (config.py) 우선 적용
                channel_config = config.CHANNEL_AI_CONFIG.get(message.channel.id, {})
                persona = channel_config.get('persona')
                rules = channel_config.get('rules')

                if persona and rules:
                    # 채널 설정이 있으면 그것을 시스템 프롬프트로 사용
                    main_system_prompt = f"{persona}\n\n{rules}\n\n{main_system_prompt}"
                else:
                    # 채널 설정이 없으면 기존처럼 DB에서 길드 설정을 가져옴
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
        """최근 대화 기록을 가져옵니다. RAG 사용 여부에 따라 기록 길이를 조절합니다."""
        history_limit = 3 if rag_prompt else 8
        history = []
        # RAG에서 사용된 내용은 제외하기 위해 history_limit * 2 만큼 가져와 필터링
        async for msg in message.channel.history(limit=history_limit * 2):
            if msg.id == message.id: continue
            if rag_prompt and msg.content in rag_prompt: continue

            role = 'model' if msg.author.id == self.bot.user.id else 'user'
            history.append({'role': role, 'parts': [msg.content]})
            if len(history) >= history_limit:
                break
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

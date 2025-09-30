# -*- coding: utf-8 -*-
"""
마사몽 봇의 AI 상호작용을 총괄하는 핵심 Cog입니다.

2-Step Agent 아키텍처에 따라 다음의 역할을 수행합니다:
1.  **의도 분석 (Lite Model)**: 사용자의 메시지를 분석하여 간단한 대화인지, 도구 사용이 필요한지 판단합니다.
2.  **도구 실행**: 분석된 계획에 따라 `ToolsCog`의 도구들을 실행하고 결과를 수집합니다.
3.  **답변 생성 (Main Model)**: 도구 실행 결과를 바탕으로 사용자에게 제공할 최종 답변을 생성합니다.
4.  **대화 기록 관리**: RAG(Retrieval-Augmented Generation)를 위해 대화 내용을 데이터베이스에 저장하고 임베딩을 생성합니다.
"""

from __future__ import annotations

import discord
from discord.ext import commands
try:
    import google.generativeai as genai
except ModuleNotFoundError:  # pragma: no cover - 환경에 따라 설치되지 않을 수 있음
    genai = None
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
import requests

import config
from logger_config import logger
from utils import db as db_utils
from utils import http

KST = pytz.timezone('Asia/Seoul')

class AIHandler(commands.Cog):
    """
    AI 에이전트의 핵심 로직을 담당하며, 대화 관리, 도구 호출, 응답 생성을 처리합니다.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.tools_cog = bot.get_cog('ToolsCog')
        self.ai_user_cooldowns: Dict[int, datetime] = {}
        self.proactive_cooldowns: Dict[int, float] = {}
        self.gemini_configured = False
        self.api_call_lock = asyncio.Lock()
        self._google_grounding_checked = False
        self._google_grounding_descriptor: dict[str, object] | None = None

        if config.GEMINI_API_KEY and genai:
            try:
                genai.configure(api_key=config.GEMINI_API_KEY)
                self.embedding_model_name = config.AI_EMBEDDING_MODEL_NAME
                logger.info("Gemini API가 성공적으로 설정되었습니다.")
                self.gemini_configured = True
            except Exception as e:
                logger.critical(f"Gemini API 설정 실패: {e}. AI 관련 기능이 비활성화됩니다.", exc_info=True)
        elif config.GEMINI_API_KEY and not genai:
            logger.critical("google-generativeai 패키지를 찾을 수 없어 Gemini 기능을 초기화하지 못했습니다.")

    @property
    def is_ready(self) -> bool:
        """AI 핸들러가 모든 의존성(Gemini, DB, ToolsCog)을 포함하여 준비되었는지 확인합니다."""
        return self.gemini_configured and self.bot.db is not None and self.tools_cog is not None

    async def _safe_generate_content(self, model: genai.GenerativeModel, prompt: Any, log_extra: dict, generation_config: genai.types.GenerationConfig = None) -> genai.types.GenerateContentResponse | None:
        """
        `generate_content_async`를 안전하게 호출하는 래퍼 함수입니다.
        API 호출 제한을 확인하고, Google API 관련 예외를 처리하여 안정성을 높입니다.
        """
        if generation_config is None:
            generation_config = genai.types.GenerationConfig(temperature=0.0)

        try:
            limit_key = 'gemini_intent' if config.AI_INTENT_MODEL_NAME in model.model_name else 'gemini_response'
            rpm = config.RPM_LIMIT_INTENT if limit_key == 'gemini_intent' else config.RPM_LIMIT_RESPONSE
            rpd = config.RPD_LIMIT_INTENT if limit_key == 'gemini_intent' else config.RPD_LIMIT_RESPONSE

            if await db_utils.check_api_rate_limit(self.bot.db, limit_key, rpm, rpd):
                logger.warning(f"Gemini API 호출 제한({limit_key})에 도달했습니다.", extra=log_extra)
                return None

            response = await model.generate_content_async(
                prompt,
                generation_config=generation_config,
                safety_settings=config.GEMINI_SAFETY_SETTINGS,
            )
            await db_utils.log_api_call(self.bot.db, limit_key)
            return response
        except Exception as e:
            logger.error(f"Gemini 응답 생성 중 예기치 않은 오류: {e}", extra=log_extra, exc_info=True)
            return None

    async def _safe_embed_content(self, model_name: str, content: str, task_type: str, log_extra: dict) -> dict | None:
        """
        `embed_content_async`를 안전하게 호출하는 래퍼 함수입니다.
        API 호출 제한 확인 및 예외 처리를 포함합니다.
        """
        try:
            if await db_utils.check_api_rate_limit(self.bot.db, 'gemini_embedding', config.RPM_LIMIT_EMBEDDING, config.RPD_LIMIT_EMBEDDING):
                logger.warning("Gemini Embedding API 호출 제한에 도달했습니다.", extra=log_extra)
                return None
            result = await genai.embed_content_async(model=model_name, content=content, task_type=task_type)
            await db_utils.log_api_call(self.bot.db, 'gemini_embedding')
            return result
        except Exception as e:
            logger.error(f"Gemini 임베딩 생성 중 오류: {e}", extra=log_extra, exc_info=True)
            return None

    async def add_message_to_history(self, message: discord.Message):
        """AI가 허용된 채널의 메시지를 대화 기록 DB에 저장합니다."""
        if not self.is_ready or not config.AI_MEMORY_ENABLED or not message.guild: return
        try:
            channel_config = config.CHANNEL_AI_CONFIG.get(message.channel.id, {})
            if not channel_config.get("allowed", False): return

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
        """메시지 내용에 대한 벡터 임베딩을 생성하고 데이터베이스에 저장합니다."""
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

    def _discover_google_grounding_tool(self) -> dict[str, object] | None:
        """모델 버전에 맞춰 Google Search 도구 구성을 동적으로 감지합니다."""
        if not self.gemini_configured:
            return None

        model_name = config.AI_RESPONSE_MODEL_NAME
        
        # 모델 이름에 '1.5'가 포함되어 있는지 여부에 따라 사용할 도구 설정 결정
        if "1.5" in model_name:
            logger.info("Gemini 1.5 모델 감지, 'google_search_retrieval' 도구를 시도합니다.")
            field_name = "google_search_retrieval"
            class_name = "GoogleSearchRetrieval"
        else:
            logger.info("Gemini 2.0+ 모델 감지, 'google_search' 도구를 시도합니다.")
            field_name = "google_search"
            class_name = "GoogleSearch"

        try:
            # getattr을 사용하여 해당 클래스가 존재하는지 확인
            google_cls = getattr(genai.types, class_name, None)
            
            if not google_cls:
                # grounding 서브모듈에서도 찾아보기 (구버전 호환성)
                grounding_module = getattr(genai.types, "grounding", None)
                if grounding_module:
                    google_cls = getattr(grounding_module, class_name, None)

            if not google_cls:
                logger.warning(f"현재 라이브러리에서 '{class_name}' 클래스를 찾을 수 없습니다.")
                return None

            # 도구 생성 테스트
            genai.types.Tool(**{field_name: google_cls()})
            
            logger.info(f"Google Grounding 도구를 '{field_name}' 필드와 '{class_name}' 타입으로 설정했습니다.")
            return {"field": field_name, "cls": google_cls}

        except Exception as exc:
            logger.error(f"Google Grounding 도구 ('{field_name}', '{class_name}') 초기화 검사 실패: {exc}")
            return None

    def _get_google_grounding_tool(self):
        if not self.gemini_configured:
            return None

        if not self._google_grounding_checked:
            self._google_grounding_descriptor = self._discover_google_grounding_tool()
            self._google_grounding_checked = True

        if not self._google_grounding_descriptor:
            return None

        field_name = self._google_grounding_descriptor.get("field")
        google_cls = self._google_grounding_descriptor.get("cls")
        if not field_name or not google_cls:
            return None

        try:
            return genai.types.Tool(**{field_name: google_cls()})
        except Exception as exc:
            logger.error("Google Grounding 도구 인스턴스화 실패: %s", exc)
            return None

    async def _google_grounded_search(self, query: str, log_extra: dict) -> dict | None:
        """Google Search Grounding을 사용해 웹 검색 결과를 생성합니다. (REST API 직접 호출)"""
        # SDK 방식의 불안정성으로 인해 안정적인 REST API 호출로 직접 연결합니다.
        return await self._google_grounded_search_rest(query, log_extra)

    async def _google_grounded_search_rest(self, query: str, log_extra: dict) -> dict | None:
        """google-generativeai SDK에서 Grounding 도구를 제공하지 않을 때 REST API로 호출합니다."""
        if not config.GEMINI_API_KEY:
            logger.warning("Gemini API 키가 없어 Google Grounding REST 호출을 건너뜁니다.", extra=log_extra)
            return None

        if await db_utils.check_api_rate_limit(self.bot.db, 'gemini_response', config.RPM_LIMIT_RESPONSE, config.RPD_LIMIT_RESPONSE):
            logger.warning("Gemini Grounding REST 호출이 Rate Limit에 막혔습니다.", extra=log_extra)
            return None

        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{config.AI_RESPONSE_MODEL_NAME}:generateContent"
        params = {"key": config.GEMINI_API_KEY}
        payload = {
            "contents": [{"role": "user", "parts": [{"text": query}]}],
            "tools": [{"googleSearch": {}}],
            "generationConfig": {"temperature": 0.0},
        }

        if config.GEMINI_SAFETY_SETTINGS:
            payload["safetySettings"] = [
                {"category": category, "threshold": threshold}
                for category, threshold in config.GEMINI_SAFETY_SETTINGS.items()
            ]

        session = http.get_modern_tls_session()
        try:
            logger.info("Google Grounding REST 호출을 실행합니다.", extra=log_extra)
            
            # 재시도 로직 추가
            for attempt in range(3):
                try:
                    response = await asyncio.to_thread(
                        session.post, endpoint, params=params, json=payload, timeout=30  # 타임아웃 30초로 증가
                    )
                    response.raise_for_status()
                    break  # 성공 시 루프 탈출
                except requests.exceptions.ReadTimeout as exc:
                    if attempt >= 2:
                        logger.error("Google Grounding REST 호출이 재시도 후에도 시간 초과되었습니다.", extra=log_extra, exc_info=True)
                        raise exc  # 마지막 시도 실패 시 예외 다시 발생
                    logger.warning(f"Google Grounding REST 호출 시간 초과 (시도 {attempt + 1}/3). {5 * (attempt + 1)}초 후 재시도합니다.", extra=log_extra)
                    await asyncio.sleep(5 * (attempt + 1))
            else: # for-else: break로 탈출하지 못한 경우 (모든 재시도 실패)
                return None

            try:
                data = response.json()
            except ValueError as exc:
                logger.error(f"Google Grounding REST 응답 JSON 파싱 실패: {exc} | 응답: {response.text}", extra=log_extra)
                return None

            await db_utils.log_api_call(self.bot.db, 'gemini_response')

            candidates = data.get("candidates", [])
            candidate = candidates[0] if candidates else {}
            content = candidate.get("content", {})
            parts = content.get("parts", []) if isinstance(content, dict) else []
            texts = [part.get("text", "") for part in parts if isinstance(part, dict) and part.get("text")]
            answer_text = "\n".join(filter(None, (text.strip() for text in texts))).strip()
            metadata = candidate.get("groundingMetadata") or candidate.get("grounding_metadata") or data.get("groundingMetadata")

            if not answer_text:
                logger.warning("Google Grounding REST 호출이 비어있는 응답을 반환했습니다.", extra=log_extra)
                return None

            web_queries = None
            if isinstance(metadata, dict):
                web_queries = metadata.get("webSearchQueries") or metadata.get("web_search_queries")

            return {
                "result": answer_text,
                "grounding_metadata": metadata,
                "web_search_queries": web_queries,
            }

        except requests.exceptions.RequestException as exc:
            logger.error("Google Grounding REST 호출 중 네트워크 오류: %s", exc, extra=log_extra, exc_info=True)
            return None
        finally:
            session.close()

    async def _run_web_search_fallback(self, query: str, log_extra: dict) -> str | None:
        """Gemini Grounding 검색 실패 시 호출되는 폴백 함수. REST API를 직접 호출하여 재시도합니다."""
        logger.info("기본 Gemini Grounding 검색에 실패하여 폴백으로 REST API 검색을 재시도합니다.", extra=log_extra)
        fallback_result = await self._google_grounded_search_rest(query, log_extra)
        if fallback_result and fallback_result.get("result"):
            return fallback_result.get("result")
        return None

    async def _get_rag_context(self, guild_id: int, channel_id: int, user_id: int, query: str) -> Tuple[str, list[str]]:
        """RAG: 사용자의 질문과 유사한 과거 대화 내용을 DB에서 검색하여 컨텍스트로 반환합니다."""
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
            async with self.bot.db.execute("SELECT content, embedding FROM conversation_history WHERE guild_id = ? AND channel_id = ? AND user_id = ? AND embedding IS NOT NULL ORDER BY created_at DESC LIMIT 100;", (guild_id, channel_id, user_id,)) as cursor:
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
        """Lite 모델의 응답에서 <tool_plan> 또는 <tool_call> XML 태그를 파싱하여 JSON으로 변환합니다."""
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

        call_match = re.search(r'<tool_call>\s*(\{.*?\})\s*</tool_call>', text, re.DOTALL)
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
        """파싱된 단일 도구 호출 계획을 실제로 실행하고 결과를 반환합니다."""
        tool_name = tool_call.get('tool_to_use')
        parameters = tool_call.get('parameters', {})
        log_extra = {'guild_id': guild_id, 'tool_name': tool_name, 'parameters': parameters}

        if not tool_name: 
            return {"error": "tool_to_use가 지정되지 않았습니다."}

        # web_search는 ToolsCog에 구현된 다른 도구들과 달리, Gemini의 Grounding 기능을 직접 사용합니다.
        if tool_name == 'web_search':
            logger.info("특별 도구 실행: web_search (Google Grounding)", extra=log_extra)
            query = parameters.get('query', user_query)
            grounded_payload = await self._google_grounded_search(query, log_extra)
            if grounded_payload and grounded_payload.get("result"):
                return grounded_payload
            fallback_result = await self._run_web_search_fallback(query, log_extra)
            if fallback_result:
                return {"result": fallback_result}
            return {"error": "Google 검색을 통해 정보를 찾는 데 실패했습니다."}

        # 그 외 일반 도구들은 ToolsCog에서 찾아 실행합니다.
        try:
            tool_method = getattr(self.tools_cog, tool_name)
            logger.info(f"일반 도구 실행: {tool_name} with params: {parameters}", extra=log_extra)
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
        """2-Step Agent의 전체 흐름을 관리합니다."""
        if not self.is_ready: return
        user_query = re.sub(f'<@!?{self.bot.user.id}>', '', message.content).strip()
        if not user_query: return

        trace_id = uuid.uuid4().hex[:8]
        log_extra = {'guild_id': message.guild.id, 'channel_id': message.channel.id, 'user_id': message.author.id, 'trace_id': trace_id}
        logger.info(f"에이전트 처리 시작. Query: '{user_query}'", extra=log_extra)

        async with message.channel.typing():
            try:
                rag_prompt, _ = await self._get_rag_context(message.guild.id, message.channel.id, message.author.id, user_query)
                history = await self._get_recent_history(message, rag_prompt)

                # --- [1단계] Lite 모델(PM) 호출: 의도 분석 및 계획 수립 --- 
                lite_system_prompt = f"{rag_prompt}\n\n{config.LITE_MODEL_SYSTEM_PROMPT}" if rag_prompt else config.LITE_MODEL_SYSTEM_PROMPT
                lite_model = genai.GenerativeModel(config.AI_INTENT_MODEL_NAME, system_instruction=lite_system_prompt)
                lite_conversation = history + [{'role': 'user', 'parts': [user_query]}]

                logger.info("1단계: Lite 모델(의도 분석) 호출...", extra=log_extra)
                lite_response = await self._safe_generate_content(lite_model, lite_conversation, log_extra)

                if not lite_response or not lite_response.text:
                    logger.error("Lite 모델이 응답을 생성하지 못했습니다.", extra=log_extra)
                    await message.reply(config.MSG_AI_ERROR, mention_author=False)
                    return

                lite_response_text = lite_response.text.strip()
                logger.info(f"Lite 모델 응답: '{lite_response_text[:150]}...'", extra=log_extra)
                
                tool_plan = self._parse_tool_calls(lite_response_text)

                # --- [분기 처리] ---
                # Case 1: 간단한 대화로 판단된 경우 (<conversation_response> 태그 포함)
                if "<conversation_response>" in lite_response_text:
                    logger.info("분기: 간단한 대화로 판단, Main 모델을 호출하여 페르소나 기반으로 응답합니다.", extra=log_extra)
                    channel_config = config.CHANNEL_AI_CONFIG.get(message.channel.id, {})
                    persona = channel_config.get('persona', config.DEFAULT_TSUNDERE_PERSONA)
                    rules = channel_config.get('rules', config.DEFAULT_TSUNDERE_RULES)
                    system_prompt = f"{persona}\n\n{rules}"
                    
                    main_model = genai.GenerativeModel(config.AI_RESPONSE_MODEL_NAME, system_instruction=system_prompt)
                    main_response = await self._safe_generate_content(main_model, user_query, log_extra)

                    if main_response and main_response.text:
                        final_response_text = main_response.text.strip()
                        await message.reply(final_response_text, mention_author=False)
                        await db_utils.log_analytics(self.bot.db, "AI_INTERACTION", {"guild_id": message.guild.id, "user_id": message.author.id, "channel_id": message.channel.id, "trace_id": trace_id, "user_query": user_query, "tool_plan": [], "final_response": final_response_text, "is_fallback": False})
                    else:
                        logger.error("간단한 대화에 대해 Main 모델이 응답을 생성하지 못했습니다.", extra=log_extra)
                        await message.reply(config.MSG_AI_ERROR, mention_author=False)
                    return

                # Case 2: 도구 계획이 없는 경우 (Lite 모델이 직접 답변)
                if not tool_plan:
                    logger.info("분기: 도구 계획이 없으며, Lite 모델의 답변으로 바로 응답합니다.", extra=log_extra)
                    await message.reply(lite_response_text, mention_author=False)
                    await db_utils.log_analytics(self.bot.db, "AI_INTERACTION", {"guild_id": message.guild.id, "user_id": message.author.id, "channel_id": message.channel.id, "trace_id": trace_id, "user_query": user_query, "tool_plan": [], "final_response": lite_response_text, "is_fallback": False})
                    return

                # --- [2단계] 도구 실행 ---
                logger.info(f"2단계: 도구 실행 시작. 총 {len(tool_plan)}단계.", extra=log_extra)
                tool_results = []
                for i, tool_call in enumerate(tool_plan):
                    step_num = i + 1
                    logger.info(f"계획 실행 ({step_num}/{len(tool_plan)}): {tool_call.get('tool_to_use')}", extra=log_extra)
                    result = await self._execute_tool(tool_call, message.guild.id, user_query)
                    tool_results.append({"step": step_num, "tool_name": tool_call.get('tool_to_use'), "parameters": tool_call.get('parameters'), "result": result})

                # --- [폴백 로직] 도구 실패 시 웹 검색으로 대체 ---
                def is_tool_failed(result): return result is None or any(keyword in str(result).lower() for keyword in ["error", "오류", "실패", "없습니다", "알 수 없는", "찾을 수"])
                any_failed = any(is_tool_failed(res.get("result")) for res in tool_results)
                use_fallback_prompt = False

                if tool_plan and any_failed and not any(tc.get('tool_to_use') == 'web_search' for tc in tool_plan):
                    logger.info("하나 이상의 도구 실행에 실패하여 웹 검색으로 대체합니다.", extra=log_extra)
                    web_search_result = await self._execute_tool({"tool_to_use": "web_search", "parameters": {"query": user_query}}, message.guild.id, user_query)
                    tool_results = [{"step": 1, "tool_name": "web_search", "parameters": {"query": user_query}, "result": web_search_result}]
                    use_fallback_prompt = True

                # --- [3단계] Main 모델 호출: 최종 답변 생성 ---
                logger.info("3단계: Main 모델(답변 생성) 호출...", extra=log_extra)
                tool_results_str = json.dumps(tool_results, ensure_ascii=False)
                
                # 프롬프트 선택: 일반, 여행용, 웹 폴백용
                if use_fallback_prompt:
                    main_system_prompt = config.WEB_FALLBACK_PROMPT.format(user_query=user_query, tool_result=tool_results_str)
                elif any(tc.get('tool_to_use') == 'get_travel_recommendation' for tc in tool_plan):
                    main_system_prompt = config.SPECIALIZED_PROMPTS["travel_assistant"].format(user_query=user_query, tool_result=tool_results_str)
                else:
                    main_system_prompt = config.AGENT_SYSTEM_PROMPT.format(user_query=user_query, tool_result=tool_results_str)
                
                # 페르소나 적용
                channel_config = config.CHANNEL_AI_CONFIG.get(message.channel.id, {})
                persona = channel_config.get('persona', config.DEFAULT_TSUNDERE_PERSONA)
                rules = channel_config.get('rules', config.DEFAULT_TSUNDERE_RULES)
                main_system_prompt = f"{persona}\n\n{rules}\n\n{main_system_prompt}"

                main_model = genai.GenerativeModel(config.AI_RESPONSE_MODEL_NAME, system_instruction=main_system_prompt)
                main_prompt = "이제 모든 도구 실행 결과를 바탕으로, 사용자의 원래 질문에 대해 페르소나를 완벽하게 적용해서 최종 답변을 생성해줘."
                main_response = await self._safe_generate_content(main_model, main_prompt, log_extra)

                if main_response and main_response.text:
                    final_response_text = main_response.text.strip()
                    await message.reply(final_response_text, mention_author=False)
                    await db_utils.log_analytics(self.bot.db, "AI_INTERACTION", {"guild_id": message.guild.id, "user_id": message.author.id, "channel_id": message.channel.id, "trace_id": trace_id, "user_query": user_query, "tool_plan": tool_plan, "final_response": final_response_text, "is_fallback": use_fallback_prompt})
                else:
                    logger.error("Main 모델이 최종 답변을 생성하지 못했습니다.", extra=log_extra)
                    # 메시지 길이 제한 오류 방지를 위해 tool_results_str를 3800자로 자릅니다.
                    truncated_results = tool_results_str[:3800]
                    await message.reply(f"모든 도구를 실행했지만, 최종 답변을 만드는 데 실패했어요. 여기 결과라도 확인해보세요.\n```json\n{truncated_results}\n```", mention_author=False)

            except Exception as e:
                logger.error(f"에이전트 처리 중 최상위 오류: {e}", exc_info=True, extra=log_extra)
                await message.reply(config.MSG_AI_ERROR, mention_author=False)

    async def _get_recent_history(self, message: discord.Message, rag_prompt: str) -> list:
        """모델에 전달할 최근 대화 기록을 채널에서 가져옵니다."""
        history_limit = 6 if rag_prompt else 12
        history = []
        
        async for msg in message.channel.history(limit=history_limit + 1):
            if msg.id == message.id: continue
            role = 'model' if msg.author.id == self.bot.user.id else 'user'
            content = msg.content[:2000]
            history.append({'role': role, 'parts': [content]})

        history.reverse()
        return history

    async def should_proactively_respond(self, message: discord.Message) -> bool:
        """봇이 대화에 능동적으로 참여할지 여부를 결정하는 게이트키퍼 로직입니다."""
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
            gatekeeper_prompt = f"""{conf['gatekeeper_persona']}\n\n--- 최근 대화 내용 ---\n{conversation_context}\n---\n사용자의 마지막 메시지: \"{message.content}\"\n---\n\n자, 판단해. Yes or No?"""

            lite_model = genai.GenerativeModel(config.AI_INTENT_MODEL_NAME)
            response = await self._safe_generate_content(lite_model, gatekeeper_prompt, log_extra)

            if response and "YES" in response.text.strip().upper():
                self.proactive_cooldowns[message.channel.id] = now
                return True
        except Exception as e:
            logger.error(f"게이트키퍼 AI 실행 중 오류: {e}", exc_info=True, extra=log_extra)

        return False

    async def get_recent_conversation_text(self, guild_id: int, channel_id: int, look_back: int = 20) -> str:
        """DB에서 최근 대화 기록을 텍스트로 가져옵니다 (요약 기능용)."""
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

    async def generate_system_alert_message(self, channel_id: int, alert_context: str, alert_title: str | None = None) -> str | None:
        """주기적 알림 등 시스템 메시지를 AI 말투로 재작성합니다."""
        if not self.is_ready:
            return None

        log_extra = {'channel_id': channel_id, 'alert_title': alert_title}

        try:
            channel_config = config.CHANNEL_AI_CONFIG.get(channel_id, {})
            persona = channel_config.get('persona', config.DEFAULT_TSUNDERE_PERSONA)
            rules = channel_config.get('rules', config.DEFAULT_TSUNDERE_RULES)

            system_prompt = (
                f"{persona}\n\n{rules}\n\n"
                "### 추가 지침\n"
                "- 지금은 서버 구성원에게 전달할 시스템 공지를 작성하는 중이다.\n"
                "- 핵심 정보는 빠뜨리지 말되 2~3문장 이내로 간결하게 정리한다.\n"
                "- 필요 시 가벼운 이모지 한두 개만 사용하고, 과한 장식은 피한다.\n"
                "- 마지막에는 자연스럽게 행동을 촉구하거나 격려하는 말을 덧붙인다."
            )

            user_prompt = (
                "다음 정보를 바탕으로 서버에 전달할 공지 메시지를 작성해줘.\n"
                f"- 알림 주제: {alert_title or '일반 알림'}\n"
                f"- 전달할 내용: {alert_context}\n\n"
                "공지 문구는 마사몽의 말투를 유지해 주고, 너무 장황하지 않게 작성해줘."
            )

            model = genai.GenerativeModel(
                model_name=config.AI_RESPONSE_MODEL_NAME,
                system_instruction=system_prompt,
            )
            response = await self._safe_generate_content(model, user_prompt, log_extra)
            if response and response.text:
                alert_message = response.text.strip()
                if len(alert_message) > config.AI_RESPONSE_LENGTH_LIMIT:
                    alert_message = alert_message[:config.AI_RESPONSE_LENGTH_LIMIT].rstrip()
                return alert_message

        except Exception as e:
            logger.error(
                "시스템 알림 메시지 생성 중 오류: %s",
                e,
                exc_info=True,
                extra=log_extra,
            )

        return None

    async def generate_creative_text(self, channel: discord.TextChannel, author: discord.User, prompt_key: str, context: dict) -> str:
        """`!운세`, `!랭킹` 등 특정 명령어에 대한 창의적인 AI 답변을 생성합니다."""
        if not self.is_ready: return config.MSG_AI_ERROR
        log_extra = {'guild_id': channel.guild.id, 'user_id': author.id, 'prompt_key': prompt_key}

        try:
            prompt_template = config.AI_CREATIVE_PROMPTS.get(prompt_key)
            if not prompt_template: return config.MSG_CMD_ERROR

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
    """Cog를 봇에 등록하는 함수"""
    await bot.add_cog(AIHandler(bot))

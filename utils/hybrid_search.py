# -*- coding: utf-8 -*-
"""하이브리드 검색(임베딩 + BM25)과 재순위화 파이프라인을 제공하는 모듈."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List

try:  # pragma: no cover - 선택적 의존성
    import numpy as np  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    np = None  # type: ignore

import config
from database.bm25_index import BM25IndexManager
from logger_config import logger
from utils.chunker import SemanticChunker, ChunkerConfig
from utils.embeddings import DiscordEmbeddingStore, KakaoEmbeddingStore, get_embedding
from utils.query_rewriter import expand_query
from utils.reranker import Reranker


@dataclass
class HybridSearchResult:
    """하이브리드 검색 결과 리스트와 부가 정보를 캡슐화합니다."""

    entries: List[dict[str, Any]]
    query_variants: List[str]
    top_score: float



class HybridSearchEngine:
    """BM25 + 임베딩 결합 검색을 처리하는 엔진."""

    def __init__(
        self,
        discord_store: DiscordEmbeddingStore,
        kakao_store: KakaoEmbeddingStore | None,
        bm25_manager: BM25IndexManager | None,
        *,
        reranker: Reranker | None = None,
        chunker: SemanticChunker | None = None,
    ):
        self.discord_store = discord_store
        self.kakao_store = kakao_store
        self.bm25_manager = bm25_manager
        self.reranker = reranker if config.RERANK_ENABLED else None
        if config.SEARCH_CHUNKING_ENABLED:
            self.chunker = chunker or SemanticChunker(ChunkerConfig(max_tokens=220, overlap_tokens=80))
        else:
            self.chunker = None

        self.embedding_limit = getattr(config, "LOCAL_EMBEDDING_QUERY_LIMIT", 200)
        self.embedding_threshold = config.RAG_SIMILARITY_THRESHOLD
        self.hybrid_top_k = getattr(config, "RAG_HYBRID_TOP_K", 4)
        self.embedding_top_n = getattr(config, "RAG_EMBEDDING_TOP_N", 8)
        self.bm25_top_n = getattr(config, "RAG_BM25_TOP_N", 8)
        self.embedding_weight = 0.55
        self.bm25_weight = 0.45
        self.neighbor_radius = max(1, getattr(config, "CONVERSATION_NEIGHBOR_RADIUS", 3))
        self.recent_turn_limit = 2
        self.query_expansion_enabled = config.SEARCH_QUERY_EXPANSION_ENABLED
        self._warned_numpy = False

    async def search(
        self,
        query: str,
        *,
        guild_id: int,
        channel_id: int,
        user_id: int | None = None,
        recent_messages: list[str] | None = None,
    ) -> HybridSearchResult:
        """주어진 질문에 대해 하이브리드 검색을 수행합니다."""
        # 최근 대화 맥락을 활용해 확장 쿼리를 만든다.
        variants = await self._expand_query_variants(query, recent_messages=recent_messages)
        if not variants:
            return HybridSearchResult(entries=[], query_variants=[], top_score=0.0)

        candidate_map: Dict[str, dict[str, Any]] = {}
        for variant in variants:
            embed_entries = await self._embedding_candidates(
                variant,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
            )
            for rank, entry in enumerate(embed_entries[: self.embedding_top_n]):
                # 임베딩 후보는 가중치 계산을 위해 랭크를 기록한다.
                self._merge_candidate(candidate_map, entry, source="embedding", rank=rank)

            bm25_entries = await self._bm25_candidates(
                variant,
                guild_id=guild_id,
                channel_id=channel_id,
            )
            for rank, entry in enumerate(bm25_entries[: self.bm25_top_n]):
                # BM25 후보도 동일한 후보 맵에 합산한다.
                self._merge_candidate(candidate_map, entry, source="bm25", rank=rank)

        if not candidate_map:
            return HybridSearchResult(entries=[], query_variants=variants, top_score=0.0)

        enriched: List[dict[str, Any]] = []
        for candidate_id, entry in candidate_map.items():
            candidate = dict(entry)
            candidate["candidate_id"] = candidate_id
            sources = candidate.get("sources")
            if isinstance(sources, set):
                candidate["sources"] = sorted(sources)

            similarity = candidate.get("similarity") or 0.0
            bm25_score = candidate.get("bm25_score") or 0.0
            combined = 0.0
            if similarity > 0.0:
                combined += similarity * self.embedding_weight  # 의미 기반 점수 비중
            if bm25_score > 0.0:
                combined += bm25_score * self.bm25_weight  # 키워드 기반 점수 비중
            if combined == 0.0:
                combined = max(similarity, bm25_score)
            candidate["combined_score"] = combined
            if not candidate.get("dialogue_block"):
                candidate["dialogue_block"] = self._format_dialogue_block(candidate.get("dialogue_messages") or [])
            enriched.append(candidate)

        enriched.sort(
            key=lambda item: (
                item.get("combined_score", 0.0),
                item.get("similarity", 0.0),
                item.get("bm25_score", 0.0),
            ),
            reverse=True,
        )
        enriched = enriched[: max(self.hybrid_top_k, 1)]

        reranked = await self._apply_reranker(query, enriched)
        top_score = reranked[0].get("combined_score", 0.0) if reranked else 0.0
        return HybridSearchResult(entries=reranked, query_variants=variants, top_score=top_score)

    async def _expand_query_variants(
        self,
        query: str,
        *,
        recent_messages: list[str] | None = None,
    ) -> List[str]:
        base = (query or "").strip()
        context = self._compose_recent_context(recent_messages)  # 직전 대화 요약본
        seed = f"{base} {context}".strip() if context else base

        variants: List[str] = []
        seen: set[str] = set()

        def _append(candidate: str) -> None:
            norm = (candidate or "").strip()
            if not norm:
                return
            key = norm.lower()
            if key in seen:
                return
            seen.add(key)
            variants.append(norm)

        _append(base)
        if context and seed:
            _append(seed)

        target = max(int(getattr(config, "RAG_QUERY_REWRITE_VARIANTS", 3)), 1)
        if not self.query_expansion_enabled:
            limit = max(target, len(variants))
            return variants[:limit] if limit else variants

        try:
            generated = await expand_query(seed or base, max_variants=target)
        except Exception as exc:  # pragma: no cover - 방어적 처리
            logger.warning("쿼리 변형 생성 실패, 원본으로 진행합니다: %s", exc)
            limit = max(target, len(variants))
            return variants[:limit] if limit else variants

        for candidate in generated:
            _append(candidate)
            if len(variants) >= target:
                break

        if not variants:
            _append(seed or base)
        limit = max(target, len(variants))
        return variants[:limit]

    async def _embedding_candidates(
        self,
        query: str,
        *,
        guild_id: int,
        channel_id: int,
        user_id: int | None,
    ) -> List[dict[str, Any]]:
        if np is None:
            if not self._warned_numpy:
                logger.warning("numpy가 없어 임베딩 기반 검색을 사용할 수 없습니다.")
                self._warned_numpy = True
            return []

        query_vector = await get_embedding(query)
        if query_vector is None:
            return []

        dispatcher: List[dict[str, Any]] = []
        discord_rows = await self.discord_store.fetch_recent_embeddings(
            server_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            limit=self.embedding_limit,
        )

        for raw_row in discord_rows:
            row = dict(raw_row)
            vector = self._to_vector(row.get("embedding"))
            message = row.get("message") or ""
            if vector is None or not message.strip():
                continue
            similarity = self._cosine_similarity(query_vector, vector)
            if similarity < self.embedding_threshold:
                continue

            message_id = row.get("message_id")
            try:
                message_id_int = int(message_id)
            except (TypeError, ValueError):
                message_id_int = None
            timestamp = row.get("timestamp") or ""

            focus = {
                "message_id": message_id_int,
                "user_name": row.get("user_name") or "User",
                "content": message,
                "created_at": timestamp,
                "is_bot": False,
            }
            dialogue_messages = await self._resolve_dialogue_messages(
                channel_id=channel_id,
                message_id=message_id_int,
                fallback=row.get("context_window"),
                focus=focus,
            )
            dialogue_block = self._format_dialogue_block(dialogue_messages)
            if not dialogue_block:
                dialogue_block = self._clean_content(message)

            dispatcher.append(
                {
                    "id": f"discord:{message_id}",
                    "message_id": message_id,
                    "message": message,
                    "origin": "Discord",
                    "speaker": row.get("user_name"),
                    "similarity": similarity,
                    "matched_server_id": str(guild_id),
                    "timestamp": timestamp,
                    "source": "embedding",
                    "dialogue_messages": dialogue_messages,
                    "dialogue_block": dialogue_block,
                }
            )

        if self.kakao_store is not None:
            kakao_rows = await self.kakao_store.fetch_recent_embeddings(
                server_ids={str(channel_id), str(guild_id)},
                limit=self.embedding_limit,
                query_vector=query_vector,
            )
            for raw_row in kakao_rows:
                row = dict(raw_row)
                vector = self._to_vector(row.get("embedding"))
                message = row.get("message") or ""
                if vector is None or not message.strip():
                    continue
                similarity = self._cosine_similarity(query_vector, vector)
                if similarity < self.embedding_threshold:
                    continue

                message_id = row.get("message_id")
                try:
                    message_id_int = int(message_id)
                except (TypeError, ValueError):
                    message_id_int = None
                timestamp = row.get("timestamp") or ""

                focus = {
                    "message_id": message_id_int,
                    "user_name": row.get("speaker") or row.get("user_name") or "카카오",
                    "content": message,
                    "created_at": timestamp,
                    "is_bot": False,
                }
                dialogue_messages = await self._resolve_dialogue_messages(
                    channel_id=channel_id,
                    message_id=message_id_int,
                    fallback=None,
                    focus=focus,
                )
                dialogue_block = self._format_dialogue_block(dialogue_messages)
                if not dialogue_block:
                    dialogue_block = self._clean_content(message)

                origin = row.get("label") or "카카오"
                dispatcher.append(
                    {
                        "id": f"kakao:{row.get('db_path')}:{message_id}",
                        "message_id": message_id,
                        "message": message,
                        "origin": origin,
                        "speaker": row.get("speaker"),
                        "similarity": similarity,
                        "matched_server_id": row.get("matched_server_id"),
                        "timestamp": timestamp,
                        "source": "embedding",
                        "dialogue_messages": dialogue_messages,
                        "dialogue_block": dialogue_block,
                        "db_path": row.get("db_path"),
                    }
                )

        dispatcher.sort(key=lambda item: item.get("similarity", 0.0), reverse=True)
        return dispatcher

    async def _bm25_candidates(
        self,
        query: str,
        *,
        guild_id: int,
        channel_id: int,
    ) -> List[dict[str, Any]]:
        if self.bm25_manager is None:
            return []

        results = await self.bm25_manager.search(
            query,
            guild_id=guild_id,
            channel_id=channel_id,
            limit=self.bm25_top_n,
        )
        dispatcher: List[dict[str, Any]] = []
        for item in results:
            normalized_score = 1.0 / (1.0 + item.bm25_score)
            focus = {
                "message_id": item.message_id,
                "user_name": item.user_name,
                "content": item.content,
                "created_at": item.created_at,
                "is_bot": False,
            }
            dialogue_messages = await self._resolve_dialogue_messages(
                channel_id=channel_id,
                message_id=item.message_id,
                fallback=item.context_window,
                focus=focus,
            )
            dialogue_block = self._format_dialogue_block(dialogue_messages)
            if not dialogue_block:
                dialogue_block = self._clean_content(item.content)

            dispatcher.append(
                {
                    "id": f"bm25:{item.message_id}",
                    "message_id": item.message_id,
                    "message": item.content,
                    "origin": "Discord",
                    "speaker": item.user_name,
                    "bm25_score": normalized_score,
                    "bm25_score_raw": item.bm25_score,
                    "matched_server_id": str(item.guild_id),
                    "timestamp": item.created_at,
                    "source": "bm25",
                    "dialogue_messages": dialogue_messages,
                    "dialogue_block": dialogue_block,
                }
            )
        return dispatcher

    def _merge_candidate(
        self,
        candidate_map: Dict[str, dict[str, Any]],
        entry: dict[str, Any],
        *,
        source: str,
        rank: int,
    ) -> None:
        candidate_id = entry.get("id")
        if not candidate_id:
            return

        candidate = candidate_map.get(candidate_id)
        if candidate is None:
            candidate = dict(entry)
            candidate["sources"] = {source}
            if source == "embedding":
                candidate["embedding_rank"] = rank
            if source == "bm25":
                candidate["bm25_rank"] = rank
            candidate_map[candidate_id] = candidate
            return

        candidate.setdefault("sources", set()).add(source)
        if source == "embedding":
            new_sim = entry.get("similarity") or 0.0
            old_sim = candidate.get("similarity") or 0.0
            candidate["embedding_rank"] = min(candidate.get("embedding_rank", rank), rank)
            if new_sim > old_sim:
                candidate["similarity"] = new_sim
                candidate["message"] = entry.get("message") or candidate.get("message")
                candidate["dialogue_messages"] = entry.get("dialogue_messages") or candidate.get("dialogue_messages")
                candidate["dialogue_block"] = entry.get("dialogue_block") or candidate.get("dialogue_block")
        elif source == "bm25":
            new_bm25 = entry.get("bm25_score") or 0.0
            old_bm25 = candidate.get("bm25_score") or 0.0
            candidate["bm25_rank"] = min(candidate.get("bm25_rank", rank), rank)
            if new_bm25 > old_bm25:
                candidate["bm25_score"] = new_bm25
                candidate["bm25_score_raw"] = entry.get("bm25_score_raw")
                candidate["message"] = entry.get("message") or candidate.get("message")
                candidate["dialogue_messages"] = entry.get("dialogue_messages") or candidate.get("dialogue_messages")
                candidate["dialogue_block"] = entry.get("dialogue_block") or candidate.get("dialogue_block")

        if not candidate.get("dialogue_block") and entry.get("dialogue_block"):
            candidate["dialogue_block"] = entry.get("dialogue_block")
            candidate["dialogue_messages"] = entry.get("dialogue_messages")

    async def _resolve_dialogue_messages(
        self,
        *,
        channel_id: int,
        message_id: int | None,
        fallback: Iterable[dict[str, Any]] | None,
        focus: dict[str, Any] | None,
    ) -> List[dict[str, Any]]:
        messages: List[dict[str, Any]] = []
        target_id_str = str(message_id) if message_id is not None else None

        if message_id is not None and self.bm25_manager is not None:
            window = await self.bm25_manager.fetch_window_for_message(
                channel_id=channel_id,
                message_id=message_id,
            )
            if window:
                messages = [self._coerce_dialogue_entry(item) for item in window]  # 미리 캐싱한 윈도우 활용
            else:
                neighbors = await self.bm25_manager.fetch_neighbors(
                    channel_id=channel_id,
                    message_id=message_id,
                    radius=self.neighbor_radius,
                )
                messages = [self._coerce_dialogue_entry(item) for item in neighbors]  # 주변 대화를 직접 조회

        if not messages and fallback:
            for item in fallback:
                messages.append(self._coerce_dialogue_entry(item))

        if focus:
            messages.append(self._coerce_dialogue_entry(focus))

        messages = self._dedupe_messages(messages)
        if target_id_str is not None:
            messages = self._trim_window(messages, target_id_str, self.neighbor_radius)
        return messages

    def _coerce_dialogue_entry(self, raw: dict[str, Any]) -> dict[str, Any]:
        message_id = raw.get("message_id")
        try:
            message_id = int(message_id)
        except (TypeError, ValueError):
            message_id = None
        user_name = raw.get("user_name") or raw.get("speaker") or "User"
        content = raw.get("content") or raw.get("message") or ""
        created_at = raw.get("created_at") or raw.get("timestamp") or ""
        is_bot = bool(raw.get("is_bot", False))
        return {
            "message_id": message_id,
            "user_name": user_name,
            "content": content,
            "created_at": created_at,
            "is_bot": is_bot,
        }

    def _trim_window(
        self,
        messages: List[dict[str, Any]],
        target_id: str,
        radius: int,
    ) -> List[dict[str, Any]]:
        if not messages:
            return []
        center_index = 0
        for idx, item in enumerate(messages):
            mid = item.get("message_id")
            if mid is not None and str(mid) == target_id:
                center_index = idx
                break
        start = max(0, center_index - radius)
        end = min(len(messages), center_index + radius + 1)
        return messages[start:end]

    def _dedupe_messages(self, messages: List[dict[str, Any]]) -> List[dict[str, Any]]:
        deduped: List[dict[str, Any]] = []
        seen: set[str] = set()
        for item in messages:
            content = self._clean_content(item.get("content") or item.get("message") or "")
            if not content:
                continue
            item = dict(item)
            item["content"] = content
            key = f"{item.get('message_id')}::{content}"
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    def _format_dialogue_block(self, messages: List[dict[str, Any]]) -> str:
        if not messages:
            return ""
        lines: List[str] = []
        seen_lines: set[str] = set()
        for item in messages:
            speaker = item.get("user_name") or "User"
            if item.get("is_bot"):
                speaker = "마사몽"
            timestamp = item.get("created_at") or ""
            content = self._clean_content(item.get("content") or "")
            if not content:
                continue
            line = f"[{speaker}][{timestamp}] {content}".strip()  # 대화 흐름을 한 줄로 요약
            if line in seen_lines:
                continue
            seen_lines.add(line)
            lines.append(line)
        combined = "\n".join(lines)
        if self.chunker and combined:
            chunks = self.chunker.chunk(combined, metadata={"origin": "dialogue"})
            if chunks:
                combined = chunks[0].text
        return combined

    def _clean_content(self, text: str) -> str:
        if not text:
            return ""
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        normalized = re.sub(r"https?://\S+", "[링크]", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized

    def _compose_recent_context(self, recent_messages: list[str] | None) -> str:
        if not recent_messages:
            return ""
        trimmed: List[str] = []
        for msg in recent_messages[: self.recent_turn_limit]:
            if not msg:
                continue
            cleaned = self._clean_content(msg)
            if cleaned:
                trimmed.append(cleaned)
        return " ".join(trimmed)

    async def _apply_reranker(
        self,
        original_query: str,
        entries: List[dict[str, Any]],
    ) -> List[dict[str, Any]]:
        if not entries:
            return []
        if self.reranker is None:
            return entries

        docs = []
        for entry in entries:
            text = entry.get("dialogue_block") or entry.get("message") or ""
            if not text:
                continue
            docs.append(
                {
                    "text": text,
                    "origin": entry.get("origin"),
                    "message_id": entry.get("message_id"),
                    "candidate_id": entry.get("candidate_id"),
                    "combined_score": entry.get("combined_score", 0.0),
                }
            )

        if not docs:
            return entries

        reranked = await self.reranker.rerank(
            original_query,
            docs,
            top_k=max(len(entries), 1),
        )
        if not reranked:
            return entries

        merged: List[dict[str, Any]] = []
        for item in reranked:
            candidate_id = item.get("candidate_id")
            base = next((entry for entry in entries if entry.get("candidate_id") == candidate_id), None)
            if base is None:
                continue
            enriched = dict(base)
            enriched["rerank_score"] = item.get("rerank_score")
            merged.append(enriched)

        if not merged:
            return entries

        merged.sort(
            key=lambda entry: (
                entry.get("rerank_score", float("-inf")),
                entry.get("combined_score", float("-inf")),
            ),
            reverse=True,
        )
        return merged[: len(entries)]

    def _to_vector(self, blob: Any) -> np.ndarray | None:
        if blob is None:
            return None
        if isinstance(blob, np.ndarray):
            return blob.astype(np.float32)
        if isinstance(blob, memoryview):
            blob = blob.tobytes()
        if isinstance(blob, (bytes, bytearray)):
            return np.frombuffer(blob, dtype=np.float32)
        if isinstance(blob, list):
            return np.asarray(blob, dtype=np.float32)
        if isinstance(blob, str):
            try:
                parsed = json.loads(blob)
            except json.JSONDecodeError:
                return None
            if isinstance(parsed, list):
                return np.asarray(parsed, dtype=np.float32)
        return None

    @staticmethod
    def _cosine_similarity(v1: np.ndarray, v2: np.ndarray) -> float:
        norm_v1 = np.linalg.norm(v1)
        norm_v2 = np.linalg.norm(v2)
        if norm_v1 == 0 or norm_v2 == 0:
            return 0.0
        return float(np.dot(v1, v2) / (norm_v1 * norm_v2))

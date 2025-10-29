# -*- coding: utf-8 -*-
"""하이브리드 검색(임베딩 + BM25)과 재순위화 파이프라인을 제공하는 모듈."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
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
        self.reranker = reranker
        self.chunker = chunker or SemanticChunker(ChunkerConfig(max_tokens=220, overlap_tokens=80))

        self.embedding_limit = getattr(config, "LOCAL_EMBEDDING_QUERY_LIMIT", 200)
        self.embedding_threshold = config.RAG_SIMILARITY_THRESHOLD
        self.hybrid_top_k = getattr(config, "RAG_HYBRID_TOP_K", 6)
        self.embedding_top_n = getattr(config, "RAG_EMBEDDING_TOP_N", 20)
        self.bm25_top_n = getattr(config, "RAG_BM25_TOP_N", 20)
        self.rrf_constant = getattr(config, "RAG_RRF_K", 60)
        self._warned_numpy = False

    async def search(
        self,
        query: str,
        *,
        guild_id: int,
        channel_id: int,
        user_id: int | None = None,
    ) -> HybridSearchResult:
        """주어진 질문에 대해 하이브리드 검색을 수행합니다."""
        variants = await self._expand_query_variants(query)
        if not variants:
            return HybridSearchResult(entries=[], query_variants=[], top_score=0.0)

        candidate_map: Dict[str, dict[str, Any]] = {}
        rankings: Dict[str, List[str]] = defaultdict(list)

        for variant_idx, variant in enumerate(variants):
            embed_entries = await self._embedding_candidates(
                variant,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
            )
            embed_entries.sort(key=lambda item: item.get("similarity", 0.0), reverse=True)
            embed_entries = embed_entries[: self.embedding_top_n]
            self._merge_candidates(
                candidate_map,
                embed_entries,
                rankings,
                source_key=f"embedding:{variant_idx}",
            )

            bm25_entries = await self._bm25_candidates(
                variant,
                guild_id=guild_id,
                channel_id=channel_id,
            )
            bm25_entries = bm25_entries[: self.bm25_top_n]
            # BM25 결과도 동일한 풀에서 스코어를 합산할 수 있도록 병합한다.
            self._merge_candidates(
                candidate_map,
                bm25_entries,
                rankings,
                source_key=f"bm25:{variant_idx}",
            )

        if not candidate_map:
            return HybridSearchResult(entries=[], query_variants=variants, top_score=0.0)

        rrf_scores = self._reciprocal_rank_fusion(rankings)
        enriched: List[dict[str, Any]] = []
        for candidate_id, entry in candidate_map.items():
            merged = dict(entry)
            merged["candidate_id"] = candidate_id
            merged["hybrid_score"] = rrf_scores.get(candidate_id, 0.0)
            sources = merged.get("sources")
            if isinstance(sources, set):
                merged["sources"] = sorted(sources)
            enriched.append(merged)

        enriched.sort(key=lambda item: item.get("hybrid_score", 0.0), reverse=True)
        enriched = enriched[: max(self.hybrid_top_k, 1)]

        reranked = await self._apply_reranker(query, enriched)
        top_score = reranked[0].get("hybrid_score", 0.0) if reranked else 0.0
        return HybridSearchResult(entries=reranked, query_variants=variants, top_score=top_score)

    async def _expand_query_variants(self, query: str) -> List[str]:
        try:
            variants = await expand_query(query, max_variants=config.RAG_QUERY_REWRITE_VARIANTS)
        except Exception as exc:  # pragma: no cover - 방어적 처리
            logger.warning("쿼리 변형 생성 실패, 원본으로 진행합니다: %s", exc)
            variants = [query]
        unique: List[str] = []
        seen = set()
        for variant in variants:
            norm = variant.strip()
            if not norm or norm.lower() in seen:
                continue
            seen.add(norm.lower())
            unique.append(norm)
        return unique or [query]

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
            message = row.get("message")
            if vector is None or not message:
                continue
            similarity = self._cosine_similarity(query_vector, vector)
            if similarity < self.embedding_threshold:
                continue
            message_id = row.get("message_id")
            timestamp = row.get("timestamp")
            context_window = await self._safe_fetch_context(channel_id, timestamp)
            dispatcher.append(
                {
                    "id": f"discord:{message_id}",
                    "message_id": message_id,
                    "message": message,
                    "origin": "Discord",
                    "speaker": row.get("user_name"),
                    "similarity": similarity,
                    "matched_server_id": str(guild_id),
                    "context_window": context_window,
                    "timestamp": timestamp,
                    "source": "embedding",
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
                message = row.get("message")
                if vector is None or not message:
                    continue
                similarity = self._cosine_similarity(query_vector, vector)
                if similarity < self.embedding_threshold:
                    continue
                message_id = row.get("message_id")
                db_path = row.get("db_path")
                label = row.get("label")
                origin = "카카오"
                if label and label != origin:
                    origin = f"카카오:{label}"
                dispatcher.append(
                    {
                        "id": f"kakao:{db_path}:{message_id}",
                        "message_id": message_id,
                        "message": message,
                        "origin": origin,
                        "speaker": row.get("speaker"),
                        "similarity": similarity,
                        "matched_server_id": row.get("matched_server_id"),
                        "context_window": row.get("context_window") or [],
                        "timestamp": row.get("timestamp"),
                        "source": "embedding",
                        "db_path": db_path,
                    }
                )

        for entry in dispatcher:
            entry["chunk_text"] = self._build_chunk_text(entry)
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
                    "context_window": item.context_window,
                    "timestamp": item.created_at,
                    "source": "bm25",
                    "chunk_text": self._build_chunk_text(
                        {
                            "message": item.content,
                            "context_window": item.context_window,
                            "speaker": item.user_name,
                            "origin": "Discord",
                        }
                    ),
                }
            )
        return dispatcher

    def _merge_candidates(
        self,
        candidate_map: Dict[str, dict[str, Any]],
        items: Iterable[dict[str, Any]],
        rankings: Dict[str, List[str]],
        *,
        source_key: str,
    ) -> None:
        for rank, entry in enumerate(items, start=1):
            candidate_id = entry.get("id")
            if not candidate_id:
                continue
            existing = candidate_map.get(candidate_id)
            if existing is None:
                combined = dict(entry)
                combined.setdefault("sources", set()).add(source_key)
                candidate_map[candidate_id] = combined
            else:
                merged = dict(existing)
                merged.setdefault("sources", set()).add(source_key)
                for key in ("similarity", "bm25_score", "bm25_score_raw"):
                    new_value = entry.get(key)
                    if new_value is None:
                        continue
                    if merged.get(key) is None or new_value > merged.get(key):
                        merged[key] = new_value
                if not merged.get("context_window") and entry.get("context_window"):
                    merged["context_window"] = entry["context_window"]
                if entry.get("chunk_text"):
                    merged["chunk_text"] = entry["chunk_text"]
                candidate_map[candidate_id] = merged
            rankings[source_key].append(candidate_id)

    async def _apply_reranker(self, original_query: str, entries: List[dict[str, Any]]) -> List[dict[str, Any]]:
        if not entries:
            return []
        if self.reranker is None:
            return entries
        docs = []
        for entry in entries:
            text = entry.get("chunk_text") or entry.get("message") or ""
            docs.append(
                {
                    "text": text,
                    "origin": entry.get("origin"),
                    "message_id": entry.get("message_id"),
                    "hybrid_score": entry.get("hybrid_score", 0.0),
                    "context_window": entry.get("context_window"),
                    "candidate_id": entry.get("candidate_id"),
                    "similarity": entry.get("similarity"),
                    "bm25_score": entry.get("bm25_score"),
                }
            )

        reranked = await self.reranker.rerank(
            original_query,
            docs,
            top_k=max(self.hybrid_top_k, 1),
        )

        if not reranked:
            return entries

        enriched: List[dict[str, Any]] = []
        for item in reranked:
            candidate_id = item.get("candidate_id")
            base = next((entry for entry in entries if entry.get("candidate_id") == candidate_id), None)
            if base is None:
                continue
            merged = dict(base)
            merged["rerank_score"] = item.get("rerank_score")
            enriched.append(merged)

        if not enriched:
            return entries
        enriched.sort(
            key=lambda entry: (
                entry.get("rerank_score", float("-inf")),
                entry.get("hybrid_score", float("-inf")),
            ),
            reverse=True,
        )
        return enriched[: max(self.hybrid_top_k, 1)]

    def _reciprocal_rank_fusion(self, rankings: Dict[str, List[str]]) -> Dict[str, float]:
        # RRF는 각 정렬 리스트에서의 순위에 따라 점수를 역수 형태로 누적한다.
        scores: Dict[str, float] = defaultdict(float)
        k = max(1, int(self.rrf_constant))
        for order in rankings.values():
            for idx, candidate_id in enumerate(order, start=1):
                scores[candidate_id] += 1.0 / (k + idx)
        return scores

    def _build_chunk_text(self, entry: dict[str, Any]) -> str:
        message = entry.get("message") or ""
        context_window = entry.get("context_window") or []
        if not context_window:
            return message
        lines = [message]
        for ctx in context_window:
            speaker = ctx.get("user_name") or ctx.get("speaker") or "?"
            ctx_message = ctx.get("message") or ""
            if not ctx_message:
                continue
            lines.append(f"{speaker}: {ctx_message}")
        combined = "\n".join(lines)
        chunks = self.chunker.chunk(combined, metadata={"origin": entry.get("origin")})
        if not chunks:
            return combined
        return chunks[0].text

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

    async def _safe_fetch_context(self, channel_id: int, timestamp: Any) -> List[dict[str, Any]]:
        if not timestamp or self.bm25_manager is None:
            return []
        try:
            # BM25 인덱스를 통해 메시지 주변의 맥락 창(window)을 재활용한다.
            return await self.bm25_manager.fetch_context(
                channel_id=int(channel_id),
                center_timestamp=str(timestamp),
            )
        except Exception:
            return []

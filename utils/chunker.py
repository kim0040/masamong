# -*- coding: utf-8 -*-
"""간단한 문장 분할 및 시맨틱 청킹 헬퍼."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, List, Sequence

_DEFAULT_SENTENCE_BOUNDARY = re.compile(r"(?<=[\.!?…])\s+")
_WHITESPACE_RE = re.compile(r"\s+")


def default_tokenizer(text: str) -> List[str]:
    """공백 기반 토큰화의 단순 구현."""
    if not text:
        return []
    return [token for token in _WHITESPACE_RE.split(text.strip()) if token]


def split_sentences(text: str) -> List[str]:
    """문장 종결부 기반의 라이트웨이트 분할기."""
    if not text:
        return []
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    candidates: List[str] = []
    for block in normalized.split("\n"):
        block = block.strip()
        if not block:
            continue
        pieces = _DEFAULT_SENTENCE_BOUNDARY.split(block)
        for piece in pieces:
            piece = piece.strip()
            if piece:
                candidates.append(piece)
    return candidates


@dataclass
class Chunk:
    """시맨틱 청크 결과를 표현합니다."""

    text: str
    token_count: int
    sentence_start: int
    sentence_end: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChunkerConfig:
    """청킹 동작을 제어하는 설정."""

    max_tokens: int = 180
    overlap_tokens: int = 60
    tokenizer: Callable[[str], List[str]] = default_tokenizer


class SemanticChunker:
    """단순 문장 기반 청킹 로직."""

    def __init__(self, config: ChunkerConfig | None = None):
        self.config = config or ChunkerConfig()

    def chunk(self, text: str, *, metadata: dict[str, Any] | None = None) -> List[Chunk]:
        """주어진 텍스트를 문장 단위로 청킹합니다."""
        sentences = split_sentences(text)
        if not sentences:
            return []

        tokenizer = self.config.tokenizer
        max_tokens = max(1, self.config.max_tokens)
        overlap_tokens = max(0, self.config.overlap_tokens)

        chunks: List[Chunk] = []
        cursor = 0
        sentence_count = len(sentences)
        while cursor < sentence_count:
            token_total = 0
            start = cursor
            end = cursor
            while end < sentence_count:
                # 토큰 길이를 누적하면서 최대 토큰 수를 초과하지 않는 범위까지 확장한다.
                token_total += len(tokenizer(sentences[end]))
                if token_total > max_tokens and end > start:
                    break
                end += 1

            if end == start:
                end += 1

            chunk_text = " ".join(sentences[start:end]).strip()
            chunk_tokens = len(tokenizer(chunk_text))
            chunk_metadata = dict(metadata or {})
            chunk_metadata.update(
                {
                    "sentence_start": start,
                    "sentence_end": end,
                    "sentence_count": sentence_count,
                }
            )
            chunks.append(
                Chunk(
                    text=chunk_text,
                    token_count=chunk_tokens,
                    sentence_start=start,
                    sentence_end=end,
                    metadata=chunk_metadata,
                )
            )

            # 마지막 청크이거나 오버랩이 필요 없는 경우 그대로 다음 위치로 이동한다.
            if overlap_tokens <= 0 or end >= sentence_count:
                cursor = end
                continue

            overlap_sentence_count = self._compute_overlap_sentences(
                sentences[start:end],
                tokenizer,
                overlap_tokens,
            )
            cursor = max(start + 1, end - overlap_sentence_count)

        return chunks

    @staticmethod
    def _compute_overlap_sentences(
        sentences: Sequence[str],
        tokenizer: Callable[[str], List[str]],
        overlap_tokens: int,
    ) -> int:
        """토큰 기준 오버랩 문장 개수를 계산합니다."""
        remaining = overlap_tokens
        count = 0
        for sentence in reversed(sentences):
            tokens = len(tokenizer(sentence))
            if tokens == 0:
                continue
            remaining -= tokens
            count += 1
            if remaining <= 0:
                break
        return min(count, len(sentences))

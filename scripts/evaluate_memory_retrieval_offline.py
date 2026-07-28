#!/usr/bin/env python3
"""합성 기억으로 기존/구조화 임베딩 문서의 검색 순위를 비교한다.

운영 DB와 사용자 원문은 읽지 않는다. 로컬 E5 모델이 준비돼 있어야 하며,
모델이 없으면 실패로 종료해 평가를 통과한 것처럼 오인하지 않게 한다.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from utils.embeddings import get_embedding  # noqa: E402
from utils.memory_units import compose_memory_text  # noqa: E402


DOCUMENTS: tuple[dict[str, Any], ...] = (
    {
        "summary": (
            "민수의 부산 회의 이동 계획: 8월 3일 오후 2시 부산역에서 만나며 "
            "자가용은 쓰지 않고 KTX 예약은 아직 미정이다."
        ),
        "raw": (
            "민수: 부산 회의는 8월 3일 오후 2시 부산역이야. "
            "자가용은 쓰지 말자. KTX는 아직 예약하지 않았어."
        ),
        "keywords": ("부산역", "8월 3일", "자가용", "KTX", "예약 미정"),
        "speakers": ("민수",),
        "memory_type": "plan",
        "timestamp": "2026-07-28T20:00:00+09:00",
    },
    {
        "summary": (
            "민수는 페퍼로니 피자를 좋아하지만 파인애플 피자는 싫어한다."
        ),
        "raw": "민수: 페퍼로니는 좋아. 파인애플 올라간 피자는 싫어.",
        "keywords": ("페퍼로니", "파인애플 피자", "싫어"),
        "speakers": ("민수",),
        "memory_type": "preference",
        "timestamp": "2026-07-27T19:00:00+09:00",
    },
    {
        "summary": (
            "전북대학교 편입 원서 접수 마감은 12월 18일이며 "
            "토익 성적표 제출이 필요하다."
        ),
        "raw": (
            "지연: 전북대 편입 원서는 12월 18일까지고 "
            "토익 성적표도 내야 해."
        ),
        "keywords": ("전북대학교", "편입", "12월 18일", "토익"),
        "speakers": ("지연",),
        "memory_type": "event",
        "timestamp": "2026-07-26T12:00:00+09:00",
    },
    {
        "summary": (
            "수현은 매주 월요일과 목요일 저녁 7시에 "
            "헬스장에서 운동하기로 했다."
        ),
        "raw": "수현: 월요일 목요일 저녁 7시에 헬스장 가기로 했어.",
        "keywords": ("월요일", "목요일", "저녁 7시", "헬스장"),
        "speakers": ("수현",),
        "memory_type": "plan",
        "timestamp": "2026-07-25T11:00:00+09:00",
    },
)

QUERIES = (
    ("부산 갈 때 어떤 교통수단은 안 쓰기로 했지?", 0),
    ("KTX 예약은 확정됐어?", 0),
    ("전북대 편입 원서 마감과 필요한 영어 성적은?", 2),
    ("민수가 싫어하는 피자는 뭐였지?", 1),
    ("수현이 운동하기로 한 요일과 시간", 3),
)


async def _embed_all(texts: list[str], *, prefix: str) -> np.ndarray:
    vectors: list[np.ndarray] = []
    for text in texts:
        vector = await get_embedding(text, prefix=prefix)
        if vector is None:
            raise RuntimeError(
                "로컬 임베딩 모델을 사용할 수 없어 검색 품질을 평가하지 못했습니다."
            )
        vectors.append(np.asarray(vector, dtype=np.float32))
    return np.stack(vectors)


async def run() -> int:
    baseline_documents = [
        f"{item['summary']}\n원문 맥락:\n{item['raw']}"
        for item in DOCUMENTS
    ]
    structured_documents = [
        compose_memory_text(
            item["summary"],
            item["raw"],
            limit=1_200,
            keywords=item["keywords"],
            speaker_names=item["speakers"],
            memory_type=item["memory_type"],
            timestamp_iso=item["timestamp"],
        )
        for item in DOCUMENTS
    ]
    baseline_vectors = await _embed_all(
        baseline_documents,
        prefix="passage: ",
    )
    structured_vectors = await _embed_all(
        structured_documents,
        prefix="passage: ",
    )

    rows: list[dict[str, Any]] = []
    for query, expected_index in QUERIES:
        query_vector = (
            await _embed_all([query], prefix="query: ")
        )[0]
        baseline_scores = baseline_vectors @ query_vector
        structured_scores = structured_vectors @ query_vector
        baseline_order = list(np.argsort(-baseline_scores))
        structured_order = list(np.argsort(-structured_scores))
        baseline_other = np.delete(baseline_scores, expected_index)
        structured_other = np.delete(structured_scores, expected_index)
        rows.append(
            {
                "query": query,
                "baseline_rank": baseline_order.index(expected_index) + 1,
                "structured_rank": structured_order.index(expected_index) + 1,
                "baseline_score": round(
                    float(baseline_scores[expected_index]),
                    4,
                ),
                "structured_score": round(
                    float(structured_scores[expected_index]),
                    4,
                ),
                "baseline_margin": round(
                    float(
                        baseline_scores[expected_index]
                        - np.max(baseline_other)
                    ),
                    4,
                ),
                "structured_margin": round(
                    float(
                        structured_scores[expected_index]
                        - np.max(structured_other)
                    ),
                    4,
                ),
            }
        )

    for row in rows:
        print(json.dumps(row, ensure_ascii=False))
    baseline_top1 = sum(row["baseline_rank"] == 1 for row in rows)
    structured_top1 = sum(row["structured_rank"] == 1 for row in rows)
    baseline_mean_margin = round(
        sum(float(row["baseline_margin"]) for row in rows) / len(rows),
        4,
    )
    structured_mean_margin = round(
        sum(float(row["structured_margin"]) for row in rows) / len(rows),
        4,
    )
    summary = {
        "event": "memory_retrieval_summary",
        "cases": len(rows),
        "baseline_top1": baseline_top1,
        "structured_top1": structured_top1,
        "baseline_mean_margin": baseline_mean_margin,
        "structured_mean_margin": structured_mean_margin,
    }
    print(json.dumps(summary, ensure_ascii=False))
    # 구조화 문서가 기존 형식보다 top-1 recall을 악화시키면 실패로 취급한다.
    return 0 if structured_top1 >= baseline_top1 else 1


def main() -> int:
    try:
        return asyncio.run(run())
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "memory_retrieval_error",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

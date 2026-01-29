# -*- coding: utf-8 -*-
"""
Scenario Verification Script

Tests specific user scenarios:
1. "우리가 부산여행 간게 언제더라??"
2. "동준이 취향이 뭐더라?"
3. "순천대 간 친구는 누구더라?"
"""

import sys
import os
import json
import numpy as np
import torch
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity

# Load Config
DATA_PATH = "data/kakao_store/room1"
VECTORS_PATH = os.path.join(DATA_PATH, "vectors.npy")
METADATA_PATH = os.path.join(DATA_PATH, "metadata.json")
MODEL_NAME = "dragonkue/multilingual-e5-small-ko-v2"

def main():
    print(f"🔎 [시나리오 검증] 데이터 로드 중... ({DATA_PATH})")
    
    if not os.path.exists(VECTORS_PATH):
        print("❌ 데이터 없음.")
        return

    vectors = np.load(VECTORS_PATH)
    with open(METADATA_PATH, 'r', encoding='utf-8') as f:
        metadata = json.load(f)

    print(f"✅ 데이터 로드: {len(metadata)}개")

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model = SentenceTransformer(MODEL_NAME, device=device)

    queries = [
        "우리가 부산여행 간게 언제더라??",
        "동준이 취향이 뭐더라?",
        "순천대 간 친구는 누구더라?"
    ]

    for q in queries:
        print(f"\n🧪 Query: '{q}'")
        q_vec = model.encode([f"query: {q}"], normalize_embeddings=True)
        sims = cosine_similarity(q_vec, vectors)[0]
        top_k = np.argsort(sims)[::-1][:3]
        
        for rank, idx in enumerate(top_k, 1):
            item = metadata[idx]
            original_text = item.get('text', '')
            score = sims[idx]
            
            # Extract date if available (usually in the start_date field or regex from text)
            date_info = item.get('start_date', 'Unknown Date')

            print(f"  Rank {rank} (Score: {score:.4f}) [{date_info}]")
            
            # Print snippet
            snippet = original_text.replace('\n', ' ')[:200]
            print(f"    {snippet}...")

if __name__ == "__main__":
    main()

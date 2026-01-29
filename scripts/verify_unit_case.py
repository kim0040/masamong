# -*- coding: utf-8 -*-
"""
Specific Case Verification (Hyunmin's Unit)

Tests if 'Olympic' (올림픽) or 'Unit' (부대) retrieval works in the new Kakao V2.1 data.
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
    print(f"🔎 [정밀 검증] 데이터 로드 중... ({DATA_PATH})")
    
    if not os.path.exists(VECTORS_PATH):
        print("❌ 데이터 없음.")
        return

    vectors = np.load(VECTORS_PATH)
    with open(METADATA_PATH, 'r', encoding='utf-8') as f:
        metadata = json.load(f)

    print(f"✅ 데이터 로드: {len(metadata)}개")

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model = SentenceTransformer(MODEL_NAME, device=device)

    # Queries related to the user's complaint
    queries = [
        "현민이 부대",
        "김현민 부대",
        "올림픽 부대",
        "현민이 군대 어디"
    ]

    for q in queries:
        print(f"\n🧪 Query: '{q}'")
        q_vec = model.encode([f"query: {q}"], normalize_embeddings=True)
        sims = cosine_similarity(q_vec, vectors)[0]
        top_k = np.argsort(sims)[::-1][:3]
        
        found = False
        for rank, idx in enumerate(top_k, 1):
            item = metadata[idx]
            match_in_text = "올림픽" in item['text'] or "부대" in item['text']
            
            print(f"  Rank {rank} (Score: {sims[idx]:.4f}) {'[MATCH]' if match_in_text else ''}")
            
            # Print snippet
            snippet = item['text'].replace('\n', ' ')[:150]
            print(f"    {snippet}...")
            
            if match_in_text: 
                found = True

    # Check the rank of actual "Olympic" chunks for the query "현민이 부대"
    print("\n🔎 [순위 분석] '현민이 부대' 검색 시 '올림픽' 포함 청크의 순위는?")
    
    # 1. Identify Target Chunk IDs
    target_indices = []
    for idx, item in enumerate(metadata):
        if "올림픽" in item['text'] and "부대" in item['text']:
            target_indices.append(idx)
            
    print(f"  🎯 '올림픽+부대' 포함 청크 개수: {len(target_indices)}개")
    
    # 2. Check Rank
    q = "현민이 부대"
    q_vec = model.encode([f"query: {q}"], normalize_embeddings=True)
    sims = cosine_similarity(q_vec, vectors)[0]
    
    # Sort all indices by score descending
    sorted_indices = np.argsort(sims)[::-1]
    
    for rank, idx in enumerate(sorted_indices, 1):
        if idx in target_indices:
            score = sims[idx]
            text = metadata[idx]['text'].replace('\n', ' ')[:100]
            print(f"  🏅 Rank {rank} (Score: {score:.4f}): {text}...")
            if rank > 20:
                break # Show only top relevant matches or first few deep ones

if __name__ == "__main__":
    main()

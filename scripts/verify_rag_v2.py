# -*- coding: utf-8 -*-
"""
RAG V2.1 Verification Script (Precision Test)

This script loads the new embeddings and performs a cosine similarity search
to verify that we can retrieve specific chunks ("Dialog Details") instead of just summaries.
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
    print(f"🔎 [RAG V2.1 검증] 데이터 로드 중... ({DATA_PATH})")
    
    # 1. Load Data
    if not os.path.exists(VECTORS_PATH) or not os.path.exists(METADATA_PATH):
        print(f"❌ 데이터 파일이 없습니다. ({VECTORS_PATH})")
        return

    vectors = np.load(VECTORS_PATH)
    with open(METADATA_PATH, 'r', encoding='utf-8') as f:
        metadata = json.load(f)
        
    print(f"✅ 벡터 로드 완료: {vectors.shape}")
    print(f"✅ 메타데이터 로드 완료: {len(metadata)}개")

    # 2. Load Model
    print(f"⏳ 모델 로드 중... ({MODEL_NAME})")
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model = SentenceTransformer(MODEL_NAME, device=device)
    print("✅ 모델 로드 완료")

    # 3. Test Queries
    # Case: "병원옥상" -> This specific keyword was in the original text (chunk) but might be missed by generic summary
    # Check if we retrieve the chunk containing "병원옥상"
    queries = [
        "병원옥상", 
        "운전면허"
    ]

    for q in queries:
        print(f"\n🧪 [검색 테스트] 검색어: '{q}'")
        
        # Embed Query (E5 expects 'query: ' prefix)
        q_vec = model.encode([f"query: {q}"], normalize_embeddings=True)
        
        # Similarity Search
        sims = cosine_similarity(q_vec, vectors)[0]
        
        # Get Top 3
        top_k_indices = np.argsort(sims)[::-1][:3]
        
        found_target = False
        for rank, idx in enumerate(top_k_indices, 1):
            item = metadata[idx]
            score = sims[idx]
            
            # Extract content showing it's a chunk
            content = item.get('text', '')
            summary_part = item.get('summary', '')
            original_part = item.get('original_text', '') # V2 metadata might have this? No, V2.1 metadata has 'text' formatted.
            
            # In V2.1, 'text' field is "[Summary] ... [Detail] ..."
            # Let's peek at the structure
            
            print(f"  🥈 Rank {rank} (Score: {score:.4f})")
            if q in content:
                print(f"     ✅ 정답 키워드 '{q}' 포함됨!")
                found_target = True
            else:
                print(f"     ❌ 정답 키워드 미포함")
                
            # Show a snippet
            snippet = content.replace('\n', ' ')[:100]
            print(f"     📄 내용: {snippet}...")
            
        if found_target:
            print(f"  🎉 '{q}' 검색 성공! (상세 대화 내용에서 찾아냄)")
        else:
            print(f"  ⚠️ '{q}' 검색 실패.")

if __name__ == "__main__":
    main()

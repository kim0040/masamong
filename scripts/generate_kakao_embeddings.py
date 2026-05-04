# -*- coding: utf-8 -*-
"""
카카오톡 대화 내용(.csv)을 로컬에서 미리 임베딩하여 서버에 업로드하기 위한 스크립트입니다.
서버 부하를 줄이기 위해 로컬 PC(고성능)에서 실행한 후, 생성된 결과물만 서버로 옮기세요.

[사용 방법]
1. CSV 파일 준비
   - 컬럼: date, user, message (또는 sender, content 등 유연하게 처리함)
   - 위치: data/kakao_raw/kakao_chat.csv (기본값)

2. 스크립트 실행
   python scripts/generate_kakao_embeddings.py

3. 생성된 파일(data/kakao_store/)을 서버의 동일한 경로로 업로드
"""

import sys
import os
import glob
import json
import logging
import argparse
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any

import pandas as pd
import numpy as np
from sentence_transformers import SentenceTransformer
from tqdm import tqdm

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("KakaoEmbedder")

# 설정값 (config.py 의존성 제거를 위해 직접 정의)
DEFAULT_MODEL_NAME = "dragonkue/multilingual-e5-small-ko-v2"
CHUNK_SIZE = 12  # config.py: CONVERSATION_WINDOW_SIZE
CHUNK_STRIDE = 6 # config.py: CONVERSATION_WINDOW_STRIDE
MAX_MESSAGES_PER_CHUNK = 15 # 청킹 시 최대 메시지 수 (약 15개)
TIME_WINDOW_MINUTES = 10    # 대화 끊김 판별 기준

def load_csv_flexible(path: str) -> pd.DataFrame:
    """다양한 형식의 카카오톡 CSV를 읽어 표준 컬럼(date, user, message)으로 변환합니다."""
    try:
        df = pd.read_csv(path)
        
        # 컬럼 정규화
        col_map = {}
        for col in df.columns:
            l_col = col.lower().strip()
            if l_col in ['date', 'time', 'timestamp', '날짜', '시간']:
                col_map[col] = 'date'
            elif l_col in ['user', 'sender', 'author', 'name', '보낸이', '사람']:
                col_map[col] = 'user'
            elif l_col in ['message', 'content', 'text', 'msg', '내용', '메시지']:
                col_map[col] = 'message'
                
        df = df.rename(columns=col_map)
        
        # 필수 컬럼 확인
        required = ['date', 'user', 'message']
        if not all(c in df.columns for c in required):
            logger.error(f"CSV 파일에 필수 컬럼이 없습니다. (발견된 컬럼: {df.columns.tolist()})")
            return pd.DataFrame()
            
        # 날짜 정렬
        try:
            df['date'] = pd.to_datetime(df['date'])
            df = df.sort_values('date')
        except Exception:
            pass # 날짜 파싱 실패해도 순서대로 처리
            
        return df[['date', 'user', 'message']].fillna('')
        
    except Exception as e:
        logger.error(f"CSV 로드 실패: {e}")
        return pd.DataFrame()

def chunk_conversations(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """시간 간격과 최대 메시지 수 기준으로 대화를 청크로 분할합니다."""
    chunks = []
    current_chunk_msgs = []
    last_time = None
    
    logger.info("대화 내용 청킹 중...")
    
    for idx, row in tqdm(df.iterrows(), total=len(df)):
        msg_date = row['date']
        
        # 1. 시간 차이에 따른 분할
        is_time_split = False
        if isinstance(msg_date, (datetime, pd.Timestamp)) and isinstance(last_time, (datetime, pd.Timestamp)):
            diff = (msg_date - last_time).total_seconds() / 60
            if diff > TIME_WINDOW_MINUTES:
                is_time_split = True
        
        # 2. 청크 크기 제한에 따른 분할
        if len(current_chunk_msgs) >= MAX_MESSAGES_PER_CHUNK or is_time_split:
            if current_chunk_msgs:
                chunks.append(format_chunk(current_chunk_msgs))
                current_chunk_msgs = []
        
        current_chunk_msgs.append({
            'user': str(row['user']),
            'message': str(row['message']),
            'date': str(row['date'])
        })
        last_time = msg_date
        
    # 남은 내용 처리
    if current_chunk_msgs:
        chunks.append(format_chunk(current_chunk_msgs))
        
    logger.info(f"총 {len(chunks)}개의 대화 청크가 생성되었습니다.")
    return chunks

def format_chunk(messages: List[Dict[str, str]]) -> Dict[str, Any]:
    """메시지 리스트를 병합하여 텍스트 블록으로 변환합니다."""
    
    # 1. 텍스트 병합 (같은 화자끼리 묶기)
    lines = []
    start_time = messages[0]['date']
    lines.append(f"[대화 시간: {start_time}]")
    
    prev_user = None
    current_block = []
    
    for msg in messages:
        user = msg['user']
        text = msg['message']
        
        if user == prev_user:
            current_block.append(text)
        else:
            if prev_user:
                merged_line = f"{prev_user}: {' '.join(current_block)}"
                lines.append(merged_line)
            prev_user = user
            current_block = [text]
            
    if prev_user:
        merged_line = f"{prev_user}: {' '.join(current_block)}"
        lines.append(merged_line)
        
    combined_text = "\n".join(lines)
    
    # E5 모델용 Prefix
    embedding_text = f"passage: {combined_text}"
    
    return {
        "text": combined_text,
        "embedding_text": embedding_text,
        "start_date": str(start_time),
        "message_count": len(messages)
    }

def main():
    """카카오톡 CSV → 청킹 → 임베딩 → 저장 전 과정을 실행합니다."""
    parser = argparse.ArgumentParser(description="KakaoTalk Offline Embedding Generator")
    parser.add_argument("--input", "-i", type=str, default="data/kakao_raw/kakao_chat.csv", help="Input CSV file path")
    parser.add_argument("--output", "-o", type=str, default="data/kakao_store", help="Output directory path")
    parser.add_argument("--model", "-m", type=str, default=DEFAULT_MODEL_NAME, help="HuggingFace model name")
    args = parser.parse_args()
    
    input_path = Path(args.input)
    output_dir = Path(args.output)
    
    # 1. 파일 확인
    if not input_path.exists():
        logger.error(f"입력 파일을 찾을 수 없습니다: {input_path}")
        logger.info("팁: data/kakao_raw 폴더를 만들고 kakao_chat.csv 파일을 넣어주세요.")
        return
        
    # 2. 출력 디렉토리 생성
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # 3. 데이터 로드
    logger.info(f"파일 로드 중: {input_path}")
    df = load_csv_flexible(str(input_path))
    if df.empty:
        return
    logger.info(f"총 {len(df)}개의 메시지를 읽었습니다.")
    
    # 4. 청킹
    chunks = chunk_conversations(df)
    if not chunks:
        logger.warning("생성된 청크가 없습니다.")
        return
        
    # 5. 모델 로드
    logger.info(f"임베딩 모델 로드 중 ({args.model})...")
    try:
        model = SentenceTransformer(args.model)
    except Exception as e:
        logger.error(f"모델 로드 실패: {e}")
        return
        
    # 6. 임베딩 생성
    batch_size = 32
    vectors = []
    
    logger.info("임베딩 생성 시작...")
    chunk_texts = [c['embedding_text'] for c in chunks]
    
    for i in tqdm(range(0, len(chunk_texts), batch_size)):
        batch = chunk_texts[i:i+batch_size]
        # normalize_embeddings=True for cosine similarity
        emb = model.encode(batch, normalize_embeddings=True)
        vectors.extend(emb)
        
    # 7. 저장
    vectors_np = np.array(vectors, dtype=np.float32)
    
    # 메타데이터에는 text와 기타 정보만 저장 (유사도 검색 후 원본 텍스트 표시용)
    metadata = []
    for i, c in enumerate(chunks):
        metadata.append({
            "id": i,
            "text": c['text'],
            "start_date": c['start_date'],
            "message_count": c['message_count']
        })
        
    np.save(output_dir / "vectors.npy", vectors_np)
    with open(output_dir / "metadata.json", "w", encoding='utf-8') as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)
        
    logger.info("✅ 작업 완료!")
    logger.info(f"📂 저장 위치: {output_dir.absolute()}")
    logger.info(f"   - vectors.npy (Shape: {vectors_np.shape})")
    logger.info(f"   - metadata.json ({len(metadata)} items)")
    logger.info("\n📢 이 'kakao_store' 폴더 전체를 서버의 동일한 위치로 업로드하세요.")

if __name__ == "__main__":
    main()

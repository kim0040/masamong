# -*- coding: utf-8 -*-
"""
KakaoTalk Embedding Generator V2 (Session Summary Edition)

This script upgrades the embedding generation process by:
1. Grouping messages into 'Sessions' based on a 1-hour silence gap.
2. Summarizing each session using DeepSeek (via CometAPI) to extract key points and context.
3. Embedding the 'Summary' instead of raw text for better semantic retrieval.
4. Saving the original text in metadata for full context display.

Usage:
    python scripts/generate_kakao_embeddings_v2.py
"""

import os
import sys
import json
import asyncio
import argparse
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Any

import pandas as pd
import numpy as np
from tqdm.asyncio import tqdm
from sentence_transformers import SentenceTransformer
from openai import AsyncOpenAI

# Add project root to path for importing config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import config
except ImportError:
    config = type('Config', (), {})

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("KakaoEmbedderV2")

# Constants
DEFAULT_MODEL_NAME = "dragonkue/multilingual-e5-small-ko-v2"
SESSION_GAP_MINUTES = 60  # 1 hour
SUMMARIZATION_MODEL = "DeepSeek-V3.2-Exp-nothinking"
MAX_TOKENS_PER_CHUNK = 10000 

# Pricing (User Provided)
PRICE_INPUT_PER_M = 0.27
PRICE_OUTPUT_PER_M = 0.432

class KakaoSessionEmbedder:
    def __init__(self, model_name: str, api_key: str, base_url: str):
        self.model_name = model_name
        self.embedding_model = None
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    def load_model(self):
        """Loads the SentenceTransformer model."""
        logger.info(f"Loading embedding model: {self.model_name}...")
        self.embedding_model = SentenceTransformer(self.model_name)

    def load_csv(self, path: str) -> pd.DataFrame:
        """Loads and normalizes the KakaoTalk CSV."""
        logger.info(f"Loading CSV: {path}")
        try:
            df = pd.read_csv(path)
            
            # Normalize columns
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
            
            if not all(k in df.columns for k in ['date', 'user', 'message']):
                logger.error(f"Missing required columns. Found: {df.columns.tolist()}")
                return pd.DataFrame()
            
            df['date'] = pd.to_datetime(df['date'])
            df = df.sort_values('date')
            return df[['date', 'user', 'message']].fillna('')
            
        except Exception as e:
            logger.error(f"Failed to load CSV: {e}")
            return pd.DataFrame()

    def group_into_sessions(self, df: pd.DataFrame) -> List[Dict[str, Any]]:
        """Groups messages into sessions based on time gaps."""
        logger.info("Grouping messages into sessions...")
        sessions = []
        current_session_msgs = []
        last_time = None
        
        for idx, row in df.iterrows():
            msg_date = row['date']
            
            is_new_session = False
            if last_time:
                diff = (msg_date - last_time).total_seconds() / 60
                if diff > SESSION_GAP_MINUTES:
                    is_new_session = True
            
            if is_new_session and current_session_msgs:
                sessions.append(self._format_session(current_session_msgs))
                current_session_msgs = []
            
            current_session_msgs.append({
                'user': str(row['user']),
                'message': str(row['message']),
                'date': msg_date
            })
            last_time = msg_date
            
        if current_session_msgs:
            sessions.append(self._format_session(current_session_msgs))
            
        logger.info(f"Found {len(sessions)} distinct conversation sessions.")
        return sessions

    def _format_session(self, messages: List[Dict]) -> Dict[str, Any]:
        """Formats a list of messages into a single text block."""
        lines = []
        start_time = messages[0]['date']
        end_time = messages[-1]['date']
        
        merged = []
        prev_user = None
        current_block = []
        
        for msg in messages:
            user = msg['user']
            text = msg['message']
            if user == prev_user:
                current_block.append(text)
            else:
                if prev_user:
                    merged.append(f"{prev_user}: {' '.join(current_block)}")
                prev_user = user
                current_block = [text]
        if prev_user:
            merged.append(f"{prev_user}: {' '.join(current_block)}")
            
        full_text = "\n".join(merged)
        
        return {
            'start_date': start_time,
            'end_date': end_time,
            'full_text': full_text,
            'message_count': len(messages)
        }

    async def summarize_session(self, session_text: str, semaphore: asyncio.Semaphore) -> str:
        """Uses DeepSeek to summarize the session with key details, respecting rate limits."""
        async with semaphore:
            # Rate limiting: 1 request per second
            await asyncio.sleep(1.0) 

            # [Cost Optimization] 텍스트가 너무 짧으면(200자 미만) 요약하지 않고 원문 그대로 사용
            # 인사말이나 짧은 문답에 API를 태우는 것은 낭비임.
            if len(session_text) < 200:
                logger.info("Skipping summarization for short session (<200 chars).")
                return session_text

            # Truncate if too long (strict limit)
            truncated_text = session_text
            if len(session_text) > 20000:
                truncated_text = session_text[:20000] + "\n...(내용이 너무 길어 생략됨)"

            system_prompt = """역할: 카카오톡 대화 내용 요약가 (비용 절감 모드)
목표: 검색용 핵심 정보 추출. 300자 이내, 핵심만.
주의: 인사말, 감탄사, 무의미한 반복(ㅋㅋ 등)은 완전히 제거."""

            user_prompt = f"""[입력 데이터]
{truncated_text}

[출력 형식]
요약: (대화의 주제와 결론을 2-3문장 건조체로 작성)
키워드: (날짜, 시간, 장소, URL, 고유명사, 숫자, 주식종목 등 검색에 걸릴만한 단어만 나열)"""

            try:
                response = await self.client.chat.completions.create(
                    model=SUMMARIZATION_MODEL,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    max_tokens=300  # 요약은 300토큰이면 충분, 비용 절감
                )
                return response.choices[0].message.content.strip()
            except Exception as e:
                logger.error(f"Summarization failed: {e}")
                return "요약 실패(API Error). 원문 일부: " + session_text[:200]

    async def process(self, input_path: str, output_dir: str, confirmed: bool = False):
        # 1. Load Data
        df = self.load_csv(input_path)
        if df.empty: return

        # 2. Group into Sessions
        sessions = self.group_into_sessions(df)
        total_sessions = len(sessions)
        
        # --- Cost Estimation ---
        total_chars = sum(len(s['full_text']) for s in sessions)
        # Approx tokens: English/Code ~ 4 chars/token, Korean ~ 1.5-2 chars/token. 
        est_input_tokens = total_chars / 2 
        est_output_tokens = total_sessions * 100
        
        # Pricing: Input $0.27/M, Output $0.432/M
        cost_input = (est_input_tokens / 1_000_000) * PRICE_INPUT_PER_M
        cost_output = (est_output_tokens / 1_000_000) * PRICE_OUTPUT_PER_M
        total_est_cost = cost_input + cost_output
        exchange_rate = 1450 
        
        print("\n" + "="*50)
        print(f"📊 [사전 비용/규모 분석]")
        print(f"총 세션 수     : {total_sessions:,} 개")
        print(f"총 입력 글자 수 : {total_chars:,} 자")
        print(f"예상 입력 토큰  : 약 {int(est_input_tokens):,} tokens")
        print(f"예상 출력 토큰  : 약 {int(est_output_tokens):,} tokens")
        print("-" * 30)
        print(f"💰 예상 비용    : ${total_est_cost:.4f} (약 {int(total_est_cost * exchange_rate)}원)")
        print(f"* 사용 모델: {SUMMARIZATION_MODEL}")
        print(f"* 기준 요금: Input ${PRICE_INPUT_PER_M}/M, Output ${PRICE_OUTPUT_PER_M}/M")
        print("="*50 + "\n")

        if not confirmed:
            user_input = input("💡 위 예상 비용으로 작업을 진행하시겠습니까? (y/n): ")
            if user_input.lower() not in ['y', 'yes']:
                print("작업을 취소합니다.")
                return

        # 3. Summarize Sessions (Async with Semaphore)
        # 진정한 초당 1회 제한: Semaphore=1, sleep=1.0
        semaphore = asyncio.Semaphore(1)
        
        logger.info(f"Summarizing sessions using {SUMMARIZATION_MODEL}...")
        summarized_sessions = []
        
        # Progress bar
        pbar = tqdm(total=total_sessions, desc="Processing Sessions")
        
        tasks = [self.summarize_session(s['full_text'], semaphore) for s in sessions]
        summaries = await asyncio.gather(*tasks)
        
        for session, summary in zip(sessions, summaries):
            embedding_text = f"passage: {summary}"
            summarized_sessions.append({
                'id': 0, # Placeholder
                'summary': summary,
                'embedding_text': embedding_text,
                'original_text': session['full_text'],
                'start_date': str(session['start_date']),
                'end_date': str(session['end_date']),
                'message_count': session['message_count']
            })
            pbar.update(1)
            
        pbar.close()

        # 4. Generate Embeddings
        self.load_model()
        logger.info("Generating embeddings...")
        
        texts_to_embed = [s['embedding_text'] for s in summarized_sessions]
        embeddings = self.embedding_model.encode(texts_to_embed, normalize_embeddings=True, show_progress_bar=True)
        
        # 5. Save Results
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        
        np_embeddings = np.array(embeddings, dtype=np.float32)
        np.save(output_path / "vectors.npy", np_embeddings)
        
        metadata = []
        for i, s in enumerate(summarized_sessions):
            metadata.append({
                "id": i,
                "text": f"[대화 일시: {s['start_date']}]\n\n📌 {s['summary']}\n\n---\n[상세 내용]\n{s['original_text']}",
                "summary": s['summary'],
                "start_date": s['start_date'],
                "message_count": s['message_count']
            })
            
        with open(output_path / "metadata.json", "w", encoding='utf-8') as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
            
        logger.info(f"✅ Analysis & Embedding Complete!")
        logger.info(f"Saved {len(metadata)} items to {output_path}")

def main():
    parser = argparse.ArgumentParser(description="KakaoEmbedder V2 (Session Summary)")
    parser.add_argument("--input", "-i", type=str, default="data/kakao_raw/kakao_chat.csv")
    parser.add_argument("--output", "-o", type=str, default="data/kakao_store_v2")
    parser.add_argument("--model", "-m", type=str, default=DEFAULT_MODEL_NAME)
    parser.add_argument("--key", "-k", type=str, help="CometAPI Key (optional if in env)")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    args = parser.parse_args()
    
    # API Key Priority: Arg > Env > Config File
    api_key = args.key or os.environ.get("COMETAPI_KEY") or getattr(config, 'COMETAPI_KEY', None)
    base_url = os.environ.get("COMETAPI_BASE_URL") or getattr(config, 'COMETAPI_BASE_URL', "https://api.cometapi.com/v1")
    
    if not api_key:
        logger.error("API Key not found. Please provide via --key or set COMETAPI_KEY env var.")
        return

    embedder = KakaoSessionEmbedder(args.model, api_key, base_url)
    asyncio.run(embedder.process(args.input, args.output, args.yes))

if __name__ == "__main__":
    main()

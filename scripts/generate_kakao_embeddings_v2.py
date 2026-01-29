# -*- coding: utf-8 -*-
"""
KakaoTalk Embedding Generator V2.1 (Chunked Session Edition)

This script upgrades the embedding generation process by:
1. Grouping messages into 'Sessions' based on a 1-hour silence gap.
2. Summarizing each session using DeepSeek (via CometAPI) to extract key points and context.
3. [NEW V2.1] Chunking the session's original text into smaller pieces.
4. [NEW V2.1] Embedding each CHUNK instead of just the summary.
   - Metadata includes: [Session Summary] + [Chunk Original Text]
   - This allows retrieving specific details while maintaining high-level context.

Usage:
    python scripts/generate_kakao_embeddings_v2.py
    python scripts/generate_kakao_embeddings_v2.py --migrate-v2  (Recycle existing V2 metadata)
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

# Add project root to path for importing config/utils
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import config
    # Attempt to import SemanticChunker from utils
    from utils.chunker import SemanticChunker, ChunkerConfig
except ImportError:
    config = type('Config', (), {})
    SemanticChunker = None

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("KakaoEmbedderV2.1")

# Constants
DEFAULT_MODEL_NAME = "dragonkue/multilingual-e5-small-ko-v2"
SESSION_GAP_MINUTES = 60  # 1 hour

# Chunking Config (V2.1)
CHUNK_MAX_TOKENS = 250   # Approx 500-600 Korean chars
CHUNK_OVERLAP = 50       # Context overlap

# Summarization Model Configs
SUMMARIZATION_MODELS = {
    "1": {
        "name": "DeepSeek-V3.2-Exp-nothinking",
        "price_input": 0.27,
        "price_output": 0.432,
        "desc": "표준형 (DeepSeek V3.2)"
    },
    "2": {
        "name": "gpt-5-nano-2025-08-07",
        "price_input": 0.05,
        "price_output": 0.40,
        "desc": "절약형 (GPT-5 Nano)"
    }
}
EXCHANGE_RATE = 1470 

class KakaoSessionEmbedder:
    def __init__(self, embedding_model_name: str, api_key: str, base_url: str, summary_model_config: Dict[str, Any]):
        self.embedding_model_name = embedding_model_name
        self.summary_model_config = summary_model_config
        self.embedding_model = None
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        
        # Initialize Chunker
        if SemanticChunker:
            self.chunker = SemanticChunker(ChunkerConfig(max_tokens=CHUNK_MAX_TOKENS, overlap_tokens=CHUNK_OVERLAP))
        else:
            logger.warning("SemanticChunker not found. Using simple split.")
            self.chunker = None

    def load_model(self):
        """Loads the SentenceTransformer model."""
        logger.info(f"Loading embedding model: {self.embedding_model_name}...")
        self.embedding_model = SentenceTransformer(self.embedding_model_name)

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
        """Uses DeepSeek/GPT-5 to summarize the session with key details, respecting rate limits."""
        
        # [Speed Optimization] 짧은 세션은 굳이 API 호출도, 대기도 필요 없음 (즉시 처리)
        if len(session_text) < 200:
            return session_text

        retries = 0
        max_retries = 3
        backoff = 2.0

        async with semaphore:
            while retries < max_retries:
                # Truncate if too long (strict limit)
                truncated_text = session_text
                if len(session_text) > 20000:
                    truncated_text = session_text[:20000] + "\n...(내용이 너무 길어 생략됨)"

                system_prompt = """역할: 카카오톡 대화 내용 요약가 (비용 절감 모드)
목표: 검색용 핵심 정보 추출. 600자 이내, 핵심만.
주의: 인사말, 감탄사, 무의미한 반복(ㅋㅋ 등)은 완전히 제거."""

                user_prompt = f"""[입력 데이터]
{truncated_text}

[출력 형식]
요약: (대화의 주제와 결론을 3-4문장 건조체로 작성)
키워드: (날짜, 시간, 장소, URL, 고유명사, 숫자, 주식종목 등 검색에 걸릴만한 단어만 나열)"""

                try:
                    api_args = {
                        "model": self.summary_model_config['name'],
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt}
                        ],
                        "extra_body": {"max_tokens": 400}
                    }

                    response = await self.client.chat.completions.create(**api_args)
                    summary = response.choices[0].message.content.strip()
                
                    if not summary:
                        summary = session_text[:500].replace('\n', ' ')

                    # Preview
                    preview = summary.replace('\n', ' ')[:80]
                    tqdm.write(f"📝 {preview}...") 
                    return summary
                except Exception as e:
                    if "429" in str(e):
                        logger.warning(f"Rate limited. Sleeping {backoff}s...")
                        await asyncio.sleep(backoff)
                        backoff *= 2
                        retries += 1
                        continue
                    else:
                        logger.error(f"Summarization failed: {e}")
                        return session_text[:500].replace('\n', ' ')
            
            return session_text[:500].replace('\n', ' ')

    async def _summarize_with_progress(self, idx, session, semaphore, pbar):
        """Wrapper to update progress bar and save incremental checkpoint."""
        summary = await self.summarize_session(session['full_text'], semaphore)
        
        result = {
            'id': idx,
            'summary': summary,
            'original_text': session['full_text'],
            'start_date': str(session['start_date']),
            'end_date': str(session['end_date']),
            'message_count': session['message_count']
        }
        
        pbar.update(1)
        return result

    def chunk_session(self, session_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Splits a session into multiple chunks for embedding (V2.1 Logic)."""
        chunks = []
        original_text = session_data.get('original_text', '') or session_data.get('full_text', '')
        summary = session_data.get('summary', '') or "요약 없음"
        date_str = str(session_data.get('start_date', ''))[:10]
        
        if not original_text:
            return []

        # Use SemanticChunker if available, else simple split
        if self.chunker:
            chunk_objs = self.chunker.chunk(original_text)
            text_chunks = [c.text for c in chunk_objs]
        else:
            # Fallback: simple character split
            text_chunks = [original_text[i:i+500] for i in range(0, len(original_text), 450)]

        # If no chunks (e.g. empty text), create at least one from summary
        if not text_chunks:
            text_chunks = [summary]

        for i, chunk_text in enumerate(text_chunks):
            # V2.1 Formatting
            # Display Text: What LLM sees (Summary + Chunk)
            display_text = f"📌 [세션 요약]\n{summary}\n\n💬 [대화 상세]\n{chunk_text}"
            
            # Embedding Text: What Search Vector sees (Original Chunk Text)
            # Prefix for E5 model
            embedding_text = f"passage: [{date_str}] {chunk_text}"
            
            chunks.append({
                "session_id": session_data['id'],
                "chunk_id": i,
                "text": display_text,          # Legacy compatible field name for retrieval display
                "embedding_text": embedding_text, # Used for vector generation
                "chunk_text": chunk_text,
                "summary": summary,
                "start_date": session_data.get('start_date'),
                "message_count": session_data.get('message_count')
            })
            
        return chunks

    async def process(self, input_path: str, output_dir: str, reset: bool = False, confirmed: bool = False, migrate_v2_path: str = None):
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        
        summarized_sessions = []
        
        # --- PHASE 1: ACQUIRE SESSION DATA (Summarized) ---
        if migrate_v2_path:
            logger.info(f"🚀 [MIGRATION MODE] Loading existing V2 metadata from: {migrate_v2_path}")
            v2_meta_path = Path(migrate_v2_path) / "metadata.json"
            v2_checkpoint_path = Path(migrate_v2_path) / "checkpoint.jsonl"
            v2_checkpoint_v2_path = Path(migrate_v2_path) / "checkpoint_v2.jsonl"
            
            loaded = False
            # 1. Try metadata.json
            if v2_meta_path.exists() and v2_meta_path.stat().st_size > 100: # Check if meaningful data
                try:
                    with open(v2_meta_path, 'r', encoding='utf-8') as f:
                        summarized_sessions = json.load(f)
                    
                    
                    # Check if metadata actually has 'original_text' (needed for chunking)
                    if summarized_sessions:
                        sample = summarized_sessions[0]
                        if 'original_text' not in sample and 'text' in sample:
                            logger.info("⚠️ 'original_text' missing but 'text' found. Mapping 'text' -> 'original_text' (Legacy Format)")
                            for s in summarized_sessions:
                                s['original_text'] = s['text']
                                s['summary'] = "요약 정보 없음" # Fallback summary
                            loaded = True
                            
                        elif 'original_text' not in sample:
                            logger.warning("⚠️ metadata.json exists but lacks 'original_text' and 'text'. Ignoring it.")
                            summarized_sessions = []
                            loaded = False
                        else:
                             loaded = True
                             logger.info("✅ Loaded from metadata.json")
                except Exception:
                    logger.warning("Failed to load metadata.json")

            # 2. Fallback to checkpoint.jsonl
            if not loaded:
                target_cp = v2_checkpoint_path if v2_checkpoint_path.exists() else v2_checkpoint_v2_path
                if target_cp.exists():
                     logger.info(f"⚠️ metadata.json missing/empty. Loading from {target_cp.name}...")
                     with open(target_cp, 'r', encoding='utf-8') as f:
                        for line in f:
                            if line.strip():
                                try:
                                    summarized_sessions.append(json.loads(line))
                                except: pass
                     if summarized_sessions:
                         loaded = True
                         logger.info(f"✅ Recovered {len(summarized_sessions)} items from checkpoint!")

            if not loaded:
                logger.error("❌ V2 data not found (neither metadata.json nor checkpoint.jsonl).")
                return
            
            logger.info(f"✅ Loaded {len(summarized_sessions)} items. Skipping summarization cost!")
            
        else:
            # Standard Processing (CSV -> Summary)
            checkpoint_path = output_path / "checkpoint_v2.jsonl"
            completed_indices = set()
            
            if checkpoint_path.exists() and not reset:
                logger.info("loading checkpoint...")
                with open(checkpoint_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        if line.strip():
                            data = json.loads(line)
                            completed_indices.add(data['id'])
                            summarized_sessions.append(data)
            
            # Load Data
            df = self.load_csv(input_path)
            if df.empty: return

            sessions = self.group_into_sessions(df)
            total_sessions = len(sessions)
            remaining_tasks = [(i, s) for i, s in enumerate(sessions) if i not in completed_indices]
            
            # ... (Pricing check omitted for brevity in V2.1 script update, can rely on earlier user trust or re-add if needed)
            if remaining_tasks and not confirmed:
                 print(f"⚡ {len(remaining_tasks)}개 세션에 대해 요약을 진행합니다.")
                 if input("진행하시겠습니까? (y/n) ").lower() != 'y': return

            # Summarize
            if remaining_tasks:
                semaphore = asyncio.Semaphore(5) 
                pbar = tqdm(total=total_sessions, initial=len(completed_indices), desc="Summarizing")
                
                # Split into chunks for saving
                chunk_size = 10
                tasks_iter = iter(remaining_tasks)
                
                while True:
                    chunk = []
                    try:
                        for _ in range(chunk_size): chunk.append(next(tasks_iter))
                    except StopIteration: pass
                    
                    if not chunk: break
                        
                    chunk_tasks = [self._summarize_with_progress(idx, s, semaphore, pbar) for idx, s in chunk]
                    results = await asyncio.gather(*chunk_tasks)
                    
                    with open(checkpoint_path, 'a', encoding='utf-8') as f:
                        for res in results:
                            f.write(json.dumps(res, ensure_ascii=False) + "\n")
                    summarized_sessions.extend(results)
                pbar.close()

        # Sort by ID
        summarized_sessions.sort(key=lambda x: x['id'])
        
        # --- PHASE 2: CHUNKING (V2.1) ---
        logger.info("🔪 Chunking sessions into retrieval units...")
        all_chunks = []
        for session in summarized_sessions:
            chunks = self.chunk_session(session)
            all_chunks.extend(chunks)
            
        logger.info(f"🧩 Created {len(all_chunks)} chunks from {len(summarized_sessions)} sessions.")
        
        # --- PHASE 3: EMBEDDING ---
        self.load_model()
        logger.info("Generating embeddings for chunks...")
        
        texts_to_embed = [c['embedding_text'] for c in all_chunks]
        embeddings = self.embedding_model.encode(texts_to_embed, normalize_embeddings=True, show_progress_bar=True)
        
        # --- PHASE 4: SAVE ---
        np_embeddings = np.array(embeddings, dtype=np.float32)
        np.save(output_path / "vectors.npy", np_embeddings)
        
        # Save metadata (lite version for loading)
        final_metadata = []
        for i, chunk in enumerate(all_chunks):
            # Ensure critical fields exist
            final_metadata.append({
                "id": i, # New global ID
                "session_id": chunk['session_id'],
                "text": chunk['text'], # [Summary]\n[Chunk]
                "summary": chunk['summary'],
                "start_date": chunk.get('start_date'),
                "message_count": chunk.get('message_count', 0)
            })
            
        with open(output_path / "metadata.json", "w", encoding='utf-8') as f:
            json.dump(final_metadata, f, ensure_ascii=False, indent=2)
            
        logger.info(f"✅ V2.1 Complete! Saved {len(final_metadata)} items to {output_path}")

def main():
    parser = argparse.ArgumentParser(description="KakaoEmbedder V2.1 (Chunked)")
    parser.add_argument("--input", "-i", type=str, default="data/kakao_raw/kakao_chat.csv")
    parser.add_argument("--output", "-o", type=str, default="data/kakao_store_v2")
    parser.add_argument("--model", "-m", type=str, default=DEFAULT_MODEL_NAME)
    parser.add_argument("--key", "-k", type=str, help="CometAPI Key")
    parser.add_argument("--migrate-v2", action="store_true", help="Migrate from existing V2 data (skips summary)")
    parser.add_argument("--migrate-path", type=str, default="data/kakao_store_v2", help="Path to existing V2 data")
    
    args = parser.parse_args()
    
    api_key = args.key or os.environ.get("COMETAPI_KEY") or getattr(config, 'COMETAPI_KEY', None)
    base_url = os.environ.get("COMETAPI_BASE_URL") or getattr(config, 'COMETAPI_BASE_URL', "https://api.cometapi.com/v1")
    
    # Auto-detect migration if flag is not set but path is default and exists
    migrate_path = None
    if args.migrate_v2:
        migrate_path = args.migrate_path
    
    if not migrate_path and not api_key:
         # Check if we can just migrate
         if os.path.exists(args.migrate_path) and os.path.exists(os.path.join(args.migrate_path, "metadata.json")):
             print("💡 기존 V2 데이터를 감지했습니다. API 키 없이 마이그레이션 모드로 진행하시겠습니까?")
             if input("(y/n): ").lower() == 'y':
                 migrate_path = args.migrate_path
         if not migrate_path:
            logger.error("API Key required for new generation.")
            return

    # Use dummy key for migration to bypass client validation (client not used for summarization in migration)
    if migrate_path and not api_key:
        api_key = "dummy-key-migration"

    embedder = KakaoSessionEmbedder(args.model, api_key, base_url, SUMMARIZATION_MODELS['1'])
    
    # If migrating, output to a safe new dir usually, but here we might overwrite or use suffix
    # The user said "use this data", implies overwrite or update.
    # To be safe and compliant with user request "same name linked", we might want to backup first or just overwrite.
    # I'll implement backup inside process or just output to temp then rename?
    # Script just writes. Let's ask user or just overwrite if they said so.
    # Actually, let's output to same dir since user wants it there.
    
    asyncio.run(embedder.process(args.input, args.output, migrate_v2_path=migrate_path))

if __name__ == "__main__":
    main()

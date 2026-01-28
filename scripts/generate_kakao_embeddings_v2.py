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

    def load_model(self):
        """Loads the SentenceTransformer model."""
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
                    # [Model Compatibility]
                    # 최신 OpenAI SDK는 'gpt-5', 'o1' 등이 이름에 포함되면 max_tokens -> max_completion_tokens로 자동 변환함.
                    # 하지만 CometAPI(Relay)는 아직 max_tokens만 인식하여 400 에러 발생.
                    # 이를 방지하기 위해 max_tokens를 kwargs가 아닌 extra_body로 직접 주입하여 변환을 우회함.
                    
                    # Common args
                    api_args = {
                        "model": self.summary_model_config['name'],
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt}
                        ],
                        # "max_tokens": 400  <-- Do NOT pass this directly if model name contains 'gpt-5' or 'o1'
                        "extra_body": {"max_tokens": 400}
                    }

                    response = await self.client.chat.completions.create(**api_args)
                    summary = response.choices[0].message.content.strip()
                
                    # Preview Verification & Fallback
                    if not summary:
                        tqdm.write("⚠️ [Warning] 모델 응답이 비어있음 -> 원문 앞부분 사용")
                        summary = session_text[:500].replace('\n', ' ')

                    # Preview: 한 줄로 공백 제거해서 깔끔하게 출력
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
                        # Fallback for API Error
                        return session_text[:500].replace('\n', ' ')
            
            return session_text[:500].replace('\n', ' ')

    async def _summarize_with_progress(self, idx, session, semaphore, pbar, output_dir):
        """Wrapper to update progress bar and save incremental checkpoint."""
        # Note: summarize_session no longer takes date, we attach it here for embedding
        summary = await self.summarize_session(session['full_text'], semaphore)
        
        # [Date Injection] 임베딩 텍스트에 날짜를 명시적으로 포함
        date_str = str(session['start_date'])[:10] # YYYY-MM-DD only
        embedding_text = f"passage: [{date_str}] {summary}"
        
        result = {
            'id': idx,
            'summary': summary,
            'embedding_text': embedding_text,
            'original_text': session['full_text'],
            'start_date': str(session['start_date']),
            'end_date': str(session['end_date']),
            'message_count': session['message_count']
        }
        
        pbar.update(1)
        return result

    async def process(self, input_path: str, output_dir: str, reset: bool = False, confirmed: bool = False):
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        checkpoint_path = output_path / "checkpoint.jsonl"
        
        # 1. Load Checkpoint (unless reset is requested)
        completed_indices = set()
        completed_results = []
        
        if checkpoint_path.exists():
            if reset:
                print(f"🗑️ [초기화] 기존 체크포인트를 무시하고 처음부터 시작합니다: {checkpoint_path}")
                # We don't delete the file immediately to be safe, just don't load it.
                # However, we should properly clear it if we start writing.
                with open(checkpoint_path, 'w') as f: # Clear file
                    pass
            else:
                print(f"\n📂 [체크포인트 발견] {checkpoint_path}")
                try:
                    with open(checkpoint_path, 'r', encoding='utf-8') as f:
                        for line in f:
                            if line.strip():
                                data = json.loads(line)
                                completed_indices.add(data['id'])
                                completed_results.append(data)
                    print(f"✅ {len(completed_indices)}개 세션은 이미 처리됨. 이어서 작업을 시작합니다.")
                    
                    if not confirmed:
                        q = input("🔄 기존 작업을 이어하시겠습니까? (y=이어하기 / n=처음부터 다시): ").strip().lower()
                        if q in ['n', 'no', 'new']:
                            print("🗑️ 작업을 처음부터 다시 시작합니다.")
                            completed_indices = set()
                            completed_results = []
                            with open(checkpoint_path, 'w') as f: pass # Clear
                except Exception as e:
                    logger.error(f"체크포인트 로드 실패: {e}")

        # 2. Load Data
        df = self.load_csv(input_path)
        if df.empty: return

        # 3. Group into Sessions
        sessions = self.group_into_sessions(df)
        total_sessions = len(sessions)
        
        # Filter already processed sessions
        remaining_tasks = []
        for i, s in enumerate(sessions):
            if i not in completed_indices:
                remaining_tasks.append((i, s))

        # Pricing
        total_remaining_sessions = len(remaining_tasks)
        if total_remaining_sessions == 0:
            print("🎉 모든 작업이 완료되어 있습니다. 임베딩 생성 단계로 넘어갑니다.")
        else:
            total_chars = sum(len(s['full_text']) for i, s in remaining_tasks)
            est_input_tokens = total_chars / 2 
            est_output_tokens = total_remaining_sessions * 200 

            model_name = self.summary_model_config['name']
            p_in = self.summary_model_config['price_input']
            p_out = self.summary_model_config['price_output']
            
            cost_input = (est_input_tokens / 1_000_000) * p_in
            cost_output = (est_output_tokens / 1_000_000) * p_out
            total_est_cost = cost_input + cost_output
            
            print("\n" + "="*50)
            print(f"📊 [남은 작업 비용/규모 분석]")
            print(f"남은 세션 수     : {total_remaining_sessions:,} 개 (총 {total_sessions:,} 개)")
            print(f"총 입력 글자 수 : {total_chars:,} 자")
            print(f"예상 입력 토큰  : 약 {int(est_input_tokens):,} tokens")
            print(f"예상 출력 토큰  : 약 {int(est_output_tokens):,} tokens")
            print("-" * 30)
            print(f"💰 예상 비용    : ${total_est_cost:.4f} (약 {int(total_est_cost * EXCHANGE_RATE)}원)")
            print(f"* 사용 모델: {model_name}")
            print("="*50 + "\n")

            if not confirmed:
                user_input = input("💡 위 예상 비용으로 작업을 진행하시겠습니까? (y/n): ")
                if user_input.lower() not in ['y', 'yes']:
                    print("작업을 취소합니다.")
                    return

        # 4. Summarize Sessions (Async with Semaphore)
        # Concurrency Increased: 5
        semaphore = asyncio.Semaphore(5) 
        
        logger.info(f"Summarizing sessions using {self.summary_model_config['name']} (Concurrency=5)...")
        
        pbar = tqdm(total=total_sessions, initial=len(completed_indices), desc="Processing Sessions")
        
        # Run remaining tasks
        # We need to save incrementally
        chunk_size = 10  # Save every 10 items
        
        tasks_iter = iter(remaining_tasks)
        
        while True:
            chunk = []
            try:
                for _ in range(chunk_size):
                    chunk.append(next(tasks_iter))
            except StopIteration:
                pass
            
            if not chunk:
                break
                
            chunk_tasks = [self._summarize_with_progress(idx, s, semaphore, pbar, output_dir) for idx, s in chunk]
            results = await asyncio.gather(*chunk_tasks)
            
            # Save Checkpoint immediately
            with open(checkpoint_path, 'a', encoding='utf-8') as f:
                for res in results:
                    f.write(json.dumps(res, ensure_ascii=False) + "\n")
            
            completed_results.extend(results)

        pbar.close()
        
        # Sort results by ID to restore order
        completed_results.sort(key=lambda x: x['id'])
        summarized_sessions = completed_results

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
    parser.add_argument("--sum-model", type=str, help="Summarization Model Key (1 or 2)")
    parser.add_argument("--reset", action="store_true", help="Ignore checkpoint and restart")
    args = parser.parse_args()
    
    # --- Interactive Setup ---
    # 0. Summarization Model Selection
    selected_model_config = None
    if args.sum_model and args.sum_model in SUMMARIZATION_MODELS:
        selected_model_config = SUMMARIZATION_MODELS[args.sum_model]
    else:
        print("\n🤖 [요약 모델 선택]")
        for key, conf in SUMMARIZATION_MODELS.items():
            print(f"  {key}. {conf['name']} ({conf['desc']})")
            print(f"     └─ Input ${conf['price_input']}/M, Output ${conf['price_output']}/M")
        
        while True:
            choice = input(f"👉 모델 번호를 선택하세요 (기본값 1): ").strip()
            if not choice:
                choice = "1"
            if choice in SUMMARIZATION_MODELS:
                selected_model_config = SUMMARIZATION_MODELS[choice]
                break
            print("❌ 올바른 번호를 선택해주세요.")
    
    # 1. API Key Setup
    api_key = args.key or os.environ.get("COMETAPI_KEY") or getattr(config, 'COMETAPI_KEY', None)
    if not api_key:
        print("\n🔑 [API 키 설정]")
        print("CometAPI Key가 환경 변수나 설정 파일에 없습니다.")
        api_key = input("👉 API Key를 입력해주세요 (입력 내용은 숨겨지지 않습니다): ").strip()
        if not api_key:
            logger.error("API Key가 입력되지 않아 종료합니다.")
            return

    # 2. Input File Selection
    input_path = args.input
    # 기본값이고 실제 파일이 없다면, 혹은 사용자가 선택하고 싶을 수 있으므로 목록 보여주기 루틴
    # (단, args로 명시적으로 경로를 줬다면 그것을 우선)
    if input_path == "data/kakao_raw/kakao_chat.csv" and not os.path.exists(input_path):
        # Scan directory
        raw_dir = Path("data/kakao_raw")
        csv_files = list(raw_dir.glob("*.csv")) if raw_dir.exists() else []
        
        if not csv_files:
            print(f"\n📂 [파일 선택] '{raw_dir}' 경로에 CSV 파일이 없습니다.")
            input_path = input("👉 분석할 카카오톡 CSV 파일의 전체 경로를 입력하세요: ").strip()
        else:
            print(f"\n📂 [파일 선택] '{raw_dir}' 경로에서 파일을 발견했습니다:")
            for idx, f in enumerate(csv_files, 1):
                print(f"  {idx}. {f.name}")
            print("  0. 직접 경로 입력")
            
            while True:
                try:
                    choice = input(f"👉 작업할 파일 번호를 선택하세요 (1~{len(csv_files)}, 0=직접입력): ")
                    idx = int(choice)
                    if idx == 0:
                        input_path = input("👉 파일 경로 입력: ").strip()
                        break
                    if 1 <= idx <= len(csv_files):
                        input_path = str(csv_files[idx-1])
                        break
                except ValueError:
                    pass
                print("❌ 올바른 번호를 입력해주세요.")

    if not os.path.exists(input_path):
        logger.error(f"파일을 찾을 수 없습니다: {input_path}")
        return

    print(f"\n✅ 선택된 파일: {input_path}")
    print(f"✅ 사용 API Key: {api_key[:8]}..." if api_key else "✅ API Key 확인됨")
    base_url = os.environ.get("COMETAPI_BASE_URL") or getattr(config, 'COMETAPI_BASE_URL', "https://api.cometapi.com/v1")
    
    if not api_key:
        logger.error("API Key not found. Please provide via --key or set COMETAPI_KEY env var.")
        return

    embedder = KakaoSessionEmbedder(args.model, api_key, base_url, selected_model_config)
    asyncio.run(embedder.process(input_path, args.output, args.reset, args.yes))

if __name__ == "__main__":
    main()

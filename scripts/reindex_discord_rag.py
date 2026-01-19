# -*- coding: utf-8 -*-
"""
Discord 대화 기록을 기반으로 RAG용 임베딩을 재생성하는 스크립트입니다.
'emb/' 폴더의 Chunking 로직과 E5 모델 요구사항(prefix)을 반영합니다.

실행 방법:
    python -m scripts.reindex_discord_rag
"""

import sys
import os
import asyncio
import logging
from datetime import datetime, timedelta
import json
import shutil

# 프로젝트 루트 경로 추가
sys.path.append(os.getcwd())

import config
from utils.embeddings import DiscordEmbeddingStore, get_embedding
import aiosqlite

# 로깅 설정
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ReindexRAG")

# 청킹 설정 (emb/embedding_chunked.py와 동일하게 설정 가능)
TIME_WINDOW_MINUTES = 10
MAX_MESSAGES_PER_CHUNK = 15

def parse_iso(iso_str):
    try:
        return datetime.fromisoformat(iso_str)
    except:
        return datetime.now()

def format_chunk(messages):
    """메시지 리스트를 텍스트 블록으로 포맷팅"""
    if not messages:
        return ""
    
    lines = []
    # 시작 시간
    start_time = messages[0]['created_at']
    lines.append(f"[대화 시간: {start_time}]")
    
    prev_user = None
    current_block = []
    
    for msg in messages:
        user = msg['user_name']
        content = msg['content']
        
        if user == prev_user:
            current_block.append(content)
        else:
            if prev_user:
                merged_content = " ".join(current_block)
                lines.append(f"{prev_user}: {merged_content}")
            prev_user = user
            current_block = [content]
            
    if prev_user:
        merged_content = " ".join(current_block)
        lines.append(f"{prev_user}: {merged_content}")
        
    return "\n".join(lines)

def chunk_messages(messages):
    """
    메시지 리스트를 시간 및 개수 기준으로 청킹합니다.
    (emb/embedding_chunked.py 로직 포팅)
    """
    chunks = []
    current_chunk = []
    last_time = None
    
    for msg in messages:
        current_time = parse_iso(msg['created_at'])
        
        should_split = False
        
        if last_time:
            diff = (current_time - last_time).total_seconds() / 60.0
            if diff > TIME_WINDOW_MINUTES:
                should_split = True
        
        if len(current_chunk) >= MAX_MESSAGES_PER_CHUNK:
            should_split = True
            
        if should_split and current_chunk:
            chunks.append(current_chunk)
            current_chunk = []
            
        current_chunk.append(msg)
        last_time = current_time
        
    if current_chunk:
        chunks.append(current_chunk)
        
    return chunks

async def main():
    print(f"🚀 Reindexing Discord Embeddings...")
    print(f"   Model: {config.LOCAL_EMBEDDING_MODEL_NAME or 'BM-K/KoSimCSE-roberta'}")
    print(f"   Database: {config.DATABASE_FILE}")
    print(f"   Embedding DB: {config.DISCORD_EMBEDDING_DB_PATH}")
    
    # 1. Load History from Main DB
    if not os.path.exists(config.DATABASE_FILE):
        print(f"⚠️ Main database not found at {config.DATABASE_FILE}. Skipping re-indexing.")
        return

    print("📊 Loading conversation history...")
    rows = []
    async with aiosqlite.connect(config.DATABASE_FILE) as db:
        async with db.execute(
            "SELECT guild_id, channel_id, user_id, user_name, content, created_at, message_id "
            "FROM conversation_history "
            "ORDER BY guild_id, channel_id, created_at ASC"
        ) as cursor:
            rows = await cursor.fetchall()
            
    print(f"   Found {len(rows)} messages.")
    
    # 2. Group by Channel
    channels = {}
    for row in rows:
        key = (row[0], row[1]) # guild_id, channel_id
        if key not in channels: channels[key] = []
        channels[key].append({
            'guild_id': row[0],
            'channel_id': row[1],
            'user_id': row[2],
            'user_name': row[3] or 'Unknown',
            'content': row[4] or '',
            'created_at': row[5],
            'message_id': row[6]
        })
        
    print(f"   Grouped into {len(channels)} channels.")
    
    # 3. Reset Embedding DB
    discord_db_path = config.DISCORD_EMBEDDING_DB_PATH
    if os.path.exists(discord_db_path):
        backup_path = discord_db_path + ".bak"
        shutil.move(discord_db_path, backup_path)
        print(f"⚠️  Existing embedding DB moved to {backup_path}")
        
    store = DiscordEmbeddingStore(discord_db_path)
    await store.initialize()
    
    # 4. Process Chunks
    total_chunks = 0
    processed_chunks = 0
    
    print("\n🧩 Chunking and Embedding...")
    
    for (guild_id, channel_id), msgs in channels.items():
        channel_chunks = chunk_messages(msgs)
        total_chunks += len(channel_chunks)
        
        for chunk in channel_chunks:
            chunk_text = format_chunk(chunk)
            
            # E5 Prefix 적용 ("passage: ")
            embedding = await get_embedding(chunk_text, prefix="passage: ")
            
            if embedding is not None:
                last_msg = chunk[-1]
                await store.upsert_message_embedding(
                    message_id=last_msg['message_id'], # Chunk Anchor
                    server_id=guild_id,
                    channel_id=channel_id,
                    user_id=last_msg['user_id'],
                    user_name="Conversation Chunk",
                    message=chunk_text,
                    timestamp_iso=last_msg['created_at'],
                    embedding=embedding
                )
                processed_chunks += 1
                
                if processed_chunks % 50 == 0:
                    print(f"   Processed {processed_chunks} chunks...", end='\r')
                    
    print(f"\n✅ Reindexing complete!")
    print(f"   Total Messages: {len(rows)}")
    print(f"   Total Chunks Embedded: {processed_chunks}")
    print(f"   Saved to: {discord_db_path}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Process interrupted.")
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()

# -*- coding: utf-8 -*-
"""
데이터베이스를 초기화하는 스크립트입니다.

이 스크립트는 `database/schema.sql` 파일을 읽어 데이터베이스와 테이블을 생성하고,
API 호출 횟수 제한을 위한 카운터를 초기화하며, 필요한 경우 기존 데이터베이스 스키마를
업데이트(마이그레이션)합니다.

봇을 처음 설정할 때 한 번 실행해야 합니다.
"""

import sqlite3
import os
from datetime import datetime
import pytz

# --- 상수 정의 ---
DB_DIR = 'database'
DB_PATH = os.path.join(DB_DIR, 'remasamong.db')
SCHEMA_PATH = os.path.join(DB_DIR, 'schema.sql')

def initialize_database():
    """
    데이터베이스 디렉토리와 파일을 생성하고, 스키마를 적용하여 테이블을 초기화합니다.
    """
    # 데이터베이스 디렉토리가 없으면 생성
    if not os.path.exists(DB_DIR):
        os.makedirs(DB_DIR)
        print(f"INFO: '{DB_DIR}' 디렉토리를 생성했습니다.")

    if not os.path.exists(SCHEMA_PATH):
        print(f"[오류] 스키마 파일 '{SCHEMA_PATH}'을(를) 찾을 수 없습니다.")
        return

    try:
        # 데이터베이스 연결 (파일이 없으면 자동 생성)
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        print(f"INFO: 데이터베이스에 성공적으로 연결되었습니다: {DB_PATH}")

        # 1. 스키마 파일 실행하여 테이블 생성
        with open(SCHEMA_PATH, 'r', encoding='utf-8') as f:
            schema_sql = f.read()
        cursor.executescript(schema_sql)
        print("INFO: SQL 스키마를 성공적으로 적용하여 테이블을 생성/확인했습니다.")

        # 2. 데이터베이스 스키마 마이그레이션 (필요시)
        migrate_database(cursor)

        # 3. 시스템 카운터 초기값 설정
        print("INFO: 시스템 카운터 초기값을 확인하고 설정합니다...")
        now_iso_str = datetime.now(pytz.utc).isoformat()
        counters = ['kma_daily_calls', 'gemini_lite_daily_calls', 'gemini_flash_daily_calls', 'gemini_embedding_calls']
        for name in counters:
            # INSERT OR IGNORE: 카운터가 이미 존재하면 무시합니다.
            cursor.execute("INSERT OR IGNORE INTO system_counters (counter_name, counter_value, last_reset_at) VALUES (?, 0, ?)", (name, now_iso_str))
        print("INFO: 시스템 카운터 초기화가 완료되었습니다.")

        # 변경사항 저장 및 연결 종료
        conn.commit()
        conn.close()
        print("\n✅ 데이터베이스 초기화가 성공적으로 완료되었습니다.")

    except sqlite3.Error as e:
        print(f"[오류] 데이터베이스 초기화 중 오류가 발생했습니다: {e}")
    except Exception as e:
        print(f"[오류] 예기치 않은 오류가 발생했습니다: {e}")

def migrate_database(cursor):
    """
    기존 데이터베이스 스키마에 필요한 변경사항(예: 새로운 컬럼 추가)을 적용합니다.
    이 함수는 하위 호환성을 유지하기 위해 필요합니다.
    """
    try:
        print("INFO: 데이터베이스 스키마 마이그레이션을 확인합니다...")

        # guild_settings 테이블에 persona_text 컬럼 추가 (v5.1 -> v5.2 마이그레이션)
        cursor.execute("PRAGMA table_info(guild_settings)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'persona_text' not in columns:
            print("INFO: 'guild_settings' 테이블에 'persona_text' 컬럼을 추가합니다...")
            cursor.execute("ALTER TABLE guild_settings ADD COLUMN persona_text TEXT")
            print("INFO: 컬럼 추가 완료.")

        # [Safety Check] Ensure new tables exist (redundant but safe)
        tables_to_check = ['user_profiles', 'dm_usage_logs']
        for table in tables_to_check:
            cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'")
            if not cursor.fetchone():
                print(f"INFO: '{table}' 테이블이 누락되어 생성을 시도합니다.")
                # schema.sql이 이미 실행되었으므로 여기로 올 확률은 낮지만,
                # 만약의 경우를 대비해 스키마 파일 다시 읽어서 실행하면 중복될 수 있으므로
                # 여기서는 로그만 남기고 schema.sql 실행에 의존하거나, 
                # 또는 직접 CREATE 문을 실행할 수도 있음. 
                # 여기서는 schema.sql이 앞서 실행되므로 Pass.
                print(f"WARNING: schema.sql 실행에도 불구하고 '{table}' 테이블이 없습니다. DB 파일을 확인하세요.")

        # --- 향후 필요한 마이그레이션 로직을 여기에 추가 --- #

        print("INFO: 데이터베이스 마이그레이션 확인이 완료되었습니다.")
    except sqlite3.Error as e:
        print(f"[오류] 데이터베이스 마이그레이션 중 오류가 발생했습니다: {e}")

if __name__ == '__main__':
    """스크립트가 직접 실행될 때 데이터베이스 초기화 함수를 호출합니다."""
    initialize_database()
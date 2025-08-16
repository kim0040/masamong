# -*- coding: utf-8 -*-
import sqlite3
import os
from datetime import datetime
import pytz

# 이 스크립트는 봇을 시작하기 전에 한번만 실행하여
# database/schema.sql 파일에 정의된 대로 데이터베이스와 테이블을 생성합니다.

DB_DIR = 'database'
DB_PATH = os.path.join(DB_DIR, 'remasamong.db')
SCHEMA_PATH = os.path.join(DB_DIR, 'schema.sql')

def initialize_database():
    """
    스키마 파일을 읽어 데이터베이스를 초기화합니다.
    """
    # 데이터베이스 디렉토리 생성
    if not os.path.exists(DB_DIR):
        os.makedirs(DB_DIR)
        print(f"'{DB_DIR}' 디렉토리를 생성했습니다.")

    # 스키마 파일 존재 여부 확인
    if not os.path.exists(SCHEMA_PATH):
        print(f"[오류] 스키마 파일 '{SCHEMA_PATH}'을(를) 찾을 수 없습니다.")
        print("프로젝트 루트에 database/schema.sql 파일이 있는지 확인해주세요.")
        return

    try:
        # 데이터베이스 연결
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        print(f"데이터베이스에 연결되었습니다: {DB_PATH}")

        # 스키마 파일 읽기
        with open(SCHEMA_PATH, 'r', encoding='utf-8') as f:
            schema_sql = f.read()

        # SQL 스크립트 실행 (여러 CREATE 문을 한번에 실행)
        cursor.executescript(schema_sql)
        print("스키마를 성공적으로 적용하여 테이블을 생성/확인했습니다.")

        # 시스템 카운터 초기값 설정
        print("시스템 카운터 초기값을 확인하고 설정합니다...")
        now_iso_str = datetime.now(pytz.utc).isoformat()
        counters_to_initialize = {
            'kma_daily_calls': (0, now_iso_str),
            'gemini_lite_daily_calls': (0, now_iso_str),
            'gemini_flash_daily_calls': (0, now_iso_str),
            'gemini_embedding_calls': (0, now_iso_str)
        }
        for name, (value, date_str) in counters_to_initialize.items():
            cursor.execute("""
                INSERT OR IGNORE INTO system_counters (counter_name, counter_value, last_reset_at)
                VALUES (?, ?, ?)
            """, (name, value, date_str))

        # 이전 카운터 삭제 (마이그레이션)
        cursor.execute("DELETE FROM system_counters WHERE counter_name = 'gemini_daily_calls'")
        print("이전 카운터(gemini_daily_calls)를 삭제했습니다.")

        print("시스템 카운터 초기화가 완료되었습니다.")

        # 변경사항 저장 및 연결 종료
        conn.commit()
        conn.close()
        print("데이터베이스 초기화가 완료되었습니다.")

    except sqlite3.Error as e:
        print(f"데이터베이스 초기화 중 오류가 발생했습니다: {e}")
    except Exception as e:
        print(f"예기치 않은 오류가 발생했습니다: {e}")

if __name__ == '__main__':
    initialize_database()

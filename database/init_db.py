# -*- coding: utf-8 -*-
import sqlite3
import os
import sqlite_vss

# 이 스크립트는 봇을 시작하기 전에 한번만 실행하여
# database/schema.sql 파일에 정의된 대로 데이터베이스와 테이블을 생성합니다.

DB_DIR = 'database'
DB_PATH = os.path.join(DB_DIR, 'remasamong.db')
SCHEMA_PATH = os.path.join(DB_DIR, 'schema.sql')

def initialize_database():
    """
    스키마 파일을 읽어 데이터베이스를 초기화하고 VSS 확장을 로드합니다.
    """
    if not os.path.exists(DB_DIR):
        os.makedirs(DB_DIR)
        print(f"'{DB_DIR}' 디렉토리를 생성했습니다.")

    if not os.path.exists(SCHEMA_PATH):
        print(f"[오류] 스키마 파일 '{SCHEMA_PATH}'을(를) 찾을 수 없습니다.")
        return

    conn = None  # 연결 객체를 try 블록 외부에서 초기화
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.enable_load_extension(True)
        sqlite_vss.load(conn)
        conn.enable_load_extension(False) # 보안을 위해 사용 후 비활성화

        cursor = conn.cursor()
        print(f"데이터베이스 연결 및 VSS 확장 로드 성공: {DB_PATH}")

        with open(SCHEMA_PATH, 'r', encoding='utf-8') as f:
            schema_sql = f.read()

        cursor.executescript(schema_sql)
        print("스키마를 성공적으로 적용하여 테이블을 생성/확인했습니다.")

        conn.commit()
        print("데이터베이스 초기화가 완료되었습니다.")

    except sqlite3.Error as e:
        print(f"데이터베이스 초기화 중 오류가 발생했습니다: {e}")
    except Exception as e:
        print(f"예기치 않은 오류가 발생했습니다: {e}")
    finally:
        if conn:
            conn.close()

if __name__ == '__main__':
    initialize_database()

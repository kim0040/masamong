-- 이 파일은 데이터베이스의 전체 구조를 정의합니다.
-- init_db.py 스크립트를 통해 이 스키마를 기반으로 DB 파일이 생성됩니다.

-- 서버(길드)별 설정을 관리하는 테이블
-- 서버(길드)별 설정을 관리하는 테이블 (Key-Value 방식)
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER NOT NULL,
    setting_name TEXT NOT NULL,
    setting_value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc')),
    PRIMARY KEY (guild_id, setting_name)
);

-- 사용자별 활동을 기록하는 테이블
-- user_id와 guild_id를 함께 기본 키로 사용하여, 동일 서버 내 동일 유저의 중복을 방지
CREATE TABLE IF NOT EXISTS user_activity (
    user_id INTEGER NOT NULL,
    guild_id INTEGER NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    last_active_at TEXT NOT NULL,
    PRIMARY KEY(user_id, guild_id)
);

-- 모든 대화 내용을 순차적으로 저장하는 테이블
CREATE TABLE IF NOT EXISTS conversation_history (
    message_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    user_name TEXT NOT NULL,
    content TEXT NOT NULL,
    is_bot BOOLEAN NOT NULL,
    created_at TEXT NOT NULL,
    embedding BLOB
);

-- 대화 내용 벡터 검색을 위한 VSS(Vector Search) 가상 테이블
-- `conversation_history`의 rowid와 연결하여 사용
CREATE VIRTUAL TABLE IF NOT EXISTS vss_conversations USING vss0(
    embedding(768)
);

-- 시스템 전체의 카운터(예: API 호출 횟수)를 관리하는 테이블
CREATE TABLE IF NOT EXISTS system_counters (
    counter_name TEXT PRIMARY KEY,
    counter_value INTEGER NOT NULL DEFAULT 0,
    last_reset_at TEXT NOT NULL
);

-- 봇의 운영 지표를 기록하기 위한 분석용 로그 테이블
CREATE TABLE IF NOT EXISTS analytics_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    log_timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc')),
    event_type TEXT NOT NULL, -- 'COMMAND_USAGE', 'AI_INTERACTION' 등
    guild_id INTEGER,
    user_id INTEGER,
    details TEXT -- JSON 형태로 상세 정보 저장 (예: { "command": "ranking", "latency_ms": 120 })
);

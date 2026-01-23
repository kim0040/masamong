-- 이 파일은 데이터베이스의 전체 구조를 정의합니다.
-- init_db.py 스크립트를 통해 이 스키마를 기반으로 DB 파일이 생성됩니다.

-- 서버(길드)별 설정을 관리하는 테이블
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    ai_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    ai_allowed_channels TEXT, -- JSON 배열 형태의 채널 ID 목록
    proactive_response_probability REAL NOT NULL DEFAULT 0.05,
    proactive_response_cooldown INTEGER NOT NULL DEFAULT 300, -- 초 단위
    persona_text TEXT, -- 사용자 정의 페르소나
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc'))
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

CREATE TABLE IF NOT EXISTS conversation_windows (
    window_id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    start_message_id INTEGER NOT NULL,
    end_message_id INTEGER NOT NULL,
    message_count INTEGER NOT NULL,
    messages_json TEXT NOT NULL,
    anchor_timestamp TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc'))
);

CREATE INDEX IF NOT EXISTS idx_conversation_windows_channel ON conversation_windows (channel_id, anchor_timestamp DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_windows_span ON conversation_windows (channel_id, start_message_id, end_message_id);

-- 시스템 전체의 카운터(예: API 호출 횟수)를 관리하는 테이블
CREATE TABLE IF NOT EXISTS system_counters (
    counter_name TEXT PRIMARY KEY,
    counter_value INTEGER NOT NULL DEFAULT 0,
    last_reset_at TEXT NOT NULL
);

-- API 호출 기록을 저장하여 RPM/RPD를 관리하는 테이블
CREATE TABLE IF NOT EXISTS api_call_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    api_type TEXT NOT NULL, -- 'gemini_intent', 'gemini_response', 'gemini_embedding' 등
    called_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc'))
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

-- 보관된(archived) 대화 내용을 저장하는 테이블
CREATE TABLE IF NOT EXISTS conversation_history_archive (
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

-- 사용자 선호도 및 알림 설정을 저장하는 테이블
CREATE TABLE IF NOT EXISTS user_preferences (
    user_id INTEGER NOT NULL,
    preference_type TEXT NOT NULL,
    preference_value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc')),
    PRIMARY KEY(user_id, preference_type)
);

-- 날씨 기능에서 사용할 지역별 격자 좌표 정보
CREATE TABLE IF NOT EXISTS locations (
    name TEXT PRIMARY KEY,
    nx INTEGER NOT NULL,
    ny INTEGER NOT NULL
);

-- [NEW] 운세 정보 저장을 위한 유저 프로필 테이블
CREATE TABLE IF NOT EXISTS user_profiles (
    user_id INTEGER PRIMARY KEY,
    birth_date TEXT, -- YYYY-MM-DD
    birth_time TEXT, -- HH:MM
    is_lunar BOOLEAN DEFAULT 0, -- 0: 양력, 1: 음력
    subscription_active BOOLEAN DEFAULT 0, -- 모닝 브리핑 구독 여부 (0: 비활성, 1: 활성)
    subscription_time TEXT DEFAULT '07:30', -- 모닝 브리핑 발송 시간
    pending_payload TEXT, -- [NEW] 미리 생성된 브리핑 내용
    last_fortune_sent TEXT, -- YYYY-MM-DD (중복 발송 방지)
    created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc'))
);

-- [NEW] DM 사용량 제한을 위한 로그 테이블
CREATE TABLE IF NOT EXISTS dm_usage_logs (
    user_id INTEGER PRIMARY KEY,
    usage_count INTEGER DEFAULT 0, -- 현재 윈도우 내 사용 횟수
    window_start_at TEXT, -- 윈도우 시작 시각
    reset_at TEXT -- 제한 해제 예정 시각 (window_start + 3H)
);

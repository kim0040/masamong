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
    language TEXT DEFAULT 'ko', -- 서버 언어 설정 (ko/en/ja)
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

-- 채널/기간별 랭킹 집계를 위한 메시지 단위 활동 로그
CREATE TABLE IF NOT EXISTS user_activity_log (
    message_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_activity_log_scope_time ON user_activity_log (guild_id, channel_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_activity_log_user_time ON user_activity_log (guild_id, channel_id, user_id, created_at DESC);

-- Linkup 사용량/비용 추적 (월 예산 제한)
CREATE TABLE IF NOT EXISTS linkup_usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    used_at TEXT NOT NULL,
    endpoint TEXT NOT NULL, -- search | fetch
    depth TEXT, -- fast | standard | deep (search 전용)
    render_js BOOLEAN, -- fetch 전용
    cost_eur REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_linkup_usage_time ON linkup_usage_log (used_at DESC);

-- 채널 요약의 증분 기준점. 기존 대화 행은 수정하거나 삭제하지 않는다.
CREATE TABLE IF NOT EXISTS channel_summary_state (
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    anchor_message_id INTEGER NOT NULL,
    summary_text TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);

-- 프로필별 봇 관리자. Discord 서버 관리자 권한과 섞지 않는다.
CREATE TABLE IF NOT EXISTS bot_admin_accounts (
    instance_name TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    changed_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (instance_name, user_id)
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
    gender TEXT, -- [NEW] 성별 (M/F)
    is_lunar BOOLEAN DEFAULT 0, -- 0: 양력, 1: 음력
    subscription_active BOOLEAN DEFAULT 0, -- 모닝 브리핑 구독 여부 (0: 비활성, 1: 활성)
    subscription_time TEXT DEFAULT '07:30', -- 모닝 브리핑 발송 시간
    pending_payload TEXT, -- 날짜/단계/시도수/backoff/생성물을 담는 모닝 브리핑 JSON 상태
    last_fortune_sent TEXT, -- YYYY-MM-DD (중복 발송 방지)
    last_fortune_content TEXT, -- [NEW] 마지막으로 조회한 운세 내용 (컨텍스트용)
    birth_place TEXT, -- 출생지
    created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc'))
);

-- 사용자가 직접 제공하는 개인정보의 목적별 현재 동의 상태.
-- 기존 기능 데이터와 분리하여, 동의 철회가 누적 프로필/구독을 자동 삭제하지 않게 한다.
CREATE TABLE IF NOT EXISTS privacy_consents (
    user_id INTEGER NOT NULL,
    scope TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    notice_hash TEXT NOT NULL,
    status TEXT NOT NULL, -- granted | withdrawn
    granted_at TEXT,
    withdrawn_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, scope)
);

-- 동의/철회 이력은 감사 목적으로 append-only로 보관한다.
CREATE TABLE IF NOT EXISTS privacy_consent_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    scope TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    notice_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    granted_at TEXT,
    withdrawn_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_privacy_consent_events_user_scope
    ON privacy_consent_events (user_id, scope, created_at);

-- [NEW] DM 사용량 제한을 위한 로그 테이블
CREATE TABLE IF NOT EXISTS dm_usage_logs (
    user_id INTEGER PRIMARY KEY,
    usage_count INTEGER DEFAULT 0, -- 현재 윈도우 내 사용 횟수
    window_start_at TEXT, -- 윈도우 시작 시각
    reset_at TEXT -- 제한 해제 예정 시각 (window_start + 3H)
);

-- ============================================================
-- 학교 공지 추적
-- 수집 부산물(공지 snapshot/분석 캐시)은 batch가 소유한 별도 SQLite에 있고,
-- 여기에는 Discord 사용자와 결합된 데이터만 둔다.
-- ============================================================

CREATE TABLE IF NOT EXISTS school_notice_profiles (
    user_id INTEGER PRIMARY KEY, -- Discord user id
    user_key TEXT NOT NULL UNIQUE, -- "discord-<user_id>"
    school_id TEXT NOT NULL,
    profile_json TEXT NOT NULL, -- 코어 프로필 스키마 전체
    profile_version INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    delivery_time TEXT NOT NULL DEFAULT '09:00', -- Asia/Seoul HH:MM
    created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc')),
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS school_notice_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key TEXT NOT NULL,
    source_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    feedback_type TEXT NOT NULL, -- useful | saved | applied | not_interested | already_knew | completed | dismiss_once | not_eligible | mute_topic
    topic TEXT, -- mute_topic 전용
    interaction_id TEXT NOT NULL UNIQUE, -- 버튼 중복 클릭 차단
    created_at TEXT NOT NULL,
    consumed_at TEXT -- batch가 코어로 반영한 시각
);

CREATE INDEX IF NOT EXISTS idx_school_notice_feedback_pending
    ON school_notice_feedback (user_key, consumed_at, created_at);

CREATE TABLE IF NOT EXISTS school_notice_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key TEXT NOT NULL,
    digest_date TEXT NOT NULL,
    notice_id INTEGER NOT NULL,
    revision_count INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL, -- sent | failed | skipped
    failure_reason TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 1,
    delivered_at TEXT NOT NULL,
    -- 같은 공지는 날짜가 바뀌어도 반복하지 않고, 내용 revision이 바뀌면 다시 보낸다.
    UNIQUE (user_key, notice_id, revision_count)
);

CREATE TABLE IF NOT EXISTS school_notice_batch_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key TEXT NOT NULL,
    run_date TEXT NOT NULL,
    profile_version INTEGER NOT NULL DEFAULT 0,
    profile_hash TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL, -- succeeded | partial | failed
    collection_status TEXT, -- healthy | degraded | failed
    may_include_stale INTEGER NOT NULL DEFAULT 0,
    item_count INTEGER NOT NULL DEFAULT 0,
    http_requests INTEGER,
    llm_calls INTEGER,
    finished_at TEXT NOT NULL,
    UNIQUE (user_key, run_date)
);

-- 전날 digest를 사용자별 시각에 전달했는지 날짜 단위로 내구 기록한다.
-- healthy empty digest도 completed 상태로 남겨 매분 다시 읽지 않는다.
CREATE TABLE IF NOT EXISTS school_notice_delivery_runs (
    user_key TEXT NOT NULL,
    digest_date TEXT NOT NULL,
    status TEXT NOT NULL, -- processing | retry | completed | failed
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_key, digest_date)
);

-- 활성 구독이 있으나 현 정책 동의가 없는 기존 사용자에게 동의 버튼을
-- 한 정책 버전당 한 번만 안내한다. 취소/비활성 구독자는 후보가 아니다.
CREATE TABLE IF NOT EXISTS privacy_consent_prompts (
    user_id INTEGER NOT NULL,
    scope TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    notice_hash TEXT NOT NULL,
    status TEXT NOT NULL, -- sent | retry | failed
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    sent_at TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, scope, policy_version, notice_hash)
);

-- ============================================================
-- 공인영어(TOEIC 포함) 편입 공지 구독
-- 공개 공지 snapshot은 별도 SQLite에 있고 여기에는 구독 설정만 둔다.
-- ============================================================
CREATE TABLE IF NOT EXISTS transfer_notice_subscriptions (
    user_id INTEGER PRIMARY KEY,
    schools_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transfer_notice_deliveries (
    user_id INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL, -- processing | retry | sent | failed
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    delivered_at TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, source_id, external_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_transfer_notice_deliveries_due
    ON transfer_notice_deliveries (status, next_attempt_at, updated_at);

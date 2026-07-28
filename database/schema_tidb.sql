CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id BIGINT PRIMARY KEY,
    ai_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    ai_allowed_channels LONGTEXT,
    proactive_response_probability DOUBLE NOT NULL DEFAULT 0.05,
    proactive_response_cooldown INT NOT NULL DEFAULT 300,
    persona_text LONGTEXT,
    language VARCHAR(10) DEFAULT 'ko',
    created_at VARCHAR(64) NOT NULL DEFAULT '',
    updated_at VARCHAR(64) NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS user_activity (
    user_id BIGINT NOT NULL,
    guild_id BIGINT NOT NULL,
    message_count INT NOT NULL DEFAULT 0,
    last_active_at VARCHAR(64) NOT NULL,
    PRIMARY KEY(user_id, guild_id)
);

CREATE TABLE IF NOT EXISTS user_activity_log (
    message_id BIGINT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    KEY idx_user_activity_log_scope_time (guild_id, channel_id, created_at),
    KEY idx_user_activity_log_user_time (guild_id, channel_id, user_id, created_at)
);

CREATE TABLE IF NOT EXISTS linkup_usage_log (
    id BIGINT PRIMARY KEY AUTO_RANDOM,
    used_at VARCHAR(64) NOT NULL,
    endpoint VARCHAR(32) NOT NULL,
    depth VARCHAR(32),
    render_js BOOLEAN,
    cost_eur DOUBLE NOT NULL,
    KEY idx_linkup_usage_time (used_at)
);

-- 채널 요약의 증분 기준점. 기존 대화 행은 수정하거나 삭제하지 않는다.
CREATE TABLE IF NOT EXISTS channel_summary_state (
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    anchor_message_id BIGINT NOT NULL,
    summary_text MEDIUMTEXT NOT NULL,
    updated_at VARCHAR(64) NOT NULL,
    PRIMARY KEY (guild_id, channel_id),
    KEY idx_channel_summary_updated (updated_at)
);

CREATE TABLE IF NOT EXISTS conversation_history (
    message_id BIGINT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    user_name VARCHAR(255) NOT NULL,
    content MEDIUMTEXT NOT NULL,
    is_bot BOOLEAN NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    embedding BLOB
);

CREATE INDEX IF NOT EXISTS idx_conversation_history_channel_created_at
ON conversation_history (channel_id, created_at);

CREATE TABLE IF NOT EXISTS conversation_windows (
    window_id BIGINT PRIMARY KEY AUTO_RANDOM,
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    start_message_id BIGINT NOT NULL,
    end_message_id BIGINT NOT NULL,
    message_count INT NOT NULL,
    messages_json MEDIUMTEXT NOT NULL,
    anchor_timestamp VARCHAR(64) NOT NULL,
    created_at VARCHAR(64) NOT NULL DEFAULT '',
    UNIQUE KEY idx_conversation_windows_span (channel_id, start_message_id, end_message_id),
    KEY idx_conversation_windows_channel (channel_id, anchor_timestamp)
);

CREATE TABLE IF NOT EXISTS system_counters (
    counter_name VARCHAR(255) PRIMARY KEY,
    counter_value BIGINT NOT NULL DEFAULT 0,
    last_reset_at VARCHAR(64) NOT NULL
);

CREATE TABLE IF NOT EXISTS api_call_log (
    id BIGINT PRIMARY KEY AUTO_RANDOM,
    api_type VARCHAR(255) NOT NULL,
    called_at VARCHAR(64) NOT NULL DEFAULT '',
    KEY idx_api_call_log_type_called_at (api_type, called_at)
);

CREATE TABLE IF NOT EXISTS analytics_log (
    log_id BIGINT PRIMARY KEY AUTO_RANDOM,
    log_timestamp VARCHAR(64) NOT NULL DEFAULT '',
    event_type VARCHAR(255) NOT NULL,
    guild_id VARCHAR(64),
    user_id VARCHAR(64),
    details LONGTEXT,
    KEY idx_analytics_event_type (event_type),
    KEY idx_analytics_guild_user (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS conversation_history_archive (
    message_id BIGINT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    user_name VARCHAR(255) NOT NULL,
    content MEDIUMTEXT NOT NULL,
    is_bot BOOLEAN NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    embedding BLOB
);

CREATE TABLE IF NOT EXISTS user_preferences (
    user_id BIGINT NOT NULL,
    preference_type VARCHAR(255) NOT NULL,
    preference_value LONGTEXT NOT NULL,
    updated_at VARCHAR(64) NOT NULL DEFAULT '',
    PRIMARY KEY(user_id, preference_type)
);

CREATE TABLE IF NOT EXISTS locations (
    name VARCHAR(255) PRIMARY KEY,
    nx INT NOT NULL,
    ny INT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_profiles (
    user_id BIGINT PRIMARY KEY,
    birth_date VARCHAR(32),
    birth_time VARCHAR(32),
    gender VARCHAR(16),
    is_lunar BOOLEAN DEFAULT 0,
    subscription_active BOOLEAN DEFAULT 0,
    subscription_time VARCHAR(16) DEFAULT '07:30',
    pending_payload LONGTEXT, -- 모닝 브리핑의 날짜/단계/시도수/backoff/생성물 JSON 상태
    last_fortune_sent VARCHAR(32),
    last_fortune_content LONGTEXT,
    birth_place VARCHAR(255),
    created_at VARCHAR(64) DEFAULT ''
);

CREATE TABLE IF NOT EXISTS privacy_consents (
    user_id BIGINT NOT NULL,
    scope VARCHAR(64) NOT NULL,
    policy_version VARCHAR(64) NOT NULL,
    notice_hash CHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    granted_at VARCHAR(64),
    withdrawn_at VARCHAR(64),
    updated_at VARCHAR(64) NOT NULL,
    PRIMARY KEY (user_id, scope)
);

CREATE TABLE IF NOT EXISTS privacy_consent_events (
    id BIGINT PRIMARY KEY AUTO_RANDOM,
    user_id BIGINT NOT NULL,
    scope VARCHAR(64) NOT NULL,
    policy_version VARCHAR(64) NOT NULL,
    notice_hash CHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    granted_at VARCHAR(64),
    withdrawn_at VARCHAR(64),
    created_at VARCHAR(64) NOT NULL,
    KEY idx_privacy_consent_events_user_scope (user_id, scope, created_at)
);

CREATE TABLE IF NOT EXISTS dm_usage_logs (
    user_id BIGINT PRIMARY KEY,
    usage_count INT DEFAULT 0,
    window_start_at VARCHAR(64),
    reset_at VARCHAR(64)
);

CREATE TABLE IF NOT EXISTS discord_chat_embeddings (
    id BIGINT PRIMARY KEY AUTO_RANDOM,
    message_id VARCHAR(64) NOT NULL,
    server_id VARCHAR(64) NOT NULL,
    channel_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    user_name VARCHAR(255),
    message MEDIUMTEXT,
    timestamp VARCHAR(64),
    embedding BLOB NOT NULL,
    UNIQUE KEY uq_discord_embeddings_message (message_id),
    KEY idx_discord_embeddings_scuid (server_id, channel_id, user_id),
    KEY idx_discord_embeddings_timestamp (timestamp)
);

CREATE TABLE IF NOT EXISTS discord_memory_entries (
    id BIGINT PRIMARY KEY AUTO_RANDOM,
    memory_id VARCHAR(191) NOT NULL,
    anchor_message_id VARCHAR(64) NOT NULL,
    server_id VARCHAR(64) NOT NULL,
    channel_id VARCHAR(64) NOT NULL,
    owner_user_id VARCHAR(64),
    owner_user_name VARCHAR(255),
    memory_scope VARCHAR(32) NOT NULL,
    memory_type VARCHAR(64) NOT NULL,
    summary_text MEDIUMTEXT NOT NULL,
    memory_text MEDIUMTEXT NOT NULL,
    raw_context MEDIUMTEXT,
    source_message_ids MEDIUMTEXT,
    speaker_names MEDIUMTEXT,
    keyword_json MEDIUMTEXT,
    timestamp VARCHAR(64),
    embedding BLOB NOT NULL,
    UNIQUE KEY uq_discord_memory_entries_memory_id (memory_id),
    KEY idx_discord_memory_scope (server_id, channel_id, memory_scope, owner_user_id),
    KEY idx_discord_memory_timestamp (timestamp)
);

CREATE TABLE IF NOT EXISTS kakao_chunks (
    id BIGINT PRIMARY KEY AUTO_RANDOM,
    room_key VARCHAR(64) NOT NULL,
    source_room_label VARCHAR(255),
    chunk_id BIGINT NOT NULL,
    session_id BIGINT,
    start_date VARCHAR(64),
    message_count INT,
    summary TEXT,
    text_long MEDIUMTEXT NOT NULL,
    embedding VECTOR(384),
    UNIQUE KEY uq_kakao_chunks_room_chunk (room_key, chunk_id),
    KEY idx_kakao_chunks_room_date (room_key, start_date)
);

-- ============================================================
-- 학교 공지 추적
-- 수집 부산물은 batch가 소유한 별도 SQLite에 있고,
-- 여기에는 Discord 사용자와 결합된 데이터만 둔다.
-- ============================================================

CREATE TABLE IF NOT EXISTS school_notice_profiles (
    user_id BIGINT PRIMARY KEY,
    user_key VARCHAR(128) NOT NULL UNIQUE,
    school_id VARCHAR(64) NOT NULL,
    profile_json TEXT NOT NULL,
    profile_version INT NOT NULL DEFAULT 1,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    delivery_time VARCHAR(5) NOT NULL DEFAULT '09:00',
    created_at VARCHAR(64),
    updated_at VARCHAR(64)
);

CREATE TABLE IF NOT EXISTS school_notice_feedback (
    id BIGINT PRIMARY KEY AUTO_RANDOM,
    user_key VARCHAR(128) NOT NULL,
    source_id VARCHAR(128) NOT NULL,
    external_id VARCHAR(128) NOT NULL,
    feedback_type VARCHAR(32) NOT NULL,
    topic VARCHAR(256),
    interaction_id VARCHAR(128) NOT NULL UNIQUE,
    created_at VARCHAR(64) NOT NULL,
    consumed_at VARCHAR(64),
    KEY idx_school_notice_feedback_pending (user_key, consumed_at, created_at)
);

CREATE TABLE IF NOT EXISTS school_notice_deliveries (
    id BIGINT PRIMARY KEY AUTO_RANDOM,
    user_key VARCHAR(128) NOT NULL,
    digest_date VARCHAR(10) NOT NULL,
    notice_id BIGINT NOT NULL,
    revision_count INT NOT NULL DEFAULT 1,
    status VARCHAR(32) NOT NULL,
    failure_reason VARCHAR(64),
    attempt_count INT NOT NULL DEFAULT 1,
    delivered_at VARCHAR(64) NOT NULL,
    UNIQUE KEY uq_school_notice_delivery_revision
        (user_key, notice_id, revision_count),
    KEY idx_school_notice_deliveries_user_date (user_key, digest_date)
);

CREATE TABLE IF NOT EXISTS school_notice_batch_runs (
    id BIGINT PRIMARY KEY AUTO_RANDOM,
    user_key VARCHAR(128) NOT NULL,
    run_date VARCHAR(10) NOT NULL,
    profile_version INT NOT NULL,
    profile_hash CHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    collection_status VARCHAR(32),
    may_include_stale BOOLEAN NOT NULL DEFAULT FALSE,
    item_count INT NOT NULL DEFAULT 0,
    http_requests INT,
    llm_calls INT,
    finished_at VARCHAR(64) NOT NULL,
    UNIQUE KEY uq_school_notice_batch_run (user_key, run_date)
);

CREATE TABLE IF NOT EXISTS school_notice_delivery_runs (
    user_key VARCHAR(128) NOT NULL,
    digest_date VARCHAR(10) NOT NULL,
    status VARCHAR(32) NOT NULL,
    attempt_count INT NOT NULL DEFAULT 0,
    next_attempt_at VARCHAR(64),
    last_error VARCHAR(64),
    finished_at VARCHAR(64),
    updated_at VARCHAR(64) NOT NULL,
    PRIMARY KEY (user_key, digest_date),
    KEY idx_school_notice_delivery_due (status, next_attempt_at, updated_at)
);

CREATE TABLE IF NOT EXISTS privacy_consent_prompts (
    user_id BIGINT NOT NULL,
    scope VARCHAR(64) NOT NULL,
    policy_version VARCHAR(64) NOT NULL,
    notice_hash CHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    attempt_count INT NOT NULL DEFAULT 0,
    next_attempt_at VARCHAR(64),
    sent_at VARCHAR(64),
    last_error VARCHAR(64),
    updated_at VARCHAR(64) NOT NULL,
    PRIMARY KEY (user_id, scope, policy_version, notice_hash),
    KEY idx_privacy_consent_prompts_due (status, next_attempt_at, updated_at)
);

CREATE TABLE IF NOT EXISTS transfer_notice_subscriptions (
    user_id BIGINT PRIMARY KEY,
    schools_json TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at VARCHAR(64) NOT NULL,
    updated_at VARCHAR(64) NOT NULL
);

CREATE TABLE IF NOT EXISTS transfer_notice_deliveries (
    user_id BIGINT NOT NULL,
    run_id VARCHAR(64) NOT NULL,
    source_id VARCHAR(64) NOT NULL,
    external_id VARCHAR(64) NOT NULL,
    revision INT NOT NULL DEFAULT 1,
    payload_json TEXT NOT NULL,
    status VARCHAR(16) NOT NULL,
    attempt_count INT NOT NULL DEFAULT 0,
    next_attempt_at VARCHAR(64),
    delivered_at VARCHAR(64),
    last_error VARCHAR(64),
    updated_at VARCHAR(64) NOT NULL,
    PRIMARY KEY (user_id, source_id, external_id, revision),
    KEY idx_transfer_notice_deliveries_due
        (status, next_attempt_at, updated_at)
);

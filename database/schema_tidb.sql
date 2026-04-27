CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id BIGINT PRIMARY KEY,
    ai_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    ai_allowed_channels LONGTEXT,
    proactive_response_probability DOUBLE NOT NULL DEFAULT 0.05,
    proactive_response_cooldown INT NOT NULL DEFAULT 300,
    persona_text LONGTEXT,
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
    pending_payload LONGTEXT,
    last_fortune_sent VARCHAR(32),
    last_fortune_content LONGTEXT,
    birth_place VARCHAR(255),
    created_at VARCHAR(64) DEFAULT ''
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

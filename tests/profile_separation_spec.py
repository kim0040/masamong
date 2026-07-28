from pathlib import Path

import pytest

from scripts.validate_profile_separation import _load_env, validate


def _write_env(path: Path, *, profile: str, token: str, database: str, db_user: str) -> None:
    config_path = path.parent / f"{profile}.config.json"
    emb_path = path.parent / f"{profile}.emb.json"
    prompt_path = path.parent / f"{profile}.prompts.json"
    ca_path = path.parent / f"{profile}.ca.pem"
    config_path.write_text("{}", encoding="utf-8")
    prompt_path.write_text("{}", encoding="utf-8")
    ca_path.write_text("test-ca", encoding="utf-8")
    emb_path.write_text(
        (
            '{"discord_db_path":"'
            + str(path.parent / profile / "discord.db")
            + '","kakao_db_path":"'
            + str(path.parent / profile / "kakao.db")
            + '","kakao_servers":'
            + (
                '[{"server_id":"1","room_key":"masamo-room"}]'
                if profile == "masamo"
                else "[]"
            )
            + "}"
        ),
        encoding="utf-8",
    )
    path.write_text(
        "\n".join(
            [
                f"MASAMONG_PROFILE={profile}",
                f"MASAMONG_INSTANCE_NAME={profile}",
                "MASAMONG_REQUIRE_EXPLICIT_PROFILE=true",
                f"MASAMONG_ENV_FILE={path}",
                f"MASAMONG_CONFIG_FILE={config_path}",
                "MASAMONG_REQUIRED_COGS=tools_cog,events,ai_handler",
                "MASAMONG_AUTO_MIGRATE=false",
                "SCHOOL_NOTICE_ENABLED=false",
                (
                    "MASAMONG_GUILD_SETTINGS_MODE=static"
                    if profile == "masamo"
                    else "MASAMONG_GUILD_SETTINGS_MODE=database"
                ),
                f"DISCORD_BOT_TOKEN={token}",
                (
                    "MASAMONG_EXPECTED_DISCORD_BOT_USER_ID=replace-with-current-masamo-bot-user-id"
                    if profile == "masamo"
                    else "MASAMONG_EXPECTED_DISCORD_BOT_USER_ID=replace-with-current-masamo-bot-user-id"
                ),
                f"MASAMONG_COMMAND_PREFIX={'!' if profile == 'masamo' else '?'}",
                "MASAMONG_DB_BACKEND=tidb",
                "MASAMONG_DB_HOST=db.internal.local",
                "MASAMONG_DB_PORT=4000",
                f"MASAMONG_DB_NAME={database}",
                f"MASAMONG_EXPECTED_DB_NAME={database}",
                f"MASAMONG_DB_USER={db_user}",
                "MASAMONG_DB_PASSWORD=real-test-password",
                f"MASAMONG_DB_SSL_CA={ca_path}",
                "MASAMONG_DB_SSL_VERIFY_IDENTITY=true",
                "MASAMONG_DB_STRICT_REMOTE_ONLY=true",
                "MASAMONG_DB_REQUIRE_TLS=true",
                (
                    "MASAMONG_MEMORY_SOURCES=discord,kakao"
                    if profile == "masamo"
                    else "MASAMONG_MEMORY_SOURCES=discord"
                ),
                "DISCORD_EMBEDDING_BACKEND=tidb",
                "KAKAO_STORE_BACKEND=tidb",
                "DISCORD_EMBEDDING_TIDB_TABLE=discord_chat_embeddings",
                "KAKAO_TIDB_TABLE=kakao_chunks",
                f"MASAMONG_LOG_FILE=/var/log/masamong/{profile}.log",
                f"MASAMONG_ERROR_LOG_FILE=/var/log/masamong/{profile}-error.log",
                f"EMB_CONFIG_PATH={emb_path}",
                f"PROMPT_CONFIG_PATH={prompt_path}",
                "AI_MEMORY_ENABLED=true" if profile == "masamo" else "AI_MEMORY_ENABLED=false",
                "EMBEDDING_ENABLED=true" if profile == "masamo" else "EMBEDDING_ENABLED=false",
                "RERANK_ENABLED=true" if profile == "masamo" else "RERANK_ENABLED=false",
                "MASAMONG_CPU_THREADS=1",
                "MASAMONG_EXECUTOR_WORKERS=1",
                "AI_MAX_CONCURRENT_PROCESSING=1",
                "AI_QUEUE_WAIT_TIMEOUT_SECONDS=5",
                "LLM_MAX_CONCURRENT_CALLS=1",
                "LLM_ACQUIRE_TIMEOUT_SECONDS=10",
                "LLM_CALL_TIMEOUT_SECONDS=120",
                "EMBEDDING_MAX_CONCURRENCY=1",
                (
                    "RAG_MAX_BACKGROUND_TASKS=8"
                    if profile == "masamo"
                    else "RAG_MAX_BACKGROUND_TASKS=2"
                ),
                (
                    "RAG_MAX_TRACKED_WINDOWS=256"
                    if profile == "masamo"
                    else "RAG_MAX_TRACKED_WINDOWS=64"
                ),
                (
                    "KAKAO_API_MAX_CONCURRENCY=1"
                    if profile == "masamo"
                    else ""
                ),
                (
                    "MASAMONG_DISCORD_MAX_MESSAGES=200"
                    if profile == "masamo"
                    else "MASAMONG_DISCORD_MAX_MESSAGES=100"
                ),
                "TOKENIZERS_PARALLELISM=false",
                # 명시적 프로필은 상속 환경을 쓰지 않으므로 켜 둔 기능의
                # 자격증명이 env 파일 안에 있어야 한다.
                f"COMETAPI_KEY={profile}-real-cometapi-key",
                f"KMA_API_KEY={profile}-real-kma-key",
                (
                    ""
                    if profile == "masamo"
                    else "\n".join(
                        [
                            "ENABLE_RAIN_NOTIFICATION=false",
                            "ENABLE_GREETING_NOTIFICATION=false",
                            "ENABLE_EARTHQUAKE_ALERT=false",
                            "FORTUNE_MORNING_BRIEFING_ENABLED=false",
                            "RAG_ARCHIVING_ENABLED=false",
                            "BM25_AUTO_REBUILD_ENABLED=false",
                        ]
                    )
                ),
            ]
        ),
        encoding="utf-8",
    )


def _use_sqlite_database(env_path: Path, database_path: str) -> None:
    env_path.write_text(
        env_path.read_text(encoding="utf-8").replace(
            "MASAMONG_DB_BACKEND=tidb",
            "MASAMONG_DB_BACKEND=sqlite",
        )
        + f"\nMASAMONG_DATABASE_FILE={database_path}\n",
        encoding="utf-8",
    )


def test_distinct_masamo_and_general_profiles_pass(tmp_path):
    masamo = tmp_path / "masamo.env"
    general = tmp_path / "general.env"
    _write_env(
        masamo,
        profile="masamo",
        token="real-masamo-token",
        database="masamong",
        db_user="masamo_user",
    )
    _write_env(
        general,
        profile="general",
        token="real-general-token",
        database="masamong_general",
        db_user="general_user",
    )

    errors, warnings = validate(masamo, general)

    assert errors == []
    assert warnings == []


def test_distinct_profile_owned_sqlite_paths_pass(tmp_path):
    masamo = tmp_path / "masamo.env"
    general = tmp_path / "general.env"
    _write_env(
        masamo,
        profile="masamo",
        token="real-masamo-token",
        database="masamong",
        db_user="masamo_user",
    )
    _write_env(
        general,
        profile="general",
        token="real-general-token",
        database="masamong_general",
        db_user="general_user",
    )
    _use_sqlite_database(
        masamo,
        str(tmp_path / "masamo" / "main.db"),
    )
    _use_sqlite_database(
        general,
        str(tmp_path / "general" / "main.db"),
    )

    errors, warnings = validate(masamo, general)

    assert errors == []
    assert warnings == []


@pytest.mark.parametrize(
    ("database_path", "expected_error"),
    [
        ("database/general.db", "절대 파일 경로"),
        (":memory:", "절대 파일 경로"),
        ("/var/lib/masamong/shared.db", "인스턴스 이름"),
    ],
)
def test_sqlite_profile_rejects_non_owned_database_path(
    tmp_path,
    database_path,
    expected_error,
):
    masamo = tmp_path / "masamo.env"
    general = tmp_path / "general.env"
    _write_env(
        masamo,
        profile="masamo",
        token="real-masamo-token",
        database="masamong",
        db_user="masamo_user",
    )
    _write_env(
        general,
        profile="general",
        token="real-general-token",
        database="masamong_general",
        db_user="general_user",
    )
    _use_sqlite_database(
        masamo,
        str(tmp_path / "masamo" / "main.db"),
    )
    _use_sqlite_database(general, database_path)

    errors, _ = validate(masamo, general)

    assert any(expected_error in error for error in errors)


def test_sqlite_pair_rejects_same_resolved_database_path(tmp_path):
    masamo = tmp_path / "masamo.env"
    general = tmp_path / "general.env"
    _write_env(
        masamo,
        profile="masamo",
        token="real-masamo-token",
        database="masamong",
        db_user="masamo_user",
    )
    _write_env(
        general,
        profile="general",
        token="real-general-token",
        database="masamong_general",
        db_user="general_user",
    )
    shared_path = tmp_path / "masamo-general" / "shared.db"
    _use_sqlite_database(masamo, str(shared_path))
    _use_sqlite_database(general, str(shared_path))

    errors, _ = validate(masamo, general)

    assert any("DB 쓰기 대상이 같습니다" in error for error in errors)


def test_general_bootstrap_auto_migrate_true_is_allowed_with_warning(tmp_path):
    masamo = tmp_path / "masamo.env"
    general = tmp_path / "general.env"
    _write_env(
        masamo,
        profile="masamo",
        token="real-masamo-token",
        database="masamong",
        db_user="masamo_user",
    )
    _write_env(
        general,
        profile="general",
        token="real-general-token",
        database="masamong_general",
        db_user="general_user",
    )
    general.write_text(
        general.read_text(encoding="utf-8").replace(
            "MASAMONG_AUTO_MIGRATE=false",
            "MASAMONG_AUTO_MIGRATE=true",
        ),
        encoding="utf-8",
    )

    errors, warnings = validate(masamo, general)

    assert errors == []
    assert any("bootstrap 전용" in warning for warning in warnings)


def test_general_rejects_unknown_auto_migrate_mode(tmp_path):
    masamo = tmp_path / "masamo.env"
    general = tmp_path / "general.env"
    _write_env(
        masamo,
        profile="masamo",
        token="real-masamo-token",
        database="masamong",
        db_user="masamo_user",
    )
    _write_env(
        general,
        profile="general",
        token="real-general-token",
        database="masamong_general",
        db_user="general_user",
    )
    general.write_text(
        general.read_text(encoding="utf-8").replace(
            "MASAMONG_AUTO_MIGRATE=false",
            "MASAMONG_AUTO_MIGRATE=typo",
        ),
        encoding="utf-8",
    )

    errors, _ = validate(masamo, general)

    assert any("bootstrap의 true 또는 정상 운영의 false" in error for error in errors)


def test_masamo_auto_migrate_true_is_rejected(tmp_path):
    masamo = tmp_path / "masamo.env"
    general = tmp_path / "general.env"
    _write_env(
        masamo,
        profile="masamo",
        token="real-masamo-token",
        database="masamong",
        db_user="masamo_user",
    )
    _write_env(
        general,
        profile="general",
        token="real-general-token",
        database="masamong_general",
        db_user="general_user",
    )
    masamo.write_text(
        masamo.read_text(encoding="utf-8").replace(
            "MASAMONG_AUTO_MIGRATE=false",
            "MASAMONG_AUTO_MIGRATE=true",
        ),
        encoding="utf-8",
    )

    errors, _ = validate(masamo, general)

    assert any(
        "누적 운영 데이터 보호" in error
        and "MASAMONG_AUTO_MIGRATE" in error
        for error in errors
    )


def test_masamo_school_notice_enablement_is_rejected(tmp_path):
    masamo = tmp_path / "masamo.env"
    general = tmp_path / "general.env"
    _write_env(
        masamo,
        profile="masamo",
        token="real-masamo-token",
        database="masamong",
        db_user="masamo_user",
    )
    _write_env(
        general,
        profile="general",
        token="real-general-token",
        database="masamong_general",
        db_user="general_user",
    )
    masamo.write_text(
        masamo.read_text(encoding="utf-8").replace(
            "SCHOOL_NOTICE_ENABLED=false",
            "SCHOOL_NOTICE_ENABLED=true",
        ),
        encoding="utf-8",
    )

    errors, _ = validate(masamo, general)

    assert any(
        "general 소유" in error and "SCHOOL_NOTICE_ENABLED=false" in error
        for error in errors
    )


def test_shared_writable_targets_are_rejected(tmp_path):
    masamo = tmp_path / "masamo.env"
    general = tmp_path / "general.env"
    _write_env(
        masamo,
        profile="masamo",
        token="same-token",
        database="masamong",
        db_user="shared_user",
    )
    _write_env(
        general,
        profile="general",
        token="same-token",
        database="masamong",
        db_user="shared_user",
    )

    errors, _ = validate(masamo, general)

    assert any("Discord 토큰" in error for error in errors)
    assert any("DB 쓰기 대상" in error for error in errors)
    assert any("같은 DB 계정" in error for error in errors)
    assert all("same-token" not in error for error in errors)


def test_same_expected_discord_bot_identity_is_rejected(tmp_path):
    masamo = tmp_path / "masamo.env"
    general = tmp_path / "general.env"
    _write_env(
        masamo,
        profile="masamo",
        token="real-masamo-token",
        database="masamong",
        db_user="masamo_user",
    )
    _write_env(
        general,
        profile="general",
        token="real-general-token",
        database="masamong_general",
        db_user="general_user",
    )
    general.write_text(
        general.read_text(encoding="utf-8").replace(
            "MASAMONG_EXPECTED_DISCORD_BOT_USER_ID=replace-with-current-masamo-bot-user-id",
            "MASAMONG_EXPECTED_DISCORD_BOT_USER_ID=replace-with-current-masamo-bot-user-id",
        ),
        encoding="utf-8",
    )

    errors, _ = validate(masamo, general)

    assert any("Discord bot user ID" in error for error in errors)


def test_general_cannot_enable_kakao_memory(tmp_path):
    masamo = tmp_path / "masamo.env"
    general = tmp_path / "general.env"
    _write_env(
        masamo,
        profile="masamo",
        token="real-masamo-token",
        database="masamong",
        db_user="masamo_user",
    )
    _write_env(
        general,
        profile="general",
        token="real-general-token",
        database="masamong_general",
        db_user="general_user",
    )
    general.write_text(
        general.read_text(encoding="utf-8").replace(
            "MASAMONG_MEMORY_SOURCES=discord",
            "MASAMONG_MEMORY_SOURCES=discord,kakao",
        ),
        encoding="utf-8",
    )

    errors, _ = validate(masamo, general)

    assert any("Kakao 기억 소스" in error for error in errors)


@pytest.mark.parametrize(
    ("feature_key", "mode"),
    [
        ("AI_MEMORY_ENABLED", "missing"),
        ("EMBEDDING_ENABLED", "missing"),
        ("RERANK_ENABLED", "missing"),
        ("AI_MEMORY_ENABLED", "enabled"),
        ("EMBEDDING_ENABLED", "enabled"),
        ("RERANK_ENABLED", "enabled"),
    ],
)
def test_general_low_spec_memory_features_must_be_explicitly_disabled(
    tmp_path,
    feature_key,
    mode,
):
    masamo = tmp_path / "masamo.env"
    general = tmp_path / "general.env"
    _write_env(
        masamo,
        profile="masamo",
        token="real-masamo-token",
        database="masamong",
        db_user="masamo_user",
    )
    _write_env(
        general,
        profile="general",
        token="real-general-token",
        database="masamong_general",
        db_user="general_user",
    )
    general.write_text(
        "\n".join(
            (
                f"{feature_key}=true"
                if mode == "enabled" and line.startswith(f"{feature_key}=")
                else line
            )
            for line in general.read_text(encoding="utf-8").splitlines()
            if not (
                mode == "missing"
                and line.startswith(f"{feature_key}=")
            )
        ),
        encoding="utf-8",
    )

    errors, _ = validate(masamo, general)

    assert any(
        feature_key in error and "steady-state" in error
        for error in errors
    )


def test_missing_low_spec_limit_is_rejected(tmp_path):
    masamo = tmp_path / "masamo.env"
    general = tmp_path / "general.env"
    _write_env(
        masamo,
        profile="masamo",
        token="real-masamo-token",
        database="masamong",
        db_user="masamo_user",
    )
    _write_env(
        general,
        profile="general",
        token="real-general-token",
        database="masamong_general",
        db_user="general_user",
    )
    masamo.write_text(
        "\n".join(
            line
            for line in masamo.read_text(encoding="utf-8").splitlines()
            if not line.startswith("MASAMONG_CPU_THREADS=")
        ),
        encoding="utf-8",
    )

    errors, _ = validate(masamo, general)

    assert any("MASAMONG_CPU_THREADS" in error for error in errors)


def test_env_aliases_resolve_only_from_the_selected_file(tmp_path):
    env_path = tmp_path / "aliases.env"
    env_path.write_text(
        "\n".join(
            [
                "LOCAL_KEY=file-value",
                "FIRST=${LOCAL_KEY}",
                "SECOND=prefix-${FIRST}-suffix",
            ]
        ),
        encoding="utf-8",
    )

    loaded = _load_env(env_path)

    assert loaded["FIRST"] == "file-value"
    assert loaded["SECOND"] == "prefix-file-value-suffix"


def test_env_alias_does_not_fall_back_to_inherited_process_value(
    tmp_path,
    monkeypatch,
):
    env_path = tmp_path / "unresolved.env"
    env_path.write_text("SECRET=${INHERITED_SECRET}", encoding="utf-8")
    monkeypatch.setenv("INHERITED_SECRET", "must-not-be-used")

    with pytest.raises(ValueError) as exc_info:
        _load_env(env_path)

    rendered_error = str(exc_info.value)
    assert "INHERITED_SECRET" in rendered_error
    assert "must-not-be-used" not in rendered_error


def test_env_alias_supports_file_local_default_values(tmp_path):
    env_path = tmp_path / "defaults.env"
    env_path.write_text(
        "EMPTY=\nFIRST=${EMPTY:-fallback}\nSECOND=${MISSING:-safe-default}",
        encoding="utf-8",
    )

    loaded = _load_env(env_path)

    assert loaded["FIRST"] == "fallback"
    assert loaded["SECOND"] == "safe-default"


def test_env_alias_cycle_is_rejected_without_printing_values(tmp_path):
    env_path = tmp_path / "cycle.env"
    env_path.write_text(
        "FIRST=${SECOND}\nSECOND=${FIRST}",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="FIRST -> SECOND -> FIRST"):
        _load_env(env_path)


def _valid_pair(tmp_path: Path) -> tuple[Path, Path]:
    """경계 검사를 통과하는 masamo/general env 쌍을 만듭니다."""
    masamo = tmp_path / "masamo.env"
    general = tmp_path / "general.env"
    _write_env(
        masamo,
        profile="masamo",
        token="real-masamo-token",
        database="masamong",
        db_user="masamo_user",
    )
    _write_env(
        general,
        profile="general",
        token="real-general-token",
        database="masamong_general",
        db_user="general_user",
    )
    return masamo, general


def _drop_line(path: Path, prefix: str) -> None:
    path.write_text(
        "\n".join(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.startswith(prefix)
        ),
        encoding="utf-8",
    )


def test_validator_flags_credentials_missing_from_the_selected_file(tmp_path):
    # 명시적 프로필은 systemd/shell 상속값을 쓰지 않으므로, 파일에 없는 key는
    # 배포 후 런타임이 아니라 이 검사에서 드러나야 한다.
    masamo, general = _valid_pair(tmp_path)
    assert validate(masamo, general)[0] == []

    _drop_line(masamo, "COMETAPI_KEY=")

    errors, _ = validate(masamo, general)

    assert any("COMETAPI_KEY" in error for error in errors)


def test_validator_flags_placeholder_credentials(tmp_path):
    masamo, general = _valid_pair(tmp_path)
    _drop_line(masamo, "KMA_API_KEY=")
    with masamo.open("a", encoding="utf-8") as handle:
        handle.write("\nKMA_API_KEY=replace-with-key\n")

    errors, _ = validate(masamo, general)

    assert any("KMA_API_KEY" in error and "placeholder" in error for error in errors)


def test_validator_accepts_missing_weather_key_when_cog_is_disabled(tmp_path):
    masamo, general = _valid_pair(tmp_path)
    _drop_line(masamo, "KMA_API_KEY=")
    with masamo.open("a", encoding="utf-8") as handle:
        handle.write("\nMASAMONG_DISABLED_COGS=weather_cog\n")

    errors, _ = validate(masamo, general)

    assert errors == []


def test_validator_requires_linkup_key_only_for_the_active_provider(tmp_path):
    masamo, general = _valid_pair(tmp_path)
    with masamo.open("a", encoding="utf-8") as handle:
        handle.write("\nLINKUP_ENABLED=true\nWEB_SEARCH_PROVIDER=legacy\n")
    assert validate(masamo, general)[0] == []

    _drop_line(masamo, "WEB_SEARCH_PROVIDER=")
    with masamo.open("a", encoding="utf-8") as handle:
        handle.write("\nWEB_SEARCH_PROVIDER=linkup\n")

    errors, _ = validate(masamo, general)

    assert any("LINKUP_API_KEY" in error for error in errors)

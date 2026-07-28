#!/usr/bin/env python3
"""두 인스턴스 env 파일의 쓰기 대상 충돌을 네트워크 없이 검사합니다."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Mapping

from dotenv import dotenv_values


PLACEHOLDER_MARKERS = (
    "replace-with",
    "replace_with",
    "your_",
    "your-",
    "example",
    "changeme",
    "placeholder",
)
CORE_REQUIRED_COGS = {"tools_cog", "events", "ai_handler"}
TRUE_VALUES = {"1", "true", "yes", "y", "on"}
FALSE_VALUES = {"0", "false", "no", "n", "off"}
LOW_SPEC_POSITIVE_LIMIT_KEYS = (
    "MASAMONG_CPU_THREADS",
    "MASAMONG_EXECUTOR_WORKERS",
    "AI_MAX_CONCURRENT_PROCESSING",
    "AI_QUEUE_WAIT_TIMEOUT_SECONDS",
    "LLM_MAX_CONCURRENT_CALLS",
    "LLM_ACQUIRE_TIMEOUT_SECONDS",
    "LLM_CALL_TIMEOUT_SECONDS",
    "EMBEDDING_MAX_CONCURRENCY",
    "RAG_MAX_BACKGROUND_TASKS",
    "RAG_MAX_TRACKED_WINDOWS",
    "MASAMONG_DISCORD_MAX_MESSAGES",
)
LOW_SPEC_LIMIT_MAXIMUMS = {
    "MASAMONG_CPU_THREADS": 64,
    "MASAMONG_EXECUTOR_WORKERS": 16,
    "AI_MAX_CONCURRENT_PROCESSING": 16,
    "AI_QUEUE_WAIT_TIMEOUT_SECONDS": 30,
    "LLM_MAX_CONCURRENT_CALLS": 16,
    "LLM_ACQUIRE_TIMEOUT_SECONDS": 60,
    "LLM_CALL_TIMEOUT_SECONDS": 300,
    "EMBEDDING_MAX_CONCURRENCY": 8,
    "RAG_MAX_BACKGROUND_TASKS": 64,
    "RAG_MAX_TRACKED_WINDOWS": 4_096,
    "KAKAO_API_MAX_CONCURRENCY": 16,
    "MASAMONG_DISCORD_MAX_MESSAGES": 1_000,
}
ENV_REFERENCE_PATTERN = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}"
)


def _load_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ValueError(f"env 파일을 찾을 수 없습니다: {path}")
    raw_values = {
        str(key): str(value or "").strip()
        for key, value in dotenv_values(path, interpolate=False).items()
        if key
    }
    resolved: dict[str, str] = {}

    def resolve_key(key: str, stack: tuple[str, ...] = ()) -> str:
        if key in resolved:
            return resolved[key]
        if key in stack:
            cycle = " -> ".join((*stack, key))
            raise ValueError(
                f"{path.name}: env 파일 내부 참조가 순환합니다: {cycle}"
            )
        value = raw_values[key]

        def replace_reference(match: re.Match[str]) -> str:
            referenced_key = match.group(1)
            default = match.group(2)
            if referenced_key in raw_values:
                referenced_value = resolve_key(
                    referenced_key,
                    (*stack, key),
                )
                if referenced_value or default is None:
                    return referenced_value
                return default or ""
            if default is not None:
                return default
            raise ValueError(
                f"{path.name}: env 파일 내부에 정의되지 않은 변수 참조입니다: "
                f"{referenced_key}"
            )

        expanded = ENV_REFERENCE_PATTERN.sub(replace_reference, value)
        resolved[key] = expanded
        return expanded

    for env_key in raw_values:
        resolve_key(env_key)
    return resolved


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return not value or any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def _truthy(value: str) -> bool:
    return value.lower() in TRUE_VALUES


def _positive_int(value: str) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _resolved_path(value: str) -> str:
    return str(Path(value).expanduser().resolve())


def _path_has_profile_marker(path: Path, profile: str) -> bool:
    return bool(
        profile
        and re.search(
            rf"(^|[^a-z0-9]){re.escape(profile)}([^a-z0-9]|$)",
            path.as_posix().lower(),
        )
    )


def _load_json_object(path_value: str, *, label: str, errors: list[str]) -> dict:
    if not path_value:
        return {}
    path = Path(path_value).expanduser()
    if not path.is_file():
        errors.append(f"{label}: 설정 파일을 찾을 수 없습니다.")
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        errors.append(f"{label}: 유효한 JSON 설정 파일이 아닙니다.")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{label}: JSON 최상위 값이 객체가 아닙니다.")
        return {}
    return value


def _db_identity(env: Mapping[str, str]) -> tuple[str, ...]:
    backend = env.get("MASAMONG_DB_BACKEND", "sqlite").lower()
    if backend == "tidb":
        return (
            "tidb",
            env.get("MASAMONG_DB_HOST", "").lower(),
            env.get("MASAMONG_DB_PORT", "4000"),
            env.get("MASAMONG_DB_NAME", ""),
        )
    return ("sqlite", str(Path(env.get("MASAMONG_DATABASE_FILE", "database/remasamong.db")).resolve()))


def _check_profile(
    label: str,
    env: Mapping[str, str],
    *,
    errors: list[str],
    warnings: list[str],
) -> None:
    profile = env.get("MASAMONG_PROFILE", "").lower()
    instance = env.get("MASAMONG_INSTANCE_NAME", "").lower()
    if not profile or profile == "legacy":
        errors.append(f"{label}: MASAMONG_PROFILE이 명시되지 않았습니다.")
    if not instance:
        errors.append(f"{label}: MASAMONG_INSTANCE_NAME이 명시되지 않았습니다.")
    if profile and instance and profile != instance:
        errors.append(f"{label}: profile과 instance 이름이 다릅니다.")

    if env.get("MASAMONG_REQUIRE_EXPLICIT_PROFILE", "").lower() != "true":
        errors.append(
            f"{label}: MASAMONG_REQUIRE_EXPLICIT_PROFILE=true를 "
            "literal로 명시해야 합니다."
        )

    token = env.get("DISCORD_BOT_TOKEN", "")
    if _is_placeholder(token):
        errors.append(f"{label}: 실제 Discord 토큰이 설정되지 않았습니다.")
    expected_bot_user_id = env.get("MASAMONG_EXPECTED_DISCORD_BOT_USER_ID", "")
    if _is_placeholder(expected_bot_user_id) or _positive_int(expected_bot_user_id) is None:
        errors.append(
            f"{label}: MASAMONG_EXPECTED_DISCORD_BOT_USER_ID가 "
            "실제 양의 정수 ID가 아닙니다."
        )

    backend = env.get("MASAMONG_DB_BACKEND", "sqlite").lower()
    if backend == "tidb":
        for key in (
            "MASAMONG_DB_HOST",
            "MASAMONG_DB_USER",
            "MASAMONG_DB_PASSWORD",
            "MASAMONG_DB_SSL_CA",
        ):
            if _is_placeholder(env.get(key, "")):
                errors.append(f"{label}: {key}가 누락됐거나 placeholder입니다.")
        db_name = env.get("MASAMONG_DB_NAME", "")
        expected_name = env.get("MASAMONG_EXPECTED_DB_NAME", "")
        if not db_name or db_name != expected_name:
            errors.append(f"{label}: DB 이름과 MASAMONG_EXPECTED_DB_NAME이 일치하지 않습니다.")
        profile_db_name = {
            "masamo": "masamong",
            "general": "masamong_general",
        }.get(profile)
        if profile_db_name and db_name != profile_db_name:
            errors.append(
                f"{label}: {profile} 프로필 DB 이름은 {profile_db_name}이어야 합니다."
            )
        if env.get("MASAMONG_DB_STRICT_REMOTE_ONLY", "").lower() not in {"1", "true", "yes", "on"}:
            errors.append(f"{label}: TiDB인데 strict remote 모드가 꺼져 있습니다.")
        if env.get("MASAMONG_DB_REQUIRE_TLS", "").lower() not in {"1", "true", "yes", "on"}:
            errors.append(f"{label}: TiDB TLS 필수 모드가 꺼져 있습니다.")
        ca_path = Path(env.get("MASAMONG_DB_SSL_CA", "")).expanduser()
        if not ca_path.is_absolute() or not ca_path.is_file():
            errors.append(
                f"{label}: TiDB CA는 존재하는 절대 파일이어야 합니다."
            )
        if env.get("MASAMONG_DB_SSL_VERIFY_IDENTITY", "").lower() not in {"1", "true", "yes", "on"}:
            errors.append(f"{label}: TiDB TLS hostname 검증이 꺼져 있습니다.")
        for key, expected_value in (
            ("DISCORD_EMBEDDING_BACKEND", "tidb"),
            ("KAKAO_STORE_BACKEND", "tidb"),
            ("DISCORD_EMBEDDING_TIDB_TABLE", "discord_chat_embeddings"),
            ("KAKAO_TIDB_TABLE", "kakao_chunks"),
        ):
            if env.get(key, "").lower() != expected_value:
                errors.append(
                    f"{label}: {key}는 {expected_value}여야 합니다."
                )
    elif backend == "sqlite":
        database_file = env.get("MASAMONG_DATABASE_FILE", "").strip()
        database_path = Path(database_file).expanduser()
        if (
            not database_file
            or database_file == ":memory:"
            or not database_path.is_absolute()
        ):
            errors.append(
                f"{label}: 명시적 SQLite의 MASAMONG_DATABASE_FILE은 "
                "프로필별 절대 파일 경로여야 합니다."
            )
        elif not _path_has_profile_marker(database_path, profile):
            errors.append(
                f"{label}: SQLite DB 경로에는 인스턴스 이름 "
                f"{profile!r}이 포함되어야 합니다."
            )
    else:
        errors.append(f"{label}: 지원하지 않는 DB backend입니다.")

    memory_sources = {
        item.strip().lower()
        for item in env.get("MASAMONG_MEMORY_SOURCES", "").split(",")
        if item.strip()
    }
    if profile == "general":
        if memory_sources != {"discord"}:
            errors.append(
                "general: Kakao 기억 소스는 허용되지 않으며 기억 소스는 정확히 discord여야 합니다."
            )
        for disabled_feature_key in (
            "AI_MEMORY_ENABLED",
            "EMBEDDING_ENABLED",
            "RERANK_ENABLED",
        ):
            if env.get(disabled_feature_key, "").lower() not in FALSE_VALUES:
                errors.append(
                    "general: 저사양 steady-state 보호를 위해 "
                    f"{disabled_feature_key}=false를 명시해야 합니다."
                )
    elif profile == "masamo" and memory_sources != {"discord", "kakao"}:
        errors.append("masamo: 기존 기억 보존을 위해 기억 소스는 discord,kakao여야 합니다.")

    required_cogs = {
        item.strip().lower()
        for item in env.get("MASAMONG_REQUIRED_COGS", "").split(",")
        if item.strip()
    }
    disabled_cogs = {
        item.strip().lower()
        for item in env.get("MASAMONG_DISABLED_COGS", "").split(",")
        if item.strip()
    }
    if not required_cogs:
        errors.append(f"{label}: MASAMONG_REQUIRED_COGS가 비어 있습니다.")
    elif not CORE_REQUIRED_COGS.issubset(required_cogs):
        missing = ", ".join(sorted(CORE_REQUIRED_COGS - required_cogs))
        errors.append(f"{label}: 필수 core Cog가 빠졌습니다: {missing}")
    if required_cogs & disabled_cogs:
        errors.append(
            f"{label}: 같은 Cog가 필수/비활성 목록에 동시에 있습니다."
        )
    auto_migrate = env.get("MASAMONG_AUTO_MIGRATE", "").lower()
    if profile == "masamo" and auto_migrate not in FALSE_VALUES:
        errors.append(
            "masamo: 누적 운영 데이터 보호를 위해 MASAMONG_AUTO_MIGRATE를 "
            "항상 명시적으로 false로 지정해야 합니다."
        )
    if profile == "general" and auto_migrate not in TRUE_VALUES | FALSE_VALUES:
        errors.append(
            "general: MASAMONG_AUTO_MIGRATE는 빈 DB bootstrap의 true 또는 "
            "정상 운영의 false여야 합니다."
        )
    elif profile == "general" and auto_migrate in TRUE_VALUES:
        warnings.append(
            "general: MASAMONG_AUTO_MIGRATE=true는 새 빈 DB bootstrap 전용입니다. "
            "schema 검증 후 정상 운영에서는 false로 전환하세요."
        )
    if profile == "general":
        for scheduler_key in (
            "ENABLE_RAIN_NOTIFICATION",
            "ENABLE_GREETING_NOTIFICATION",
            "ENABLE_EARTHQUAKE_ALERT",
            "FORTUNE_MORNING_BRIEFING_ENABLED",
            "RAG_ARCHIVING_ENABLED",
            "BM25_AUTO_REBUILD_ENABLED",
        ):
            if env.get(scheduler_key, "").lower() not in FALSE_VALUES:
                errors.append(
                    f"general: 첫 배포에서는 {scheduler_key}=false를 명시해야 합니다."
                )
    school_notice_enabled = env.get("SCHOOL_NOTICE_ENABLED", "").lower()
    if school_notice_enabled not in TRUE_VALUES | FALSE_VALUES:
        errors.append(
            f"{label}: SCHOOL_NOTICE_ENABLED=true 또는 false를 직접 명시해야 합니다."
        )
    elif school_notice_enabled in TRUE_VALUES:
        for path_key in (
            "SCHOOL_NOTICE_DIGEST_DIR",
            "SCHOOL_NOTICE_CORE_DB",
            "SCHOOL_NOTICE_CATALOG_PATH",
            "SCHOOL_NOTICE_SOURCE_CONFIG",
        ):
            configured_path = Path(env.get(path_key, "")).expanduser()
            if not configured_path.is_absolute():
                errors.append(
                    f"{label}: {path_key}는 절대 경로여야 합니다."
                )
                continue
            if (
                path_key
                in {"SCHOOL_NOTICE_DIGEST_DIR", "SCHOOL_NOTICE_CORE_DB"}
                and profile not in configured_path.parts
            ):
                errors.append(
                    f"{label}: {path_key} 경로에는 인스턴스 이름 "
                    f"{profile!r}이 독립 구성요소로 포함되어야 합니다."
                )
            if (
                path_key
                in {"SCHOOL_NOTICE_CATALOG_PATH", "SCHOOL_NOTICE_SOURCE_CONFIG"}
                and not configured_path.is_file()
            ):
                errors.append(f"{label}: {path_key} 파일을 찾을 수 없습니다.")
    if profile in {"masamo", "general"}:
        for limit_key in LOW_SPEC_POSITIVE_LIMIT_KEYS:
            parsed_limit = _positive_int(env.get(limit_key, ""))
            if parsed_limit is None:
                errors.append(
                    f"{label}: {limit_key}는 env 파일에 양의 정수로 명시해야 합니다."
                )
            elif parsed_limit > LOW_SPEC_LIMIT_MAXIMUMS[limit_key]:
                errors.append(
                    f"{label}: {limit_key}={parsed_limit}은 안전 상한 "
                    f"{LOW_SPEC_LIMIT_MAXIMUMS[limit_key]}를 넘습니다."
                )
        if profile == "masamo":
            kakao_limit = _positive_int(
                env.get("KAKAO_API_MAX_CONCURRENCY", "")
            )
            if kakao_limit is None:
                errors.append(
                    f"{label}: KAKAO_API_MAX_CONCURRENCY는 env 파일에 "
                    "양의 정수로 명시해야 합니다."
                )
            elif kakao_limit > LOW_SPEC_LIMIT_MAXIMUMS[
                "KAKAO_API_MAX_CONCURRENCY"
            ]:
                errors.append(
                    f"{label}: KAKAO_API_MAX_CONCURRENCY={kakao_limit}은 "
                    "안전 상한 16을 넘습니다."
                )
        if env.get("TOKENIZERS_PARALLELISM", "").lower() not in FALSE_VALUES:
            errors.append(
                f"{label}: TOKENIZERS_PARALLELISM=false를 env 파일에 명시해야 합니다."
            )
    # 명시적 프로필은 systemd/shell 상속값을 무시하므로, 지금 운영 중인 값이
    # env 파일 밖(예: systemd Environment=)에만 있으면 전환 후 조용히 사라진다.
    # 기동 시점에도 config가 같은 검사를 하지만, 배포 전에 먼저 드러내는 편이 낫다.
    if profile in {"masamo", "general"}:
        lane_key = "LLM_MAIN_PRIMARY_API_KEY"
        if not env.get(lane_key, "") and not env.get("COMETAPI_KEY", ""):
            errors.append(
                f"{label}: {lane_key} 또는 COMETAPI_KEY를 env 파일에 적어야 "
                "합니다. 명시적 프로필은 상속 환경을 사용하지 않습니다."
            )
        for credential_key in (lane_key, "COMETAPI_KEY", "KMA_API_KEY"):
            value = env.get(credential_key, "")
            if value and _is_placeholder(value):
                errors.append(
                    f"{label}: {credential_key}에 예제 placeholder가 남아 있습니다."
                )
        if "weather_cog" not in disabled_cogs and not env.get("KMA_API_KEY", ""):
            errors.append(
                f"{label}: weather_cog를 올리려면 KMA_API_KEY가 필요합니다. "
                "key가 없으면 MASAMONG_DISABLED_COGS에 weather_cog를 넣으세요."
            )
        linkup_active = (
            env.get("MASAMONG_WEB_SEARCH_PROVIDER", env.get("WEB_SEARCH_PROVIDER", "")).lower()
            == "linkup"
            and env.get("LINKUP_ENABLED", "true").lower() in TRUE_VALUES
        )
        if linkup_active and not env.get("LINKUP_API_KEY", ""):
            errors.append(
                f"{label}: WEB_SEARCH_PROVIDER=linkup인데 LINKUP_API_KEY가 없습니다."
            )
    if profile == "masamo" and env.get(
        "MASAMONG_GUILD_SETTINGS_MODE", ""
    ).lower() != "static":
        warnings.append(
            "masamo: stale guild_settings 감사 전에는 "
            "MASAMONG_GUILD_SETTINGS_MODE=static이 안전합니다."
        )


def validate(first_path: Path, second_path: Path) -> tuple[list[str], list[str]]:
    first = _load_env(first_path)
    second = _load_env(second_path)
    errors: list[str] = []
    warnings: list[str] = []

    _check_profile(first_path.name, first, errors=errors, warnings=warnings)
    _check_profile(second_path.name, second, errors=errors, warnings=warnings)

    first_profile = first.get("MASAMONG_PROFILE", "").lower()
    second_profile = second.get("MASAMONG_PROFILE", "").lower()
    if first_profile == second_profile:
        errors.append("두 env의 MASAMONG_PROFILE이 같습니다.")
    if {first_profile, second_profile} != {"masamo", "general"}:
        errors.append("두 env는 각각 masamo와 general 프로필이어야 합니다.")

    first_token = first.get("DISCORD_BOT_TOKEN", "")
    second_token = second.get("DISCORD_BOT_TOKEN", "")
    if first_token and first_token == second_token:
        errors.append("두 인스턴스의 Discord 토큰이 같습니다.")
    first_bot_user_id = first.get("MASAMONG_EXPECTED_DISCORD_BOT_USER_ID", "")
    second_bot_user_id = second.get("MASAMONG_EXPECTED_DISCORD_BOT_USER_ID", "")
    if (
        _positive_int(first_bot_user_id) is not None
        and _positive_int(first_bot_user_id) == _positive_int(second_bot_user_id)
    ):
        errors.append("두 인스턴스의 예상 Discord bot user ID가 같습니다.")

    if _db_identity(first) == _db_identity(second):
        errors.append("두 인스턴스의 DB 쓰기 대상이 같습니다.")

    first_user = first.get("MASAMONG_DB_USER", "")
    second_user = second.get("MASAMONG_DB_USER", "")
    if first_user and first_user == second_user:
        errors.append("두 TiDB 인스턴스가 같은 DB 계정을 사용합니다.")

    for path, env in ((first_path, first), (second_path, second)):
        configured_env_path = env.get("MASAMONG_ENV_FILE", "")
        if not configured_env_path:
            errors.append(f"{path.name}: MASAMONG_ENV_FILE이 없습니다.")
        elif _resolved_path(configured_env_path) != str(path.resolve()):
            errors.append(f"{path.name}: MASAMONG_ENV_FILE이 검사 중인 파일과 다릅니다.")

    for field in (
        "MASAMONG_LOG_FILE",
        "MASAMONG_ERROR_LOG_FILE",
        "MASAMONG_CONFIG_FILE",
        "EMB_CONFIG_PATH",
        "PROMPT_CONFIG_PATH",
    ):
        left = first.get(field, "")
        right = second.get(field, "")
        if not left or not right:
            errors.append(f"{field}: 두 프로필 모두 명시해야 합니다.")
        elif not Path(left).expanduser().is_absolute() or not Path(right).expanduser().is_absolute():
            errors.append(f"{field}: 두 프로필 모두 절대 경로여야 합니다.")
        elif _resolved_path(left) == _resolved_path(right):
            errors.append(f"{field}: 두 프로필의 경로가 같습니다.")

    first_config = _load_json_object(
        first.get("MASAMONG_CONFIG_FILE", ""),
        label=f"{first_path.name} config",
        errors=errors,
    )
    second_config = _load_json_object(
        second.get("MASAMONG_CONFIG_FILE", ""),
        label=f"{second_path.name} config",
        errors=errors,
    )
    del first_config, second_config
    first_emb = _load_json_object(
        first.get("EMB_CONFIG_PATH", ""),
        label=f"{first_path.name} embedding",
        errors=errors,
    )
    second_emb = _load_json_object(
        second.get("EMB_CONFIG_PATH", ""),
        label=f"{second_path.name} embedding",
        errors=errors,
    )

    emb_by_profile = {
        first_profile: first_emb,
        second_profile: second_emb,
    }
    general_emb = emb_by_profile.get("general", {})
    masamo_emb = emb_by_profile.get("masamo", {})
    if general_emb.get("kakao_servers"):
        errors.append("general embedding 설정에 Kakao mapping이 있습니다.")
    masamo_mappings = masamo_emb.get("kakao_servers")
    if not masamo_mappings:
        errors.append("masamo embedding 설정에 기존 Kakao mapping이 없습니다.")
    elif not isinstance(masamo_mappings, (list, dict)):
        errors.append("masamo embedding Kakao mapping 형식이 올바르지 않습니다.")
    else:
        mapping_items = (
            list(masamo_mappings.items())
            if isinstance(masamo_mappings, dict)
            else [(None, mapping) for mapping in masamo_mappings]
        )
        for index, (mapping_key, mapping) in enumerate(mapping_items):
            if not isinstance(mapping, dict):
                errors.append(f"masamo Kakao mapping #{index + 1}이 객체가 아닙니다.")
                continue
            for field, value in (
                ("server_id", str(mapping.get("server_id") or mapping_key or "")),
                ("room_key", str(mapping.get("room_key") or "")),
            ):
                if _is_placeholder(value):
                    errors.append(
                        f"masamo Kakao mapping #{index + 1}의 {field}가 "
                        "누락됐거나 placeholder입니다."
                    )

    effective_embedding_paths: dict[str, dict[str, str]] = {}
    for profile, env, emb in (
        (first_profile, first, first_emb),
        (second_profile, second, second_emb),
    ):
        effective_embedding_paths[profile] = {
            "discord_db_path": env.get("DISCORD_EMBEDDING_DB_PATH", "")
            or str(emb.get("discord_db_path") or ""),
            "kakao_db_path": env.get("KAKAO_EMBEDDING_DB_PATH", "")
            or str(emb.get("kakao_db_path") or ""),
        }
        for field, value in effective_embedding_paths[profile].items():
            if not value:
                errors.append(f"{profile}: 실제 embedding {field}가 비어 있습니다.")
            elif not Path(value).expanduser().is_absolute():
                errors.append(
                    f"{profile}: 실제 embedding {field}는 절대 경로여야 합니다."
                )
    for field in ("discord_db_path", "kakao_db_path"):
        left = effective_embedding_paths.get(first_profile, {}).get(field, "")
        right = effective_embedding_paths.get(second_profile, {}).get(field, "")
        if left and right and _resolved_path(left) == _resolved_path(right):
            errors.append(f"embedding 실제 {field}가 두 프로필에서 같습니다.")

    prompt_objects: dict[str, dict] = {}
    for path, env in ((first_path, first), (second_path, second)):
        prompt_objects[env.get("MASAMONG_PROFILE", "").lower()] = _load_json_object(
            env.get("PROMPT_CONFIG_PATH", ""),
            label=f"{path.name} prompt",
            errors=errors,
        )
    allowed_prompt_channels: dict[str, set[int]] = {}
    for profile, prompt in prompt_objects.items():
        channels = prompt.get("channels", {})
        allowed: set[int] = set()
        if isinstance(channels, dict):
            for channel_id, metadata in channels.items():
                if isinstance(metadata, dict) and _truthy(
                    str(metadata.get("allowed", ""))
                ):
                    try:
                        allowed.add(int(channel_id))
                    except (TypeError, ValueError):
                        errors.append(
                            f"{profile}: prompt에 유효하지 않은 channel ID가 있습니다."
                        )
        fallback_channels = (
            first if profile == first_profile else second
        ).get("DEFAULT_AI_CHANNELS", "")
        for channel_id in fallback_channels.split(","):
            if not channel_id.strip():
                continue
            try:
                allowed.add(int(channel_id.strip()))
            except ValueError:
                errors.append(
                    f"{profile}: DEFAULT_AI_CHANNELS에 유효하지 않은 ID가 있습니다."
                )
        allowed_prompt_channels[profile] = allowed
    shared_channels = (
        allowed_prompt_channels.get("masamo", set())
        & allowed_prompt_channels.get("general", set())
    )
    if shared_channels:
        errors.append(
            "두 프로필의 AI 허용 Discord channel ID가 겹칩니다: "
            + ", ".join(str(item) for item in sorted(shared_channels))
        )

    if first.get("MASAMONG_COMMAND_PREFIX") == second.get("MASAMONG_COMMAND_PREFIX"):
        warnings.append("명령 prefix가 같습니다. 같은 Discord guild에 둘 다 들어가면 중복 응답할 수 있습니다.")

    first_ops_channel = first.get("MASAMONG_OPERATIONS_LOG_CHANNEL_ID", "0")
    second_ops_channel = second.get("MASAMONG_OPERATIONS_LOG_CHANNEL_ID", "0")
    if first_ops_channel not in {"", "0"} and first_ops_channel == second_ops_channel:
        warnings.append("두 인스턴스가 같은 Discord 운영 로그 채널을 사용합니다.")

    for shared_key in ("COMETAPI_KEY", "LINKUP_API_KEY"):
        left = first.get(shared_key, "")
        right = second.get(shared_key, "")
        if left and left == right and not _is_placeholder(left):
            warnings.append(f"{shared_key}: 공급자 키/예산을 공유합니다.")

    return errors, warnings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("first_env", type=Path)
    parser.add_argument("second_env", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        errors, warnings = validate(args.first_env.resolve(), args.second_env.resolve())
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2

    for warning in warnings:
        print(f"WARNING: {warning}")
    for error in errors:
        print(f"ERROR: {error}")
    if errors:
        print(f"FAILED: errors={len(errors)} warnings={len(warnings)}")
        return 2
    print(f"OK: profile boundary checks passed (warnings={len(warnings)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _profile_env(tmp_path: Path, *, memory_sources: str = "discord") -> Path:
    config_path = tmp_path / "config.json"
    emb_path = tmp_path / "emb.json"
    prompt_path = tmp_path / "prompts.json"
    profile_path = tmp_path / "general.env"
    config_path.write_text("{}", encoding="utf-8")
    emb_path.write_text(
        json.dumps(
            {
                "embedding_enabled": False,
                "enable_local_embeddings": False,
                "discord_db_path": str(tmp_path / "general" / "discord.db"),
                "kakao_db_path": str(tmp_path / "general" / "kakao.db"),
                "kakao_servers": [],
            }
        ),
        encoding="utf-8",
    )
    prompt_path.write_text("{}", encoding="utf-8")
    profile_path.write_text(
        "\n".join(
            [
                "MASAMONG_PROFILE=general",
                "MASAMONG_INSTANCE_NAME=general",
                "MASAMONG_REQUIRE_EXPLICIT_PROFILE=true",
                f"MASAMONG_ENV_FILE={profile_path}",
                f"MASAMONG_CONFIG_FILE={config_path}",
                f"EMB_CONFIG_PATH={emb_path}",
                f"PROMPT_CONFIG_PATH={prompt_path}",
                "MASAMONG_LOG_FILE=/dev/null",
                "MASAMONG_ERROR_LOG_FILE=/dev/null",
                "MASAMONG_REQUIRED_COGS=tools_cog,events,ai_handler",
                "MASAMONG_AUTO_MIGRATE=false",
                "MASAMONG_DB_BACKEND=sqlite",
                f"MASAMONG_DATABASE_FILE={tmp_path / 'general' / 'main.db'}",
                f"MASAMONG_MEMORY_SOURCES={memory_sources}",
                "DISCORD_EMBEDDING_BACKEND=sqlite",
                "KAKAO_STORE_BACKEND=local",
                "AI_MEMORY_ENABLED=false",
                "EMBEDDING_ENABLED=false",
                "RERANK_ENABLED=false",
                "SCHOOL_NOTICE_ENABLED=false",
                "DISCORD_BOT_TOKEN=general-token",
                "MASAMONG_EXPECTED_DISCORD_BOT_USER_ID=replace-with-current-masamo-bot-user-id",
                # 명시적 프로필은 켜 둔 기능의 자격증명을 기동 시점에 요구한다.
                # LINKUP_API_KEY는 비워 두어 provider가 legacy로 정해지게 한다.
                "COMETAPI_KEY=test-cometapi-key",
                "KMA_API_KEY=test-kma-key",
            ]
        ),
        encoding="utf-8",
    )
    return profile_path


def _masamo_profile_env(tmp_path: Path) -> Path:
    profile_path = _profile_env(tmp_path, memory_sources="discord,kakao")
    profile_text = profile_path.read_text(encoding="utf-8")
    profile_text = profile_text.replace(
        "MASAMONG_PROFILE=general",
        "MASAMONG_PROFILE=masamo",
    ).replace(
        "MASAMONG_INSTANCE_NAME=general",
        "MASAMONG_INSTANCE_NAME=masamo",
    ).replace(
        str(tmp_path / "general" / "main.db"),
        str(tmp_path / "masamo" / "main.db"),
    )
    profile_text += "\n" + "\n".join(
        [
            "MASAMONG_CPU_THREADS=1",
            "MASAMONG_EXECUTOR_WORKERS=1",
            "AI_MAX_CONCURRENT_PROCESSING=1",
            "AI_QUEUE_WAIT_TIMEOUT_SECONDS=5",
            "LLM_MAX_CONCURRENT_CALLS=1",
            "LLM_ACQUIRE_TIMEOUT_SECONDS=10",
            "LLM_CALL_TIMEOUT_SECONDS=120",
            "EMBEDDING_MAX_CONCURRENCY=1",
            "RAG_MAX_BACKGROUND_TASKS=2",
            "RAG_MAX_TRACKED_WINDOWS=64",
            "KAKAO_API_MAX_CONCURRENCY=1",
            "MASAMONG_DISCORD_MAX_MESSAGES=200",
            "SCHOOL_NOTICE_ENABLED=false",
            "TOKENIZERS_PARALLELISM=false",
        ]
    )
    profile_path.write_text(profile_text, encoding="utf-8")

    emb_path = tmp_path / "emb.json"
    emb_config = json.loads(emb_path.read_text(encoding="utf-8"))
    emb_config["kakao_servers"] = [
        {"server_id": "1", "room_key": "masamo-room"}
    ]
    emb_path.write_text(json.dumps(emb_config), encoding="utf-8")
    return profile_path


def _convert_profile_to_tidb(
    profile_path: Path,
    *,
    database_name: str,
) -> None:
    ca_path = profile_path.parent / "ca.pem"
    ca_path.write_text("test-ca", encoding="utf-8")
    profile_text = profile_path.read_text(encoding="utf-8")
    profile_text = profile_text.replace(
        "MASAMONG_DB_BACKEND=sqlite",
        "\n".join(
            [
                "MASAMONG_DB_BACKEND=tidb",
                "MASAMONG_DB_HOST=db.internal.local",
                "MASAMONG_DB_PORT=4000",
                f"MASAMONG_DB_NAME={database_name}",
                f"MASAMONG_EXPECTED_DB_NAME={database_name}",
                "MASAMONG_DB_USER=general_user",
                "MASAMONG_DB_PASSWORD=test-password",
                f"MASAMONG_DB_SSL_CA={ca_path}",
                "MASAMONG_DB_SSL_VERIFY_IDENTITY=true",
                "MASAMONG_DB_REQUIRE_TLS=true",
                "MASAMONG_DB_STRICT_REMOTE_ONLY=true",
            ]
        ),
    )
    profile_text = profile_text.replace(
        "DISCORD_EMBEDDING_BACKEND=sqlite",
        "DISCORD_EMBEDDING_BACKEND=tidb",
    ).replace(
        "KAKAO_STORE_BACKEND=local",
        "KAKAO_STORE_BACKEND=tidb",
    )
    profile_path.write_text(profile_text, encoding="utf-8")


def test_explicit_profile_file_overrides_inherited_masamo_values(tmp_path):
    profile_path = _profile_env(tmp_path)
    env = os.environ.copy()
    env.update(
        {
            "MASAMONG_ENV_FILE": str(profile_path),
            "MASAMONG_PROFILE": "masamo",
            "MASAMONG_INSTANCE_NAME": "masamo",
            "MASAMONG_MEMORY_SOURCES": "discord,kakao",
            "MASAMONG_DB_BACKEND": "tidb",
            "MASAMONG_DB_NAME": "masamong",
        }
    )
    code = (
        "import json, config; "
        "print(json.dumps({'profile': config.PROFILE, "
        "'backend': config.DB_BACKEND, "
        "'sources': sorted(config.MEMORY_SOURCES)}))"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    )

    loaded = json.loads(result.stdout.strip().splitlines()[-1])
    assert loaded == {
        "profile": "general",
        "backend": "sqlite",
        "sources": ["discord"],
    }


def test_general_profile_rejects_kakao_memory(tmp_path):
    profile_path = _profile_env(tmp_path, memory_sources="discord,kakao")
    env = os.environ.copy()
    env["MASAMONG_ENV_FILE"] = str(profile_path)

    result = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )

    assert result.returncode != 0
    assert "정확히 discord" in result.stderr


def test_masamo_runtime_accepts_owned_school_notice_paths(tmp_path):
    profile_path = _masamo_profile_env(tmp_path)
    catalog_path = ROOT / "profiles" / "catalogs" / "school_notice_catalog.v1.json"
    source_path = ROOT / "school_notice" / "sources.json"
    profile_path.write_text(
        profile_path.read_text(encoding="utf-8").replace(
            "SCHOOL_NOTICE_ENABLED=false",
            "\n".join(
                [
                    "SCHOOL_NOTICE_ENABLED=true",
                    f"SCHOOL_NOTICE_DIGEST_DIR={tmp_path / 'masamo' / 'notice' / 'out'}",
                    f"SCHOOL_NOTICE_CORE_DB={tmp_path / 'masamo' / 'notice' / 'core.db'}",
                    f"SCHOOL_NOTICE_CATALOG_PATH={catalog_path}",
                    f"SCHOOL_NOTICE_SOURCE_CONFIG={source_path}",
                ]
            ),
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["MASAMONG_ENV_FILE"] = str(profile_path)

    result = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr


def test_masamo_runtime_rejects_cross_instance_school_notice_paths(tmp_path):
    profile_path = _masamo_profile_env(tmp_path)
    catalog_path = ROOT / "profiles" / "catalogs" / "school_notice_catalog.v1.json"
    source_path = ROOT / "school_notice" / "sources.json"
    profile_path.write_text(
        profile_path.read_text(encoding="utf-8").replace(
            "SCHOOL_NOTICE_ENABLED=false",
            "\n".join(
                [
                    "SCHOOL_NOTICE_ENABLED=true",
                    f"SCHOOL_NOTICE_DIGEST_DIR={tmp_path / 'general' / 'notice' / 'out'}",
                    f"SCHOOL_NOTICE_CORE_DB={tmp_path / 'general' / 'notice' / 'core.db'}",
                    f"SCHOOL_NOTICE_CATALOG_PATH={catalog_path}",
                    f"SCHOOL_NOTICE_SOURCE_CONFIG={source_path}",
                ]
            ),
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["MASAMONG_ENV_FILE"] = str(profile_path)

    result = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )

    assert result.returncode != 0
    assert "인스턴스 이름 'masamo'" in result.stderr


def test_explicit_profile_rejects_non_object_config_json(tmp_path):
    profile_path = _profile_env(tmp_path)
    (tmp_path / "config.json").write_text("[]", encoding="utf-8")
    env = os.environ.copy()
    env["MASAMONG_ENV_FILE"] = str(profile_path)

    result = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )

    assert result.returncode != 0
    assert "config 파일 오류" in result.stderr


def test_explicit_profile_requires_dedicated_embedding_and_prompt_paths(tmp_path):
    for missing_key in ("EMB_CONFIG_PATH", "PROMPT_CONFIG_PATH"):
        case_dir = tmp_path / missing_key.lower()
        case_dir.mkdir()
        profile_path = _profile_env(case_dir)
        filtered_lines = [
            line
            for line in profile_path.read_text(encoding="utf-8").splitlines()
            if not line.startswith(f"{missing_key}=")
        ]
        profile_path.write_text("\n".join(filtered_lines), encoding="utf-8")
        env = os.environ.copy()
        env["MASAMONG_ENV_FILE"] = str(profile_path)

        result = subprocess.run(
            [sys.executable, "-c", "import config"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
        )

        assert result.returncode != 0
        assert f"{missing_key} 미지정" in result.stderr


def test_explicit_profile_interpolates_only_values_from_selected_file(tmp_path):
    profile_path = _profile_env(tmp_path)
    profile_path.write_text(
        profile_path.read_text(encoding="utf-8")
        + "\n"
        + "\n".join(
            [
                "NANOGPT_KEY=profile-key",
                "NANOGPT_BASE_URL=https://profile.example/v1",
                "LLM_MAIN_PRIMARY_API_KEY=${NANOGPT_KEY}",
                "LLM_MAIN_PRIMARY_BASE_URL=${NANOGPT_BASE_URL}",
                "GEMINI_API_KEY=",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "config.json").write_text(
        json.dumps({"GEMINI_API_KEY": "must-not-be-restored"}),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "MASAMONG_ENV_FILE": str(profile_path),
            "NANOGPT_KEY": "inherited-key",
            "NANOGPT_BASE_URL": "https://inherited.invalid/v1",
            "GEMINI_API_KEY": "inherited-gemini-key",
            "MASAMONG_ANALYTICS_STORE_CONTENT": "true",
        }
    )
    code = (
        "import json, config; "
        "print(json.dumps({"
        "'api_key': config.LLM_MAIN_PRIMARY_API_KEY, "
        "'base_url': config.LLM_MAIN_PRIMARY_BASE_URL, "
        "'gemini_key': config.GEMINI_API_KEY, "
        "'analytics_content': config.ANALYTICS_STORE_CONTENT"
        "}))"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    )

    loaded = json.loads(result.stdout.strip().splitlines()[-1])
    assert loaded == {
        "api_key": "profile-key",
        "base_url": "https://profile.example/v1",
        "gemini_key": "",
        "analytics_content": False,
    }


def test_explicit_profile_rejects_unresolved_file_local_reference_without_leak(
    tmp_path,
):
    profile_path = _profile_env(tmp_path)
    secret_value = "must-not-appear-in-error-output"
    profile_path.write_text(
        profile_path.read_text(encoding="utf-8")
        + "\nOPTIONAL_PROVIDER_KEY=${INHERITED_PROVIDER_SECRET}\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "MASAMONG_ENV_FILE": str(profile_path),
            "INHERITED_PROVIDER_SECRET": secret_value,
        }
    )

    result = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )

    assert result.returncode != 0
    assert "INHERITED_PROVIDER_SECRET" in result.stderr
    assert secret_value not in result.stderr


@pytest.mark.parametrize(
    "feature_key",
    [
        "AI_MEMORY_ENABLED",
        "EMBEDDING_ENABLED",
        "RERANK_ENABLED",
    ],
)
def test_explicit_general_missing_memory_feature_flag_fails_safe_to_false(
    tmp_path,
    feature_key,
):
    profile_path = _profile_env(tmp_path)
    profile_path.write_text(
        "\n".join(
            line
            for line in profile_path.read_text(encoding="utf-8").splitlines()
            if not line.startswith(f"{feature_key}=")
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["MASAMONG_ENV_FILE"] = str(profile_path)
    config_attribute = {
        "AI_MEMORY_ENABLED": "AI_MEMORY_ENABLED",
        "EMBEDDING_ENABLED": "EMBEDDING_ENABLED",
        "RERANK_ENABLED": "RERANK_ENABLED",
    }[feature_key]

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import config; print(config.{config_attribute})",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    )

    assert result.stdout.strip().splitlines()[-1] == "False"


@pytest.mark.parametrize(
    "feature_key",
    [
        "AI_MEMORY_ENABLED",
        "EMBEDDING_ENABLED",
        "RERANK_ENABLED",
    ],
)
def test_explicit_general_rejects_enabled_low_spec_memory_feature(
    tmp_path,
    feature_key,
):
    profile_path = _profile_env(tmp_path)
    profile_path.write_text(
        "\n".join(
            (
                f"{feature_key}=true"
                if line.startswith(f"{feature_key}=")
                else line
            )
            for line in profile_path.read_text(encoding="utf-8").splitlines()
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["MASAMONG_ENV_FILE"] = str(profile_path)

    result = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )

    assert result.returncode != 0
    assert feature_key in result.stderr
    assert "steady-state" in result.stderr


@pytest.mark.parametrize(
    "feature_key",
    [
        "AI_MEMORY_ENABLED",
        "EMBEDDING_ENABLED",
        "RERANK_ENABLED",
    ],
)
def test_explicit_general_config_json_cannot_reenable_low_spec_memory_feature(
    tmp_path,
    feature_key,
):
    profile_path = _profile_env(tmp_path)
    profile_path.write_text(
        "\n".join(
            line
            for line in profile_path.read_text(encoding="utf-8").splitlines()
            if not line.startswith(f"{feature_key}=")
        ),
        encoding="utf-8",
    )
    (tmp_path / "config.json").write_text(
        json.dumps({feature_key: True}),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["MASAMONG_ENV_FILE"] = str(profile_path)

    result = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )

    assert result.returncode != 0
    assert feature_key in result.stderr
    assert "steady-state" in result.stderr


def test_explicit_profile_rejects_declared_env_path_mismatch(tmp_path):
    profile_path = _profile_env(tmp_path)
    profile_path.write_text(
        profile_path.read_text(encoding="utf-8").replace(
            f"MASAMONG_ENV_FILE={profile_path}",
            f"MASAMONG_ENV_FILE={tmp_path / 'other.env'}",
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["MASAMONG_ENV_FILE"] = str(profile_path)

    result = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )

    assert result.returncode != 0
    assert "프로필 파일 내부 경로가 다릅니다" in result.stderr


def test_explicit_profile_rejects_profile_instance_mismatch(tmp_path):
    profile_path = _profile_env(tmp_path)
    profile_path.write_text(
        profile_path.read_text(encoding="utf-8").replace(
            "MASAMONG_INSTANCE_NAME=general",
            "MASAMONG_INSTANCE_NAME=masamo",
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["MASAMONG_ENV_FILE"] = str(profile_path)

    result = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )

    assert result.returncode != 0
    assert "MASAMONG_PROFILE과 MASAMONG_INSTANCE_NAME이 다릅니다" in result.stderr


def test_explicit_general_tidb_rejects_noncanonical_database_name(tmp_path):
    profile_path = _profile_env(tmp_path)
    _convert_profile_to_tidb(
        profile_path,
        database_name="some_other_database",
    )
    env = os.environ.copy()
    env["MASAMONG_ENV_FILE"] = str(profile_path)

    result = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )

    assert result.returncode != 0
    assert "고정 경계와 다릅니다" in result.stderr


def test_explicit_general_tidb_accepts_canonical_strict_tls_profile(tmp_path):
    profile_path = _profile_env(tmp_path)
    _convert_profile_to_tidb(
        profile_path,
        database_name="masamong_general",
    )
    env = os.environ.copy()
    env["MASAMONG_ENV_FILE"] = str(profile_path)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import config; print(config.TIDB_NAME)",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    )

    assert result.stdout.strip().splitlines()[-1] == "masamong_general"


def test_explicit_masamo_tidb_keeps_canonical_strict_profile_compatible(
    tmp_path,
):
    profile_path = _masamo_profile_env(tmp_path)
    _convert_profile_to_tidb(
        profile_path,
        database_name="masamong",
    )
    env = os.environ.copy()
    env["MASAMONG_ENV_FILE"] = str(profile_path)
    code = (
        "import json, config; print(json.dumps({"
        "'profile': config.PROFILE, "
        "'database': config.TIDB_NAME, "
        "'strict': config.REMOTE_DB_STRICT_MODE, "
        "'tls': config.REQUIRE_DB_TLS"
        "}))"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    )

    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "profile": "masamo",
        "database": "masamong",
        "strict": True,
        "tls": True,
    }


@pytest.mark.parametrize(
    ("original", "replacement", "expected_error"),
    [
        (
            "DISCORD_EMBEDDING_BACKEND=sqlite",
            "DISCORD_EMBEDDING_BACKEND=typo",
            "DISCORD_EMBEDDING_BACKEND는 sqlite 또는 tidb",
        ),
        (
            "KAKAO_STORE_BACKEND=local",
            "KAKAO_STORE_BACKEND=typo",
            "KAKAO_STORE_BACKEND는 local 또는 tidb",
        ),
    ],
)
def test_explicit_profile_rejects_unknown_storage_backend(
    tmp_path,
    original,
    replacement,
    expected_error,
):
    profile_path = _profile_env(tmp_path)
    profile_path.write_text(
        profile_path.read_text(encoding="utf-8").replace(original, replacement),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["MASAMONG_ENV_FILE"] = str(profile_path)

    result = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )

    assert result.returncode != 0
    assert expected_error in result.stderr


def test_general_profile_enforces_low_spec_defaults_over_inherited_threads(
    tmp_path,
):
    profile_path = _profile_env(tmp_path)
    env = os.environ.copy()
    env.update(
        {
            "MASAMONG_ENV_FILE": str(profile_path),
            "OMP_NUM_THREADS": "64",
            "TOKENIZERS_PARALLELISM": "true",
        }
    )
    code = (
        "import json, os, config; "
        "print(json.dumps({"
        "'cpu_threads': config.CPU_THREAD_LIMIT, "
        "'omp_threads': os.environ.get('OMP_NUM_THREADS'), "
        "'tokenizers_parallelism': os.environ.get('TOKENIZERS_PARALLELISM'), "
        "'ai_concurrency': config.AI_MAX_CONCURRENT_PROCESSING, "
        "'rag_tasks': config.RAG_MAX_BACKGROUND_TASKS, "
        "'rag_windows': config.RAG_MAX_TRACKED_WINDOWS, "
        "'members_intent': config.MEMBERS_INTENT_ENABLED, "
        "'member_cache': config.MEMBER_CACHE_ENABLED"
        "}))"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    )

    loaded = json.loads(result.stdout.strip().splitlines()[-1])
    assert loaded == {
        "cpu_threads": 1,
        "omp_threads": "1",
        "tokenizers_parallelism": "false",
        "ai_concurrency": 1,
        "rag_tasks": 2,
        "rag_windows": 64,
        "members_intent": False,
        "member_cache": False,
    }


def test_member_cache_cannot_enable_without_members_intent(tmp_path):
    profile_path = _profile_env(tmp_path)
    profile_path.write_text(
        profile_path.read_text(encoding="utf-8")
        + "\nMASAMONG_MEMBER_CACHE_ENABLED=true\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["MASAMONG_ENV_FILE"] = str(profile_path)

    result = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )

    assert result.returncode != 0
    assert "MASAMONG_MEMBERS_INTENT_ENABLED=true" in result.stderr


@pytest.mark.parametrize(
    "missing_key",
    [
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
        "KAKAO_API_MAX_CONCURRENCY",
        "MASAMONG_DISCORD_MAX_MESSAGES",
        "TOKENIZERS_PARALLELISM",
    ],
)
def test_explicit_masamo_requires_each_low_spec_limit_in_profile_file(
    tmp_path,
    missing_key,
):
    profile_path = _masamo_profile_env(tmp_path)
    profile_path.write_text(
        "\n".join(
            line
            for line in profile_path.read_text(encoding="utf-8").splitlines()
            if not line.startswith(f"{missing_key}=")
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["MASAMONG_ENV_FILE"] = str(profile_path)

    result = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )

    assert result.returncode != 0
    assert missing_key in result.stderr
    assert "현재 운영 서버의 저사양 제한값" in result.stderr


@pytest.mark.parametrize(
    ("key", "invalid_value"),
    [
        ("MASAMONG_CPU_THREADS", "0"),
        ("AI_MAX_CONCURRENT_PROCESSING", "not-a-number"),
        ("TOKENIZERS_PARALLELISM", "true"),
        ("MASAMONG_CPU_THREADS", "99999"),
        ("MASAMONG_EXECUTOR_WORKERS", "99999"),
        ("AI_MAX_CONCURRENT_PROCESSING", "99999"),
        ("AI_QUEUE_WAIT_TIMEOUT_SECONDS", "99999"),
        ("LLM_MAX_CONCURRENT_CALLS", "99999"),
        ("LLM_ACQUIRE_TIMEOUT_SECONDS", "99999"),
        ("LLM_CALL_TIMEOUT_SECONDS", "99999"),
        ("EMBEDDING_MAX_CONCURRENCY", "99999"),
        ("RAG_MAX_BACKGROUND_TASKS", "99999"),
        ("RAG_MAX_TRACKED_WINDOWS", "99999"),
        ("KAKAO_API_MAX_CONCURRENCY", "99999"),
        ("MASAMONG_DISCORD_MAX_MESSAGES", "99999"),
    ],
)
def test_explicit_masamo_rejects_unsafe_low_spec_limit(
    tmp_path,
    key,
    invalid_value,
):
    profile_path = _masamo_profile_env(tmp_path)
    profile_path.write_text(
        "\n".join(
            (
                f"{key}={invalid_value}"
                if line.startswith(f"{key}=")
                else line
            )
            for line in profile_path.read_text(encoding="utf-8").splitlines()
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["MASAMONG_ENV_FILE"] = str(profile_path)

    result = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )

    assert result.returncode != 0
    assert key in result.stderr


def test_explicit_masamo_uses_profile_low_spec_limits(tmp_path):
    profile_path = _masamo_profile_env(tmp_path)
    env = os.environ.copy()
    env.update(
        {
            "MASAMONG_ENV_FILE": str(profile_path),
            "AI_MAX_CONCURRENT_PROCESSING": "99",
            "RAG_MAX_TRACKED_WINDOWS": "999",
            "TOKENIZERS_PARALLELISM": "true",
        }
    )
    code = (
        "import json, os, config; "
        "print(json.dumps({"
        "'cpu': config.CPU_THREAD_LIMIT, "
        "'ai': config.AI_MAX_CONCURRENT_PROCESSING, "
        "'embedding': config.EMBEDDING_MAX_CONCURRENCY, "
        "'rag_tasks': config.RAG_MAX_BACKGROUND_TASKS, "
        "'rag_windows': config.RAG_MAX_TRACKED_WINDOWS, "
        "'tokenizers': os.environ.get('TOKENIZERS_PARALLELISM')"
        "}))"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    )

    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "cpu": 1,
        "ai": 1,
        "embedding": 1,
        "rag_tasks": 2,
        "rag_windows": 64,
        "tokenizers": "false",
    }


@pytest.mark.parametrize(
    ("replacement", "expected_error"),
    [
        ("", "MASAMONG_REQUIRE_EXPLICIT_PROFILE=true"),
        (
            "MASAMONG_REQUIRE_EXPLICIT_PROFILE=1",
            "MASAMONG_REQUIRE_EXPLICIT_PROFILE=true",
        ),
        (
            "MASAMONG_REQUIRE_EXPLICIT_PROFILE=typo",
            "true 또는 false 계열 값",
        ),
    ],
)
def test_named_operational_profile_cannot_bypass_strict_mode(
    tmp_path,
    replacement,
    expected_error,
):
    profile_path = _profile_env(tmp_path)
    profile_path.write_text(
        profile_path.read_text(encoding="utf-8").replace(
            "MASAMONG_REQUIRE_EXPLICIT_PROFILE=true",
            replacement,
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["MASAMONG_ENV_FILE"] = str(profile_path)

    result = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )

    assert result.returncode != 0
    assert expected_error in result.stderr


@pytest.mark.parametrize(
    ("database_file", "expected_error"),
    [
        ("database/shared.db", "절대 경로"),
        ("/var/lib/masamong/shared.db", "인스턴스 이름"),
        (":memory:", "재시작 시 사라지는"),
    ],
)
def test_explicit_sqlite_profile_rejects_shared_database_path(
    tmp_path,
    database_file,
    expected_error,
):
    profile_path = _profile_env(tmp_path)
    current_database_file = tmp_path / "general" / "main.db"
    profile_path.write_text(
        profile_path.read_text(encoding="utf-8").replace(
            f"MASAMONG_DATABASE_FILE={current_database_file}",
            f"MASAMONG_DATABASE_FILE={database_file}",
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["MASAMONG_ENV_FILE"] = str(profile_path)

    result = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )

    assert result.returncode != 0
    assert expected_error in result.stderr


def test_explicit_sqlite_profile_accepts_profile_owned_database_path(tmp_path):
    profile_path = _profile_env(tmp_path)
    database_file = tmp_path / "general" / "main.db"
    env = os.environ.copy()
    env["MASAMONG_ENV_FILE"] = str(profile_path)

    result = subprocess.run(
        [sys.executable, "-c", "import config; print(config.DATABASE_FILE)"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    )

    assert result.stdout.strip().splitlines()[-1] == str(database_file)


def test_background_polling_intervals_never_become_busy_loops(tmp_path):
    profile_path = _profile_env(tmp_path)
    profile_path.write_text(
        profile_path.read_text(encoding="utf-8")
        + "\n"
        + "\n".join(
            [
                "BM25_AUTO_REBUILD_IDLE_MINUTES=0",
                "BM25_AUTO_REBUILD_POLL_MINUTES=0",
                "RAG_ARCHIVE_INTERVAL_HOURS=0",
                "WEATHER_CHECK_INTERVAL_MINUTES=0",
            ]
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["MASAMONG_ENV_FILE"] = str(profile_path)
    code = (
        "import json, config; print(json.dumps({"
        "'idle': config.BM25_AUTO_REBUILD_CONFIG['idle_minutes'], "
        "'poll': config.BM25_AUTO_REBUILD_CONFIG['poll_minutes'], "
        "'archive': config.RAG_ARCHIVING_CONFIG['check_interval_hours'], "
        "'weather': config.WEATHER_CHECK_INTERVAL_MINUTES"
        "}))"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    )

    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "idle": 1,
        "poll": 1,
        "archive": 1,
        "weather": 1,
    }


@pytest.mark.parametrize(
    ("replacement", "expected_error"),
    [
        ("", "MASAMONG_AUTO_MIGRATE"),
        (
            "MASAMONG_AUTO_MIGRATE=true",
            "masamo 프로필에서는 런타임 MASAMONG_AUTO_MIGRATE=true",
        ),
    ],
)
def test_explicit_masamo_requires_runtime_auto_migrate_false(
    tmp_path,
    replacement,
    expected_error,
):
    profile_path = _masamo_profile_env(tmp_path)
    profile_path.write_text(
        profile_path.read_text(encoding="utf-8").replace(
            "MASAMONG_AUTO_MIGRATE=false",
            replacement,
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["MASAMONG_ENV_FILE"] = str(profile_path)

    result = subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )

    assert result.returncode != 0
    assert expected_error in result.stderr


@pytest.mark.parametrize("auto_migrate", ["true", "false"])
def test_explicit_general_accepts_bootstrap_and_steady_state_migration_modes(
    tmp_path,
    auto_migrate,
):
    profile_path = _profile_env(tmp_path)
    profile_path.write_text(
        profile_path.read_text(encoding="utf-8").replace(
            "MASAMONG_AUTO_MIGRATE=false",
            f"MASAMONG_AUTO_MIGRATE={auto_migrate}",
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["MASAMONG_ENV_FILE"] = str(profile_path)

    result = subprocess.run(
        [sys.executable, "-c", "import config; print(config.AUTO_MIGRATE)"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    )

    assert result.stdout.strip().splitlines()[-1] == str(
        auto_migrate == "true"
    )


def _strip_env_key(profile_path: Path, key: str) -> None:
    """선택한 프로필 env 파일에서 특정 키 줄만 제거합니다."""
    kept = [
        line
        for line in profile_path.read_text(encoding="utf-8").splitlines()
        if not line.startswith(f"{key}=")
    ]
    profile_path.write_text("\n".join(kept), encoding="utf-8")


def _boot(profile_path: Path, code: str = "import config"):
    """선택한 프로필로 config를 import하는 하위 프로세스를 실행합니다."""
    env = os.environ.copy()
    env["MASAMONG_ENV_FILE"] = str(profile_path)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )


def test_explicit_profile_rejects_missing_llm_credentials(tmp_path):
    # 명시적 프로필은 상속 환경을 무시하므로 env 파일에서 key가 빠지면
    # 빈 문자열로 조용히 기동한 뒤 사용자가 봇을 부를 때에야 실패했다.
    profile_path = _profile_env(tmp_path)
    _strip_env_key(profile_path, "COMETAPI_KEY")

    result = _boot(profile_path)

    assert result.returncode != 0
    assert "LLM_MAIN_PRIMARY_API_KEY" in result.stderr


def test_explicit_profile_rejects_placeholder_credentials(tmp_path):
    profile_path = _profile_env(tmp_path)
    _strip_env_key(profile_path, "KMA_API_KEY")
    with profile_path.open("a", encoding="utf-8") as handle:
        handle.write("\nKMA_API_KEY=replace-with-key\n")

    result = _boot(profile_path)

    assert result.returncode != 0
    assert "KMA_API_KEY" in result.stderr
    assert "placeholder" in result.stderr


def test_explicit_profile_allows_missing_key_for_disabled_cog(tmp_path):
    # key가 없는 인스턴스는 Cog를 명시적으로 빼는 것이 정직한 표현이다.
    profile_path = _profile_env(tmp_path)
    _strip_env_key(profile_path, "KMA_API_KEY")
    with profile_path.open("a", encoding="utf-8") as handle:
        handle.write("\nMASAMONG_DISABLED_COGS=weather_cog\n")

    result = _boot(profile_path, "import config; print(config.PROFILE)")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "general"


def test_credential_check_ignores_lane_with_disabled_provider(tmp_path):
    # provider가 none인 레인은 호출되지 않으므로 key를 요구하면 안 된다.
    profile_path = _profile_env(tmp_path)
    with profile_path.open("a", encoding="utf-8") as handle:
        handle.write(
            "\nLLM_MAIN_FALLBACK_PROVIDER=none"
            "\nLLM_MAIN_FALLBACK_API_KEY=\n"
        )

    result = _boot(profile_path, "import config; print(config.PROFILE)")

    assert result.returncode == 0, result.stderr


def test_linkup_key_required_only_when_linkup_is_active_provider(tmp_path):
    # provider가 legacy면 Linkup은 호출되지 않으므로 key가 없어도 된다.
    profile_path = _profile_env(tmp_path)
    with profile_path.open("a", encoding="utf-8") as handle:
        handle.write("\nLINKUP_ENABLED=true\nWEB_SEARCH_PROVIDER=legacy\n")

    assert _boot(profile_path, "import config").returncode == 0

    _strip_env_key(profile_path, "WEB_SEARCH_PROVIDER")
    with profile_path.open("a", encoding="utf-8") as handle:
        handle.write("\nWEB_SEARCH_PROVIDER=linkup\n")

    result = _boot(profile_path)

    assert result.returncode != 0
    assert "LINKUP_API_KEY" in result.stderr


def test_legacy_profile_does_not_require_feature_credentials(tmp_path):
    # 현재 원격 운영(legacy 경로)은 이 검사의 영향을 받지 않아야 한다.
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith("MASAMONG_"):
            env.pop(key, None)
    env["MASAMONG_LOG_FILE"] = os.devnull
    env["MASAMONG_ERROR_LOG_FILE"] = os.devnull

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import config; print(config.PROFILE, config.REQUIRE_EXPLICIT_PROFILE)",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "legacy False"

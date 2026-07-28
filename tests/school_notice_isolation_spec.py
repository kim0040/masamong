"""학교 공지 기능이 기존 인스턴스를 깨지 않는지 확인합니다.

masamo는 MASAMONG_AUTO_MIGRATE=false로 기동하므로, 신규 테이블을 무조건
요구하면 운영 봇이 기동하지 못한다. 그 경계를 코드로 고정한다.
"""

import os
import subprocess
import sys
from pathlib import Path

import aiosqlite
import discord
import pytest

import config
from main import SCHOOL_NOTICE_TABLES, ReMasamongBot

ROOT = Path(__file__).resolve().parents[1]


async def _requested_tables(monkeypatch, *, enabled: bool) -> set[str]:
    """지정한 설정에서 startup 검증이 요구하는 테이블 집합."""
    monkeypatch.setattr(config, "SCHOOL_NOTICE_ENABLED", enabled)
    monkeypatch.setattr(config, "REQUIRE_EXPLICIT_PROFILE", True)
    monkeypatch.setattr(config, "DB_BACKEND", "sqlite")

    bot = ReMasamongBot(command_prefix="!", intents=discord.Intents.none())
    bot.db = await aiosqlite.connect(":memory:")
    await bot.db.executescript((ROOT / "database" / "schema.sql").read_text(encoding="utf-8"))
    await bot.db.commit()

    requested: set[str] = set()

    async def _fake_existing(table_names):
        requested.update(str(name) for name in table_names)
        return set(requested)

    async def _fake_columns(_db, _table):
        return [
            "guild_id", "ai_enabled", "ai_allowed_channels", "persona_text", "language",
            "user_id", "birth_date", "birth_time", "gender", "birth_place",
            "subscription_active", "subscription_time", "pending_payload",
            "last_fortune_sent", "last_fortune_content", "created_at",
            "id", "scope", "policy_version", "notice_hash", "status",
            "granted_at", "withdrawn_at", "updated_at",
            "user_key", "school_id", "profile_json", "profile_version",
            "enabled", "delivery_time", "source_id", "external_id",
            "feedback_type", "topic", "interaction_id", "consumed_at",
            "digest_date", "notice_id", "revision_count", "failure_reason",
            "attempt_count", "delivered_at", "run_date", "profile_hash",
            "collection_status", "may_include_stale", "item_count",
            "http_requests", "llm_calls", "finished_at",
            "next_attempt_at", "last_error",
        ]

    monkeypatch.setattr(bot, "_existing_tables", _fake_existing)
    monkeypatch.setattr("main.get_table_columns", _fake_columns)
    try:
        await bot._verify_runtime_schema()
    finally:
        await bot.db.close()
    return requested


@pytest.mark.asyncio
async def test_disabled_instance_does_not_require_school_notice_tables(monkeypatch):
    # masamo는 AUTO_MIGRATE=false라 없는 테이블을 요구받으면 기동이 막힌다.
    requested = await _requested_tables(monkeypatch, enabled=False)

    assert not requested & set(SCHOOL_NOTICE_TABLES)


@pytest.mark.asyncio
async def test_enabled_instance_requires_school_notice_tables(monkeypatch):
    requested = await _requested_tables(monkeypatch, enabled=True)

    assert set(SCHOOL_NOTICE_TABLES) <= requested


def test_schema_files_define_every_required_table():
    for schema_name in ("schema.sql", "schema_tidb.sql"):
        sql = (ROOT / "database" / schema_name).read_text(encoding="utf-8")
        for table in SCHOOL_NOTICE_TABLES:
            assert f"CREATE TABLE IF NOT EXISTS {table}" in sql, (
                f"{schema_name}에 {table} 정의가 없습니다."
            )


def _boot(tmp_path: Path, extra_lines: list[str]):
    """명시적 프로필 env 파일로 config를 기동합니다.

    명시적 프로필은 상속 환경을 읽지 않으므로 설정을 os.environ이 아니라 선택한
    env 파일에 적어야 한다. 이 테스트 자체가 그 계약을 확인한다.
    """
    config_path = tmp_path / "config.json"
    emb_path = tmp_path / "emb.json"
    prompt_path = tmp_path / "prompts.json"
    profile_path = tmp_path / "general.env"
    config_path.write_text("{}", encoding="utf-8")
    prompt_path.write_text("{}", encoding="utf-8")
    emb_path.write_text(
        '{"discord_db_path":"'
        + str(tmp_path / "d.db")
        + '","kakao_db_path":"'
        + str(tmp_path / "k.db")
        + '","kakao_servers":[]}',
        encoding="utf-8",
    )
    lines = [
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
        "MASAMONG_MEMORY_SOURCES=discord",
        "DISCORD_EMBEDDING_BACKEND=sqlite",
        "KAKAO_STORE_BACKEND=local",
        "AI_MEMORY_ENABLED=false",
        "EMBEDDING_ENABLED=false",
        "RERANK_ENABLED=false",
        "SCHOOL_NOTICE_ENABLED=false",
        "DISCORD_BOT_TOKEN=general-token",
        "MASAMONG_EXPECTED_DISCORD_BOT_USER_ID=replace-with-current-masamo-bot-user-id",
        "COMETAPI_KEY=test-cometapi-key",
        "KMA_API_KEY=test-kma-key",
    ]
    profile_path.write_text("\n".join(lines + extra_lines), encoding="utf-8")

    env = os.environ.copy()
    env["MASAMONG_ENV_FILE"] = str(profile_path)
    return subprocess.run(
        [sys.executable, "-c", "import config; print(config.SCHOOL_NOTICE_ENABLED)"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )


def test_feature_is_off_by_default(tmp_path):
    result = _boot(tmp_path, [])

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "False"


def test_enabling_without_paths_fails_closed(tmp_path):
    # 경로가 없으면 매일 조용히 아무것도 전달하지 않는 상태가 된다.
    result = _boot(tmp_path, ["SCHOOL_NOTICE_ENABLED=true"])

    assert result.returncode != 0
    assert "SCHOOL_NOTICE_DIGEST_DIR" in result.stderr


def test_enabling_with_relative_paths_fails_closed(tmp_path):
    result = _boot(
        tmp_path,
        [
            "SCHOOL_NOTICE_ENABLED=true",
            "SCHOOL_NOTICE_DIGEST_DIR=relative/dir",
            "SCHOOL_NOTICE_CORE_DB=relative/core.db",
        ],
    )

    assert result.returncode != 0
    assert "절대 경로" in result.stderr


def test_enabling_with_absolute_paths_boots(tmp_path):
    source_config_path = tmp_path / "sources.json"
    source_config_path.write_text("{}", encoding="utf-8")
    result = _boot(
        tmp_path,
        [
            "SCHOOL_NOTICE_ENABLED=true",
            f"SCHOOL_NOTICE_DIGEST_DIR={tmp_path / 'general' / 'digests'}",
            f"SCHOOL_NOTICE_CORE_DB={tmp_path / 'general' / 'core.db'}",
            f"SCHOOL_NOTICE_SOURCE_CONFIG={source_config_path}",
        ],
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "True"

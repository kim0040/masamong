from __future__ import annotations

from pathlib import Path

from scripts import audit_tracked_secrets as audit
from scripts import sanitize_sensitive_git_history as sanitizer


def test_read_env_secrets_skips_placeholders_and_keeps_only_sensitive(tmp_path):
    env_file = tmp_path / "runtime.env"
    env_file.write_text(
        "COMETAPI_KEY=replace-with-real-key\n"
        "DISCORD_BOT_TOKEN=real-token-value\n"
        "NORMAL_SETTING=not-a-secret\n"
        "MASAMONG_DB_HOST=prod-db.internal\n",
        encoding="utf-8",
    )

    secrets = audit._read_env_secrets([str(env_file)])

    assert sorted(secrets) == ["DISCORD_BOT_TOKEN", "MASAMONG_DB_HOST"]
    assert secrets["DISCORD_BOT_TOKEN"] == b"real-token-value"


def test_split_history_match_never_requires_content():
    revision, path = audit._split_history_match("abc123:docs/README.md")

    assert revision == "abc123"
    assert path == "docs/README.md"


def test_current_pattern_finds_identity_without_returning_value(
    monkeypatch,
    tmp_path,
):
    tracked = tmp_path / "tracked.env"
    tracked.write_text(
        "MASAMONG_SUPERADMIN_USER_IDS=" + "1" * 18,
        encoding="utf-8",
    )
    monkeypatch.setattr(audit, "ROOT", Path(tmp_path))
    monkeypatch.setattr(audit, "_tracked_paths", lambda: ["tracked.env"])

    findings = audit._scan_current({})

    assert [item.rule for item in findings] == ["tracked_discord_identity"]
    assert all("1" * 18 not in str(item.as_dict()) for item in findings)


def test_discord_shape_fixture_is_ignored_but_exact_secret_scan_is_not():
    assert audit._is_expected_test_fixture(
        "tracked_discord_identity",
        "tests/config_profile_spec.py",
    )
    assert not audit._is_expected_test_fixture(
        "known_env_value:DISCORD_BOT_TOKEN",
        "tests/config_profile_spec.py",
    )


def test_history_replacement_payload_contains_rules_not_reported_values(tmp_path):
    env_file = tmp_path / "runtime.env"
    env_file.write_text(
        "MASAMONG_DB_HOST=private-db.internal\n",
        encoding="utf-8",
    )

    payload, labels = sanitizer._replacement_payload(str(env_file))

    assert b"MASAMONG_SUPERADMIN_USER_IDS" in payload
    assert b"private-db.internal" in payload
    assert "known_env_value:MASAMONG_DB_HOST" in labels

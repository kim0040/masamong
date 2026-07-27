import logger_config


def test_log_redaction_removes_loaded_secrets_and_inline_credentials(
    monkeypatch,
):
    monkeypatch.setattr(
        logger_config,
        "_SENSITIVE_VALUES",
        ("super-secret-token",),
    )

    rendered = logger_config._redact_log_text(
        "token=inline-value Authorization: Bearer super-secret-token "
        "serviceKey=query-secret"
    )

    assert "inline-value" not in rendered
    assert "super-secret-token" not in rendered
    assert "query-secret" not in rendered
    assert rendered.count("[REDACTED]") == 3

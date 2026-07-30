import json
import logging

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


def test_json_formatter_keeps_only_bounded_diagnostic_fields():
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="도구 실행 완료",
        args=(),
        exc_info=None,
    )
    record.trace_id = "trace-123"
    record.event = "tool_execution_completed"
    record.outcome = "failed"
    record.failure_kind = "x" * 200
    record.duration_ms = 321
    record.shared_history_ref = True
    record.parameters = {"query": "저장되면 안 되는 질문"}

    rendered = json.loads(logger_config.JsonFormatter().format(record))

    assert rendered["trace_id"] == "trace-123"
    assert rendered["event"] == "tool_execution_completed"
    assert rendered["outcome"] == "failed"
    assert rendered["duration_ms"] == 321
    assert rendered["shared_history_ref"] is True
    assert len(rendered["failure_kind"]) == 128
    assert "parameters" not in rendered
    assert "저장되면 안 되는 질문" not in json.dumps(
        rendered,
        ensure_ascii=False,
    )

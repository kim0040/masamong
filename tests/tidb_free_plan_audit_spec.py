from __future__ import annotations

from scripts import audit_tidb_free_plan_readonly as audit


def test_storage_report_applies_free_quota_and_sorts_largest(monkeypatch):
    monkeypatch.setattr(audit.config, "TIDB_STARTER_FREE_ROW_STORAGE_BYTES", 1_000)
    monkeypatch.setattr(audit.config, "TIDB_STARTER_USAGE_WARNING_RATIO", 0.8)

    report = audit._storage_report(
        [
            {
                "TABLE_NAME": "small",
                "TABLE_ROWS": 3,
                "DATA_LENGTH": 100,
                "INDEX_LENGTH": 20,
            },
            {
                "TABLE_NAME": "large",
                "TABLE_ROWS": 7,
                "DATA_LENGTH": 650,
                "INDEX_LENGTH": 50,
            },
        ]
    )

    assert report["logical_bytes"] == 820
    assert report["approximate_rows"] == 10
    assert report["status"] == "warning"
    assert report["largest_tables"][0]["table"] == "large"


def test_monthly_ru_report_fails_closed_to_console_check(monkeypatch):
    monkeypatch.setattr(audit.config, "TIDB_STARTER_FREE_MONTHLY_RU", 50_000_000)

    class UnavailableSession:
        def read(self, _sql):
            raise RuntimeError("not permitted")

    report = audit._monthly_ru_report(UnavailableSession())

    assert report["available"] is False
    assert report["quota_ru"] == 50_000_000
    assert "Usage this month" in report["authoritative_source"]


def test_monthly_ru_report_marks_80_percent_as_warning(monkeypatch):
    monkeypatch.setattr(audit.config, "TIDB_STARTER_FREE_MONTHLY_RU", 100)
    monkeypatch.setattr(audit.config, "TIDB_STARTER_USAGE_WARNING_RATIO", 0.8)

    class Session:
        def read(self, _sql):
            return [
                {
                    "total_ru": 80,
                    "first_record": "2026-07-01",
                    "last_record": "2026-07-27",
                }
            ]

    report = audit._monthly_ru_report(Session())

    assert report["available"] is True
    assert report["status"] == "warning"
    assert report["headroom_ru"] == 20

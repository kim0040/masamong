"""읽기 전용 메모리 감사 보고서의 JSON 경계 테스트."""

from decimal import Decimal
import json

import pytest

from scripts.audit_memory_quality_readonly import _json_default


def test_memory_audit_decimal_values_remain_json_numbers():
    rendered = json.dumps(
        {
            "whole": Decimal("12"),
            "average": Decimal("42.75"),
        },
        default=_json_default,
    )

    assert json.loads(rendered) == {
        "whole": 12,
        "average": 42.75,
    }


def test_memory_audit_rejects_unknown_json_types():
    with pytest.raises(TypeError):
        _json_default(object())

"""국내 주식 공공 API는 인증서 검증을 유지한 TLS 세션만 사용합니다."""

from __future__ import annotations

import pytest

import config
from utils.api_handlers import krx


class _Response:
    status_code = 200
    text = ""

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {
            "response": {
                "body": {
                    "items": {
                        "item": [
                            {
                                "itmsNm": "삼성전자",
                                "clpr": "1000",
                                "vs": "10",
                            }
                        ]
                    }
                }
            }
        }


class _Session:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return _Response()


@pytest.mark.asyncio
async def test_krx_uses_tls12_session_without_disabling_certificate_check(
    monkeypatch,
):
    session = _Session()
    monkeypatch.setattr(config, "KRX_API_KEY", "test-key")
    monkeypatch.setattr(krx.http, "get_tlsv12_session", lambda: session)
    monkeypatch.setattr(
        krx.http,
        "get_insecure_session",
        lambda: (_ for _ in ()).throw(
            AssertionError("insecure session must not be used")
        ),
    )

    async def immediate_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(krx.asyncio, "to_thread", immediate_to_thread)

    result = await krx.get_stock_price("삼성전자")

    assert result == "삼성전자: 1,000원 (+10)"
    assert len(session.calls) == 1
    assert session.calls[0]["timeout"] == 10
    assert "verify" not in session.calls[0]

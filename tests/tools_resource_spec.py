"""ToolsCog의 무거운 import 및 이미지 동시성 회귀 테스트."""

import asyncio
import base64
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import config
import cogs.tools_cog as tools_module
from cogs.tools_cog import ToolsCog


ROOT = Path(__file__).resolve().parents[1]


class _FakeBot:
    db = object()

    def get_cog(self, _name):
        return None


def test_tools_cog_import_does_not_load_yfinance_stack():
    """봇 시작만으로 yfinance→pandas→numpy를 메모리에 올리지 않는다."""
    script = """
import importlib.abc
import sys

class HeavyFinanceImportBlocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if (
            fullname == "yfinance"
            or fullname.startswith("yfinance.")
            or fullname == "pandas"
            or fullname.startswith("pandas.")
            or fullname == "numpy"
            or fullname.startswith("numpy.")
        ):
            raise RuntimeError("eager finance import attempted: " + fullname)
        return None

sys.meta_path.insert(0, HeavyFinanceImportBlocker())
import cogs.tools_cog  # noqa: F401

heavy = [
    name
    for name in sys.modules
    if name.split(".", 1)[0] in {"yfinance", "pandas", "numpy"}
]
if heavy:
    raise SystemExit("unexpected eager finance imports: " + ", ".join(sorted(heavy)))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.asyncio
async def test_yfinance_loader_imports_once_under_concurrency(monkeypatch):
    cog = ToolsCog(_FakeBot())
    fake_module = SimpleNamespace(get_stock_info=object())
    import_calls: list[str] = []

    def fake_import(name: str):
        import_calls.append(name)
        return fake_module

    monkeypatch.setattr("cogs.tools_cog.importlib.import_module", fake_import)

    loaded = await asyncio.gather(
        cog._load_yfinance_handler(),
        cog._load_yfinance_handler(),
        cog._load_yfinance_handler(),
    )

    assert loaded == [fake_module, fake_module, fake_module]
    assert import_calls == ["utils.api_handlers.yfinance_handler"]


@pytest.mark.asyncio
async def test_image_generation_wrapper_is_process_serialized(monkeypatch):
    """복수 사용자의 동시 요청도 최종 quota/API/log 구간은 한 번에 하나만 돈다."""
    cog = ToolsCog(_FakeBot())
    active = 0
    max_active = 0

    async def fake_exclusive(
        *,
        prompt,
        user_id,
        aspect_ratio=None,
        guild_id=None,
    ):
        _ = guild_id
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"image_data": b"ok", "remaining": user_id}

    monkeypatch.setattr(cog, "_generate_image_exclusive", fake_exclusive)

    results = await asyncio.gather(
        *(cog.generate_image("test", user_id) for user_id in range(1, 6))
    )

    assert max_active == 1
    assert [result["remaining"] for result in results] == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_image_generation_queue_wait_is_bounded(monkeypatch):
    cog = ToolsCog(_FakeBot())
    monkeypatch.setattr(config, "IMAGE_GENERATION_QUEUE_TIMEOUT_SECONDS", 0.01)

    await cog._image_generation_lock.acquire()
    try:
        result = await cog.generate_image("test", 1)
    finally:
        cog._image_generation_lock.release()

    assert "대기열" in result["error"]


@pytest.mark.asyncio
async def test_failed_provider_attempt_still_consumes_reserved_quota(monkeypatch):
    cog = ToolsCog(_FakeBot())
    monkeypatch.setattr(config, "COMETAPI_IMAGE_ENABLED", True)
    monkeypatch.setattr(config, "COMETAPI_IMAGE_API_KEY", "test-key")
    monkeypatch.setattr(
        cog,
        "check_image_quota",
        AsyncMock(
            return_value={
                "allowed": True,
                "remaining": 3,
                "global_remaining": 10,
            }
        ),
    )
    reservation = AsyncMock(return_value=True)
    monkeypatch.setattr(
        tools_module.db_utils,
        "log_image_generation",
        reservation,
    )

    def fail_before_http(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(tools_module.aiohttp, "ClientSession", fail_before_http)

    result = await cog._generate_image_exclusive("safe prompt", 42)

    assert "error" in result
    reservation.assert_awaited_once_with(cog.bot.db, 42, None)


@pytest.mark.asyncio
async def test_image_provider_is_not_called_when_quota_reservation_fails(monkeypatch):
    cog = ToolsCog(_FakeBot())
    monkeypatch.setattr(config, "COMETAPI_IMAGE_ENABLED", True)
    monkeypatch.setattr(config, "COMETAPI_IMAGE_API_KEY", "test-key")
    monkeypatch.setattr(
        cog,
        "check_image_quota",
        AsyncMock(
            return_value={
                "allowed": True,
                "remaining": 3,
                "global_remaining": 10,
            }
        ),
    )
    monkeypatch.setattr(
        tools_module.db_utils,
        "log_image_generation",
        AsyncMock(return_value=False),
    )

    def forbidden_http(*_args, **_kwargs):
        raise AssertionError("reservation 실패 뒤 provider를 호출하면 안 됩니다.")

    monkeypatch.setattr(tools_module.aiohttp, "ClientSession", forbidden_http)

    result = await cog._generate_image_exclusive("safe prompt", 42)

    assert "사용량" in result["error"]


@pytest.mark.asyncio
async def test_image_generation_uses_exact_gemini_native_contract(monkeypatch):
    cog = ToolsCog(_FakeBot())
    monkeypatch.setattr(config, "COMETAPI_IMAGE_ENABLED", True)
    monkeypatch.setattr(config, "COMETAPI_IMAGE_API_KEY", "test-key")
    monkeypatch.setattr(config, "COMETAPI_IMAGE_BASE_URL", "https://api.example")
    monkeypatch.setattr(config, "IMAGE_MODEL", "gemini-3.1-flash-lite-image")
    monkeypatch.setattr(
        cog,
        "check_image_quota",
        AsyncMock(
            return_value={
                "allowed": True,
                "remaining": 3,
                "global_remaining": 10,
                "guild_remaining": 5,
            }
        ),
    )
    monkeypatch.setattr(
        tools_module.db_utils,
        "log_image_generation",
        AsyncMock(return_value=True),
    )
    recorded = {}
    draft_png = b"\x89PNG\r\n\x1a\n" + b"draft-image"
    png = b"\x89PNG\r\n\x1a\n" + b"final-image"

    class _Response:
        status = 200

        async def read(self):
            return __import__("json").dumps(
                {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "generated"},
                                {
                                    "thought": True,
                                    "inlineData": {
                                        "mimeType": "image/png",
                                        "data": base64.b64encode(
                                            draft_png
                                        ).decode(),
                                    },
                                },
                                {
                                    "inlineData": {
                                        "mimeType": "image/png",
                                        "data": base64.b64encode(png).decode(),
                                    }
                                },
                            ]
                        }
                    }
                ]
                }
            ).encode()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class _Session:
        def __init__(self, *, timeout):
            recorded["timeout"] = timeout

        def post(self, endpoint, *, json, headers):
            recorded.update(
                endpoint=endpoint,
                payload=json,
                headers=headers,
            )
            return _Response()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    monkeypatch.setattr(tools_module.aiohttp, "ClientSession", _Session)

    result = await cog._generate_image_exclusive(
        "a safe watercolor landscape",
        42,
        guild_id=99,
    )

    assert recorded["endpoint"] == (
        "https://api.example/v1beta/models/"
        "gemini-3.1-flash-lite-image:generateContent"
    )
    assert recorded["headers"]["Authorization"] == "Bearer test-key"
    assert recorded["payload"]["generationConfig"]["responseModalities"] == [
        "TEXT",
        "IMAGE",
    ]
    assert recorded["payload"]["contents"][0]["role"] == "user"
    assert recorded["payload"]["generationConfig"]["imageConfig"] == {
        "aspectRatio": "1:1",
        "imageSize": "1K",
    }
    assert result == {
        "image_data": png,
        "mime_type": "image/png",
        "remaining": 2,
    }


def test_image_selection_uses_last_image_when_provider_omits_thought_marker():
    """중간 이미지 표식이 없더라도 첫 시안이 아닌 마지막 렌더를 고른다."""
    first = base64.b64encode(b"first").decode()
    final = base64.b64encode(b"final").decode()

    selected = ToolsCog._select_final_inline_image(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "inlineData": {
                                    "mimeType": "image/png",
                                    "data": first,
                                }
                            },
                            {
                                "inlineData": {
                                    "mimeType": "image/png",
                                    "data": final,
                                }
                            },
                        ]
                    }
                }
            ]
        }
    )

    assert selected is not None
    assert selected["encoded"] == final
    assert selected["image_part_count"] == 2
    assert selected["thought_image_count"] == 0


@pytest.mark.asyncio
async def test_image_model_contract_mismatch_never_reserves_usage(monkeypatch):
    cog = ToolsCog(_FakeBot())
    monkeypatch.setattr(config, "COMETAPI_IMAGE_ENABLED", True)
    monkeypatch.setattr(config, "COMETAPI_IMAGE_API_KEY", "test-key")
    monkeypatch.setattr(config, "IMAGE_MODEL", "gpt-image-2-all")
    reserve = AsyncMock(return_value=True)
    monkeypatch.setattr(tools_module.db_utils, "log_image_generation", reserve)

    result = await cog._generate_image_exclusive(
        "a safe watercolor landscape",
        42,
        guild_id=99,
    )

    assert "사용량에 포함되지" in result["error"]
    reserve.assert_not_awaited()

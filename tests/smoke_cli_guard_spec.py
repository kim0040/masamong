import argparse
import importlib.util
from pathlib import Path
import sys

import pytest

import config


ROOT = Path(__file__).resolve().parents[1]


def _load_script(module_name: str, filename: str):
    path = ROOT / "scripts" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_semantic_smoke_is_dry_without_explicit_run(monkeypatch, capsys):
    module = _load_script(
        "smoke_semantic_routing_guard_test",
        "smoke_semantic_routing_live.py",
    )
    monkeypatch.setattr(config, "PROFILE", "masamo")
    monkeypatch.setattr(config, "LLM_ROUTING_PRIMARY_MODEL", "routing-test")
    args = argparse.Namespace(
        expected_profile="masamo",
        max_calls=3,
        run=False,
        confirm=None,
    )

    assert module.validate_execution(args) is False
    assert "provider를 호출하지 않았습니다" in capsys.readouterr().out


def test_semantic_smoke_rejects_wrong_confirmation(monkeypatch):
    module = _load_script(
        "smoke_semantic_routing_confirmation_test",
        "smoke_semantic_routing_live.py",
    )
    monkeypatch.setattr(config, "PROFILE", "masamo")
    args = argparse.Namespace(
        expected_profile="masamo",
        max_calls=2,
        run=True,
        confirm="wrong",
    )

    with pytest.raises(SystemExit, match="--confirm"):
        module.validate_execution(args)


def test_main_quality_smoke_is_dry_without_explicit_run(monkeypatch, capsys):
    module = _load_script(
        "smoke_llm_quality_guard_test",
        "smoke_llm_quality_live.py",
    )
    monkeypatch.setattr(config, "PROFILE", "masamo")
    monkeypatch.setattr(config, "LLM_MAIN_PRIMARY_MODEL", "main-test")
    args = argparse.Namespace(
        expected_profile="masamo",
        run=False,
        confirm=None,
    )

    assert module.validate_execution(args) is False
    assert "provider를 호출하지 않았습니다" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_linkup_smoke_is_dry_before_database_or_provider(
    monkeypatch,
    capsys,
):
    module = _load_script(
        "smoke_linkup_guard_test",
        "smoke_linkup_live.py",
    )
    monkeypatch.setattr(config, "PROFILE", "masamo")

    async def unexpected_connect(*_args, **_kwargs):
        raise AssertionError("dry-run must not open the database")

    monkeypatch.setattr(module, "connect_main_db", unexpected_connect)
    args = argparse.Namespace(
        expected_profile="masamo",
        query="OpenAI API 최신 공식 업데이트",
        max_cost_usd=0.005,
        run=False,
        confirm=None,
    )

    assert await module.run(args) == 0
    assert "provider를 호출하지 않았습니다" in capsys.readouterr().out

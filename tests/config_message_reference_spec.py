# -*- coding: utf-8 -*-
"""코드가 참조하는 config 메시지 상수가 실제로 존재하는지 정적으로 검증한다.

2026-08-10 운영 장애에서 `utils/weather.py`가 정의되지 않은
`config.MSG_KMA_API_DAILY_LIMIT_REACHED`를 참조해 AttributeError가 났다.
해당 분기는 평소에 실행되지 않아 테스트와 리뷰를 모두 통과했고, 일일 한도에
도달한 뒤에야 드러났다. 같은 형태의 지뢰를 배포 전에 잡는다.
"""

import ast
import pathlib

import pytest

import config
from utils.locale import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, get as locale_get

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCAN_DIRS = ("cogs", "utils", "database", "school_notice", "transfer_notice")
SCAN_FILES = ("main.py",)


def _python_sources() -> list[pathlib.Path]:
    paths: list[pathlib.Path] = []
    for name in SCAN_FILES:
        candidate = REPO_ROOT / name
        if candidate.is_file():
            paths.append(candidate)
    for directory in SCAN_DIRS:
        root = REPO_ROOT / directory
        if not root.is_dir():
            continue
        paths.extend(
            path
            for path in root.rglob("*.py")
            if "__pycache__" not in path.parts
        )
    return sorted(paths)


def _referenced_config_messages() -> dict[str, list[str]]:
    """`config.MSG_*` 형태로 참조된 상수 이름과 참조 위치를 모은다."""
    references: dict[str, list[str]] = {}
    for path in _python_sources():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - 파싱 불가 파일은 건너뛴다
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if not isinstance(node.value, ast.Name) or node.value.id != "config":
                continue
            if not node.attr.startswith("MSG_"):
                continue
            where = f"{path.relative_to(REPO_ROOT)}:{node.lineno}"
            references.setdefault(node.attr, []).append(where)
    return references


def test_referenced_config_messages_exist():
    """코드가 쓰는 config.MSG_* 상수는 모두 정의되어 있어야 한다."""
    references = _referenced_config_messages()
    assert references, "config.MSG_* 참조를 하나도 찾지 못했다면 스캔 대상이 잘못된 것이다."

    missing = {
        name: sites
        for name, sites in references.items()
        if not hasattr(config, name)
    }
    assert not missing, "정의되지 않은 config 메시지 상수를 참조합니다: " + "; ".join(
        f"{name} ({', '.join(sites)})" for name, sites in sorted(missing.items())
    )


def test_referenced_config_messages_are_not_placeholder():
    """상수는 존재하지만 로케일 키가 없어 키 이름만 반환되는 경우를 잡는다."""
    references = _referenced_config_messages()
    placeholders = []
    for name in sorted(references):
        value = getattr(config, name, None)
        if isinstance(value, str) and value.strip() == name:
            placeholders.append(f"{name} ({', '.join(references[name])})")
    assert not placeholders, (
        "로케일 정의가 없어 키 이름이 그대로 노출됩니다: " + "; ".join(placeholders)
    )


@pytest.mark.parametrize("lang", sorted(SUPPORTED_LANGUAGES))
def test_referenced_messages_translated_in_every_locale(lang):
    """참조되는 메시지는 지원 언어 전부에 번역이 있어야 한다."""
    references = _referenced_config_messages()
    untranslated = [
        name
        for name in sorted(references)
        if hasattr(config, name) and locale_get(name, lang=lang).strip() == name
    ]
    assert not untranslated, (
        f"[{lang}] 로케일에 누락된 메시지 키: {', '.join(untranslated)} "
        f"(기본 언어: {DEFAULT_LANGUAGE})"
    )

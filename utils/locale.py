# -*- coding: utf-8 -*-
"""
다국어(i18n) 지원 모듈입니다.

봇의 사용자 facing 메시지를 관리하며, 서버별(guild별) 언어 설정을 지원합니다.
기본 언어는 한국어(ko)이며, .env의 MASAMONG_LANG 또는 guild_settings로 변경 가능합니다.

사용법:
    from utils.locale import t
    await t(guild_id, "MSG_AI_ERROR")
    await t(guild_id, "MSG_WEATHER_FETCH_ERROR")
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

# 순환 import 방지를 위해 logger_config 대신 표준 logging 사용
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 로케일 파일 경로
# ---------------------------------------------------------------------------
_LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"

# ---------------------------------------------------------------------------
# 캐시
# ---------------------------------------------------------------------------
_loaded: Dict[str, Dict[str, str]] = {}
_fallback: Dict[str, str] = {}

# ---------------------------------------------------------------------------
# 지원 언어 목록
# ---------------------------------------------------------------------------
SUPPORTED_LANGUAGES = {"ko", "en", "ja"}
DEFAULT_LANGUAGE = "ko"


def _load_lang(lang: str) -> Dict[str, str]:
    """지정 언어의 로케일 JSON을 로드합니다."""
    if lang in _loaded:
        return _loaded[lang]

    path = _LOCALES_DIR / f"{lang}.json"
    if not path.exists():
        logger.warning(f"로케일 파일을 찾을 수 없습니다: {path}")
        _loaded[lang] = {}
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _loaded[lang] = data
        logger.debug(f"로케일 로드 완료: {lang} ({len(data)}개 키)")
        return data
    except Exception as e:
        logger.error(f"로케일 파일 로드 실패 ({lang}): {e}", exc_info=True)
        _loaded[lang] = {}
        return {}


def _ensure_fallback() -> Dict[str, str]:
    """기본 언어(한국어)를 폴백으로 로드합니다."""
    if not _fallback:
        _fallback.update(_load_lang(DEFAULT_LANGUAGE))
    return _fallback


# ---------------------------------------------------------------------------
# Guild 언어 설정 캐시 (DB에서 로드)
# ---------------------------------------------------------------------------
_guild_lang_cache: Dict[int, str] = {}


def set_guild_language(guild_id: int, lang: str) -> None:
    """guild의 언어 설정을 캐시에 반영합니다 (DB 저장은 별도)."""
    if lang in SUPPORTED_LANGUAGES:
        _guild_lang_cache[guild_id] = lang


def get_guild_language(guild_id: Optional[int]) -> str:
    """guild의 언어 설정을 반환합니다."""
    if guild_id and guild_id in _guild_lang_cache:
        return _guild_lang_cache[guild_id]
    # .env에서 전역 기본값 로드
    return os.environ.get("MASAMONG_LANG", DEFAULT_LANGUAGE).strip().lower()


async def load_guild_languages_from_db(db) -> None:
    """DB의 guild_settings에서 언어 설정을 로드하여 캐시에 반영합니다."""
    try:
        cursor = await db.execute(
            "SELECT guild_id, language FROM guild_settings WHERE language IS NOT NULL"
        )
        rows = await cursor.fetchall()
        for row in rows:
            gid = row[0] if isinstance(row, tuple) else row["guild_id"]
            lang = row[1] if isinstance(row, tuple) else row["language"]
            if lang and lang in SUPPORTED_LANGUAGES:
                _guild_lang_cache[int(gid)] = lang
        logger.debug(f"guild 언어 설정 로드 완료: {len(_guild_lang_cache)}개")
    except Exception as e:
        # guild_settings에 language 컬럼이 없을 수 있음 (마이그레이션 전)
        logger.debug(f"guild 언어 설정 로드 실패 (무시 가능): {e}")


# ---------------------------------------------------------------------------
# 메시지 조회 함수
# ---------------------------------------------------------------------------
def get(key: str, lang: Optional[str] = None, **kwargs: Any) -> str:
    """
    지정 키의 메시지를 반환합니다.

    Args:
        key: 메시지 키 (예: "MSG_AI_ERROR")
        lang: 언어 코드 (None이면 기본값)
        **kwargs: 메시지 내 포맷 변수 ({variable} 치환)

    Returns:
        포맷된 메시지 문자열
    """
    lang = lang or DEFAULT_LANGUAGE
    messages = _load_lang(lang)

    # 해당 언어에 없으면 폴백(한국어)에서 조회
    template = messages.get(key)
    if template is None:
        fb = _ensure_fallback()
        template = fb.get(key)
    if template is None:
        # 어디에도 없으면 키 자체를 반환
        logger.warning(f"로케일 키를 찾을 수 없습니다: {key} (lang={lang})")
        return key

    if kwargs:
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError):
            return template
    return template


async def t(guild_id: Optional[int], key: str, **kwargs: Any) -> str:
    """
    guild 언어 설정을 고려하여 메시지를 반환합니다 (비동기).

    Args:
        guild_id: Discord guild ID (None이면 기본 언어)
        key: 메시지 키
        **kwargs: 포맷 변수

    Returns:
        포맷된 메시지 문자열
    """
    lang = get_guild_language(guild_id)
    return get(key, lang=lang, **kwargs)


# ---------------------------------------------------------------------------
# 동기 편의 함수 (guild_id 없이 사용)
# ---------------------------------------------------------------------------
def msg(key: str, **kwargs: Any) -> str:
    """기본 언어(한국어)로 메시지를 반환합니다 (동기)."""
    return get(key, lang=DEFAULT_LANGUAGE, **kwargs)

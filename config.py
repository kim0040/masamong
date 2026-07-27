# -*- coding: utf-8 -*-
"""
마사몽 봇의 전체 설정을 로드하고 관리하는 모듈입니다.

환경변수(.env) → config.json → 코드 기본값 순으로 값을 조회하며,
LLM 레인 구성, API 키, RAG 파라미터, Rate Limit 등 모든 설정 상수를
모듈 레벨 변수로 노출합니다.
"""

import os
import json
import re
from pathlib import Path
from typing import Any, Dict
from dotenv import dotenv_values, load_dotenv
import discord

try:  # Optional dependency for YAML-based prompt configuration
    import yaml  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - yaml is optional
    yaml = None  # type: ignore

PROJECT_ROOT = Path(__file__).resolve().parent
_ENV_REFERENCE_RE = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}"
)


def _resolve_profile_env_values(path: Path) -> dict[str, str]:
    """선택한 env 파일 내부 값만 사용해 `${VAR}` 참조를 해석합니다."""
    raw_values = {
        str(key): "" if value is None else str(value)
        for key, value in dotenv_values(path, interpolate=False).items()
        if key
    }
    resolved: dict[str, str] = {}

    def resolve_key(key: str, stack: tuple[str, ...] = ()) -> str:
        if key in resolved:
            return resolved[key]
        if key in stack:
            chain = " -> ".join((*stack, key))
            raise RuntimeError(f"env 파일 변수 참조가 순환합니다: {chain}")
        raw_value = raw_values.get(key, "")

        def replace_reference(match: re.Match[str]) -> str:
            referenced_key = match.group(1)
            default = match.group(2)
            if referenced_key in raw_values:
                value = resolve_key(referenced_key, (*stack, key))
                if value or default is None:
                    return value
            return default or ""

        value = _ENV_REFERENCE_RE.sub(replace_reference, raw_value)
        resolved[key] = value
        return value

    for env_key in raw_values:
        resolve_key(env_key)
    return resolved

# 운영 인스턴스별 환경 파일을 명시할 수 있게 한다. 명시한 파일이 없는데
# 암묵적으로 다른 .env로 폴백하면 일반/마사모 설정이 뒤섞일 수 있으므로 즉시 실패한다.
_explicit_env_file = os.environ.get("MASAMONG_ENV_FILE", "").strip()
_EXPLICIT_ENV_VALUES: dict[str, str] = {}
_EXPLICIT_ENV_KEYS: frozenset[str] = frozenset()
if _explicit_env_file:
    _env_path = Path(_explicit_env_file).expanduser()
    if not _env_path.is_absolute():
        _env_path = PROJECT_ROOT / _env_path
    if not _env_path.is_file():
        raise RuntimeError(f"MASAMONG_ENV_FILE을 찾을 수 없습니다: {_env_path}")
    # 명시한 인스턴스 파일이 해당 프로세스 설정을 소유해야 한다. 상위 shell/systemd에
    # 남은 다른 인스턴스 값이 파일보다 우선하면 general이 masamo DB로 붙을 수 있다.
    _EXPLICIT_ENV_VALUES = _resolve_profile_env_values(_env_path)
    _EXPLICIT_ENV_KEYS = frozenset(_EXPLICIT_ENV_VALUES)
    for _inherited_key in tuple(os.environ):
        if (
            _inherited_key.startswith("MASAMONG_")
            and _inherited_key != "MASAMONG_ENV_FILE"
            and _inherited_key not in _EXPLICIT_ENV_KEYS
        ):
            os.environ.pop(_inherited_key, None)
    os.environ.update(_EXPLICIT_ENV_VALUES)
    ENV_FILE_PATH: Path | None = _env_path.resolve()
    _declared_env_file = _EXPLICIT_ENV_VALUES.get(
        "MASAMONG_ENV_FILE",
        _explicit_env_file,
    ).strip()
    if _declared_env_file:
        _declared_env_path = Path(_declared_env_file).expanduser()
        if not _declared_env_path.is_absolute():
            _declared_env_path = PROJECT_ROOT / _declared_env_path
        if _declared_env_path.resolve() != ENV_FILE_PATH:
            raise RuntimeError(
                "선택한 MASAMONG_ENV_FILE과 프로필 파일 내부 경로가 다릅니다: "
                f"selected={ENV_FILE_PATH}, declared={_declared_env_path.resolve()}"
            )
else:
    # 기존 배포와의 호환성을 위해 명시하지 않은 경우 python-dotenv의 기본 탐색을 유지한다.
    load_dotenv()
    ENV_FILE_PATH = None


def _direct_config_value(key: str, default: str) -> str:
    """명시 프로필에서는 상속 환경이 아닌 선택 파일의 직접 값만 반환합니다."""
    if ENV_FILE_PATH is not None:
        return _EXPLICIT_ENV_VALUES.get(key, default)
    return os.environ.get(key, default)

# 로케일 시스템 초기화 (다국어 지원)
from utils.locale import msg as _locale_msg, SUPPORTED_LANGUAGES, DEFAULT_LANGUAGE

# [NEW] Stock Config (yfinance)
USE_YFINANCE = True
YFINANCE_CACHE_TTL = 600 # 10분 캐시

# config.json 메모리 캐시 (매 호출마다 디스크 I/O 방지)
_CONFIG_JSON_CACHE: dict | None = None
_CONFIG_JSON_LOADED: bool = False
_CONFIG_JSON_ERROR: str | None = None
CONFIG_JSON_PATH = Path(
    _direct_config_value("MASAMONG_CONFIG_FILE", "config.json")
).expanduser()
if not CONFIG_JSON_PATH.is_absolute():
    CONFIG_JSON_PATH = PROJECT_ROOT / CONFIG_JSON_PATH


def _read_config_json() -> dict:
    """config.json 파일을 읽어 메모리에 캐싱합니다.

    최초 호출 시 파일을 읽고, 이후 호출에서는 캐시된 값을 반환하여
    디스크 I/O를 방지합니다. 파일이 없거나 JSON 파싱에 실패하면 빈 dict를 반환합니다.

    Returns:
        dict: config.json의 파싱 결과 또는 빈 dict
    """
    global _CONFIG_JSON_CACHE, _CONFIG_JSON_LOADED, _CONFIG_JSON_ERROR
    if _CONFIG_JSON_LOADED:
        return _CONFIG_JSON_CACHE or {}
    _CONFIG_JSON_LOADED = True
    try:
        with CONFIG_JSON_PATH.open('r', encoding='utf-8') as f:
            data = json.load(f)
            if not isinstance(data, dict):
                _CONFIG_JSON_ERROR = "root is not an object"
                print("경고: config.json 최상위 값은 JSON 객체여야 합니다.")
                _CONFIG_JSON_CACHE = {}
                return {}
            _CONFIG_JSON_CACHE = data
            return _CONFIG_JSON_CACHE
    except FileNotFoundError:
        _CONFIG_JSON_CACHE = {}
        return {}
    except json.JSONDecodeError:
        print("경고: config.json 파일이 유효한 JSON 형식이 아닙니다.")
        _CONFIG_JSON_ERROR = "invalid JSON"
        _CONFIG_JSON_CACHE = {}
        return {}
    except OSError as exc:
        print(f"경고: config.json 파일을 읽을 수 없습니다: {exc}")
        _CONFIG_JSON_ERROR = type(exc).__name__
        _CONFIG_JSON_CACHE = {}
        return {}


def load_config_value(key, default=None):
    """환경 변수 → `config.json` 순으로 값을 조회하고, 없으면 기본값을 반환합니다.

    Args:
        key (str): 조회할 설정 키 이름.
        default (Any, optional): 키가 어디에서도 발견되지 않을 때 사용할 기본값.

    Returns:
        Any: 발견된 설정값 또는 기본값.
    """
    if ENV_FILE_PATH is not None:
        if key in _EXPLICIT_ENV_KEYS:
            return _EXPLICIT_ENV_VALUES[key]
    else:
        value = os.environ.get(key)
        if value is not None and value != "":
            return value
    config_json = _read_config_json()
    if key in config_json:
        return config_json[key]
    return default


def as_bool(value, default: bool = False) -> bool:
    """문자열/불리언 값을 안전하게 bool로 변환합니다."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return default


def as_strict_bool(key: str, value, default: bool = False) -> bool:
    """운영 경계용 불리언을 모호한 값 없이 변환합니다."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise RuntimeError(
        f"{key}은 true 또는 false 계열 값이어야 합니다: {value!r}"
    )


def as_float(value, default: float) -> float:
    """입력값을 float로 변환하되 실패 시 기본값을 반환합니다."""
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value, default: int) -> int:
    """입력값을 int로 변환하되 실패 시 기본값을 반환합니다."""
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def as_str(value, default: str = "") -> str:
    """입력값을 문자열로 변환하되, None이면 기본값을 반환합니다."""
    if value is None:
        return default
    try:
        rendered = str(value).strip()
    except Exception:
        return default
    return rendered if rendered else default


def _resolve_project_storage_path(value: Any, default: str) -> str:
    """상대 저장소 경로를 실행 cwd가 아닌 repository root 기준으로 고정합니다."""
    rendered = as_str(value, default)
    if rendered == ":memory:":
        return rendered
    path = Path(rendered).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return str(path.resolve())


def _normalize_profile_name(value: Any, default: str = "legacy") -> str:
    """프로필/인스턴스 식별자를 파일·로그에 안전한 형태로 검증합니다."""
    rendered = as_str(value, default).lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", rendered):
        raise RuntimeError(
            "MASAMONG_PROFILE/MASAMONG_INSTANCE_NAME은 영문 소문자, 숫자, _, -만 "
            "사용한 1~32자 식별자여야 합니다."
        )
    return rendered


def _parse_csv_set(value: Any) -> frozenset[str]:
    """쉼표 구분 설정을 소문자 집합으로 정규화합니다."""
    return frozenset(
        item.strip().lower()
        for item in str(value or "").split(",")
        if item.strip()
    )


def normalize_llm_provider(value: Any, default: str = "none") -> str:
    """LLM provider 식별자를 정규화합니다."""
    raw = as_str(value, default).lower()
    aliases = {
        "": "none",
        "none": "none",
        "off": "none",
        "disabled": "none",
        "openai": "openai_compat",
        "openai_compat": "openai_compat",
        "openai-compatible": "openai_compat",
        "openai_compatible": "openai_compat",
        "cometapi": "openai_compat",
        "gemini": "gemini_compat",
        "gemini_compat": "gemini_compat",
        "gemini-compatible": "gemini_compat",
        "gemini_compatible": "gemini_compat",
        "google_genai": "gemini_compat",
    }
    return aliases.get(raw, "none")


def default_reasoning_effort_for_model(model: Any) -> str:
    """추론 effort 파라미터가 필요한 OpenAI 호환 모델의 기본값을 반환합니다."""
    model_name = as_str(model, "").lower()
    if "gpt-oss" in model_name:
        return "low"
    return ""


_embed_config_path = Path(
    _direct_config_value('EMB_CONFIG_PATH', 'emb_config.json')
).expanduser()
if not _embed_config_path.is_absolute():
    _embed_config_path = PROJECT_ROOT / _embed_config_path
EMBED_CONFIG_PATH = str(_embed_config_path)
_EMBED_CONFIG_ERROR: str | None = None


def load_emb_config() -> dict:
    """임베딩 관련 별도 설정 파일을 읽어옵니다."""
    global _EMBED_CONFIG_ERROR
    try:
        with open(EMBED_CONFIG_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
            print("경고: emb_config.json 내용이 JSON 객체가 아닙니다.")
            _EMBED_CONFIG_ERROR = "root is not an object"
    except FileNotFoundError:
        _EMBED_CONFIG_ERROR = "file not found"
    except json.JSONDecodeError:
        print("경고: emb_config.json 파일이 유효한 JSON 형식이 아닙니다.")
        _EMBED_CONFIG_ERROR = "invalid JSON"
    return {}


EMBED_CONFIG = load_emb_config()


_prompt_config_path = Path(
    _direct_config_value("PROMPT_CONFIG_PATH", "prompts.json")
).expanduser()
if not _prompt_config_path.is_absolute():
    _prompt_config_path = PROJECT_ROOT / _prompt_config_path
PROMPT_CONFIG_PATH = str(_prompt_config_path)
_PROMPT_CONFIG_EXPLICIT = (
    "PROMPT_CONFIG_PATH" in _EXPLICIT_ENV_KEYS
    if ENV_FILE_PATH is not None
    else "PROMPT_CONFIG_PATH" in os.environ
)
_PROMPT_CONFIG_ERROR: str | None = None


def _read_prompt_file(path: Path) -> dict[str, Any]:
    """프롬프트 설정 파일(JSON/YAML)을 읽어 dict 형태로 반환합니다.

    Args:
        path: 설정 파일 경로 (.json 또는 .yaml/.yml)

    Returns:
        파싱된 설정 dict. 파일이 없거나 파싱 실패 시 빈 dict
    """
    if path.suffix.lower() == ".json":
        with path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
            if isinstance(data, dict):
                return data
            raise ValueError("프롬프트 JSON 최상위 값은 객체여야 합니다.")
    if path.suffix.lower() in {".yaml", ".yml"}:
        if yaml is None:
            raise RuntimeError("YAML 프롬프트 파일을 읽으려면 PyYAML 패키지가 필요합니다.")
        with path.open("r", encoding="utf-8") as fp:
            data = yaml.safe_load(fp)
            if isinstance(data, dict):
                return data
            raise ValueError("YAML 프롬프트 설정 최상위 값은 매핑이어야 합니다.")
    raise ValueError(f"지원하지 않는 프롬프트 파일 형식입니다: {path.suffix}")


def load_prompt_config() -> dict[str, Any]:
    """프롬프트 관련 별도 설정 파일을 읽어옵니다."""
    global _PROMPT_CONFIG_ERROR
    if not PROMPT_CONFIG_PATH:
        return {}
    path = Path(PROMPT_CONFIG_PATH)
    if not path.exists():
        _PROMPT_CONFIG_ERROR = "file not found"
        if _PROMPT_CONFIG_EXPLICIT:
            print(f"경고: 프롬프트 설정 파일 '{path}'을(를) 찾을 수 없습니다.")
        return {}
    try:
        return _read_prompt_file(path)
    except Exception as exc:  # pragma: no cover - 방어적 로깅
        _PROMPT_CONFIG_ERROR = f"{type(exc).__name__}"
        print(f"경고: 프롬프트 설정 파일을 읽는 중 오류가 발생했습니다: {exc}")
        return {}


PROMPT_CONFIG = load_prompt_config()


MENTION_GUARD_SNIPPET = (
    "[MENTION_POLICY]\n"
    "- 반드시 사용자가 봇을 @멘션한 메시지에만 응답한다.\n"
    "- 멘션이 없으면 모든 처리를 즉시 중단하고 응답하지 않는다."
)


def _with_mention_guard(text: Any, fallback: str) -> str:
    """프롬프트에 멘션 제한 안내를 추가합니다."""
    base = fallback.strip()
    if isinstance(text, str) and text.strip():
        base = text.strip()
    guard_line = MENTION_GUARD_SNIPPET.splitlines()[1]
    if guard_line not in base:
        base = f"{base}\n\n{MENTION_GUARD_SNIPPET}"
    return base


def _extract_prompt_value(key: str, default: str) -> str:
    """프롬프트 파일에서 값을 읽어오되, 기본값과 멘션 가드를 포함시킵니다."""
    prompt_section = PROMPT_CONFIG.get("prompts", PROMPT_CONFIG)
    return _with_mention_guard(prompt_section.get(key), default)


FALLBACK_LITE_PROMPT = (
    "You are '마사몽', a lightweight planner model for Discord. "
    "Only proceed when the user explicitly mentions this bot and produce concise plans."
)
FALLBACK_AGENT_PROMPT = (
    "너는 디스코드 서버의 봇 '마사몽'이야. 반말로 친근하게 답하지만, "
    "근거가 없는 내용은 만들지 말고 모르면 모른다고 이야기해. "
    "다음 정보를 참고해서 답을 준비해.\n"
    "- 사용자 질문: {user_query}\n"
    "- 참고 자료 요약:\n{tool_result}"
)
FALLBACK_WEB_PROMPT = (
    "너는 디스코드 서버의 봇 '마사몽'이야. 현재는 웹 검색 결과만 사용할 수 있어. "
    "자료가 부족하면 모른다고 답해.\n"
    "- 사용자 질문: {user_query}\n"
    "- 웹 검색 요약:\n{tool_result}"
)
FALLBACK_PERSONA = (
    "### 역할\n"
    "너는 디스코드 봇 '마사몽'이고, 짧고 위트 있는 반말로 응답한다."
)
FALLBACK_RULES = (
    "### 기본 규칙\n"
    "- 사실에 근거해 대답하고 추측은 피한다.\n"
    "- 욕설이나 혐오 표현은 금지한다.\n"
    "- 개인정보나 민감한 데이터는 요청해도 제공하지 않는다."
)


def _normalize_kakao_servers(raw_value) -> dict[str, dict[str, str]]:
    """카카오 임베딩 서버 설정을 일관된 딕셔너리로 변환합니다."""
    if isinstance(raw_value, dict):
        normalized = {}
        for server_id, meta in raw_value.items():
            if not server_id or not isinstance(meta, dict):
                continue
            db_path = meta.get('db_path')
            room_key = meta.get('room_key')
            if not db_path and not room_key:
                continue
            normalized[str(server_id)] = {
                'db_path': db_path,
                'room_key': room_key,
                'label': meta.get('label', '')
            }
        return normalized

    if isinstance(raw_value, list):
        normalized = {}
        for entry in raw_value:
            if not isinstance(entry, dict):
                continue
            server_id = entry.get('server_id')
            db_path = entry.get('db_path')
            room_key = entry.get('room_key')
            if not server_id or (not db_path and not room_key):
                continue
            normalized[str(server_id)] = {
                'db_path': db_path,
                'room_key': room_key,
                'label': entry.get('label', '')
            }
        return normalized

    return {}

PROFILE = _normalize_profile_name(load_config_value("MASAMONG_PROFILE", "legacy"))
INSTANCE_NAME = _normalize_profile_name(
    load_config_value("MASAMONG_INSTANCE_NAME", PROFILE),
    PROFILE,
)
_require_explicit_profile_raw = load_config_value(
    "MASAMONG_REQUIRE_EXPLICIT_PROFILE",
    "false",
)
REQUIRE_EXPLICIT_PROFILE = as_strict_bool(
    "MASAMONG_REQUIRE_EXPLICIT_PROFILE",
    _require_explicit_profile_raw,
)
if (
    PROFILE in {"masamo", "general"}
    and str(_require_explicit_profile_raw).strip().lower() != "true"
):
    raise RuntimeError(
        "masamo/general 운영 프로필은 "
        "MASAMONG_REQUIRE_EXPLICIT_PROFILE=true를 파일에 명시해야 합니다."
    )
if REQUIRE_EXPLICIT_PROFILE and PROFILE == "legacy":
    raise RuntimeError(
        "MASAMONG_REQUIRE_EXPLICIT_PROFILE=true 이지만 MASAMONG_PROFILE이 지정되지 않았습니다."
    )
if REQUIRE_EXPLICIT_PROFILE and PROFILE not in {"masamo", "general"}:
    raise RuntimeError(
        "명시적 운영 프로필은 masamo 또는 general이어야 합니다."
    )
if REQUIRE_EXPLICIT_PROFILE and ENV_FILE_PATH is None:
    raise RuntimeError(
        "명시적 프로필은 MASAMONG_ENV_FILE을 프로세스 외부에서 지정해야 합니다. "
        "env 파일 안의 MASAMONG_ENV_FILE 항목만으로는 그 파일을 선택할 수 없습니다."
    )
if REQUIRE_EXPLICIT_PROFILE:
    if PROFILE != INSTANCE_NAME:
        raise RuntimeError(
            "명시적 프로필의 MASAMONG_PROFILE과 MASAMONG_INSTANCE_NAME이 다릅니다: "
            f"profile={PROFILE!r}, instance={INSTANCE_NAME!r}"
        )
    if PROFILE == "masamo":
        # 기존 저사양 서버의 값을 코드가 추정하면 배포 시 CPU/RU 제한이 조용히
        # 풀릴 수 있다. 현재 운영값을 선택한 masamo env 파일에 그대로 옮기도록
        # 강제하고, 무제한이나 오타는 시작 전에 거부한다.
        _masamo_positive_limit_keys = (
            "MASAMONG_CPU_THREADS",
            "AI_MAX_CONCURRENT_PROCESSING",
            "EMBEDDING_MAX_CONCURRENCY",
            "RAG_MAX_BACKGROUND_TASKS",
            "RAG_MAX_TRACKED_WINDOWS",
        )
        _masamo_resource_errors: list[str] = []
        for _limit_key in _masamo_positive_limit_keys:
            _limit_raw = _EXPLICIT_ENV_VALUES.get(_limit_key)
            try:
                _limit_value = int(str(_limit_raw).strip())
            except (TypeError, ValueError):
                _limit_value = 0
            if _limit_value <= 0:
                _masamo_resource_errors.append(
                    f"{_limit_key} (파일 내 양의 정수 필요)"
                )
        _tokenizers_parallelism_raw = _EXPLICIT_ENV_VALUES.get(
            "TOKENIZERS_PARALLELISM"
        )
        if (
            _tokenizers_parallelism_raw is None
            or str(_tokenizers_parallelism_raw).strip().lower()
            not in {"0", "false", "no", "n", "off"}
        ):
            _masamo_resource_errors.append(
                "TOKENIZERS_PARALLELISM (파일 내 false 필요)"
            )
        if _masamo_resource_errors:
            raise RuntimeError(
                "명시적 masamo 프로필은 현재 운영 서버의 저사양 제한값을 "
                "코드가 추정하지 않고 선택 env 파일에 그대로 복사해야 합니다: "
                + ", ".join(_masamo_resource_errors)
            )
    # config.json은 값이 없어도 인스턴스별 빈 객체 파일을 명시해 공용 fallback을 막는다.
    _read_config_json()
    _profile_file_errors: list[str] = []
    if "MASAMONG_CONFIG_FILE" not in _EXPLICIT_ENV_KEYS:
        _profile_file_errors.append("MASAMONG_CONFIG_FILE 미지정")
    elif not CONFIG_JSON_PATH.is_file():
        _profile_file_errors.append(f"config 파일 없음: {CONFIG_JSON_PATH}")
    elif _CONFIG_JSON_ERROR:
        _profile_file_errors.append(f"config 파일 오류: {_CONFIG_JSON_ERROR}")
    if "EMB_CONFIG_PATH" not in _EXPLICIT_ENV_KEYS:
        _profile_file_errors.append("EMB_CONFIG_PATH 미지정")
    elif not Path(EMBED_CONFIG_PATH).is_file() or _EMBED_CONFIG_ERROR:
        _profile_file_errors.append(
            f"embedding 설정 오류: {_EMBED_CONFIG_ERROR or 'file not found'}"
        )
    if "PROMPT_CONFIG_PATH" not in _EXPLICIT_ENV_KEYS:
        _profile_file_errors.append("PROMPT_CONFIG_PATH 미지정")
    elif not Path(PROMPT_CONFIG_PATH).is_file() or _PROMPT_CONFIG_ERROR:
        _profile_file_errors.append(
            f"prompt 설정 오류: {_PROMPT_CONFIG_ERROR or 'file not found'}"
        )
    if _profile_file_errors:
        raise RuntimeError(
            "명시적 프로필의 필수 설정 파일 검증에 실패했습니다: "
            + "; ".join(_profile_file_errors)
        )

TOKEN = load_config_value('DISCORD_BOT_TOKEN')
if REQUIRE_EXPLICIT_PROFILE and (
    not str(TOKEN or "").strip()
    or "replace-with" in str(TOKEN).strip().lower()
):
    raise RuntimeError(
        "명시적 운영 프로필은 실제 DISCORD_BOT_TOKEN을 지정해야 합니다."
    )
EXPECTED_DISCORD_BOT_USER_ID = as_int(
    load_config_value("MASAMONG_EXPECTED_DISCORD_BOT_USER_ID", 0),
    0,
)
if REQUIRE_EXPLICIT_PROFILE and EXPECTED_DISCORD_BOT_USER_ID <= 0:
    raise RuntimeError(
        "명시적 운영 프로필은 MASAMONG_EXPECTED_DISCORD_BOT_USER_ID를 "
        "양의 정수로 지정해야 합니다."
    )
COMMAND_PREFIX = as_str(load_config_value("MASAMONG_COMMAND_PREFIX", "!"), "!")
_LOG_FILE_NAME_RAW = as_str(
    load_config_value("MASAMONG_LOG_FILE", "discord_logs.txt"),
    "discord_logs.txt",
)
_ERROR_LOG_FILE_NAME_RAW = as_str(
    load_config_value("MASAMONG_ERROR_LOG_FILE", "error_logs.txt"),
    "error_logs.txt",
)
if REQUIRE_EXPLICIT_PROFILE:
    _invalid_log_paths = [
        setting_name
        for setting_name, configured_path in (
            ("MASAMONG_LOG_FILE", _LOG_FILE_NAME_RAW),
            ("MASAMONG_ERROR_LOG_FILE", _ERROR_LOG_FILE_NAME_RAW),
        )
        if not Path(configured_path).expanduser().is_absolute()
    ]
    if _invalid_log_paths:
        raise RuntimeError(
            "명시적 운영 프로필의 로그 경로는 절대 경로여야 합니다: "
            + ", ".join(_invalid_log_paths)
        )
    if (
        Path(_LOG_FILE_NAME_RAW).expanduser().resolve()
        == Path(_ERROR_LOG_FILE_NAME_RAW).expanduser().resolve()
        and Path(_LOG_FILE_NAME_RAW).expanduser().resolve()
        != Path(os.devnull).resolve()
    ):
        raise RuntimeError(
            "명시적 운영 프로필의 일반/오류 로그 파일은 서로 달라야 합니다."
        )
LOG_FILE_NAME = _resolve_project_storage_path(
    _LOG_FILE_NAME_RAW,
    "discord_logs.txt",
)
ERROR_LOG_FILE_NAME = _resolve_project_storage_path(
    _ERROR_LOG_FILE_NAME_RAW,
    "error_logs.txt",
)
LOG_MAX_BYTES = max(
    1024 * 1024,
    as_int(load_config_value("MASAMONG_LOG_MAX_BYTES", 10 * 1024 * 1024), 10 * 1024 * 1024),
)
LOG_BACKUP_COUNT = max(
    1,
    as_int(load_config_value("MASAMONG_LOG_BACKUP_COUNT", 5), 5),
)
DISCORD_LOG_QUEUE_MAXSIZE = max(
    10,
    as_int(load_config_value("MASAMONG_DISCORD_LOG_QUEUE_MAXSIZE", 500), 500),
)
DISCORD_OPERATIONS_LOG_CHANNEL_ID = as_int(
    load_config_value("MASAMONG_OPERATIONS_LOG_CHANNEL_ID", 0),
    0,
)
ANALYTICS_STORE_CONTENT = as_bool(
    load_config_value("MASAMONG_ANALYTICS_STORE_CONTENT", "false")
)
DISABLED_COGS = _parse_csv_set(load_config_value("MASAMONG_DISABLED_COGS", ""))
REQUIRED_COGS = _parse_csv_set(load_config_value("MASAMONG_REQUIRED_COGS", ""))
if REQUIRED_COGS & DISABLED_COGS:
    raise RuntimeError(
        "같은 Cog를 필수와 비활성 목록에 동시에 지정할 수 없습니다: "
        + ", ".join(sorted(REQUIRED_COGS & DISABLED_COGS))
    )
_auto_migrate_default = (
    "false"
    if REQUIRE_EXPLICIT_PROFILE and PROFILE == "masamo"
    else "true"
)
_auto_migrate_raw = load_config_value(
    "MASAMONG_AUTO_MIGRATE",
    _auto_migrate_default,
)
if REQUIRE_EXPLICIT_PROFILE and "MASAMONG_AUTO_MIGRATE" not in _EXPLICIT_ENV_KEYS:
    raise RuntimeError(
        "명시적 운영 프로필은 MASAMONG_AUTO_MIGRATE를 env 파일에 "
        "true 또는 false로 지정해야 합니다."
    )
AUTO_MIGRATE = (
    as_strict_bool("MASAMONG_AUTO_MIGRATE", _auto_migrate_raw)
    if REQUIRE_EXPLICIT_PROFILE
    else as_bool(_auto_migrate_raw, _auto_migrate_default == "true")
)
if REQUIRE_EXPLICIT_PROFILE and PROFILE == "masamo" and AUTO_MIGRATE:
    raise RuntimeError(
        "누적 운영 데이터가 있는 masamo 프로필에서는 런타임 "
        "MASAMONG_AUTO_MIGRATE=true를 허용하지 않습니다. "
        "MASAMONG_AUTO_MIGRATE=false로 기동하고 별도 검증된 migration 절차를 사용하세요."
    )
GUILD_SETTINGS_MODE = as_str(
    load_config_value("MASAMONG_GUILD_SETTINGS_MODE", "static"),
    "static",
).lower()
if GUILD_SETTINGS_MODE not in {"static", "database"}:
    raise RuntimeError(
        "MASAMONG_GUILD_SETTINGS_MODE은 static 또는 database여야 합니다."
    )

_cpu_thread_default = 1 if PROFILE == "general" else 0
CPU_THREAD_LIMIT = max(
    0,
    as_int(
        load_config_value("MASAMONG_CPU_THREADS", _cpu_thread_default),
        _cpu_thread_default,
    ),
)
if CPU_THREAD_LIMIT > 0:
    # torch/numpy가 import되기 전에 운영 제한을 전달한다. 명시적 프로필에서는
    # 상위 service/shell의 오래된 값이 선택한 프로필의 CPU 예산을 우회하지 못하게 한다.
    for _thread_env in (
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    ):
        if REQUIRE_EXPLICIT_PROFILE:
            os.environ[_thread_env] = str(CPU_THREAD_LIMIT)
        else:
            os.environ.setdefault(_thread_env, str(CPU_THREAD_LIMIT))
    if REQUIRE_EXPLICIT_PROFILE:
        os.environ["TOKENIZERS_PARALLELISM"] = "false"
    else:
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

DB_BACKEND = str(load_config_value('MASAMONG_DB_BACKEND', 'sqlite')).strip().lower()
if DB_BACKEND not in {"sqlite", "tidb"}:
    raise RuntimeError(f"지원하지 않는 MASAMONG_DB_BACKEND 값입니다: {DB_BACKEND}")
if REQUIRE_EXPLICIT_PROFILE and DB_BACKEND == "tidb":
    _relative_profile_config_paths = [
        setting_name
        for setting_name in (
            "MASAMONG_CONFIG_FILE",
            "EMB_CONFIG_PATH",
            "PROMPT_CONFIG_PATH",
        )
        if not Path(_EXPLICIT_ENV_VALUES.get(setting_name, "")).expanduser().is_absolute()
    ]
    if _relative_profile_config_paths:
        raise RuntimeError(
            "명시적 TiDB 프로필의 설정 파일 경로는 절대 경로여야 합니다: "
            + ", ".join(_relative_profile_config_paths)
        )
_DATABASE_FILE_RAW = load_config_value(
    "MASAMONG_DATABASE_FILE",
    "database/remasamong.db",
)
DATABASE_FILE = _resolve_project_storage_path(
    _DATABASE_FILE_RAW,
    "database/remasamong.db",
)
if REQUIRE_EXPLICIT_PROFILE and DB_BACKEND == "sqlite":
    if "MASAMONG_DATABASE_FILE" not in _EXPLICIT_ENV_KEYS:
        raise RuntimeError(
            "명시적 SQLite 프로필은 MASAMONG_DATABASE_FILE을 파일에 지정해야 합니다."
        )
    _database_file_text = str(_DATABASE_FILE_RAW).strip()
    if _database_file_text != ":memory:":
        _database_file_path = Path(_database_file_text).expanduser()
        if not _database_file_path.is_absolute():
            raise RuntimeError(
                "명시적 SQLite 프로필의 MASAMONG_DATABASE_FILE은 "
                "절대 경로여야 합니다."
            )
        if not re.search(
            rf"(^|[^a-z0-9]){re.escape(PROFILE)}([^a-z0-9]|$)",
            _database_file_path.as_posix().lower(),
        ):
            raise RuntimeError(
                "명시적 SQLite 프로필의 MASAMONG_DATABASE_FILE 경로에는 "
                f"인스턴스 이름 {PROFILE!r}이 포함되어야 합니다."
            )
TIDB_HOST = load_config_value('MASAMONG_DB_HOST')
TIDB_PORT = as_int(load_config_value('MASAMONG_DB_PORT', 4000), 4000)
TIDB_NAME = as_str(load_config_value('MASAMONG_DB_NAME', 'masamong'), 'masamong')
TIDB_USER = load_config_value('MASAMONG_DB_USER')
TIDB_PASSWORD = load_config_value('MASAMONG_DB_PASSWORD')
TIDB_SSL_CA = load_config_value('MASAMONG_DB_SSL_CA')
TIDB_SSL_VERIFY_IDENTITY = as_bool(load_config_value('MASAMONG_DB_SSL_VERIFY_IDENTITY', 'true'))
TIDB_CONNECT_TIMEOUT = max(
    1,
    as_int(load_config_value("MASAMONG_DB_CONNECT_TIMEOUT", 10), 10),
)
TIDB_READ_TIMEOUT = max(
    1,
    as_int(load_config_value("MASAMONG_DB_READ_TIMEOUT", 30), 30),
)
TIDB_WRITE_TIMEOUT = max(
    1,
    as_int(load_config_value("MASAMONG_DB_WRITE_TIMEOUT", 30), 30),
)
TIDB_CONN_MAX_LIFETIME_SECONDS = max(
    60,
    as_int(load_config_value("MASAMONG_DB_CONN_MAX_LIFETIME_SECONDS", 600), 600),
)
REMOTE_DB_STRICT_MODE = as_bool(load_config_value('MASAMONG_DB_STRICT_REMOTE_ONLY', 'false'))
# 기존 strict 운영 환경에 별도 REQUIRE_TLS 키가 없어도 strict 모드 자체가
# TLS 필수를 의미하도록 한다. 명시적으로 false를 적어도 strict를 우회할 수 없다.
REQUIRE_DB_TLS = (
    as_bool(load_config_value("MASAMONG_DB_REQUIRE_TLS", "false"))
    or REMOTE_DB_STRICT_MODE
)
EXPECTED_DB_NAME = as_str(load_config_value("MASAMONG_EXPECTED_DB_NAME", ""), "")
if REQUIRE_EXPLICIT_PROFILE and DB_BACKEND == "tidb" and not EXPECTED_DB_NAME:
    raise RuntimeError(
        "명시적 TiDB 프로필은 MASAMONG_EXPECTED_DB_NAME을 지정해야 합니다."
    )
if EXPECTED_DB_NAME and DB_BACKEND == "tidb" and TIDB_NAME != EXPECTED_DB_NAME:
    raise RuntimeError(
        "설정된 TiDB 이름이 MASAMONG_EXPECTED_DB_NAME과 다릅니다: "
        f"configured={TIDB_NAME!r}, expected={EXPECTED_DB_NAME!r}"
    )
if REQUIRE_EXPLICIT_PROFILE and DB_BACKEND == "tidb":
    _profile_db_name = {
        "masamo": "masamong",
        "general": "masamong_general",
    }[PROFILE]
    if TIDB_NAME != _profile_db_name:
        raise RuntimeError(
            "명시적 운영 프로필의 TiDB 이름이 고정 경계와 다릅니다: "
            f"profile={PROFILE!r}, configured={TIDB_NAME!r}, "
            f"required={_profile_db_name!r}"
        )
    _explicit_tidb_missing = [
        setting_name
        for setting_name, setting_value in (
            ("MASAMONG_DB_HOST", TIDB_HOST),
            ("MASAMONG_DB_USER", TIDB_USER),
            ("MASAMONG_DB_PASSWORD", TIDB_PASSWORD),
            ("MASAMONG_DB_SSL_CA", TIDB_SSL_CA),
        )
        if not setting_value
    ]
    if _explicit_tidb_missing:
        raise RuntimeError(
            "명시적 TiDB 프로필의 필수 설정이 누락되었습니다: "
            + ", ".join(_explicit_tidb_missing)
        )
    if not REMOTE_DB_STRICT_MODE or not REQUIRE_DB_TLS:
        raise RuntimeError(
            "명시적 TiDB 프로필은 strict remote 및 TLS 필수 모드여야 합니다."
        )
    if not TIDB_SSL_VERIFY_IDENTITY:
        raise RuntimeError(
            "명시적 TiDB 프로필은 TLS hostname 검증을 켜야 합니다."
        )
    _explicit_ca_path = Path(str(TIDB_SSL_CA)).expanduser()
    if not _explicit_ca_path.is_absolute() or not _explicit_ca_path.is_file():
        raise RuntimeError(
            "명시적 TiDB 프로필의 MASAMONG_DB_SSL_CA는 "
            f"존재하는 절대 파일이어야 합니다: {_explicit_ca_path}"
        )
if DB_BACKEND == "tidb" and REQUIRE_DB_TLS and not TIDB_SSL_CA:
    raise RuntimeError(
        "MASAMONG_DB_REQUIRE_TLS=true 이지만 MASAMONG_DB_SSL_CA가 없습니다."
    )
if REMOTE_DB_STRICT_MODE:
    if DB_BACKEND != "tidb":
        raise RuntimeError(
            "MASAMONG_DB_STRICT_REMOTE_ONLY=true 인 경우 MASAMONG_DB_BACKEND=tidb 여야 합니다."
        )
    _missing_tidb = []
    if not TIDB_HOST:
        _missing_tidb.append("MASAMONG_DB_HOST")
    if not TIDB_USER:
        _missing_tidb.append("MASAMONG_DB_USER")
    if not TIDB_NAME:
        _missing_tidb.append("MASAMONG_DB_NAME")
    if REQUIRE_DB_TLS and not TIDB_SSL_CA:
        _missing_tidb.append("MASAMONG_DB_SSL_CA")
    if _missing_tidb:
        raise RuntimeError(
            "MASAMONG_DB_STRICT_REMOTE_ONLY=true 이지만 TiDB 필수 설정이 누락되었습니다: "
            + ", ".join(_missing_tidb)
        )
    if not TIDB_SSL_VERIFY_IDENTITY:
        raise RuntimeError(
            "MASAMONG_DB_STRICT_REMOTE_ONLY=true 인 경우 "
            "MASAMONG_DB_SSL_VERIFY_IDENTITY=true 여야 합니다."
        )
GEMINI_API_KEY = load_config_value('GEMINI_API_KEY')
GOOGLE_API_KEY = load_config_value('GOOGLE_API_KEY')
GOOGLE_CX = load_config_value('GOOGLE_CX')
GOOGLE_CUSTOM_SEARCH_DAILY_LIMIT = as_int(load_config_value('GOOGLE_CUSTOM_SEARCH_DAILY_LIMIT', 100), 100)
LINKUP_API_KEY = as_str(load_config_value('LINKUP_API_KEY', ''), '')
LINKUP_BASE_URL = as_str(
    load_config_value('LINKUP_BASE_URL', 'https://api.linkup.so/v1'),
    'https://api.linkup.so/v1',
).rstrip("/")

# ========== API 안전장치 설정 ==========
# 사용자별 쿨다운 (초) - 한 사용자가 연속 요청 시 대기 시간
USER_COOLDOWN_SECONDS = as_int(load_config_value('USER_COOLDOWN_SECONDS', 3), 3)
# 일일 LLM 호출 제한 (사용자당)
USER_DAILY_LLM_LIMIT = as_int(load_config_value('USER_DAILY_LLM_LIMIT', 200), 200)
# 글로벌 일일 LLM 호출 제한
GLOBAL_DAILY_LLM_LIMIT = as_int(load_config_value('GLOBAL_DAILY_LLM_LIMIT', 5000), 5000)
# [저사양 보호] 동시에 처리할 수 있는 AI 메시지 최대 개수 (전역 세마포어).
# 저사양 서버에서 동시 요청이 몰릴 때 임베딩/LLM 폭주를 막는다. 서버 사양에 맞게 조정.
_ai_concurrency_default = 1 if PROFILE == "general" else 3
AI_MAX_CONCURRENT_PROCESSING = max(
    1,
    as_int(
        load_config_value(
            'AI_MAX_CONCURRENT_PROCESSING',
            _ai_concurrency_default,
        ),
        _ai_concurrency_default,
    ),
)
# 로컬 임베딩 encode는 CPU 사용량이 크므로 별도 동시성/대기 태스크 상한을 둔다.
EMBEDDING_MAX_CONCURRENCY = max(
    1,
    as_int(load_config_value("EMBEDDING_MAX_CONCURRENCY", 1), 1),
)
_rag_background_tasks_default = 2 if PROFILE == "general" else 16
RAG_MAX_BACKGROUND_TASKS = max(
    1,
    as_int(
        load_config_value(
            "RAG_MAX_BACKGROUND_TASKS",
            _rag_background_tasks_default,
        ),
        _rag_background_tasks_default,
    ),
)
_rag_tracked_windows_default = 64 if PROFILE == "general" else 256
RAG_MAX_TRACKED_WINDOWS = max(
    1,
    as_int(
        load_config_value(
            "RAG_MAX_TRACKED_WINDOWS",
            _rag_tracked_windows_default,
        ),
        _rag_tracked_windows_default,
    ),
)
# 프롬프트 최대 토큰 (초과 시 RAG 컨텍스트 줄임)
MAX_PROMPT_TOKENS = as_int(load_config_value('MAX_PROMPT_TOKENS', 4000), 4000)
# 동일 메시지 스팸 방지 시간 (초)
SPAM_PREVENTION_SECONDS = as_int(load_config_value('SPAM_PREVENTION_SECONDS', 10), 10)

# --- 대화 히스토리 및 RAG 제한 설정 ---
# 메인 답변 시 가져올 이전 대화 개수 (RAG 사용 시 / 미사용 시)
HISTORY_LIMIT_WITH_RAG = as_int(load_config_value('HISTORY_LIMIT_WITH_RAG', 8), 8)
HISTORY_LIMIT_WITHOUT_RAG = as_int(load_config_value('HISTORY_LIMIT_WITHOUT_RAG', 12), 12)
# 도구 의도 분석 시 참고할 이전 대화 개수
INTENT_HISTORY_LIMIT = as_int(load_config_value('INTENT_HISTORY_LIMIT', 5), 5)
INTENT_LLM_ENABLED = as_bool(load_config_value('INTENT_LLM_ENABLED', 'true'))
INTENT_LLM_ALWAYS_RUN = as_bool(load_config_value('INTENT_LLM_ALWAYS_RUN', 'true'))
INTENT_LLM_RAG_STRONG_BYPASS = as_bool(load_config_value('INTENT_LLM_RAG_STRONG_BYPASS', 'true'))

# 메시지 1개당 최대 글자수 (프롬프트 포함 시)
MAX_MESSAGE_CHARS = as_int(load_config_value('MAX_MESSAGE_CHARS', 1800), 1800)
# RAG 결과 1개당 최대 글자수
MAX_RAG_BLOCK_CHARS = as_int(load_config_value('MAX_RAG_BLOCK_CHARS', 500), 500)
# RAG 컨텍스트 최대 개수
MAX_RAG_RESULTS = as_int(load_config_value('MAX_RAG_RESULTS', 5), 5)

# CometAPI 설정 (Gemini 대체 - OpenAI Compatible)
COMETAPI_KEY = load_config_value('COMETAPI_KEY')
COMETAPI_BASE_URL = load_config_value('COMETAPI_BASE_URL', 'https://api.cometapi.com/v1')
COMETAPI_MODEL = load_config_value('COMETAPI_MODEL', 'DeepSeek-V3.2-Exp-nothinking')
USE_COMETAPI = as_bool(load_config_value('USE_COMETAPI', 'true'))  # CometAPI 우선 사용
ALLOW_DIRECT_GEMINI_FALLBACK = as_bool(load_config_value('ALLOW_DIRECT_GEMINI_FALLBACK', 'false'))

# Fast 모델 (웹 검색 중간 단계: 의도 분석, 키워드 생성, 기사 요약)
# news/news_summarizer.py와 동일한 모델 사용
FAST_MODEL_NAME = load_config_value('FAST_MODEL_NAME', 'gemini-3.1-flash-lite-preview')

# ========== LLM 레인 구성 (Primary/Fallback) ==========
# 레인1: 판단/웹검색(의도 분석, 쿼리 정제, 웹 RAG 요약)
LLM_ROUTING_PRIMARY_PROVIDER = normalize_llm_provider(
    load_config_value('LLM_ROUTING_PRIMARY_PROVIDER', 'gemini_compat' if USE_COMETAPI else 'none')
)
LLM_ROUTING_PRIMARY_MODEL = as_str(
    load_config_value('LLM_ROUTING_PRIMARY_MODEL', FAST_MODEL_NAME),
    FAST_MODEL_NAME,
)
LLM_ROUTING_PRIMARY_BASE_URL = as_str(
    load_config_value('LLM_ROUTING_PRIMARY_BASE_URL', 'https://api.cometapi.com'),
    'https://api.cometapi.com',
)
LLM_ROUTING_PRIMARY_API_KEY = as_str(
    load_config_value('LLM_ROUTING_PRIMARY_API_KEY', COMETAPI_KEY),
    '',
)
LLM_ROUTING_PRIMARY_REASONING_EFFORT = as_str(
    load_config_value(
        'LLM_ROUTING_PRIMARY_REASONING_EFFORT',
        load_config_value(
            'LLM_ROUTING_REASONING_EFFORT',
            default_reasoning_effort_for_model(LLM_ROUTING_PRIMARY_MODEL),
        ),
    ),
    '',
)

LLM_ROUTING_FALLBACK_PROVIDER = normalize_llm_provider(
    load_config_value('LLM_ROUTING_FALLBACK_PROVIDER', 'none')
)
LLM_ROUTING_FALLBACK_MODEL = as_str(
    load_config_value('LLM_ROUTING_FALLBACK_MODEL', FAST_MODEL_NAME),
    FAST_MODEL_NAME,
)
LLM_ROUTING_FALLBACK_BASE_URL = as_str(
    load_config_value('LLM_ROUTING_FALLBACK_BASE_URL', COMETAPI_BASE_URL),
    COMETAPI_BASE_URL,
)
LLM_ROUTING_FALLBACK_API_KEY = as_str(
    load_config_value('LLM_ROUTING_FALLBACK_API_KEY', COMETAPI_KEY),
    '',
)
LLM_ROUTING_FALLBACK_REASONING_EFFORT = as_str(
    load_config_value(
        'LLM_ROUTING_FALLBACK_REASONING_EFFORT',
        load_config_value(
            'LLM_ROUTING_REASONING_EFFORT',
            default_reasoning_effort_for_model(LLM_ROUTING_FALLBACK_MODEL),
        ),
    ),
    '',
)
ROUTING_LLM_MAX_TOKENS = max(64, as_int(load_config_value('ROUTING_LLM_MAX_TOKENS', 1024), 1024))

# 레인2: 최종 답변/요약/명령어 생성
LLM_MAIN_PRIMARY_PROVIDER = normalize_llm_provider(
    load_config_value('LLM_MAIN_PRIMARY_PROVIDER', 'openai_compat' if USE_COMETAPI else 'none')
)
LLM_MAIN_PRIMARY_MODEL = as_str(
    load_config_value('LLM_MAIN_PRIMARY_MODEL', COMETAPI_MODEL),
    COMETAPI_MODEL,
)
LLM_MAIN_PRIMARY_BASE_URL = as_str(
    load_config_value('LLM_MAIN_PRIMARY_BASE_URL', COMETAPI_BASE_URL),
    COMETAPI_BASE_URL,
)
LLM_MAIN_PRIMARY_API_KEY = as_str(
    load_config_value('LLM_MAIN_PRIMARY_API_KEY', COMETAPI_KEY),
    '',
)
LLM_MAIN_PRIMARY_REASONING_EFFORT = as_str(
    load_config_value(
        'LLM_MAIN_PRIMARY_REASONING_EFFORT',
        load_config_value(
            'LLM_MAIN_REASONING_EFFORT',
            default_reasoning_effort_for_model(LLM_MAIN_PRIMARY_MODEL),
        ),
    ),
    '',
)

LLM_MAIN_FALLBACK_PROVIDER = normalize_llm_provider(
    load_config_value('LLM_MAIN_FALLBACK_PROVIDER', 'none')
)
LLM_MAIN_FALLBACK_MODEL = as_str(
    load_config_value('LLM_MAIN_FALLBACK_MODEL', COMETAPI_MODEL),
    COMETAPI_MODEL,
)
LLM_MAIN_FALLBACK_BASE_URL = as_str(
    load_config_value('LLM_MAIN_FALLBACK_BASE_URL', COMETAPI_BASE_URL),
    COMETAPI_BASE_URL,
)
LLM_MAIN_FALLBACK_API_KEY = as_str(
    load_config_value('LLM_MAIN_FALLBACK_API_KEY', COMETAPI_KEY),
    '',
)
LLM_MAIN_FALLBACK_REASONING_EFFORT = as_str(
    load_config_value(
        'LLM_MAIN_FALLBACK_REASONING_EFFORT',
        load_config_value(
            'LLM_MAIN_REASONING_EFFORT',
            default_reasoning_effort_for_model(LLM_MAIN_FALLBACK_MODEL),
        ),
    ),
    '',
)
MAIN_LLM_MAX_TOKENS = max(128, as_int(load_config_value('MAIN_LLM_MAX_TOKENS', 8192), 8192))

# Kakao 임베딩/요약 스크립트용 LLM 설정
# 기본값은 메인 레인 Primary를 따르고, 미설정 시 COMETAPI_*로 후순위 fallback
_DEFAULT_KAKAO_SUMMARY_API_KEY = LLM_MAIN_PRIMARY_API_KEY or COMETAPI_KEY or ""
_DEFAULT_KAKAO_SUMMARY_BASE_URL = LLM_MAIN_PRIMARY_BASE_URL or COMETAPI_BASE_URL or "https://api.cometapi.com/v1"
KAKAO_SUMMARY_API_KEY = as_str(
    load_config_value('KAKAO_SUMMARY_API_KEY', _DEFAULT_KAKAO_SUMMARY_API_KEY),
    '',
)
KAKAO_SUMMARY_BASE_URL = as_str(
    load_config_value('KAKAO_SUMMARY_BASE_URL', _DEFAULT_KAKAO_SUMMARY_BASE_URL),
    _DEFAULT_KAKAO_SUMMARY_BASE_URL,
)
KAKAO_SUMMARY_MODEL_STANDARD = as_str(
    load_config_value('KAKAO_SUMMARY_MODEL_STANDARD', 'DeepSeek-V3.2-Exp-nothinking'),
    'DeepSeek-V3.2-Exp-nothinking',
)
KAKAO_SUMMARY_MODEL_BUDGET = as_str(
    load_config_value('KAKAO_SUMMARY_MODEL_BUDGET', 'gpt-5-nano-2025-08-07'),
    'gpt-5-nano-2025-08-07',
)

# DuckDuckGo 웹 검색 활성화 여부 (기본: 활성화)
DDGS_ENABLED = as_bool(load_config_value('DDGS_ENABLED', 'true'))
# 웹 검색 제공자 선택 (linkup | legacy)
WEB_SEARCH_PROVIDER = as_str(
    load_config_value('WEB_SEARCH_PROVIDER', 'linkup' if LINKUP_API_KEY else 'legacy'),
    'legacy',
).lower()
if WEB_SEARCH_PROVIDER not in {"linkup", "legacy"}:
    WEB_SEARCH_PROVIDER = "legacy"

# Linkup 검색 설정
LINKUP_ENABLED = as_bool(load_config_value('LINKUP_ENABLED', 'true'))
LINKUP_TIMEOUT_SECONDS = max(5, as_int(load_config_value('LINKUP_TIMEOUT_SECONDS', 40), 40))
LINKUP_FETCH_RENDER_JS = as_bool(load_config_value('LINKUP_FETCH_RENDER_JS', 'true'))
LINKUP_OUTPUT_TYPE = as_str(load_config_value('LINKUP_OUTPUT_TYPE', 'searchResults'), 'searchResults')
if LINKUP_OUTPUT_TYPE not in {"searchResults", "sourcedAnswer", "structured"}:
    LINKUP_OUTPUT_TYPE = "searchResults"
LINKUP_FAST_MAX_RESULTS = max(1, as_int(load_config_value('LINKUP_FAST_MAX_RESULTS', 5), 5))
LINKUP_STANDARD_MAX_RESULTS = max(1, as_int(load_config_value('LINKUP_STANDARD_MAX_RESULTS', 8), 8))
LINKUP_DEEP_MAX_RESULTS = max(1, as_int(load_config_value('LINKUP_DEEP_MAX_RESULTS', 10), 10))
LINKUP_REALTIME_LOOKBACK_DAYS = max(1, as_int(load_config_value('LINKUP_REALTIME_LOOKBACK_DAYS', 30), 30))
LINKUP_QUALITY_RETRY_ENABLED = as_bool(load_config_value('LINKUP_QUALITY_RETRY_ENABLED', 'true'))
LINKUP_DEEP_RETRY_MIN_SOURCES = max(1, as_int(load_config_value('LINKUP_DEEP_RETRY_MIN_SOURCES', 2), 2))
LINKUP_MIN_ANSWER_CHARS = max(20, as_int(load_config_value('LINKUP_MIN_ANSWER_CHARS', 120), 120))
LINKUP_CONTEXT_MAX_CHARS = max(800, as_int(load_config_value('LINKUP_CONTEXT_MAX_CHARS', 3200), 3200))
LINKUP_CONTEXT_SOURCE_BLOCKS = max(1, as_int(load_config_value('LINKUP_CONTEXT_SOURCE_BLOCKS', 4), 4))
LINKUP_CONTEXT_SNIPPET_MAX_CHARS = max(80, as_int(load_config_value('LINKUP_CONTEXT_SNIPPET_MAX_CHARS', 300), 300))
LINKUP_MONTHLY_BUDGET_EUR = max(
    0.0,
    as_float(load_config_value('LINKUP_MONTHLY_BUDGET_EUR', 4.5), 4.5),
)
LINKUP_MONTHLY_BUDGET_ENFORCED = as_bool(load_config_value('LINKUP_MONTHLY_BUDGET_ENFORCED', 'true'))

# 범용 웹 탐색 파이프라인 예산/캐시 설정
WEB_RAG_FAST_LLM_MAX_CALLS = max(0, as_int(load_config_value('WEB_RAG_FAST_LLM_MAX_CALLS', 3), 3))
WEB_RAG_MAX_SELECTED_URLS = max(1, as_int(load_config_value('WEB_RAG_MAX_SELECTED_URLS', 5), 5))
WEB_RAG_MAX_SUMMARIZED_ARTICLES = max(1, as_int(load_config_value('WEB_RAG_MAX_SUMMARIZED_ARTICLES', 4), 4))
WEB_RAG_MAX_CANDIDATES = max(5, as_int(load_config_value('WEB_RAG_MAX_CANDIDATES', 24), 24))
WEB_RAG_CACHE_TTL_SECONDS = max(0, as_int(load_config_value('WEB_RAG_CACHE_TTL_SECONDS', 300), 300))
WEB_RAG_CACHE_MAX_ENTRIES = max(1, as_int(load_config_value('WEB_RAG_CACHE_MAX_ENTRIES', 128), 128))
WEB_RAG_FAST_PROMPT_MAX_CHARS = max(800, as_int(load_config_value('WEB_RAG_FAST_PROMPT_MAX_CHARS', 8000), 8000))
WEB_RAG_CONTEXT_MAX_CHARS = max(800, as_int(load_config_value('WEB_RAG_CONTEXT_MAX_CHARS', 3500), 3500))
WEB_SEARCH_REFINE_WITH_LLM = as_bool(load_config_value('WEB_SEARCH_REFINE_WITH_LLM', 'false'))
AUTO_WEB_SEARCH_COOLDOWN_SECONDS = max(0, as_int(load_config_value('AUTO_WEB_SEARCH_COOLDOWN_SECONDS', 90), 90))
AUTO_WEB_SEARCH_ALLOW_SHORT_FOLLOWUP = as_bool(load_config_value('AUTO_WEB_SEARCH_ALLOW_SHORT_FOLLOWUP', 'false'))


# CometAPI 이미지 생성 설정 (Gemini via CometAPI Gemini-compatible)
COMETAPI_IMAGE_ENABLED = as_bool(load_config_value('COMETAPI_IMAGE_ENABLED', 'true'))
COMETAPI_IMAGE_API_KEY = as_str(
    load_config_value('COMETAPI_IMAGE_API_KEY', COMETAPI_KEY),
    '',
)
COMETAPI_IMAGE_BASE_URL = as_str(
    load_config_value('COMETAPI_IMAGE_BASE_URL', 'https://api.cometapi.com'),
    'https://api.cometapi.com',
)
# 사용 모델: 'gemini-3.1-flash-image' (preview 제외, 일반 버전)
IMAGE_MODEL = as_str(load_config_value('IMAGE_MODEL', 'gemini-3.1-flash-image'), 'gemini-3.1-flash-image')
# 이미지 가로세로 비율: "1:1","2:3","3:2","3:4","4:3","4:5","5:4","9:16","16:9","21:9"
IMAGE_ASPECT_RATIO = as_str(load_config_value('IMAGE_ASPECT_RATIO', '1:1'), '1:1')

# 이미지 생성 사용량 제한
IMAGE_USER_LIMIT = as_int(load_config_value('IMAGE_USER_LIMIT', 10), 10)  # 유저당 6시간 내 최대 10장
IMAGE_USER_RESET_HOURS = as_int(load_config_value('IMAGE_USER_RESET_HOURS', 6), 6)  # 6시간 후 리셋
IMAGE_GLOBAL_DAILY_LIMIT = as_int(load_config_value('IMAGE_GLOBAL_DAILY_LIMIT', 50), 50)  # 전역 일일 50장

# 이미지 생성 안전 설정
IMAGE_SAFETY_TOLERANCE = 0  # 가장 엄격한 수준 (0=strict, 5=permissive) - 절대 변경 금지


FINNHUB_API_KEY = load_config_value('FINNHUB_API_KEY', 'YOUR_FINNHUB_API_KEY')
KAKAO_API_KEY = load_config_value('KAKAO_API_KEY', 'YOUR_KAKAO_API_KEY')
KRX_API_KEY = load_config_value('KRX_API_KEY')
EXIM_API_KEY_KR = load_config_value('EXIM_API_KEY_KR', 'YOUR_EXIM_API_KEY_KR')
KMA_API_KEY = load_config_value('KMA_API_KEY')
FINNHUB_BASE_URL = load_config_value('FINNHUB_BASE_URL', "https://finnhub.io/api/v1")
KAKAO_BASE_URL = load_config_value('KAKAO_BASE_URL', "https://dapi.kakao.com/v2/local/search/keyword.json")
KRX_BASE_URL = load_config_value('KRX_BASE_URL', "https://apis.data.go.kr/1160100/service/GetStockSecuritiesInfoService/getStockPriceInfo")
KMA_BASE_URL = load_config_value('KMA_BASE_URL', "https://apihub.kma.go.kr/api/typ02/openApi/VilageFcstInfoService_2.0")
EXIM_BASE_URL = load_config_value('EXIM_BASE_URL', "https://www.koreaexim.go.kr/site/program/financial/exchangeJSON")

DISCORD_EMBEDDING_BACKEND = str(
    load_config_value('DISCORD_EMBEDDING_BACKEND', 'tidb' if DB_BACKEND == 'tidb' else 'sqlite')
).strip().lower()
KAKAO_STORE_BACKEND = str(
    load_config_value('KAKAO_STORE_BACKEND', 'tidb' if DB_BACKEND == 'tidb' else 'local')
).strip().lower()
if DISCORD_EMBEDDING_BACKEND not in {"sqlite", "tidb"}:
    raise RuntimeError(
        "DISCORD_EMBEDDING_BACKEND는 sqlite 또는 tidb여야 합니다."
    )
if KAKAO_STORE_BACKEND not in {"local", "tidb"}:
    raise RuntimeError("KAKAO_STORE_BACKEND는 local 또는 tidb여야 합니다.")
MEMORY_SOURCES = _parse_csv_set(
    load_config_value("MASAMONG_MEMORY_SOURCES", "discord,kakao")
)
_unsupported_memory_sources = MEMORY_SOURCES - {"discord", "kakao"}
if _unsupported_memory_sources:
    raise RuntimeError(
        "지원하지 않는 MASAMONG_MEMORY_SOURCES 값입니다: "
        + ", ".join(sorted(_unsupported_memory_sources))
    )
KAKAO_MEMORY_ENABLED = "kakao" in MEMORY_SOURCES
if PROFILE == "general" and MEMORY_SOURCES != {"discord"}:
    raise RuntimeError(
        "general 프로필의 MASAMONG_MEMORY_SOURCES는 정확히 discord여야 합니다."
    )
if PROFILE == "masamo" and MEMORY_SOURCES != {"discord", "kakao"}:
    raise RuntimeError(
        "masamo 프로필의 MASAMONG_MEMORY_SOURCES는 discord,kakao여야 합니다."
    )
if REMOTE_DB_STRICT_MODE:
    # 원격 DB 강제 모드에서는 로컬 파일 기반 저장소를 사용하지 않는다.
    DISCORD_EMBEDDING_BACKEND = "tidb"
    KAKAO_STORE_BACKEND = "tidb"
_DISCORD_EMBEDDING_DB_PATH_RAW = as_str(
    load_config_value(
        "DISCORD_EMBEDDING_DB_PATH",
        EMBED_CONFIG.get("discord_db_path", "database/discord_embeddings.db"),
    ),
    "database/discord_embeddings.db",
)
DISCORD_EMBEDDING_DB_PATH = _resolve_project_storage_path(
    _DISCORD_EMBEDDING_DB_PATH_RAW,
    "database/discord_embeddings.db",
)
_KAKAO_EMBEDDING_DB_PATH_RAW = as_str(
    load_config_value(
        "KAKAO_EMBEDDING_DB_PATH",
        EMBED_CONFIG.get("kakao_db_path", "database/kakao_embeddings.db"),
    ),
    "database/kakao_embeddings.db",
)
KAKAO_EMBEDDING_DB_PATH = _resolve_project_storage_path(
    _KAKAO_EMBEDDING_DB_PATH_RAW,
    "database/kakao_embeddings.db",
)
KAKAO_EMBEDDING_SERVER_MAP = _normalize_kakao_servers(EMBED_CONFIG.get("kakao_servers", []))
KAKAO_VECTOR_EXTENSION = EMBED_CONFIG.get("kakao_vector_extension")
DISCORD_EMBEDDING_TIDB_TABLE = str(load_config_value('DISCORD_EMBEDDING_TIDB_TABLE', 'discord_chat_embeddings')).strip()
KAKAO_TIDB_TABLE = str(load_config_value('KAKAO_TIDB_TABLE', 'kakao_chunks')).strip()
for _table_setting_name, _table_name in (
    ("DISCORD_EMBEDDING_TIDB_TABLE", DISCORD_EMBEDDING_TIDB_TABLE),
    ("KAKAO_TIDB_TABLE", KAKAO_TIDB_TABLE),
):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", _table_name):
        raise RuntimeError(
            f"{_table_setting_name}은 단순 SQL 식별자만 사용할 수 있습니다."
        )
if REQUIRE_EXPLICIT_PROFILE and (
    DISCORD_EMBEDDING_TIDB_TABLE != "discord_chat_embeddings"
    or KAKAO_TIDB_TABLE != "kakao_chunks"
):
    raise RuntimeError(
        "명시적 운영 프로필은 검증된 기본 임베딩 테이블 이름만 사용할 수 있습니다."
    )
if REQUIRE_EXPLICIT_PROFILE:
    _profile_embedding_errors: list[str] = []
    if DISCORD_EMBEDDING_TIDB_TABLE != "discord_chat_embeddings":
        _profile_embedding_errors.append(
            "DISCORD_EMBEDDING_TIDB_TABLE은 discord_chat_embeddings여야 함"
        )
    if KAKAO_TIDB_TABLE != "kakao_chunks":
        _profile_embedding_errors.append(
            "KAKAO_TIDB_TABLE은 kakao_chunks여야 함"
        )
    for _embedding_path_key, _embedding_path_value in (
        ("discord_db_path", _DISCORD_EMBEDDING_DB_PATH_RAW),
        ("kakao_db_path", _KAKAO_EMBEDDING_DB_PATH_RAW),
    ):
        if not Path(_embedding_path_value).expanduser().is_absolute():
            _profile_embedding_errors.append(
                f"{_embedding_path_key}는 절대 경로여야 함"
            )
    if PROFILE == "general" and KAKAO_EMBEDDING_SERVER_MAP:
        _profile_embedding_errors.append("general에는 Kakao mapping을 둘 수 없음")
    if PROFILE == "masamo" and not KAKAO_EMBEDDING_SERVER_MAP:
        _profile_embedding_errors.append(
            "기존 Kakao 기억 보존을 위한 server mapping이 없음"
        )
    if _profile_embedding_errors:
        raise RuntimeError(
            "명시적 프로필의 embedding 경계 검증에 실패했습니다: "
            + "; ".join(_profile_embedding_errors)
        )

# 검색 엔진 활성화 설정. 환경변수로 프로필별 강제 비활성화할 수 있다.
EMBEDDING_ENABLED = as_bool(
    load_config_value("EMBEDDING_ENABLED", EMBED_CONFIG.get("embedding_enabled", True)),
    True,
)
# BM25는 현재 운영 정책상 사용하지 않음 (로컬/서버 공통 비활성화)
BM25_ENABLED = False
BM25_DATABASE_PATH = None
LOCAL_EMBEDDING_MODEL_NAME = EMBED_CONFIG.get("embedding_model_name", "dragonkue/multilingual-e5-small-ko-v2")
LOCAL_EMBEDDING_DEVICE = EMBED_CONFIG.get("embedding_device")
LOCAL_EMBEDDING_NORMALIZE = EMBED_CONFIG.get("normalize_embeddings", True)
LOCAL_EMBEDDING_LOCAL_FILES_ONLY = as_bool(
    load_config_value('LOCAL_EMBEDDING_LOCAL_FILES_ONLY', EMBED_CONFIG.get("local_files_only", False))
)
LOCAL_EMBEDDING_QUERY_LIMIT = EMBED_CONFIG.get("query_limit", 200)
RAG_SIMILARITY_THRESHOLD = as_float(EMBED_CONFIG.get("similarity_threshold"), 0.6)
STRUCTURED_MEMORY_QUERY_LIMIT = as_int(
    load_config_value(
        'STRUCTURED_MEMORY_QUERY_LIMIT',
        EMBED_CONFIG.get("structured_memory_query_limit", max(800, int(LOCAL_EMBEDDING_QUERY_LIMIT) * 4)),
    ),
    max(800, int(LOCAL_EMBEDDING_QUERY_LIMIT) * 4),
)
STRUCTURED_MEMORY_FALLBACK_QUERY_LIMIT = as_int(
    load_config_value(
        'STRUCTURED_MEMORY_FALLBACK_QUERY_LIMIT',
        EMBED_CONFIG.get("structured_memory_fallback_query_limit", max(2000, int(LOCAL_EMBEDDING_QUERY_LIMIT) * 10)),
    ),
    max(2000, int(LOCAL_EMBEDDING_QUERY_LIMIT) * 10),
)
STRUCTURED_MEMORY_SIMILARITY_THRESHOLD = as_float(
    load_config_value(
        'STRUCTURED_MEMORY_SIMILARITY_THRESHOLD',
        EMBED_CONFIG.get("structured_memory_similarity_threshold"),
    ),
    min(RAG_SIMILARITY_THRESHOLD, 0.5),
)
RAG_STRONG_SIMILARITY_THRESHOLD = as_float(EMBED_CONFIG.get("strong_similarity_threshold"), 0.72)
RAG_DEBUG_ENABLED = as_bool(load_config_value('RAG_DEBUG_ENABLED', EMBED_CONFIG.get("debug_enabled", False)))
RAG_HYBRID_TOP_K = int(EMBED_CONFIG.get("hybrid_top_k", 8))
RAG_EMBEDDING_TOP_N = int(EMBED_CONFIG.get("embedding_top_n", 14))
RAG_BM25_TOP_N = int(EMBED_CONFIG.get("bm25_top_n", 8))
RAG_RRF_K = float(EMBED_CONFIG.get("rrf_constant", 60))
RAG_QUERY_REWRITE_ENABLED = as_bool(
    load_config_value('RAG_QUERY_REWRITE_ENABLED', EMBED_CONFIG.get("query_rewrite_enabled", True))
)
RAG_QUERY_REWRITE_MODEL_NAME = EMBED_CONFIG.get("query_rewrite_model_name", "upskyy/e5-small-korean")
RAG_QUERY_REWRITE_BACKEND = EMBED_CONFIG.get("query_rewrite_backend")
RAG_QUERY_REWRITE_VARIANTS = int(EMBED_CONFIG.get("query_rewrite_variants", 3))
RAG_RERANKER_MODEL_NAME = EMBED_CONFIG.get("reranker_model_name", "BAAI/bge-reranker-v2-m3")
RAG_RERANKER_DEVICE = EMBED_CONFIG.get("reranker_device")
RAG_RERANKER_SCORE_THRESHOLD = EMBED_CONFIG.get("reranker_score_threshold")
if RAG_RERANKER_SCORE_THRESHOLD is not None:
    try:
        RAG_RERANKER_SCORE_THRESHOLD = float(RAG_RERANKER_SCORE_THRESHOLD)
    except (TypeError, ValueError):
        RAG_RERANKER_SCORE_THRESHOLD = None

SEARCH_CHUNKING_ENABLED = as_bool(load_config_value('SEARCH_CHUNKING_ENABLED', False))
SEARCH_NEIGHBORHOOD_EXPAND_ENABLED = as_bool(load_config_value('SEARCH_NEIGHBORHOOD_EXPAND_ENABLED', False))
SEARCH_QUERY_EXPANSION_ENABLED = as_bool(load_config_value('SEARCH_QUERY_EXPANSION_ENABLED', True))
RERANK_ENABLED = as_bool(load_config_value('RERANK_ENABLED', False))
USER_MEMORY_ENABLED = as_bool(load_config_value('USER_MEMORY_ENABLED', False))
SELF_REFLECTION_ENABLED = as_bool(load_config_value('SELF_REFLECTION_ENABLED', False))
DISABLE_VERBOSE_THINKING_OUTPUT = as_bool(load_config_value('DISABLE_VERBOSE_THINKING_OUTPUT', True))

# BM25 자동 재구축 설정
_BM25_AUTO_REBUILD_RAW = EMBED_CONFIG.get("bm25_auto_rebuild", {})
if not isinstance(_BM25_AUTO_REBUILD_RAW, dict):
    _BM25_AUTO_REBUILD_RAW = {}

BM25_AUTO_REBUILD_CONFIG = {
    "enabled": as_bool(
        load_config_value(
            "BM25_AUTO_REBUILD_ENABLED",
            _BM25_AUTO_REBUILD_RAW.get("enabled", False),
        ),
        False,
    ),
    "idle_minutes": max(
        1,
        as_int(
            load_config_value(
                "BM25_AUTO_REBUILD_IDLE_MINUTES",
                _BM25_AUTO_REBUILD_RAW.get("idle_minutes", 180),
            ),
            180,
        ),
    ),
    "poll_minutes": max(
        1,
        as_int(
            load_config_value(
                "BM25_AUTO_REBUILD_POLL_MINUTES",
                _BM25_AUTO_REBUILD_RAW.get("poll_minutes", 15),
            ),
            15,
        ),
    ),
}
CONVERSATION_WINDOW_SIZE = as_int(load_config_value('CONVERSATION_WINDOW_SIZE'), 12) # 윈도우 크기 (메시지 개수)
CONVERSATION_WINDOW_STRIDE = as_int(load_config_value('CONVERSATION_WINDOW_STRIDE'), 6) # 윈도우 이동 간격
CONVERSATION_WINDOW_MAX_CHARS = as_int(load_config_value('CONVERSATION_WINDOW_MAX_CHARS'), 3000) # 윈도우 최대 문자열 길이 (토큰 제한 대응)
LOCAL_EMBEDDING_MAX_TOKENS = max(
    128,
    as_int(
        load_config_value(
            'LOCAL_EMBEDDING_MAX_TOKENS',
            EMBED_CONFIG.get("embedding_max_tokens", 512),
        ),
        512,
    ),
)
CONVERSATION_WINDOW_MAX_TOKENS = as_int(
    load_config_value('CONVERSATION_WINDOW_MAX_TOKENS', 0),
    0,
)
CONVERSATION_WINDOW_TOKEN_RESERVE = max(
    8,
    as_int(load_config_value('CONVERSATION_WINDOW_TOKEN_RESERVE', 32), 32),
)
CONVERSATION_NEIGHBOR_RADIUS = max(1, as_int(load_config_value('CONVERSATION_NEIGHBOR_RADIUS', EMBED_CONFIG.get("conversation_neighbor_radius", 3)), 3))
STRUCTURED_MEMORY_MAX_SUMMARY_CHARS = max(120, as_int(load_config_value('STRUCTURED_MEMORY_MAX_SUMMARY_CHARS', 320), 320))
STRUCTURED_MEMORY_MAX_CONTEXT_CHARS = max(300, as_int(load_config_value('STRUCTURED_MEMORY_MAX_CONTEXT_CHARS', 1200), 1200))
STRUCTURED_USER_MEMORY_MIN_CHARS = max(4, as_int(load_config_value('STRUCTURED_USER_MEMORY_MIN_CHARS', 12), 12))

AI_INTENT_MODEL_NAME = as_str(load_config_value('AI_INTENT_MODEL_NAME', 'gemini-2.5-flash-lite'), 'gemini-2.5-flash-lite')
AI_RESPONSE_MODEL_NAME = as_str(load_config_value('AI_RESPONSE_MODEL_NAME', 'gemini-2.5-flash'), 'gemini-2.5-flash')
FORTUNE_MODEL_LITE = as_str(load_config_value('FORTUNE_MODEL_LITE', 'DeepSeek-V3.2-Exp-nothinking'), 'DeepSeek-V3.2-Exp-nothinking')
FORTUNE_MODEL_PRO = as_str(load_config_value('FORTUNE_MODEL_PRO', 'DeepSeek-V3.2-Exp-thinking'), 'DeepSeek-V3.2-Exp-thinking')
FORTUNE_MORNING_BRIEFING_ENABLED = as_bool(
    load_config_value(
        "FORTUNE_MORNING_BRIEFING_ENABLED",
        "false" if PROFILE == "general" else "true",
    ),
    PROFILE != "general",
)
RPM_LIMIT_INTENT = max(1, as_int(load_config_value('RPM_LIMIT_INTENT', 15), 15))
RPM_LIMIT_RESPONSE = max(1, as_int(load_config_value('RPM_LIMIT_RESPONSE', 15), 15))
RPD_LIMIT_INTENT = max(1, as_int(load_config_value('RPD_LIMIT_INTENT', 250), 250))
RPD_LIMIT_RESPONSE = max(1, as_int(load_config_value('RPD_LIMIT_RESPONSE', 250), 250))
FINNHUB_API_RPM_LIMIT = 50
AI_TEMPERATURE = 0.0
AI_FREQUENCY_PENALTY = 0.0
AI_PRESENCE_PENALTY = 0.0
KAKAO_API_RPM_LIMIT = max(1, as_int(load_config_value('KAKAO_API_RPM_LIMIT', 60), 60))
KAKAO_API_RPD_LIMIT = max(1, as_int(load_config_value('KAKAO_API_RPD_LIMIT', 95000), 95000))
KAKAO_API_MAX_CONCURRENCY = max(1, as_int(load_config_value('KAKAO_API_MAX_CONCURRENCY', 6), 6))
KAKAO_API_TIMEOUT_SECONDS = max(1, as_int(load_config_value('KAKAO_API_TIMEOUT_SECONDS', 10), 10))
KRX_API_RPD_LIMIT = 9000
AI_RESPONSE_LENGTH_LIMIT = 300
AI_COOLDOWN_SECONDS = 3
AI_REQUEST_TIMEOUT = as_int(load_config_value('AI_REQUEST_TIMEOUT', 120), 120)  # AI 응답 제한 시간 (초)
# CometAPI 보호장치 (외부 LLM 과호출/과토큰 방지)
COMETAPI_RPM_LIMIT = max(1, as_int(load_config_value('COMETAPI_RPM_LIMIT', 40), 40))
COMETAPI_RPD_LIMIT = max(1, as_int(load_config_value('COMETAPI_RPD_LIMIT', 3000), 3000))
COMETAPI_MAX_TOKENS = max(128, as_int(load_config_value('COMETAPI_MAX_TOKENS', 2048), 2048))
COMETAPI_SYSTEM_PROMPT_MAX_CHARS = max(400, as_int(load_config_value('COMETAPI_SYSTEM_PROMPT_MAX_CHARS', 6000), 6000))
COMETAPI_USER_PROMPT_MAX_CHARS = max(800, as_int(load_config_value('COMETAPI_USER_PROMPT_MAX_CHARS', 20000), 20000))
# !요약 전용 컨텍스트 압축 설정 (긴 이력을 보되 입력 토큰은 고정 예산으로 제한)
SUMMARY_MAX_LOOKBACK = max(20, as_int(load_config_value('SUMMARY_MAX_LOOKBACK', 120), 120))
SUMMARY_MAX_CONTEXT_CHARS = max(1200, as_int(load_config_value('SUMMARY_MAX_CONTEXT_CHARS', 3200), 3200))
SUMMARY_RECENT_TURNS = max(4, as_int(load_config_value('SUMMARY_RECENT_TURNS', 12), 12))
SUMMARY_OLDER_TURNS = max(0, as_int(load_config_value('SUMMARY_OLDER_TURNS', 8), 8))
SUMMARY_RECENT_LINE_CHARS = max(60, as_int(load_config_value('SUMMARY_RECENT_LINE_CHARS', 180), 180))
SUMMARY_OLDER_LINE_CHARS = max(40, as_int(load_config_value('SUMMARY_OLDER_LINE_CHARS', 90), 90))
SUMMARY_INCREMENTAL_ENABLED = as_bool(load_config_value('SUMMARY_INCREMENTAL_ENABLED', True))
SUMMARY_INCREMENTAL_MAX_NEW_MESSAGES = max(1, as_int(load_config_value('SUMMARY_INCREMENTAL_MAX_NEW_MESSAGES', 24), 24))
SUMMARY_INCREMENTAL_DELTA_LOOKBACK = max(8, as_int(load_config_value('SUMMARY_INCREMENTAL_DELTA_LOOKBACK', 48), 48))
SUMMARY_CACHE_MAX_CHANNELS = max(1, as_int(load_config_value('SUMMARY_CACHE_MAX_CHANNELS', 300), 300))

RAG_GUILD_SCOPE = str(load_config_value('RAG_GUILD_SCOPE', EMBED_CONFIG.get("guild_scope", "channel"))).strip().lower()
if RAG_GUILD_SCOPE not in {"channel", "user"}:
    RAG_GUILD_SCOPE = "channel"
# AI 메모리/RAG 기능은 기본 활성화지만, 저사양 환경에서는 환경변수/설정으로 비활성화할 수 있다.
# 과거 embedding_enabled=false가 실제 실행을 끄지 못하던 불일치를 없애기 위해
# enable_local_embeddings가 없으면 EMBEDDING_ENABLED를 기본값으로 사용한다.
AI_MEMORY_ENABLED = as_bool(
    load_config_value(
        'AI_MEMORY_ENABLED',
        EMBED_CONFIG.get("enable_local_embeddings", EMBEDDING_ENABLED),
    ),
    EMBEDDING_ENABLED,
)
LITE_MODEL_SYSTEM_PROMPT = _extract_prompt_value("lite_system_prompt", FALLBACK_LITE_PROMPT)
AGENT_SYSTEM_PROMPT = _extract_prompt_value("agent_system_prompt", FALLBACK_AGENT_PROMPT)
WEB_FALLBACK_PROMPT = _extract_prompt_value("web_fallback_prompt", FALLBACK_WEB_PROMPT)
AI_PROACTIVE_RESPONSE_CONFIG = {
    "enabled": True, 
    "keywords": ["마사몽", "마사모", "봇", "챗봇"], 
    "probability": 0.6, 
    "cooldown_seconds": 90, 
    "gatekeeper_persona": """너는 대화의 흐름을 분석하는 '눈치 빠른' AI야. 주어진 최근 대화 내용과 마지막 메시지를 보고, AI 챗봇('마사몽')이 지금 대화에 참여하는 것이 자연스럽고 대화를 더 재미있게 만들지를 판단해야 해.
- 판단 기준:
  1. 긍정적이거나 중립적인 맥락에서 챗봇을 언급하는가?
  2. 챗봇이 답변하기 좋은 질문이나 주제가 나왔는가?
  3. 이미 사용자들끼리 대화가 활발하게 진행 중이라 챗봇의 개입이 불필해 보이지는 않는가? (이 경우 'No')
  4. 부정적인 맥락이거나, 챗봇을 비난하는 내용인가? (이 경우 'No')
- 위의 기준을 종합적으로 고려해서, 참여하는 것이 좋다고 생각되면 'Yes', 아니면 'No'라고만 대답해. 다른 설명은 절대 붙이지 마.""",
    "look_back_count": 5,
    "min_message_length": 10
}
RAG_ARCHIVING_CONFIG = {
    "enabled": as_bool(
        load_config_value(
            "RAG_ARCHIVING_ENABLED",
            False if PROFILE == "general" else True,
        ),
        PROFILE != "general",
    ),
    "history_limit": as_int(load_config_value("RAG_ARCHIVE_HISTORY_LIMIT", 20000), 20000),
    "batch_size": as_int(load_config_value("RAG_ARCHIVE_BATCH_SIZE", 1000), 1000),
    "check_interval_hours": max(
        1,
        as_int(load_config_value("RAG_ARCHIVE_INTERVAL_HOURS", 24), 24),
    ),
    "startup_delay_seconds": as_int(load_config_value("RAG_ARCHIVE_STARTUP_DELAY_SECONDS", 0), 0),
    "run_on_startup": as_bool(
        load_config_value("RAG_ARCHIVE_RUN_ON_STARTUP", False if DB_BACKEND == "tidb" else True),
        False if DB_BACKEND == "tidb" else True,
    ),
    # user_activity_log 보존 일수. 0 이하이면 비활성(무한 보존).
    # 주의: 활성화하면 !랭킹 '전체'(전체기간) 집계가 이 창(window) 이내로 제한된다.
    "activity_log_retention_days": as_int(
        load_config_value("USER_ACTIVITY_LOG_RETENTION_DAYS", 0), 0
    ),
}
AI_CREATIVE_PROMPTS = {
    "fortune": "사용자 '{user_name}'를 위한 오늘의 운세를 재치있게 알려줘.",
    "summarize": (
        "다음 대화 내용을 바탕으로 요약해줘.\n"
        "입력은 [이전 맥락(압축)] + [최신 대화] 형식일 수 있어.\n\n"
        "출력 규칙:\n"
        "1) 최신 대화에 나온 사실을 우선 반영해.\n"
        "2) 추측 금지. 대화에 없는 정보는 만들지 마.\n"
        "3) 아래 형식 그대로 작성해.\n"
        "## 핵심 3줄\n"
        "- ...\n"
        "- ...\n"
        "- ...\n"
        "## 결정/할 일\n"
        "- ... (없으면 '없음')\n"
        "## 남은 이슈\n"
        "- ... (없으면 '없음')\n\n"
        "--- 대화 내용 ---\n{conversation}"
    ),
    "summarize_incremental": (
        "아래에 '이전 요약'과 '신규 대화'가 주어진다.\n"
        "이전 요약을 바탕으로, 신규 대화를 반영한 최신 요약으로 갱신해줘.\n\n"
        "출력 규칙:\n"
        "1) 최신 대화에서 바뀐 점/새 결정사항을 반드시 반영.\n"
        "2) 이전 요약의 사실 중 신규 대화와 충돌하는 내용은 수정.\n"
        "3) 추측 금지. 대화에 없는 정보 생성 금지.\n"
        "4) 아래 형식 그대로 작성.\n"
        "## 핵심 3줄\n"
        "- ...\n"
        "- ...\n"
        "- ...\n"
        "## 결정/할 일\n"
        "- ... (없으면 '없음')\n"
        "## 남은 이슈\n"
        "- ... (없으면 '없음')\n\n"
        "--- 이전 요약 ---\n{previous_summary}\n\n"
        "--- 신규 대화 ---\n{new_conversation}"
    ),
    "ranking": "다음 서버 활동 랭킹과 통계를 보고, 전체적인 서버 분위기를 북돋우는 재치 있는 발표 멘트를 작성해줘.\n\n### 출력 규칙\n1. **가독성 최우선**: 정보를 나열할 때 난해하지 않게 깔끔한 구조로 작성해.\n2. **1등 강조**: 1등({top_one_name})을 특별히 축하하고 재미있는 코멘트를 달아줘.\n3. **섹션 구분**: '서버 통계 브리핑', '명예의 전당(랭킹)', '마사몽의 한마디' 등으로 명확히 나눠서 보여줘.\n4. **이모지 활용**: 적절한 이모지를 사용하여 분위기를 살려줘.\n5. **표 금지**: 마크다운 표 문법(`|---|`)은 절대 사용하지 마.\n6. **차트 인지**: 아래 '차트 전송 상태'를 읽고, 차트가 먼저 올라간 상황이면 이를 인지한 멘트로 시작해.\n7. **표본 해석 반영**: '표본 해석 메모'를 한 줄 요약으로 반영해줘.\n\n--- 차트 전송 상태 ---\n{chart_delivery_status}\n--- 표본 해석 메모 ---\n{sample_size_note}\n--- 서버 통계 ---\n{server_stats}\n--- 활동 랭킹 ---\n{ranking_list}",
    "answer_time": "현재 시간은 '{current_time}'입니다. 이 정보를 사용하여 사용자에게 현재 시간을 알려주세요.",
    "answer_weather": "'{location_name}'의 날씨 정보는 다음과 같습니다: {weather_data}. 이 정보를 바탕으로 사용자에게 날씨를 설명해주세요.",
    "answer_weather_weekly": "'{location_name}'의 이번 주 주간 날씨 데이터(단기+중기)는 다음과 같습니다:\n{weather_data}\n\n이 데이터를 바탕으로 사용자가 이번 주 날씨 흐름(요일별 변화 등)을 한눈에 알 수 있도록 요약해서 설명해주세요. 날짜별 날씨, 기온 변화 등을 자연스럽게 언급하세요."
}
FUN_KEYWORD_TRIGGERS = { "enabled": True, "cooldown_seconds": 60, "triggers": { "fortune": ["운세", "오늘 운", "운세 좀"], "summarize": ["요약해줘", "무슨 얘기했어", "무슨 얘기함", "요약 좀", "지금까지 뭔 얘기"] } }
AI_DEBUG_ENABLED = as_bool(load_config_value('AI_DEBUG_ENABLED', False))
AI_DEBUG_LOG_MAX_LEN = int(load_config_value('AI_DEBUG_LOG_MAX_LEN', 400))
KMA_API_DAILY_CALL_LIMIT = 10000
KMA_API_MAX_RETRIES = int(load_config_value("KMA_API_MAX_RETRIES", 3))
KMA_API_RETRY_DELAY_SECONDS = int(load_config_value("KMA_API_RETRY_DELAY_SECONDS", 2))
KMA_API_TIMEOUT = int(load_config_value("KMA_API_TIMEOUT", 30))
DEFAULT_LOCATION_NAME = str(load_config_value("DEFAULT_LOCATION_NAME", "광양"))
DEFAULT_NX = str(load_config_value("DEFAULT_NX", "73"))
DEFAULT_NY = str(load_config_value("DEFAULT_NY", "70"))
ENABLE_RAIN_NOTIFICATION = as_bool(load_config_value("ENABLE_RAIN_NOTIFICATION", False))
RAIN_NOTIFICATION_CHANNEL_ID = as_int(load_config_value("RAIN_NOTIFICATION_CHANNEL_ID", 0), 0)
WEATHER_CHECK_INTERVAL_MINUTES = max(
    1,
    as_int(load_config_value("WEATHER_CHECK_INTERVAL_MINUTES", 60), 60),
)
RAIN_NOTIFICATION_THRESHOLD_POP = as_int(load_config_value("RAIN_NOTIFICATION_THRESHOLD_POP", 30), 30)
RAIN_NOTIFICATION_GREETING_THRESHOLD_POP = as_int(load_config_value("RAIN_NOTIFICATION_GREETING_THRESHOLD_POP", 60), 60)
ENABLE_GREETING_NOTIFICATION = as_bool(load_config_value("ENABLE_GREETING_NOTIFICATION", False))
GREETING_NOTIFICATION_CHANNEL_ID = as_int(load_config_value("GREETING_NOTIFICATION_CHANNEL_ID", 0), 0)
ENABLE_EARTHQUAKE_ALERT = as_bool(
    load_config_value(
        "ENABLE_EARTHQUAKE_ALERT",
        False if PROFILE == "general" else True,
    ),
    PROFILE != "general",
)
EARTHQUAKE_CHECK_INTERVAL_MINUTES = max(
    1,
    as_int(load_config_value("EARTHQUAKE_CHECK_INTERVAL_MINUTES", 1), 1),
)
MORNING_GREETING_TIME = {
    "hour": as_int(load_config_value("MORNING_GREETING_HOUR", 7), 7),
    "minute": as_int(load_config_value("MORNING_GREETING_MINUTE", 30), 30),
}
EVENING_GREETING_TIME = {
    "hour": as_int(load_config_value("EVENING_GREETING_HOUR", 23), 23),
    "minute": as_int(load_config_value("EVENING_GREETING_MINUTE", 50), 50),
}
DEFAULT_TSUNDERE_PERSONA = _extract_prompt_value("default_persona", FALLBACK_PERSONA)
DEFAULT_TSUNDERE_RULES = _extract_prompt_value("default_rules", FALLBACK_RULES)


def _build_channel_config(raw_channels: Any) -> Dict[int, Dict[str, Any]]:
    """프롬프트 설정에서 채널별 AI 구성을 추출합니다.

    prompts.json의 'channels' 섹션 또는 DEFAULT_AI_CHANNELS 환경변수에서
    채널 ID별 AI 허용 여부, 페르소나, 규칙을 매핑합니다.

    Args:
        raw_channels: prompts.json에서 읽은 채널 설정 dict

    Returns:
        {channel_id: {"allowed": bool, "persona": str, "rules": str}} 매핑
    """
    configs: Dict[int, Dict[str, Any]] = {}
    if isinstance(raw_channels, dict):
        for raw_id, meta in raw_channels.items():
            if not isinstance(meta, dict):
                continue
            try:
                channel_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            persona = _with_mention_guard(meta.get("persona"), DEFAULT_TSUNDERE_PERSONA)
            rules = _with_mention_guard(meta.get("rules"), DEFAULT_TSUNDERE_RULES)
            allowed = as_bool(meta.get("allowed"), False)
            configs[channel_id] = {
                "allowed": allowed,
                "persona": persona,
                "rules": rules,
            }
    if configs:
        return configs

    fallback_channels = load_config_value('DEFAULT_AI_CHANNELS')
    if fallback_channels:
        for item in str(fallback_channels).split(","):
            candidate = item.strip()
            if not candidate:
                continue
            try:
                channel_id = int(candidate)
            except ValueError:
                continue
            configs[channel_id] = {
                "allowed": True,
                "persona": DEFAULT_TSUNDERE_PERSONA,
                "rules": DEFAULT_TSUNDERE_RULES,
            }
    return configs


CHANNEL_AI_CONFIG = _build_channel_config(PROMPT_CONFIG.get("channels", {}))
USER_SPECIFIC_PERSONAS = {
    # 123456789012345678: {
    #     "persona": "너는 이 사용자의 개인 비서야. 항상 존댓말을 사용하고, 요청에 최대한 정확하고 상세하게 답변해줘.",
    #     "rules": "- 사용자의 요청을 최우선으로 처리해."
    # }
}
GEMINI_SAFETY_SETTINGS = {
    'HARM_CATEGORY_HARASSMENT': 'BLOCK_NONE',
    'HARM_CATEGORY_HATE_SPEECH': 'BLOCK_NONE',
    'HARM_CATEGORY_SEXUALLY_EXPLICIT': 'BLOCK_NONE',
    'HARM_CATEGORY_DANGEROUS_CONTENT': 'BLOCK_NONE',
}
MEMBERS_INTENT_ENABLED = as_bool(
    load_config_value(
        "MASAMONG_MEMBERS_INTENT_ENABLED",
        "false" if PROFILE == "general" else "true",
    ),
    PROFILE != "general",
)
MEMBER_CACHE_ENABLED = as_bool(
    load_config_value(
        "MASAMONG_MEMBER_CACHE_ENABLED",
        "false" if PROFILE == "general" else "true",
    ),
    PROFILE != "general",
)
if MEMBER_CACHE_ENABLED and not MEMBERS_INTENT_ENABLED:
    raise RuntimeError(
        "MASAMONG_MEMBER_CACHE_ENABLED=true이면 "
        "MASAMONG_MEMBERS_INTENT_ENABLED=true가 필요합니다."
    )

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True
intents.members = MEMBERS_INTENT_ENABLED
intents.emojis = True # [NEW] Enable access to custom emojis/expressions (compatible with older 2.x versions)

# ========== 다국어 설정 ==========
# .env의 MASAMONG_LANG으로 전역 기본 언어를 설정할 수 있습니다.
# 지원 언어: ko (한국어), en (English), ja (日本語)
# 서버별 언어는 DB의 guild_settings.language 컬럼으로 관리합니다.
LANGUAGE = os.environ.get("MASAMONG_LANG", DEFAULT_LANGUAGE).strip().lower()
if LANGUAGE not in SUPPORTED_LANGUAGES:
    LANGUAGE = DEFAULT_LANGUAGE

# 메시지 상수 (로케일 시스템에서 로드, 기존 코드 호환성 유지)
MSG_AI_ERROR = _locale_msg("MSG_AI_ERROR")
MSG_AI_COOLDOWN = _locale_msg("MSG_AI_COOLDOWN")
MSG_CMD_NO_PERM = _locale_msg("MSG_CMD_NO_PERM")
MSG_CMD_ERROR = _locale_msg("MSG_CMD_ERROR")
MSG_CMD_GUILD_ONLY = _locale_msg("MSG_CMD_GUILD_ONLY")
MSG_DELETE_LOG_SUCCESS = _locale_msg("MSG_DELETE_LOG_SUCCESS")
MSG_DELETE_LOG_NOT_FOUND = _locale_msg("MSG_DELETE_LOG_NOT_FOUND")
MSG_DELETE_LOG_ERROR = _locale_msg("MSG_DELETE_LOG_ERROR")
MSG_WEATHER_API_KEY_MISSING = _locale_msg("MSG_WEATHER_API_KEY_MISSING")
MSG_WEATHER_FETCH_ERROR = _locale_msg("MSG_WEATHER_FETCH_ERROR")
MSG_WEATHER_TIMEOUT = _locale_msg("MSG_WEATHER_TIMEOUT")
MSG_WEATHER_NO_DATA = _locale_msg("MSG_WEATHER_NO_DATA")

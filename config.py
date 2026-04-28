# -*- coding: utf-8 -*-
import os
import json
from pathlib import Path
from typing import Any, Dict
from dotenv import load_dotenv
import discord

try:  # Optional dependency for YAML-based prompt configuration
    import yaml  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - yaml is optional
    yaml = None  # type: ignore

load_dotenv()

# [NEW] Stock Config (yfinance)
USE_YFINANCE = True
YFINANCE_CACHE_TTL = 600 # 10분 캐시

def load_config_value(key, default=None):
    """환경 변수 → `config.json` 순으로 값을 조회하고, 없으면 기본값을 반환합니다.

    Args:
        key (str): 조회할 설정 키 이름.
        default (Any, optional): 키가 어디에서도 발견되지 않을 때 사용할 기본값.

    Returns:
        Any: 발견된 설정값 또는 기본값.
    """
    value = os.environ.get(key)
    if value:
        return value
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            config_json = json.load(f)
        value = config_json.get(key)
        if value:
            return value
    except FileNotFoundError:
        pass
    except json.JSONDecodeError:
        print("경고: config.json 파일이 유효한 JSON 형식이 아닙니다.")
    return default


def as_bool(value, default: bool = False) -> bool:
    """문자열/불리언 값을 안전하게 bool로 변환합니다."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return default


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


EMBED_CONFIG_PATH = os.environ.get('EMB_CONFIG_PATH', 'emb_config.json')


def load_emb_config() -> dict:
    """임베딩 관련 별도 설정 파일을 읽어옵니다."""
    try:
        with open(EMBED_CONFIG_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
            print("경고: emb_config.json 내용이 JSON 객체가 아닙니다.")
    except FileNotFoundError:
        pass
    except json.JSONDecodeError:
        print("경고: emb_config.json 파일이 유효한 JSON 형식이 아닙니다.")
    return {}


EMBED_CONFIG = load_emb_config()


PROMPT_CONFIG_PATH = os.environ.get("PROMPT_CONFIG_PATH", "prompts.json")
_PROMPT_CONFIG_EXPLICIT = "PROMPT_CONFIG_PATH" in os.environ


def _read_prompt_file(path: Path) -> dict[str, Any]:
    """프롬프트 설정 파일(JSON/YAML)을 읽어 dict 형태로 반환합니다."""
    if path.suffix.lower() == ".json":
        with path.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
            if isinstance(data, dict):
                return data
            print("경고: 프롬프트 설정 파일이 JSON 객체 형식이 아닙니다.")
            return {}
    if path.suffix.lower() in {".yaml", ".yml"}:
        if yaml is None:
            print("경고: YAML 프롬프트 파일을 읽으려면 PyYAML 패키지가 필요합니다.")
            return {}
        with path.open("r", encoding="utf-8") as fp:
            data = yaml.safe_load(fp)
            if isinstance(data, dict):
                return data
            print("경고: YAML 프롬프트 설정이 매핑 형태가 아닙니다.")
            return {}
    print(f"경고: 지원하지 않는 프롬프트 파일 형식입니다: {path.suffix}")
    return {}


def load_prompt_config() -> dict[str, Any]:
    """프롬프트 관련 별도 설정 파일을 읽어옵니다."""
    if not PROMPT_CONFIG_PATH:
        return {}
    path = Path(PROMPT_CONFIG_PATH)
    if not path.exists():
        if _PROMPT_CONFIG_EXPLICIT:
            print(f"경고: 프롬프트 설정 파일 '{path}'을(를) 찾을 수 없습니다.")
        return {}
    try:
        return _read_prompt_file(path)
    except Exception as exc:  # pragma: no cover - 방어적 로깅
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

TOKEN = load_config_value('DISCORD_BOT_TOKEN')
COMMAND_PREFIX = "!"
LOG_FILE_NAME = "discord_logs.txt"
ERROR_LOG_FILE_NAME = "error_logs.txt"
DB_BACKEND = str(load_config_value('MASAMONG_DB_BACKEND', 'sqlite')).strip().lower()
DATABASE_FILE = "database/remasamong.db"
TIDB_HOST = load_config_value('MASAMONG_DB_HOST')
TIDB_PORT = as_int(load_config_value('MASAMONG_DB_PORT', 4000), 4000)
TIDB_NAME = load_config_value('MASAMONG_DB_NAME', 'masamong')
TIDB_USER = load_config_value('MASAMONG_DB_USER')
TIDB_PASSWORD = load_config_value('MASAMONG_DB_PASSWORD')
TIDB_SSL_CA = load_config_value('MASAMONG_DB_SSL_CA')
TIDB_SSL_VERIFY_IDENTITY = as_bool(load_config_value('MASAMONG_DB_SSL_VERIFY_IDENTITY', 'true'))
REMOTE_DB_STRICT_MODE = as_bool(load_config_value('MASAMONG_DB_STRICT_REMOTE_ONLY', 'false'))
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
    if _missing_tidb:
        raise RuntimeError(
            "MASAMONG_DB_STRICT_REMOTE_ONLY=true 이지만 TiDB 필수 설정이 누락되었습니다: "
            + ", ".join(_missing_tidb)
        )
GEMINI_API_KEY = load_config_value('GEMINI_API_KEY')
GOOGLE_API_KEY = load_config_value('GOOGLE_API_KEY')
GOOGLE_CX = load_config_value('GOOGLE_CX')
GOOGLE_CUSTOM_SEARCH_DAILY_LIMIT = as_int(load_config_value('GOOGLE_CUSTOM_SEARCH_DAILY_LIMIT', 100), 100)
SERPAPI_KEY = load_config_value('SERPAPI_KEY')
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
GO_DATA_API_KEY_KR = load_config_value('GO_DATA_API_KEY_KR', 'YOUR_GO_DATA_API_KEY_KR')
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
if REMOTE_DB_STRICT_MODE:
    # 원격 DB 강제 모드에서는 로컬 파일 기반 저장소를 사용하지 않는다.
    DISCORD_EMBEDDING_BACKEND = "tidb"
    KAKAO_STORE_BACKEND = "tidb"
DISCORD_EMBEDDING_DB_PATH = EMBED_CONFIG.get("discord_db_path", "database/discord_embeddings.db")
KAKAO_EMBEDDING_DB_PATH = EMBED_CONFIG.get("kakao_db_path", "database/kakao_embeddings.db")
KAKAO_EMBEDDING_SERVER_MAP = _normalize_kakao_servers(EMBED_CONFIG.get("kakao_servers", []))
KAKAO_VECTOR_EXTENSION = EMBED_CONFIG.get("kakao_vector_extension")
DISCORD_EMBEDDING_TIDB_TABLE = str(load_config_value('DISCORD_EMBEDDING_TIDB_TABLE', 'discord_chat_embeddings')).strip()
KAKAO_TIDB_TABLE = str(load_config_value('KAKAO_TIDB_TABLE', 'kakao_chunks')).strip()

# 검색 엔진 활성화 설정 (emb_config.json에서 관리)
EMBEDDING_ENABLED = as_bool(EMBED_CONFIG.get("embedding_enabled", True))
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
    "idle_minutes": as_int(
        load_config_value(
            "BM25_AUTO_REBUILD_IDLE_MINUTES",
            _BM25_AUTO_REBUILD_RAW.get("idle_minutes", 180),
        ),
        180,
    ),
    "poll_minutes": as_int(
        load_config_value(
            "BM25_AUTO_REBUILD_POLL_MINUTES",
            _BM25_AUTO_REBUILD_RAW.get("poll_minutes", 15),
        ),
        15,
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
AI_MEMORY_ENABLED = as_bool(load_config_value('AI_MEMORY_ENABLED', EMBED_CONFIG.get("enable_local_embeddings", True)))
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
    "enabled": as_bool(load_config_value("RAG_ARCHIVING_ENABLED", True)),
    "history_limit": as_int(load_config_value("RAG_ARCHIVE_HISTORY_LIMIT", 20000), 20000),
    "batch_size": as_int(load_config_value("RAG_ARCHIVE_BATCH_SIZE", 1000), 1000),
    "check_interval_hours": as_int(load_config_value("RAG_ARCHIVE_INTERVAL_HOURS", 24), 24),
    "startup_delay_seconds": as_int(load_config_value("RAG_ARCHIVE_STARTUP_DELAY_SECONDS", 0), 0),
    "run_on_startup": as_bool(
        load_config_value("RAG_ARCHIVE_RUN_ON_STARTUP", False if DB_BACKEND == "tidb" else True),
        False if DB_BACKEND == "tidb" else True,
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
WEATHER_CHECK_INTERVAL_MINUTES = as_int(load_config_value("WEATHER_CHECK_INTERVAL_MINUTES", 60), 60)
RAIN_NOTIFICATION_THRESHOLD_POP = as_int(load_config_value("RAIN_NOTIFICATION_THRESHOLD_POP", 30), 30)
RAIN_NOTIFICATION_GREETING_THRESHOLD_POP = as_int(load_config_value("RAIN_NOTIFICATION_GREETING_THRESHOLD_POP", 60), 60)
ENABLE_GREETING_NOTIFICATION = as_bool(load_config_value("ENABLE_GREETING_NOTIFICATION", False))
GREETING_NOTIFICATION_CHANNEL_ID = as_int(load_config_value("GREETING_NOTIFICATION_CHANNEL_ID", 0), 0)
ENABLE_EARTHQUAKE_ALERT = as_bool(load_config_value("ENABLE_EARTHQUAKE_ALERT", True))
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
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True
intents.members = True
intents.emojis = True # [NEW] Enable access to custom emojis/expressions (compatible with older 2.x versions)
MSG_AI_ERROR = "😥 아놔, 에러났네. 뭐지? 잠시 후에 다시 물어봐봐."
MSG_AI_COOLDOWN = "😅 야, 좀 천천히 불러라! {remaining:.1f}초 뒤에 다시 말 걸어줘."
MSG_CMD_NO_PERM = "🚫 너 그거 못 써 임마. 관리자한테 허락받고 와."
MSG_CMD_ERROR = "❌ 명령어 쓰다 뭐 잘못된 듯? 다시 확인해봐."
MSG_CMD_GUILD_ONLY = "🚫 야, 그건 서버 채널에서만 쓰는 거임."
MSG_DELETE_LOG_SUCCESS = "✅ 로그 파일(`{filename}`) 지웠다. 깔끔~"
MSG_DELETE_LOG_NOT_FOUND = "ℹ️ 로그 파일(`{filename}`) 원래 없는데? 뭘 지우라는겨."
MSG_DELETE_LOG_ERROR = "❌ 로그 파일 지우는데 에러남. 뭐가 문제지?"
MSG_WEATHER_API_KEY_MISSING = "😥 기상청 API 키가 설정되지 않아서 날씨 정보를 가져올 수 없어."
MSG_WEATHER_FETCH_ERROR = "😥 날씨 정보를 가져오는데 실패했어. 잠시 후에 다시 시도해봐."
MSG_WEATHER_TIMEOUT = "⏱️ 기상청 API 응답이 너무 느려서 실패했어. 조금 있다가 다시 부탁해줘."
MSG_WEATHER_NO_DATA = "😥 해당 시간/날짜의 날씨 정보가 아직 없거나 조회할 수 없어."

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
            if not db_path:
                continue
            normalized[str(server_id)] = {
                'db_path': db_path,
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
            if not server_id or not db_path:
                continue
            normalized[str(server_id)] = {
                'db_path': db_path,
                'label': entry.get('label', '')
            }
        return normalized

    return {}

TOKEN = load_config_value('DISCORD_BOT_TOKEN')
COMMAND_PREFIX = "!"
LOG_FILE_NAME = "discord_logs.txt"
ERROR_LOG_FILE_NAME = "error_logs.txt"
DATABASE_FILE = "database/remasamong.db"
GEMINI_API_KEY = load_config_value('GEMINI_API_KEY')
GOOGLE_API_KEY = load_config_value('GOOGLE_API_KEY')
GOOGLE_CX = load_config_value('GOOGLE_CX')
GOOGLE_CUSTOM_SEARCH_DAILY_LIMIT = as_int(load_config_value('GOOGLE_CUSTOM_SEARCH_DAILY_LIMIT', 100), 100)
SERPAPI_KEY = load_config_value('SERPAPI_KEY')

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

# CometAPI 설정 (Gemini 대체 - OpenAI Compatible)
COMETAPI_KEY = load_config_value('COMETAPI_KEY')
COMETAPI_BASE_URL = load_config_value('COMETAPI_BASE_URL', 'https://api.cometapi.com/v1')
COMETAPI_MODEL = load_config_value('COMETAPI_MODEL', 'DeepSeek-V3.2-Exp-nothinking')
USE_COMETAPI = as_bool(load_config_value('USE_COMETAPI', 'true'))  # CometAPI 우선 사용

# CometAPI 이미지 생성 설정 (Gemini Native via CometAPI)
COMETAPI_IMAGE_ENABLED = as_bool(load_config_value('COMETAPI_IMAGE_ENABLED', 'true'))
COMETAPI_IMAGE_API_URL = "https://api.cometapi.com/v1beta/models/{model}:generateContent"
# 사용 가능한 모델: 'gemini-2.5-flash-image', 'gemini-3-pro-image-preview'
GEMINI_IMAGE_MODEL = load_config_value('GEMINI_IMAGE_MODEL', 'gemini-2.5-flash-image')
# 'gemini-3-pro-image-preview' 사용 시에만 적용됨 (1K, 2K, 4K)
GEMINI_IMAGE_SIZE = load_config_value('GEMINI_IMAGE_SIZE', '1K')
# 화면 비율: "1:1", "16:9", "4:3" 등
GEMINI_IMAGE_ASPECT_RATIO = load_config_value('GEMINI_IMAGE_ASPECT_RATIO', '1:1')

# 이미지 생성 사용량 제한
IMAGE_USER_LIMIT = as_int(load_config_value('IMAGE_USER_LIMIT', 5), 5)  # 유저당 12시간 내 최대 5장
IMAGE_USER_RESET_HOURS = as_int(load_config_value('IMAGE_USER_RESET_HOURS', 12), 12)  # 12시간 후 리셋
IMAGE_GLOBAL_DAILY_LIMIT = as_int(load_config_value('IMAGE_GLOBAL_DAILY_LIMIT', 50), 50)  # 전역 일일 50장

# 이미지 생성 기본 설정
# 1024x1024 = 1mp = $0.06/이미지 (Flash 기준 1290 토큰)
IMAGE_DEFAULT_WIDTH = as_int(load_config_value('IMAGE_DEFAULT_WIDTH', 768), 768) # Deprecated for Gemini
IMAGE_DEFAULT_HEIGHT = as_int(load_config_value('IMAGE_DEFAULT_HEIGHT', 768), 768) # Deprecated for Gemini
IMAGE_SAFETY_TOLERANCE = 0  # 가장 엄격한 수준 (0=strict, 5=permissive) - 절대 변경 금지
IMAGE_GENERATION_STEPS = as_int(load_config_value('IMAGE_GENERATION_STEPS', 28), 28)  # 품질 vs 비용 (max 50, 추천 28)
IMAGE_GUIDANCE_SCALE = 4.5  # 프롬프트 준수도 (1.5-10, 기본값 4.5)


FINNHUB_API_KEY = load_config_value('FINNHUB_API_KEY', 'YOUR_FINNHUB_API_KEY')
KAKAO_API_KEY = load_config_value('KAKAO_API_KEY', 'YOUR_KAKAO_API_KEY')
GO_DATA_API_KEY_KR = load_config_value('GO_DATA_API_KEY_KR', 'YOUR_GO_DATA_API_KEY_KR')
EXIM_API_KEY_KR = load_config_value('EXIM_API_KEY_KR', 'YOUR_EXIM_API_KEY_KR')
KMA_API_KEY = load_config_value('KMA_API_KEY')
FINNHUB_BASE_URL = load_config_value('FINNHUB_BASE_URL', "https://finnhub.io/api/v1")
KAKAO_BASE_URL = load_config_value('KAKAO_BASE_URL', "https://dapi.kakao.com/v2/local/search/keyword.json")
KRX_BASE_URL = load_config_value('KRX_BASE_URL', "https://apis.data.go.kr/1160100/service/GetStockSecuritiesInfoService/getStockPriceInfo")
KMA_BASE_URL = load_config_value('KMA_BASE_URL', "https://apihub.kma.go.kr/api/typ02/openApi/VilageFcstInfoService_2.0")
EXIM_BASE_URL = load_config_value('EXIM_BASE_URL', "https://www.koreaexim.go.kr/site/program/financial/exchangeJSON")

DISCORD_EMBEDDING_DB_PATH = EMBED_CONFIG.get("discord_db_path", "database/discord_embeddings.db")
KAKAO_EMBEDDING_DB_PATH = EMBED_CONFIG.get("kakao_db_path", "database/kakao_embeddings.db")
KAKAO_EMBEDDING_SERVER_MAP = _normalize_kakao_servers(EMBED_CONFIG.get("kakao_servers", []))
KAKAO_VECTOR_EXTENSION = EMBED_CONFIG.get("kakao_vector_extension")

# 검색 엔진 활성화 설정 (emb_config.json에서 관리)
EMBEDDING_ENABLED = as_bool(EMBED_CONFIG.get("embedding_enabled", True))
BM25_ENABLED = as_bool(EMBED_CONFIG.get("bm25_enabled", True))

BM25_DATABASE_PATH = EMBED_CONFIG.get("bm25_db_path", DATABASE_FILE) if BM25_ENABLED else None
LOCAL_EMBEDDING_MODEL_NAME = EMBED_CONFIG.get("embedding_model_name", "dragonkue/multilingual-e5-small-ko-v2")
LOCAL_EMBEDDING_DEVICE = EMBED_CONFIG.get("embedding_device")
LOCAL_EMBEDDING_NORMALIZE = EMBED_CONFIG.get("normalize_embeddings", True)
LOCAL_EMBEDDING_QUERY_LIMIT = EMBED_CONFIG.get("query_limit", 200)
RAG_SIMILARITY_THRESHOLD = as_float(EMBED_CONFIG.get("similarity_threshold"), 0.6)
RAG_STRONG_SIMILARITY_THRESHOLD = as_float(EMBED_CONFIG.get("strong_similarity_threshold"), 0.72)
RAG_DEBUG_ENABLED = as_bool(load_config_value('RAG_DEBUG_ENABLED', EMBED_CONFIG.get("debug_enabled", False)))
RAG_HYBRID_TOP_K = int(EMBED_CONFIG.get("hybrid_top_k", 5))
RAG_EMBEDDING_TOP_N = int(EMBED_CONFIG.get("embedding_top_n", 8))
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
CONVERSATION_WINDOW_SIZE = max(1, as_int(load_config_value('CONVERSATION_WINDOW_SIZE', EMBED_CONFIG.get("conversation_window_size", 12)), 12))
CONVERSATION_WINDOW_STRIDE = max(1, as_int(load_config_value('CONVERSATION_WINDOW_STRIDE', EMBED_CONFIG.get("conversation_window_stride", 6)), 6))
CONVERSATION_NEIGHBOR_RADIUS = max(1, as_int(load_config_value('CONVERSATION_NEIGHBOR_RADIUS', EMBED_CONFIG.get("conversation_neighbor_radius", 3)), 3))

AI_INTENT_MODEL_NAME = "gemini-2.5-flash-lite"
AI_RESPONSE_MODEL_NAME = "gemini-2.5-flash"
RPM_LIMIT_INTENT = 15
RPM_LIMIT_RESPONSE = 10
RPD_LIMIT_INTENT = 250
RPD_LIMIT_RESPONSE = 250
FINNHUB_API_RPM_LIMIT = 50
AI_TEMPERATURE = 0.7
AI_FREQUENCY_PENALTY = 0.5
AI_PRESENCE_PENALTY = 0.0
KAKAO_API_RPD_LIMIT = 95000
KRX_API_RPD_LIMIT = 9000
AI_RESPONSE_LENGTH_LIMIT = 300
AI_COOLDOWN_SECONDS = 3
# AI 메모리/RAG 기능은 기본 활성화지만, 저사양 환경에서는 환경변수/설정으로 비활성화할 수 있다.
AI_MEMORY_ENABLED = as_bool(load_config_value('AI_MEMORY_ENABLED', EMBED_CONFIG.get("enable_local_embeddings", True)))
AI_INTENT_ANALYSIS_ENABLED = as_bool(load_config_value('AI_INTENT_ANALYSIS_ENABLED', True))
ENABLE_PROACTIVE_KEYWORD_HINTS = as_bool(load_config_value('ENABLE_PROACTIVE_KEYWORD_HINTS', False))
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
    "enabled": True,
    "history_limit": 20000,
    "batch_size": 1000,
    "check_interval_hours": 24
}
AI_CREATIVE_PROMPTS = {
    "fortune": "사용자 '{user_name}'를 위한 오늘의 운세를 재치있게 알려줘.",
    "summarize": "다음 대화 내용을 분석해서, 핵심 내용을 3가지 항목으로 요약해줘.\n--- 대화 내용 ---\n{conversation}",
    "ranking": "다음 서버 활동 랭킹을 보고, 1등을 축하하고 다른 사람들을 독려하는 발표 멘트를 작성해줘.\n--- 활동 랭킹 ---\n{ranking_list}",
    "answer_time": "현재 시간은 '{current_time}'입니다. 이 정보를 사용하여 사용자에게 현재 시간을 알려주세요.",
    "answer_weather": "'{location_name}'의 날씨 정보는 다음과 같습니다: {weather_data}. 이 정보를 바탕으로 사용자에게 날씨를 설명해주세요."
}
FUN_KEYWORD_TRIGGERS = { "enabled": True, "cooldown_seconds": 60, "triggers": { "fortune": ["운세", "오늘 운", "운세 좀"], "summarize": ["요약해줘", "무슨 얘기했어", "무슨 얘기함", "요약 좀", "지금까지 뭔 얘기"] } }
AI_DEBUG_ENABLED = as_bool(load_config_value('AI_DEBUG_ENABLED', False))
AI_DEBUG_LOG_MAX_LEN = int(load_config_value('AI_DEBUG_LOG_MAX_LEN', 400))
KMA_API_KEY = load_config_value('KMA_API_KEY')
KMA_API_DAILY_CALL_LIMIT = 10000
KMA_API_MAX_RETRIES = 3
KMA_API_RETRY_DELAY_SECONDS = 2
DEFAULT_LOCATION_NAME = "광양"
DEFAULT_NX = "73"
DEFAULT_NY = "70"
ENABLE_RAIN_NOTIFICATION = True
RAIN_NOTIFICATION_CHANNEL_ID = 912210558122598450
WEATHER_CHECK_INTERVAL_MINUTES = 60
RAIN_NOTIFICATION_THRESHOLD_POP = 30
ENABLE_GREETING_NOTIFICATION = True
GREETING_NOTIFICATION_CHANNEL_ID = 912210558122598450
MORNING_GREETING_TIME = {"hour": 7, "minute": 30}
EVENING_GREETING_TIME = {"hour": 23, "minute": 50}
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
MSG_AI_ERROR = "😥 아놔, 에러났네. 뭐지? 잠시 후에 다시 물어봐봐."
MSG_AI_COOLDOWN = "😅 야, 좀 천천히 불러라! {remaining:.1f}초 뒤에 다시 말 걸어줘."
MSG_AI_NO_CONTENT = "🤔 ?? 뭘 말해야 할지 모르겠는데? @{bot_name} 하고 할 말을 써줘."
MSG_AI_BLOCKED_PROMPT = "⚠️ 야야, 그런 말은 여기서 하면 안 돼. 알지? 다른 거 물어봐."
MSG_AI_BLOCKED_RESPONSE = "⚠️ 헐, 내가 이상한 말 할 뻔했네. 자체 검열함. 다른 질문 ㄱㄱ"
MSG_AI_NOT_ALLOWED = "😥 이 채널에서는 내가 대답 못 해."
MSG_AI_RATE_LIMITED = "⏳ 아이고, 지금 나 부르는 사람이 너무 많네. 잠시 후에 다시 불러줘."
MSG_AI_DAILY_LIMITED = "😴 오늘 너무 많이 떠들었더니 피곤하다... 내일 다시 말 걸어줘."
MSG_DM_REJECT = "미안한데 DM은 안 받아. 😥 서버 채널에서 불러줘잉."
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
MSG_RAIN_ALERT_CHANNEL_NOT_SET = "🌧️ 비/눈 예보 알림 채널이 설정되지 않았어요! `config.py`에서 `RAIN_NOTIFICATION_CHANNEL_ID`를 확인해주세요."
MSG_RAIN_ALERT_CHANNEL_NOT_FOUND = "🌧️ 비/눈 예보 알림 채널을 찾을 수 없어요. ID({channel_id})가 정확한가요?"
MSG_KMA_API_DAILY_LIMIT_REACHED = "😥 기상청 API 일일 호출 한도에 도달해서 지금은 날씨 정보를 가져올 수 없어. 내일 다시 시도해줘."
MSG_GREETING_CHANNEL_NOT_SET = "☀️🌙 아침/저녁 인사 알림 채널이 설정되지 않았어요! `config.py`의 `RAIN_NOTIFICATION_CHANNEL_ID` 또는 `GREETING_NOTIFICATION_CHANNEL_ID`를 확인해주세요."
MSG_GREETING_CHANNEL_NOT_FOUND = "☀️🌙 아침/저녁 인사 알림 채널을 찾을 수 없어요. ID({channel_id})가 정확한가요?"
MSG_LOCATION_NOT_FOUND = "🗺️ 앗, '{location_name}' 지역의 날씨 정보는 아직 제가 잘 몰라요. 😅 다른 주요 도시 이름으로 다시 물어봐 주실래요? (예: 서울, 부산, 전주 등)"

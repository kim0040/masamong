# 마사몽 설정 가이드 (Setup Guide)

> **빠른 시작**: [QUICKSTART.md](QUICKSTART.md) | **배포**: [DEPLOYMENT.md](../DEPLOYMENT.md)

이 가이드는 마사몽 봇을 처음 설정하는 사용자를 위한 상세한 설정 문서입니다.

---

## 목차

1. [환경 변수 (.env)](#1-환경-변수-env)
2. [config.json (선택)](#2-configjson-선택)
3. [emb_config.json (RAG 설정)](#3-emb_configjson-rag-설정)
4. [prompts.json (페르소나 설정)](#4-promptsjson-페르소나-설정)
5. [다국어 설정](#5-다국어-설정)
6. [데이터베이스 설정](#6-데이터베이스-설정)
7. [자주 묻는 질문 (FAQ)](#7-자주-묻는-질문-faq)

---

## 1. 환경 변수 (.env)

`.env.example`을 복사하여 `.env` 파일을 생성합니다:

```bash
cp .env.example .env
```

### 1.1 필수 설정

| 변수 | 설명 | 예시 |
|------|------|------|
| `DISCORD_BOT_TOKEN` | Discord 봇 토큰 | `MTIzNDU2Nzg5...` |
| `COMETAPI_KEY` | CometAPI 키 (라우팅 모델) | `sk-xxxx` |
| `COMETAPI_IMAGE_API_KEY` | 이미지 키, 보통 `${COMETAPI_KEY}` 재사용 | `${COMETAPI_KEY}` |

### 1.2 LLM 레인 설정

마사몽은 **Dual-Lane** 아키텍처를 사용합니다:

| 레인 | 용도 | Primary 모델 | Fallback 모델 |
|------|------|-------------|---------------|
| **Routing** | 의도 분석, 쿼리 정제 | gpt-5.4-nano | 없음 |
| **Main** | 최종 답변 생성 | deepseek-v4-flash | 없음 |

```env
# Routing 레인
LLM_ROUTING_PRIMARY_PROVIDER=openai_compat
LLM_ROUTING_PRIMARY_MODEL=gpt-5.4-nano
LLM_ROUTING_PRIMARY_BASE_URL=${COMETAPI_BASE_URL}
LLM_ROUTING_PRIMARY_API_KEY=${COMETAPI_KEY}

# Main 레인
LLM_MAIN_PRIMARY_PROVIDER=openai_compat
LLM_MAIN_PRIMARY_MODEL=deepseek-v4-flash
LLM_MAIN_PRIMARY_BASE_URL=${COMETAPI_BASE_URL}
LLM_MAIN_PRIMARY_API_KEY=${COMETAPI_KEY}
LLM_MAIN_FALLBACK_PROVIDER=none

# 논리 LLM 호출의 계층형 안전 한도
COMETAPI_RPM_LIMIT=40
COMETAPI_RPD_LIMIT=3000
LLM_GUILD_RPM_LIMIT=30
LLM_GUILD_RPD_LIMIT=2000
LLM_DM_RPM_LIMIT=20
LLM_DM_RPD_LIMIT=1000
LLM_USER_RPM_LIMIT=12
LLM_USER_RPD_LIMIT=300
LLM_DM_USER_RPM_LIMIT=8
LLM_DM_USER_RPD_LIMIT=120
LLM_FEATURE_RPM_LIMIT=35
LLM_FEATURE_RPD_LIMIT=2500
```

도구 선택은 정상적으로 routing 모델의 의미 판단을 사용하고, 키워드 감지는 provider
장애 시 fallback에만 사용됩니다. 활성 대화 컨텍스트와 routing JSON 출력 예산은 다음처럼
제한합니다.

```env
INTENT_LLM_ENABLED=true
SEMANTIC_ROUTER_MAX_TOKENS=384
AI_CONTEXT_SOURCE_HISTORY_LIMIT=24
AI_CONTEXT_RECENT_TURNS=8
AI_CONTEXT_COMPACTION_TRIGGER_CHARS=3500
AI_CONTEXT_COMPACTION_SOURCE_MAX_CHARS=5000
AI_CONTEXT_DIGEST_MAX_CHARS=600
```

`AI_CONTEXT_COMPACTION_TRIGGER_CHARS`를 넘지 않으면 digest를 만들지 않습니다. 넘으면 같은
routing 호출의 JSON에만 짧은 digest를 포함하므로 별도 LLM 호출은 늘지 않습니다.
`scripts/benchmark_llm_lanes.py`는 DB·실사용자 대화 없이 현재/후보 레인을 비교하며
`--max-calls`로 물리 호출 수를 제한합니다.

### 1.3 웹 검색 설정

```env
WEB_SEARCH_PROVIDER=linkup
LINKUP_API_KEY=your_linkup_api_key_here
LINKUP_ENABLED=true
LINKUP_MONTHLY_BUDGET_ENFORCED=true
LINKUP_MONTHLY_BUDGET_EUR=4.5
LINKUP_FETCH_RENDER_JS=false
LINKUP_FETCH_JS_RETRY_ENABLED=true
WEB_SEARCH_GLOBAL_RPM_LIMIT=20
WEB_SEARCH_GLOBAL_RPD_LIMIT=1000
WEB_SEARCH_GUILD_RPM_LIMIT=10
WEB_SEARCH_GUILD_RPD_LIMIT=300
WEB_SEARCH_USER_RPM_LIMIT=4
WEB_SEARCH_USER_RPD_LIMIT=60
```

### 1.4 관리자 경계

```env
# 프로필마다 별도 지정. 실제 ID는 운영 env에만 두고 Git 예제에는 기록하지 않습니다.
MASAMONG_SUPERADMIN_USER_IDS=replace-with-current-profile-superadmin-user-id
```

- 현재 프로필의 최고 관리자만 `!관리`와 `!초대`를 사용합니다.
- `!관리`는 현재 서버 AI와 현재 채널 응답만 켜고 끌 수 있습니다.
- Discord 서버 관리자 권한이나 기존 등록 관리자 행은 설정 권한을 주지 않습니다.
- 모델·DB·수집 주기·말투는 Discord UI에서 바꿀 수 없습니다.
- 제어 상태는 `(instance_name, guild_id)`로 저장되어 다른 서버와 프로필에 영향을 주지
  않습니다.

### 1.5 이미지 생성 설정

```env
COMETAPI_IMAGE_ENABLED=true
COMETAPI_IMAGE_API_KEY=${COMETAPI_KEY}
COMETAPI_IMAGE_BASE_URL=https://api.cometapi.com
IMAGE_MODEL=gemini-3.1-flash-lite-image
IMAGE_ASPECT_RATIO=1:1
IMAGE_GLOBAL_DAILY_LIMIT=50
IMAGE_GUILD_DAILY_LIMIT=30
IMAGE_USER_LIMIT=10
IMAGE_USER_RESET_HOURS=6
TOOL_CIRCUIT_FAILURE_THRESHOLD=2
TOOL_CIRCUIT_COOLDOWN_SECONDS=60
```

이미지 모델명은 Gemini native 호출 방식과 일치해야 합니다. 다른 모델명이 들어오면
사용량 예약 전에 요청을 중단합니다. 날씨·주식·장소 API는 연속 실패 시 해당 도구만
잠시 차단하고 cooldown 뒤 사용자 요청 한 건으로 복구를 확인합니다.

### 1.6 외부 API 설정

```env
KMA_API_KEY=your_kma_api_key_here        # 기상청 API
FINNHUB_API_KEY=your_finnhub_api_key_here  # 주식 API
KAKAO_API_KEY=your_kakao_api_key_here      # 카카오 로컬 API
```

---

## 2. config.json (선택)

`config.json`은 `.env`보다 낮은 우선순위로 설정값을 오버라이드합니다.

```json
{
    "USER_COOLDOWN_SECONDS": 5,
    "USER_DAILY_LLM_LIMIT": 300,
    "FUN_KEYWORD_TRIGGERS": {
        "cooldown_seconds": 60
    }
}
```

**우선순위**: 환경변수(.env) > config.json > 코드 기본값

---

## 3. emb_config.json (RAG 설정)

RAG(Retrieval-Augmented Generation) 시스템의 설정입니다.

```json
{
    "_comment": "RAG/임베딩 검색 설정",
    "embedding_enabled": true,
    "embedding_model_name": "dragonkue/multilingual-e5-small-ko-v2",
    "similarity_threshold": 0.6,
    "strong_similarity_threshold": 0.72,
    "conversation_window_size": 12,
    "kakao_servers": []
}
```

| 설정 | 기본값 | 설명 |
|------|--------|------|
| `embedding_enabled` | true | 임베딩 검색 활성화 |
| `embedding_model_name` | dragonkue/multilingual-e5-small-ko-v2 | 한국어 최적화 E5 모델 |
| `similarity_threshold` | 0.6 | 최소 유사도 임계값 |
| `strong_similarity_threshold` | 0.72 | 강한 유사도 (웹 검색 불필요) |
| `conversation_window_size` | 12 | 대화 윈도우 크기 |
| `RAG_PASSIVE_NO_TOOL_SEARCH_ENABLED` | true | 무도구 대화에서 bounded 얕은 기억 검색 1회 허용. 관련도 임계값과 인스턴스 격리는 그대로 적용 |

---

## 4. prompts.json (페르소나 설정)

서버(채널)별 AI 페르소나를 설정합니다.

```json
{
    "prompts": {
        "lite_system_prompt": "...",
        "agent_system_prompt": "..."
    },
    "channels": {
        "YOUR_CHANNEL_ID": {
            "allowed": true,
            "persona": "너는 친절한 AI 비서야...",
            "rules": "- 존댓말 사용\n- 내부 태그 숨김"
        }
    }
}
```

### 페르소나 설정 팁

| 항목 | 설명 |
|------|------|
| `persona` | AI의 정체성, 말투, 행동 원칙 |
| `rules` | 반드시 지켜야 할 규칙 |
| `allowed` | 해당 채널에서 AI 응답 허용 여부 |

---

## 5. 다국어 설정

마사몽은 한국어(ko), 영어(en), 일본어(ja)를 지원합니다.

### 5.1 전역 언어 설정

`.env` 파일에서 설정:

```env
MASAMONG_LANG=ko  # ko, en, ja 중 선택
```

### 5.2 운영 중 언어 변경

언어는 프로필 env에 고정한다. Discord 명령으로 서버별 언어를 바꾸지 않으므로,
`MASAMONG_LANG`을 변경한 새 release를 검증·재시작한다.

### 5.3 새 언어 추가

`locales/` 디렉토리에 새 JSON 파일을 생성합니다:

```bash
# 예: 프랑스어 추가
cp locales/en.json locales/fr.json
# fr.json을 번역하여 수정
```

그 후 `utils/locale.py`의 `SUPPORTED_LANGUAGES`에 추가:

```python
SUPPORTED_LANGUAGES = {"ko", "en", "ja", "fr"}
```

---

## 6. 데이터베이스 설정

### 6.1 로컬 개발 (SQLite)

프로필을 지정하지 않은 기존 로컬 개발 모드는 기본적으로
`database/remasamong.db`를 사용합니다. General/Masamo처럼 명시적 프로필로 실행할 때는
`MASAMONG_DATABASE_FILE`에 프로필 이름이 포함된 서로 다른 절대 경로를 반드시 지정합니다.
두 프로필이 같은 SQLite 파일을 가리키거나 `:memory:`를 사용하면 시작 검증이 거부합니다.

### 6.2 프로덕션 (TiDB)

```env
MASAMONG_DB_BACKEND=tidb
MASAMONG_DB_HOST=your_tidb_host
MASAMONG_DB_PORT=4000
MASAMONG_DB_NAME=masamong
MASAMONG_DB_USER=your_user
MASAMONG_DB_PASSWORD=your_password
MASAMONG_DB_SSL_CA=/etc/ssl/certs/ca-certificates.crt
MASAMONG_DB_SSL_VERIFY_IDENTITY=true
MASAMONG_DB_STRICT_REMOTE_ONLY=true
```

### 6.3 연결 풀 설정

```env
MASAMONG_DB_CONN_MAX_LIFETIME_SECONDS=600
MASAMONG_DB_CONNECT_TIMEOUT=10
MASAMONG_DB_READ_TIMEOUT=30
MASAMONG_DB_WRITE_TIMEOUT=30
```

### 6.4 TiDB Cloud Starter 무료 플랜

```env
TIDB_STARTER_FREE_PLAN_MODE=true
TIDB_STARTER_USAGE_WARNING_RATIO=0.8
STRUCTURED_MEMORY_QUERY_LIMIT=384
STRUCTURED_MEMORY_FALLBACK_QUERY_LIMIT=768
```

무료 플랜 모드는 큰 구조화 기억 BLOB 후보 조회를 위 값으로 제한한다. 기존 데이터를
삭제하거나 자동 압축하지 않는다. TiDB Cloud Starter의 무료 한도는 행 저장소 5GiB,
열 저장소 5GiB, 월 5천만 RU다. SQL RU 이력은 당일 및 네트워크 egress가 누락될 수 있어
Cloud 콘솔의 **Usage this month**가 최종 기준이다.

```bash
MASAMONG_ENV_FILE=/etc/masamong/masamo.env \
  <venv>/bin/python scripts/audit_tidb_free_plan_readonly.py \
  --expected-profile masamo --expected-db masamong
```

---

## 7. 자주 묻는 질문 (FAQ)

### Q: 봇이 시작되지 않아요

1. `DISCORD_BOT_TOKEN`이 올바른지 확인
2. Discord Developer Portal에서 **Privileged Intents**가 활성화되어 있는지 확인
3. 로그 파일 `error_logs.txt` 확인

### Q: AI가 응답하지 않아요

1. `COMETAPI_KEY`가 설정되어 있는지 확인
2. 최고 관리자의 `!관리` 패널에서 현재 서버와 현재 채널 상태 확인
3. 보호된 채널 설정에서 해당 채널이 허용되어 있는지 확인

### Q: 날씨 정보가 안 나와요

1. `KMA_API_KEY`가 설정되어 있는지 확인
2. 기상청 API 키 발급: https://apihub.kma.go.kr/

### Q: 웹 검색이 안 돼요

1. `LINKUP_API_KEY`가 설정되어 있는지 확인
2. 월 예산을 초과했을 수 있음 (`LINKUP_MONTHLY_BUDGET_EUR` 확인)

### Q: 언어를 변경하고 싶어요

- 현재 프로필 env에서 `MASAMONG_LANG=en`으로 바꾸고 검증 후 재시작

### Q: 새 서버에 봇을 초대했어요

1. 프로필의 최고 관리자가 `!초대`로 만든 최소 권한 링크를 사용합니다.
2. 보호된 채널 설정에 새 서버 채널을 추가하고 release를 검증합니다.
3. 필요하면 최고 관리자가 서버 안에서 `!관리`를 열어 현재 서버/채널 응답만 켭니다.
4. 일반 서버 관리자에게는 운영 설정이나 말투 변경 권한이 표시되지 않습니다.

---

## 설정 파일 구조 요약

```
masamong/
├── .env                    # 환경변수 (최우선)
├── .env.example            # 환경변수 템플릿
├── config.json             # 설정 오버라이드 (선택)
├── emb_config.json         # RAG/임베딩 설정
├── prompts.json            # 채널별 페르소나
└── locales/                # 다국어 메시지
    ├── ko.json             # 한국어
    ├── en.json             # English
    └── ja.json             # 日本語
```

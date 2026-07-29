# 마사몽 설정 가이드 (Setup Guide)

> **빠른 시작**: [QUICKSTART.md](QUICKSTART.md) | **배포**: [DEPLOYMENT.md](../DEPLOYMENT.md)

이 가이드는 마사몽 봇을 처음 설정하는 사용자를 위한 운영 설정 문서입니다.
`config.py`가 내부 기본값의 최종 기준이고, `.env.example` 및
`profiles/*.env.example`이 실행 프로필의 기준 예제입니다. 여기서는 운영자가
선택해야 하는 공개 설정면을 설명하며 내부 조정 상수까지 전부 나열하지는 않습니다.

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
LLM_DYNAMIC_REASONING_ENABLED=true
LLM_DYNAMIC_REASONING_DEFAULT=low

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
장애 시 fallback에만 사용됩니다. 다만 라우터 JSON의
`requires_external_evidence=true` 요청과 시장 브리핑에는 조회 성공을 요구하는 실행
후조건이 적용됩니다. 이는 답변 품질 안전장치이며 반복 라우팅이나 무한 재시도를 만들지
않습니다. 활성 대화 컨텍스트와 routing JSON 출력 예산은 다음처럼 제한합니다.

같은 routing JSON은 최종 답변의 `reasoning_level`도 `low/high` 중 하나로 정합니다.
단순 대화·명확한 회상·단일 조회 결과 정리는 `low`, 여러 제약·충돌·모호성·긴 근거
종합은 `high`입니다. 별도 난이도 판정 호출이나 low 실패 뒤 high 재호출은 없습니다.
라우터가 값을 누락하거나 계약 밖의 값을 내면 `LLM_DYNAMIC_REASONING_DEFAULT`로 내리며,
provider 장애 fallback도 같은 기본값을 사용합니다. 이 값은 현재 Discord 요청의
지역 변수로만 전달되므로 다른 서버나 동시 요청의 모델 설정을 바꾸지 않습니다.
이름만 있는 신원 질문, 오래된 대화 압축이 필요한 요청, 두 도구 결과를 함께
종합하는 요청은 이미 확인된 구조적 복잡성을 근거로 `high` 후조건을 적용합니다.
특정 이름이나 주제 키워드 목록을 사용하지 않습니다.
`high` 요청에서는 Discord 진행 문구도 “조금 더 오래 고민 중”으로 바뀝니다.
상태 표시만 달라지며 별도 API 호출은 없습니다.
`LLM_DYNAMIC_REASONING_ENABLED=false`이면 기존
`LLM_MAIN_PRIMARY_REASONING_EFFORT` 고정 설정으로 돌아갑니다.

```env
INTENT_LLM_ENABLED=true
SEMANTIC_ROUTER_MAX_TOKENS=384
SEMANTIC_ROUTER_COMPACTION_MAX_TOKENS=768
AI_CONTEXT_SOURCE_HISTORY_LIMIT=24
AI_CONTEXT_RECENT_TURNS=8
AI_CONTEXT_COMPACTION_TRIGGER_CHARS=3500
AI_CONTEXT_COMPACTION_SOURCE_MAX_CHARS=5000
AI_CONTEXT_DIGEST_MAX_CHARS=600
```

`AI_CONTEXT_COMPACTION_TRIGGER_CHARS`를 넘지 않으면 digest를 만들지 않습니다. 넘으면 같은
routing 호출의 JSON에만 짧은 digest를 포함하므로 별도 LLM 호출은 늘지 않습니다.
이때만 `SEMANTIC_ROUTER_COMPACTION_MAX_TOKENS`가 적용되어 한국어 digest 때문에 JSON이
중간에서 잘리는 것을 막고, 평상시 요청은 384토큰 상한을 그대로 사용합니다. 정상
라우터가 `needs_memory=false`로 판정한 인사·일반 지식에는 RAG를 중복 실행하지 않으며,
provider 장애 fallback에서만 얕은 검색을 안전망으로 사용합니다. DM의 저장된 운세
컨텍스트도 `needs_fortune_context=true`인 운세 후속 질문에만 조회합니다. 설명 없이
이름·핸들 하나만 두고 신원을 묻는 짧은 질문은 공개 웹 동명이인보다 현재 Discord
서버/DM 기억을 우선하는 형식 후조건을 적용합니다. 특정 이름 목록을 사용하지 않으며,
직업·사건을 붙이거나 웹 검색을 명시한 외부 인물 질문은 이 후조건에서 제외됩니다.
최근 원문만으로 답할 수 있는 후속 질문은 보통 장기기억을 생략하지만, 라우터가 오래된
합의 확인이 유용하다고 판단한 경우에는 관련도 gate와 최대 3블록 상한 아래에서 한 번
검색할 수 있습니다. 이 보수적 검색은 새 LLM 호출을 추가하지 않습니다.
`scripts/benchmark_llm_lanes.py`는 DB·실사용자 대화 없이 현재/후보 레인을 비교하며
`--max-calls`로 물리 호출 수를 제한합니다.

### 1.3 웹 검색 설정

```env
WEB_SEARCH_PROVIDER=linkup
LINKUP_API_KEY=your_linkup_api_key_here
LINKUP_ENABLED=true
LINKUP_TIMEOUT_SECONDS=40
LINKUP_PIPELINE_TIMEOUT_SECONDS=55
WEB_SEARCH_TOTAL_TIMEOUT_SECONDS=60
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
| `RAG_PASSIVE_NO_TOOL_SEARCH_ENABLED` | true | 정상 의미 라우터가 실패한 무도구 fallback에서만 bounded 얕은 기억 검색 1회 허용. 관련도 임계값과 인스턴스 격리는 그대로 적용 |

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

> **현재 상태: 마사몽의 사용자 대면 언어는 한국어입니다.**
> `locales/`에 ko·en·ja 파일이 있지만, 실제로 번역이 적용되는 문구는
> `config.py`가 `_locale_msg()`로 노출한 소수(날씨 오류, AI/명령 오류, 로그
> 삭제 안내)뿐입니다. 나머지 Discord 문구 400여 개는 각 Cog에 한국어로
> 직접 적혀 있어 `MASAMONG_LANG`을 바꿔도 한국어로 나갑니다.
> `MASAMONG_LANG=en`을 운영에서 쓰면 **한국어와 영어가 섞여 나옵니다.**

### 5.1 전역 언어 설정

`.env` 파일에서 설정:

```env
MASAMONG_LANG=ko  # 운영에서 실질적으로 지원하는 값은 ko
```

### 5.2 운영 중 언어 변경

언어는 프로필 env에 고정한다. Discord 명령으로 서버별 언어를 바꾸지 않으므로,
`MASAMONG_LANG`을 변경한 새 release를 검증·재시작한다.

### 5.3 다른 언어를 실제로 지원하려면

파일을 번역하는 것만으로는 부족하다. 순서는 다음과 같다.

1. `locales/<lang>.json`을 만들고 번역한다.
2. `utils/locale.py`의 `SUPPORTED_LANGUAGES`에 코드를 추가한다.
3. **`config.py`에 `MSG_* = _locale_msg("MSG_*")` 배선을 추가한다.**
   현재 `locales/ko.json`의 37개 키 중 12개만 배선돼 있다.
4. **각 Cog의 한국어 하드코딩 문구를 해당 `config.MSG_*` 참조로 바꾼다.**
   3~4단계를 건너뛰면 언어를 바꿔도 대부분 한국어로 나온다.

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
BM25_AUTO_REBUILD_ENABLED=false
```

무료 플랜 모드는 큰 구조화 기억 BLOB 후보 조회를 위 값으로 제한한다. 기존 데이터를
삭제하거나 자동 압축하지 않는다. TiDB Cloud Starter의 무료 한도는 행 저장소 5GiB,
열 저장소 5GiB, 월 5천만 RU다. SQL RU 이력은 당일 및 네트워크 egress가 누락될 수 있어
Cloud 콘솔의 **Usage this month**가 최종 기준이다.

운영 RAG는 의미 임베딩과 선택적 TiDB 벡터 검색을 사용한다. BM25/FTS5 관련 호환
코드는 남아 있어도 `config.py`가 검색 관리자를 생성하지 않으며, 명시적 운영
프로필은 `BM25_AUTO_REBUILD_ENABLED=false`가 아니면 검증에 실패한다. 따라서
저사양 서버에서 BM25 인덱스 생성·조회·자동 재구축이 실행되지 않는다.

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

- 지금은 한국어만 실질적으로 지원합니다. `MASAMONG_LANG`을 바꿔도 일부 오류
  문구만 번역되고 나머지는 한국어로 나갑니다. 자세한 절차는 [5.3](#53-다른-언어를-실제로-지원하려면) 참고

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

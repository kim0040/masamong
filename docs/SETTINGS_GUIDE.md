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
| `NANOGPT_KEY` | NanoGPT 키 (메인 모델) | `sk-xxxx` |

### 1.2 LLM 레인 설정

마사몽은 **Dual-Lane** 아키텍처를 사용합니다:

| 레인 | 용도 | Primary 모델 | Fallback 모델 |
|------|------|-------------|---------------|
| **Routing** | 의도 분석, 쿼리 정제 | gemini-3.1-flash-lite-preview | gemini-2.5-flash |
| **Main** | 최종 답변 생성 | DeepSeek-V3.2-Exp | DeepSeek-V3.2-Exp |

```env
# Routing 레인
LLM_ROUTING_PRIMARY_PROVIDER=openai_compat
LLM_ROUTING_PRIMARY_MODEL=gemini-3.1-flash-lite-preview
LLM_ROUTING_PRIMARY_BASE_URL=${COMETAPI_BASE_URL}
LLM_ROUTING_PRIMARY_API_KEY=${COMETAPI_KEY}

# Main 레인
LLM_MAIN_PRIMARY_PROVIDER=openai_compat
LLM_MAIN_PRIMARY_MODEL=xiaomi/mimo-v2-flash
LLM_MAIN_PRIMARY_BASE_URL=${NANOGPT_BASE_URL}
LLM_MAIN_PRIMARY_API_KEY=${NANOGPT_KEY}
```

### 1.3 웹 검색 설정

```env
WEB_SEARCH_PROVIDER=linkup
LINKUP_API_KEY=your_linkup_api_key_here
LINKUP_ENABLED=true
LINKUP_MONTHLY_BUDGET_ENFORCED=true
LINKUP_MONTHLY_BUDGET_EUR=4.5
```

### 1.4 이미지 생성 설정

```env
COMETAPI_IMAGE_ENABLED=true
COMETAPI_IMAGE_API_KEY=${NANOGPT_KEY}
COMETAPI_IMAGE_BASE_URL=${NANOGPT_BASE_URL}
IMAGE_MODEL=qwen-image
IMAGE_ASPECT_RATIO=1:1
```

### 1.5 외부 API 설정

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

### 5.2 서버별 언어 설정

Discord에서 슬래시 명령어 사용:

```
/config language 한국어
/config language English
/config language 日本語
```

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

별도 설정 불필요. 기본값으로 `database/masamong.db` 파일을 사용합니다.

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

---

## 7. 자주 묻는 질문 (FAQ)

### Q: 봇이 시작되지 않아요

1. `DISCORD_BOT_TOKEN`이 올바른지 확인
2. Discord Developer Portal에서 **Privileged Intents**가 활성화되어 있는지 확인
3. 로그 파일 `error_logs.txt` 확인

### Q: AI가 응답하지 않아요

1. `COMETAPI_KEY` 또는 `NANOGPT_KEY`가 설정되어 있는지 확인
2. `/config set_ai True` 명령어로 AI가 활성화되어 있는지 확인
3. 해당 채널이 허용된 채널인지 확인

### Q: 날씨 정보가 안 나와요

1. `KMA_API_KEY`가 설정되어 있는지 확인
2. 기상청 API 키 발급: https://apihub.kma.go.kr/

### Q: 웹 검색이 안 돼요

1. `LINKUP_API_KEY`가 설정되어 있는지 확인
2. 월 예산을 초과했을 수 있음 (`LINKUP_MONTHLY_BUDGET_EUR` 확인)

### Q: 언어를 변경하고 싶어요

- 전역: `.env`에서 `MASAMONG_LANG=en` 설정
- 서버별: `/config language English` 명령어 사용

### Q: 새 서버에 봇을 초대했어요

1. 봇 초대 후 자동으로 DB에 서버 설정이 생성됩니다
2. `/config set_ai True`로 AI 활성화
3. `/config channel add #채널명`으로 허용 채널 추가
4. (선택) `/config language English`로 언어 설정

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

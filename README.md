<p align="center">
  <h1 align="center">🤖 마사몽 (Masamong)</h1>
  <p align="center">
    AI 기반 다기능 Discord 봇 — 대화 · 날씨 · 주식 · 운세 · 이미지 생성을 하나로
  </p>
  <p align="center">
    <img src="https://img.shields.io/badge/Python-3.9+-3776AB?logo=python&logoColor=white" alt="Python">
    <img src="https://img.shields.io/badge/discord.py-2.0+-5865F2?logo=discord&logoColor=white" alt="discord.py">
    <img src="https://img.shields.io/badge/AI-Gemini%20%7C%20CometAPI-FF6F00?logo=google&logoColor=white" alt="AI">
    <img src="https://img.shields.io/badge/DB-SQLite-003B57?logo=sqlite&logoColor=white" alt="SQLite">
    <img src="https://img.shields.io/badge/License-Private-gray" alt="License">
  </p>
</p>

---

한국어 | [English](docs/README.en.md) | [日本語](docs/README.ja.md)

## 📋 목차

- [소개](#-소개)
- [주요 기능](#-주요-기능)
- [기술 스택](#-기술-스택)
- [프로젝트 구조](#-프로젝트-구조)
- [시작하기](#-시작하기)
  - [사전 요구사항](#사전-요구사항)
  - [설치](#설치)
  - [환경 변수 설정](#환경-변수-설정)
  - [데이터베이스 초기화](#데이터베이스-초기화)
  - [실행](#실행)
- [환경 변수 레퍼런스](#-환경-변수-레퍼런스)
- [명령어 레퍼런스](#-명령어-레퍼런스)
- [아키텍처](#-아키텍처)
  - [메시지 처리 흐름](#메시지-처리-흐름)
  - [AI 파이프라인](#ai-파이프라인)
  - [RAG 시스템](#rag-시스템)
  - [백그라운드 태스크](#백그라운드-태스크)
- [데이터베이스 스키마](#-데이터베이스-스키마)
- [설정 파일](#-설정-파일)
- [배포](#-배포)
- [기여하기](#-기여하기)
- [라이선스](#-라이선스)

---

## 🎯 소개

**마사몽**은 Discord 서버에서 `@마사몽` 멘션으로 자연스러운 대화를 나눌 수 있는 AI 봇입니다. 단순한 챗봇을 넘어, 실시간 날씨 예보, 주식 시세, 환율 조회, AI 이미지 생성, 사주/운세 등 **생활 밀착형 도구**를 통합 제공합니다.

### ✨ 왜 마사몽인가?

- **멘션 기반 대화** — `@마사몽`으로 호출할 때만 응답하여, 서버 대화를 방해하지 않습니다
- **장기 기억(RAG)** — 과거 대화를 임베딩 기반으로 검색하여 문맥을 기억합니다
- **자동 알림 시스템** — 강수 예보, 아침/저녁 인사, 지진 경보를 자동으로 전송합니다
- **다중 AI 폴백** — CometAPI → Gemini 순서로 시도하여 높은 가용성을 보장합니다
- **서버별 커스터마이징** — 슬래시 커맨드로 AI 페르소나, 허용 채널 등을 서버별로 설정합니다

---

## 🛠 주요 기능

### 💬 AI 대화
| 기능 | 설명 |
|------|------|
| 멘션 기반 대화 | `@마사몽`으로 시작하는 메시지에만 응답 |
| DM 대화 | 멘션 없이 1:1 대화 가능 (사용량 제한 적용) |
| 장기 기억 (RAG) | 임베딩 + BM25 하이브리드 검색으로 과거 대화 참조 |
| 웹 검색 | 최신 정보가 필요한 질문에 자동으로 웹 검색 수행 |
| 능동적 제안 | 대화 맥락에서 관련 기능을 자연스럽게 안내 |

### 🌦️ 날씨 · 재난
| 기능 | 설명 |
|------|------|
| 현재 날씨 | 기상청 초단기실황 API 기반 현재 날씨 조회 |
| 단기/중기 예보 | 3일 / 10일 예보 조회 |
| 주간 날씨 종합 | `!날씨 이번주`로 단기+중기 통합 요약 |
| 강수 알림 | 비/눈 예보 시 지정 채널에 자동 알림 |
| 아침/저녁 인사 | 지정 시각에 날씨 요약 포함 AI 인사 전송 |
| 지진 경보 | 국내 영향권 지진 발생 시 전 채널 알림 |
| 기상 특보 | 폭염/한파/태풍 등 기상 특보 연동 |

### 📊 생활 도구
| 기능 | 설명 |
|------|------|
| 주식 시세 | yfinance 기반 글로벌 주식 + KRX 국내 주식 조회 |
| 환율 조회 | 한국수출입은행 API 기반 실시간 환율 |
| 장소 검색 | 카카오 API 기반 키워드 장소 검색 |
| 이미지 생성 | Gemini Native API 기반 텍스트-이미지 생성 |
| 이미지 검색 | 카카오 이미지 검색 |

### 🔮 운세 · 별자리
| 기능 | 설명 |
|------|------|
| 오늘의 운세 | 사주(생년월일/시) 기반 운세 생성 |
| 별자리 운세 | 12별자리 일일 운세 및 랭킹 |
| 월간/연간 운세 | 이번 달 / 올해 운세 상세 분석 |
| 운세 구독 | 매일 지정 시간에 DM으로 운세 브리핑 전송 |

### 🎉 커뮤니티
| 기능 | 설명 |
|------|------|
| 대화 요약 | 최근 대화를 AI가 3줄 요약 |
| 활동 랭킹 | 서버 멤버 메시지 활동 TOP 5 |
| 투표 | 찬반 / 다중 선택 투표 생성 |

---

## 🧰 기술 스택

| 분류 | 기술 |
|------|------|
| **언어** | Python 3.9+ |
| **프레임워크** | discord.py 2.0+ |
| **AI** | Google Gemini (google-generativeai), CometAPI (OpenAI Compatible) |
| **데이터베이스** | SQLite (aiosqlite) |
| **임베딩** | sentence-transformers, numpy |
| **외부 API** | 기상청(KMA), 카카오, yfinance, Finnhub, 한국수출입은행 |
| **점성술** | ephem, korean-lunar-calendar |

---

## 📁 프로젝트 구조

```
masamong/
├── main.py                     # 봇 엔트리포인트 (초기화, Cog 로딩, 메시지 라우팅)
├── config.py                   # 전체 설정값 관리 (환경변수 → config.json → 기본값)
├── logger_config.py            # 로깅 설정
├── requirements.txt            # Python 의존성
├── setup.py                    # 초기 설정 스크립트
│
├── cogs/                       # Discord Cog 모듈 (기능 단위 분리)
│   ├── ai_handler.py           # AI 대화 파이프라인 (도구 감지, RAG, LLM 호출)
│   ├── weather_cog.py          # 날씨 조회, 강수/인사/지진 알림
│   ├── fortune_cog.py          # 운세, 별자리, 사주
│   ├── tools_cog.py            # 외부 도구 (주식/환율/장소/웹/이미지)
│   ├── commands.py             # 일반 명령어 (로그 삭제, 이미지 생성, 업데이트)
│   ├── fun_cog.py              # 재미 기능 (대화 요약)
│   ├── activity_cog.py         # 활동 기록 및 랭킹
│   ├── poll_cog.py             # 투표 기능
│   ├── events.py               # 이벤트 리스너 (on_ready, on_error 등)
│   ├── settings_cog.py         # 서버별 설정 (슬래시 커맨드)
│   ├── maintenance_cog.py      # 유지보수 (아카이빙, BM25 재구축)
│   ├── help_cog.py             # 커스텀 도움말
│   └── proactive_assistant.py  # 능동적 제안 시스템
│
├── utils/                      # 유틸리티 모듈
│   ├── weather.py              # 기상청 API 래퍼
│   ├── coords.py               # 좌표 변환 유틸리티
│   ├── db.py                   # 데이터베이스 유틸리티
│   ├── embeddings.py           # 임베딩 생성/저장
│   ├── hybrid_search.py        # 하이브리드 RAG 검색 (임베딩 + BM25)
│   ├── query_rewriter.py       # 검색 쿼리 확장/재작성
│   ├── reranker.py             # 검색 결과 리랭킹
│   ├── fortune.py              # 사주/운세 계산 유틸리티
│   ├── http.py                 # HTTP 요청 래퍼
│   ├── text_cleaner.py         # 텍스트 전처리
│   ├── chunker.py              # 텍스트 청킹
│   ├── data_formatters.py      # 데이터 포맷팅
│   ├── kma_codes.py            # 기상청 코드 매핑
│   ├── initial_data.py         # 초기 데이터 시딩
│   └── api_handlers/           # 외부 API 핸들러
│       ├── exchange_rate.py    #   환율 (수출입은행)
│       ├── yfinance_handler.py #   주식 (yfinance)
│       ├── finnhub.py          #   주식 (Finnhub)
│       ├── krx.py / krx_v2.py  #   국내 주식 (KRX)
│       └── kakao.py            #   장소/이미지 검색 (카카오)
│
├── database/                   # 데이터베이스
│   ├── schema.sql              # 전체 스키마 정의
│   ├── init_db.py              # DB 초기화 스크립트
│   └── bm25_index.py           # BM25 인덱스 관리
│
├── tests/                      # 테스트
├── scripts/                    # 개발/진단 스크립트
├── docs/                       # 문서 (다국어 README, 아키텍처, 변경이력)
└── examples/                   # 설정 파일 예시
```

---

## 🚀 시작하기

### 사전 요구사항

- **Python 3.9** 이상
- **Discord Bot Token** ([Discord Developer Portal](https://discord.com/developers/applications)에서 발급)
- **Gemini API Key** ([Google AI Studio](https://aistudio.google.com/apikey)에서 발급) — AI 대화 필수

### 설치

```bash
# 1. 저장소 클론
git clone https://github.com/kim0040/masamong.git
cd masamong

# 2. 가상환경 생성 및 활성화
python3 -m venv .venv
source .venv/bin/activate  # macOS/Linux
# .venv\Scripts\activate   # Windows

# 3. 의존성 설치
pip install -r requirements.txt
```

#### (선택) RAG 기능 활성화

장기 기억 기능을 사용하려면 추가 패키지가 필요합니다:

```bash
# CPU 전용 환경 (서버 권장)
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install numpy sentence-transformers
```

### 환경 변수 설정

`.env` 파일 또는 `config.json`을 프로젝트 루트에 생성합니다.

```env
# === 필수 ===
DISCORD_BOT_TOKEN=your_discord_bot_token
GEMINI_API_KEY=your_gemini_api_key

# === AI (선택) ===
COMETAPI_KEY=your_cometapi_key           # CometAPI 우선 사용
USE_COMETAPI=true

# === 날씨 (선택) ===
KMA_API_KEY=your_kma_api_key             # 기상청 API

# === 주식/환율 (선택) ===
FINNHUB_API_KEY=your_finnhub_key
EXIM_API_KEY_KR=your_exim_key            # 한국수출입은행 환율

# === 검색 (선택) ===
KAKAO_API_KEY=your_kakao_key             # 장소/이미지 검색
GOOGLE_API_KEY=your_google_key           # 웹 검색
GOOGLE_CX=your_google_cx
```

> **설정 우선순위**: 환경 변수 → `config.json` → 기본값

### 데이터베이스 초기화

```bash
python3 database/init_db.py
```

> 봇 첫 실행 시 자동으로 마이그레이션이 수행되므로, 이 단계는 선택사항입니다.

### 실행

```bash
python3 main.py
```

---

## 📖 환경 변수 레퍼런스

### 필수 설정

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `DISCORD_BOT_TOKEN` | Discord 봇 토큰 | — |
| `GEMINI_API_KEY` | Google Gemini API 키 | — |

### AI 설정

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `COMETAPI_KEY` | CometAPI 키 (우선 사용) | — |
| `COMETAPI_BASE_URL` | CometAPI 엔드포인트 | `https://api.cometapi.com/v1` |
| `COMETAPI_MODEL` | CometAPI 모델명 | `DeepSeek-V3.2-Exp-nothinking` |
| `USE_COMETAPI` | CometAPI 활성화 여부 | `true` |
| `AI_RESPONSE_LENGTH_LIMIT` | 응답 최대 글자수 | `300` |
| `AI_TEMPERATURE` | 생성 온도 | `0.0` |
| `AI_REQUEST_TIMEOUT` | 응답 타임아웃 (초) | `45` |
| `AI_INTENT_ANALYSIS_ENABLED` | 의도 분석 활성화 | `true` |

### 날씨/알림 설정

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `KMA_API_KEY` | 기상청 API 키 | — |
| `ENABLE_RAIN_NOTIFICATION` | 강수 알림 활성화 | `true` |
| `RAIN_NOTIFICATION_CHANNEL_ID` | 알림 전송 채널 ID | — |
| `WEATHER_CHECK_INTERVAL_MINUTES` | 날씨 확인 주기 (분) | `60` |
| `RAIN_NOTIFICATION_THRESHOLD_POP` | 강수 알림 확률 임계값 (%) | `30` |
| `ENABLE_GREETING_NOTIFICATION` | 아침/저녁 인사 활성화 | `true` |

### 외부 API 키

| 변수 | 설명 | 용도 |
|------|------|------|
| `KAKAO_API_KEY` | 카카오 API 키 | 장소/이미지 검색 |
| `GOOGLE_API_KEY` | Google Custom Search 키 | 웹 검색 |
| `GOOGLE_CX` | Google CX 식별자 | 웹 검색 |
| `SERPAPI_KEY` | SerpAPI 키 | 웹 검색 (폴백) |
| `FINNHUB_API_KEY` | Finnhub 키 | 해외 주식 |
| `EXIM_API_KEY_KR` | 수출입은행 API 키 | 환율 |

### 이미지 생성 설정

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `COMETAPI_IMAGE_ENABLED` | 이미지 생성 활성화 | `true` |
| `IMAGE_MODEL` | 이미지 생성 모델 | `doubao-seedream-5-0-260128` |
| `IMAGE_SIZE` | 이미지 크기 설정 | `4K` |
| `IMAGE_RESPONSE_FORMAT` | 이미지 응답 형식 (url / b64_json) | `url` |
| `IMAGE_USER_LIMIT` | 유저당 이미지 제한 (12시간) | `5` |
| `IMAGE_GLOBAL_DAILY_LIMIT` | 전역 일일 이미지 제한 | `50` |

### 안전장치 설정

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `USER_COOLDOWN_SECONDS` | 유저별 요청 쿨다운 (초) | `3` |
| `USER_DAILY_LLM_LIMIT` | 유저별 일일 LLM 호출 제한 | `200` |
| `GLOBAL_DAILY_LLM_LIMIT` | 전역 일일 LLM 호출 제한 | `5000` |

---

## 📚 명령어 레퍼런스

### 🔹 일반 명령어

| 명령어 | 별명 | 설명 | 범위 |
|--------|------|------|------|
| `!도움` | `!도움말`, `!h`, `!help` | 명령어 목록 및 상세 도움말 | 전체 |
| `!날씨 [지역]` | `!weather` | 오늘/내일/모레/주간 날씨 조회 | 전체 |
| `!이미지 <설명>` | — | AI 이미지 생성 | 서버 |
| `!업데이트` | — | 최근 업데이트 내역 확인 | 전체 |

### 🔹 운세 명령어

| 명령어 | 별명 | 설명 | 범위 |
|--------|------|------|------|
| `!운세` | `!fortune` | 오늘의 운세 | 전체 |
| `!운세 등록` | — | 생년월일/시간 등록 | DM |
| `!운세 상세` | — | 상세 운세 (DM 전송) | 전체 |
| `!운세 구독 [HH:MM]` | — | 매일 운세 브리핑 구독 | 전체 |
| `!별자리 [별자리명]` | — | 별자리 운세 | 전체 |
| `!별자리 순위` | — | 12별자리 행운 랭킹 | 전체 |
| `!이번달운세` | — | 월간 운세 | 전체 |
| `!올해운세` | — | 연간 운세 | 전체 |

### 🔹 커뮤니티 명령어

| 명령어 | 별명 | 설명 | 범위 |
|--------|------|------|------|
| `!요약` | `!summarize`, `!3줄요약` | 최근 대화 AI 요약 | 서버 |
| `!랭킹` | `!수다왕`, `!ranking` | 서버 활동 TOP 5 | 서버 |
| `!투표 "주제" "항목1" "항목2"` | `!poll` | 투표 생성 | 서버 |

### 🔹 관리 명령어

| 명령어 | 설명 | 권한 |
|--------|------|------|
| `!delete_log` | 로그 파일 삭제 | 관리자 |
| `!debug status` | 시스템 상태 확인 | 봇 오너 |
| `!debug reset_dm <user_id>` | DM 제한 초기화 | 봇 오너 |
| `/config ai_enabled` | AI 기능 활성화/비활성화 | 관리자 |
| `/config channel` | AI 허용 채널 관리 | 관리자 |
| `/config persona` | AI 페르소나 설정 | 관리자 |

---

## 🏗 아키텍처

### 메시지 처리 흐름

```
Discord Message
  │
  ├─ "!" 프리픽스 → 명령어 처리 (commands.py, fun_cog, etc.)
  │
  └─ 일반 메시지
      │
      ├─ activity_cog: 활동 기록
      │
      ├─ events.py: 키워드 트리거 감지 (운세, 요약 등)
      │
      └─ ai_handler.py: AI 파이프라인 진입
          │
          ├─ 멘션 검증 (서버: 필수, DM: 비필수)
          ├─ 도구 감지 (키워드 매칭)
          ├─ 도구 실행 (tools_cog → 날씨/주식/환율/장소)
          ├─ RAG 컨텍스트 검색 (hybrid_search)
          ├─ 웹 검색 (자동 판단)
          ├─ 프롬프트 구성
          └─ LLM 응답 생성 (CometAPI → Gemini 폴백)
```

### AI 파이프라인

| 단계 | 모듈 | 설명 |
|------|------|------|
| 1. 라우팅 | `main.py` | 멘션 검사 후 AI 핸들러로 전달 |
| 2. 도구 감지 | `ai_handler.py` | 키워드 기반 도구 선택 |
| 3. 도구 실행 | `tools_cog.py` | 외부 API 호출 및 결과 수집 |
| 4. RAG 검색 | `hybrid_search.py` | 임베딩 + BM25 하이브리드 검색 |
| 5. 프롬프트 구성 | `ai_handler.py` | 페르소나 + 도구 결과 + RAG 컨텍스트 조합 |
| 6. LLM 호출 | `ai_handler.py` | CometAPI → Gemini 순서로 시도 |

### RAG 시스템

```
대화 기록 저장 (conversation_history)
        │
        ▼
윈도우 단위 분할 (conversation_windows)
        │
        ▼
임베딩 벡터 생성 (sentence-transformers)
        │
        ▼
벡터 DB 저장 (discord_embeddings.db)
        │
        ▼
질문 시 하이브리드 검색 (임베딩 + BM25 → RRF 통합 → 리랭킹)
```

### 백그라운드 태스크

| 태스크 | 주기 | 설명 |
|--------|------|------|
| 강수 알림 | 60분 | 단기예보 강수확률 기반 자동 알림 |
| 아침 인사 | 매일 07:30 | 날씨 요약 포함 AI 인사 |
| 저녁 인사 | 매일 23:50 | 날씨 요약 포함 AI 인사 |
| 지진 알림 | 1분 | 국내 영향권 지진 발생 실시간 알림 |
| 아카이빙 | 24시간 | 오래된 대화 기록 아카이빙 |
| BM25 재구축 | 15분 | 유휴 상태 감지 시 인덱스 갱신 |
| 운세 브리핑 | 매일 (구독 시간) | 구독자에게 DM 운세 전송 |

---

## 🗃 데이터베이스 스키마

SQLite 기반으로 12개 테이블을 사용합니다. 전체 스키마는 [`database/schema.sql`](database/schema.sql)에 정의되어 있습니다.

| 테이블 | 용도 |
|--------|------|
| `guild_settings` | 서버별 AI 설정 (활성화/채널/페르소나) |
| `user_activity` | 유저별 메시지 활동 기록 |
| `conversation_history` | 실시간 대화 기록 저장 |
| `conversation_windows` | 윈도우 단위 대화 요약/임베딩 |
| `conversation_history_archive` | 보관된 과거 대화 |
| `user_profiles` | 운세용 유저 프로필 (생년월일/성별/구독) |
| `user_preferences` | 유저 알림/선호 설정 |
| `locations` | 날씨 격자 좌표 매핑 |
| `system_counters` | API 호출 카운터 |
| `api_call_log` | API 호출 기록 (RPM/RPD 관리) |
| `analytics_log` | 운영 분석 로그 |
| `dm_usage_logs` | DM 사용량 제한 관리 |

---

## ⚙ 설정 파일

| 파일 | 용도 | Git 추적 |
|------|------|----------|
| `.env` | 환경 변수 (API 키 등) | ❌ |
| `config.json` | 환경 변수 대체 설정 | ❌ |
| `prompts.json` / `prompts.yaml` | AI 프롬프트, 채널 설정, 페르소나 | ❌ |
| `emb_config.json` | 임베딩/RAG 상세 설정 | ❌ |
| `config.py` | 설정 로드 로직 & 기본값 | ✅ |
| `database/schema.sql` | DB 스키마 정의 | ✅ |

> **프롬프트 커스터마이징**: `prompts.json`의 `channels` 섹션에서 채널별 페르소나와 규, 허용 여부를 설정할 수 있습니다.

---

## 🚢 배포

배포 가이드는 [`DEPLOYMENT.md`](DEPLOYMENT.md)를 참고하세요.

```bash
# 빠른 배포 (기존 서버)
git pull origin main
pip install -r requirements.txt
python3 database/init_db.py
python3 main.py
```

> **운영 시 유의사항**
> - `GEMINI_API_KEY`가 없으면 AI 기능 전체가 비활성화됩니다
> - 주식 조회는 yfinance 기본이며, CometAPI 티커 추출 실패 시 조회가 불가합니다
> - 이미지 생성은 유저/전역 제한이 적용됩니다
> - DM은 5시간당 30회 + 전역 하루 100회 제한이 있습니다

---

## 🤝 기여하기

기여 가이드는 [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md)를 참고하세요.

1. Fork 후 브랜치 생성 (`git checkout -b feature/amazing-feature`)
2. 변경사항 커밋 (`git commit -m 'feat: 새로운 기능 추가'`)
3. 브랜치에 Push (`git push origin feature/amazing-feature`)
4. Pull Request 생성

---

## 📄 라이선스

이 프로젝트는 비공개 라이선스로 관리됩니다. 자세한 사항은 프로젝트 관리자에게 문의하세요.

---

<p align="center">
  Made with ❤️ by the Masamong Team
</p>

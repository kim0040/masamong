# 마사몽 (Masamong)

마사몽은 Discord 서버에서 동작하는 **한국어 중심 AI 챗봇**입니다.  
멘션 기반 대화, 구조화 메모리/RAG, Kakao 대화 벡터 검색, 날씨/금융/웹 검색 도구, 운세, 이미지 생성, 커뮤니티 기능을 하나의 런타임으로 통합 운영합니다.

- **언어**: Python 3.9+
- **프레임워크**: `discord.py` (>=2.7.1)
- **DB 백엔드**: TiDB (운영) / SQLite (개발)
- **LLM**: CometAPI (OpenAI-compatible), Gemini (선택적 fallback)
- **듀얼 레인 아키텍처**: Routing Lane(의도 분석/쿼리 정제) + Main Lane(최종 답변 생성)

---

## 문서 언어

| 언어 | 링크 |
|------|------|
| 한국어 | (이 문서) |
| English | [docs/README.en.md](docs/README.en.md) |
| 日本語 | [docs/README.ja.md](docs/README.ja.md) |

---

## 1. 아키텍처 개요

### 1.1 듀얼 레인 LLM 시스템

```
사용자 메시지
    │
    ▼
┌─────────────────────────────┐
│  Routing Lane (경량 모델)     │
│  - 의도 분석 / 키워드 추출    │
│  - 도구 선택 (날씨/금융/웹)   │
│  - 검색 쿼리 정제             │
│  모델: gemini-3.1-flash-lite  │
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│  도구 실행 (ToolsCog)        │
│  - 날씨 (KMA API)            │
│  - 금융 (Finnhub/yfinance)   │
│  - 웹 검색 (Linkup/DDG)      │
│  - RAG 메모리 검색           │
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│  Main Lane (고성능 모델)      │
│  - 도구 결과 + RAG 기반 답변  │
│  - 채널 페르소나 적용         │
│  - 대화 기록 / 메모리 저장    │
│  모델: DeepSeek-V3.2-Exp     │
└─────────────────────────────┘
```

각 레인은 **Primary + Fallback** 타깃을 가질 수 있으며, 실패 시 자동 전환됩니다.

### 1.2 저장소 구조

```
┌─ Main DB (TiDB/SQLite) ────────────────────────┐
│ conversation_history, conversation_windows,       │
│ guild_settings, user_profiles, user_activity_log, │
│ locations, api_call_log, linkup_usage_log,        │
│ dm_usage_logs, system_counters, analytics_log     │
└──────────────────────────────────────────────────┘

┌─ Discord 메모리 저장소 (TiDB/SQLite) ───────────┐
│ discord_chat_embeddings  (레거시 단일 임베딩)     │
│ discord_memory_entries   (구조화 메모리)          │
│   - channel scope / user scope                   │
│   - summary_text + raw_context + embedding       │
└──────────────────────────────────────────────────┘

┌─ Kakao 저장소 (TiDB/로컬) ──────────────────────┐
│ kakao_chunks                                      │
│   - room_key 단위 분리                            │
│   - VECTOR(384) 검색                              │
└──────────────────────────────────────────────────┘
```

### 1.3 구조화 메모리 vs 원본 로그

| 구분 | 원본 로그 | 구조화 메모리 |
|------|-----------|---------------|
| 저장소 | `conversation_history` | `discord_memory_entries` |
| 목적 | 감사 추적, 문맥 복원 | 회상 검색 최적화 |
| 단위 | 개별 메시지 | 대화 윈도우 (6메시지, stride=3) |
| 내용 | 원문 그대로 | 요약 + 키워드 + 원문 맥락 |

---

## 2. 핵심 기능

### 2.1 AI 대화
- **서버**: 봇 멘션(`@마사몽`) 또는 역할 멘션 시 응답
- **DM**: 멘션 없이 1:1 대화 지원 (5시간당 30회 제한)
- **채널별 페르소나**: `prompts.json`에서 채널별 말투/규칙 설정
- **커스텀 이모지**: 서버 이모지 자동 감지 및 대화에 활용
- **대화 히스토리**: 최근 대화 컨텍스트를 LLM 프롬프트에 주입

### 2.2 메모리 / RAG (Retrieval-Augmented Generation)
- 슬라이딩 윈도우 방식으로 대화를 구조화 메모리로 변환
- 채널 공동 기억(`channel` scope)과 유저 개인 기억(`user` scope) 분리
- 하이브리드 검색: 임베딩(코사인 유사도) + BM25(비활성) + RRF 재순위화
- 쿼리 확장 및 Re-ranker로 검색 정밀도 향상
- Kakao 대화방 벡터 검색 지원 (room_key 단위)

### 2.3 외부 정보 도구 (ToolsCog)

| 도구 | API | 설명 |
|------|-----|------|
| 날씨 | KMA (기상청) | 현재/주간/중기 예보, 강수 알림 |
| 지진 | KMA | 국내 지진 정보 자동 알림 |
| 금융 | Finnhub, yfinance, KRX, EximBank | 주식 시세, 환율, 기업 뉴스 |
| 웹 검색 | Linkup (주력), DuckDuckGo (대체) | 실시간 웹/뉴스 탐색 RAG |
| 장소 | Kakao Local API | 맛집/카페/명소 검색 |
| 이미지 생성 | CometAPI Gemini Image | `!이미지` 명령어, AI 대화 중 자동 생성 |

### 2.4 커뮤니티 기능

| 기능 | 명령어 | 설명 |
|------|--------|------|
| 운세 | `!운세`, `!별자리` | 일간/월간/연간 운세, 별자리 랭킹, 구독 |
| 요약 | `!요약` | 채널 대화 요약, 증분 업데이트 |
| 랭킹 | `!랭킹` | 서버 활동 랭킹 + 통계 차트 |
| 투표 | `!투표` | `!투표 "주제" "항목1" "항목2" ...` |
| 설정 | `/config`, `/persona` | 슬래시 커맨드로 AI 채널/페르소나 설정 |

---

## 3. 프로젝트 구조

> **2026-04 리팩토링**: `ai_handler.py`를 3,821줄에서 2,402줄로 분할.  
> LLM 클라이언트, RAG 관리, 의도 분석을 독립 모듈로 추출.

```text
masamong/
├── main.py                    # 봇 진입점, Cog 로드, DB 마이그레이션
├── config.py                  # 전체 설정 로드
├── logger_config.py           # KST 로깅, Discord 로그 핸들러
├── prompts.json               # 채널 페르소나, 시스템 프롬프트
├── emb_config.json            # 임베딩/RAG 설정
│
├── cogs/                      # Discord Cog 확장 모듈
│   ├── ai_handler.py          # AI 메시지 파이프라인 (2,402줄)
│   ├── tools_cog.py           # 외부 도구 통합 (웹검색/날씨/금융/이미지)
│   ├── weather_cog.py         # 날씨 명령어 + 강수/인사 알림
│   ├── fortune_cog.py         # 운세/별자리/구독 기능
│   ├── activity_cog.py        # 활동 추적 + 랭킹
│   ├── fun_cog.py             # 유틸리티 명령어 + 자동 요약
│   ├── poll_cog.py            # 투표 생성/집계
│   ├── settings_cog.py        # 슬래시 커맨드 (/config, /persona)
│   ├── commands.py            # 관리자 명령어 (!업데이트, !이미지)
│   ├── events.py              # 길드/멤버 이벤트 핸들러
│   ├── maintenance_cog.py     # RAG 아카이빙 백그라운드 태스크
│   ├── proactive_assistant.py # 사전 예방적 대화 참여
│   └── help_cog.py            # 도움말 명령어
│
├── utils/                     # 유틸리티 모듈
│   ├── llm_client.py          # 🆕 LLM 레인 라우팅 클라이언트 (552줄)
│   ├── rag_manager.py         # 🆕 RAG/임베딩/메모리 관리 (369줄)
│   ├── intent_analyzer.py     # 🆕 의도 분석/도구 탐지 (855줄)
│   ├── db.py                  # DB 작업 (Rate limit, 아카이빙, Linkup 예산)
│   ├── embeddings.py          # 임베딩 모델, 벡터 저장소 관리
│   ├── hybrid_search.py       # 하이브리드 검색 엔진 (임베딩+BM25+RRF)
│   ├── memory_units.py        # 구조화 메모리 유닛 생성
│   ├── news_search.py         # DuckDuckGo 웹 검색 RAG 파이프라인
│   ├── linkup_search.py       # Linkup API 웹 검색 파이프라인
│   ├── weather.py             # KMA 기상청 API 클라이언트
│   ├── chunker.py             # 시맨틱 청킹
│   ├── query_rewriter.py      # 검색 쿼리 확장
│   ├── reranker.py            # 검색 결과 재순위화
│   ├── data_formatters.py     # 응답 포맷팅 헬퍼
│   ├── coords.py              # 좌표 변환 (위경도 → 기상청 격자)
│   ├── kma_codes.py           # 기상청 코드 매핑
│   ├── ranking_chart.py       # 랭킹 차트(matplotlib) 생성
│   ├── http.py                # HTTP 세션 관리
│   ├── initial_data.py        # 초기 좌표 데이터 로드
│   ├── fortune.py             # 운세 데이터/계산
│   ├── text_cleaner.py        # 욕설 필터
│   └── api_handlers/          # 외부 API 클라이언트
│       ├── finnhub.py         # Finnhub 금융 API
│       ├── yfinance_handler.py # yfinance 주식 API
│       ├── krx.py             # KRX 한국거래소 API
│       ├── krx_v2.py          # KRX v2
│       ├── kakao.py           # Kakao 로컬 API
│       └── exchange_rate.py   # EximBank 환율 API
│
├── database/                  # DB 관련
│   ├── compat_db.py           # TiDB/SQLite 통합 비동기 어댑터
│   ├── bm25_index.py          # BM25 키워드 인덱스 (현재 비활성)
│   ├── init_db.py             # DB 초기화 스크립트
│   ├── schema.sql             # SQLite 스키마
│   └── schema_tidb.sql        # TiDB 스키마
│
├── tests/                     # pytest 테스트
│   ├── conftest.py
│   ├── test_ai_handler_*.py   # AI 핸들러 테스트
│   ├── test_hybrid_search.py
│   ├── test_reranker.py
│   ├── linkup_search_spec.py
│   └── ...
│
├── scripts/                   # 운영/검증 스크립트 (31개)
├── docs/                      # 추가 문서
├── examples/                  # 설정 파일 예제
├── tmp/server_config/         # 서버 전용 설정 파일
│
├── requirements.txt           # 공통 의존성
├── requirements-cpu.txt       # CPU 서버 추가 의존성
├── requirements-gpu.txt       # GPU 개발 추가 의존성
└── .env.example               # 환경변수 예제
```

---

## 4. 설치

### 4.1 사전 요구사항
- Python 3.9+
- Discord Bot Token ([Discord Developer Portal](https://discord.com/developers/applications))
- CometAPI API Key (또는 Gemini API Key)
- Privileged Intents 활성화 (Message Content, Server Members)

### 4.2 가상환경 생성 및 의존성 설치

```bash
git clone <repo-url>
cd masamong

python3 -m venv venv
source venv/bin/activate

# 공통 의존성
pip install -r requirements.txt

# CPU 서버 추가 의존성 (torch CPU + sentence-transformers)
pip install -r requirements-cpu.txt

# GPU 개발 환경 (선택)
# pip install -r requirements-gpu.txt
```

### 4.3 환경변수 설정

```bash
cp .env.example .env
# .env 파일을 편집하여 실제 API 키 입력
```

### 4.4 데이터베이스 초기화

```bash
python database/init_db.py
```

---

## 5. 설정 가이드

### 5.1 설정 로드 우선순위

```
1. 환경변수 (.env)          ← 최우선
2. config.json              ← 보조
3. 코드 기본값 (config.py)   ← 최후 순위
```

### 5.2 최소 필수 `.env`

```env
# Discord 봇 토큰 (필수)
DISCORD_BOT_TOKEN=your_token_here

# CometAPI (기본 LLM 제공자)
COMETAPI_KEY=your_cometapi_key
COMETAPI_BASE_URL=https://api.cometapi.com/v1
USE_COMETAPI=true
```

### 5.3 듀얼 레인 LLM 설정

```env
# ── Routing Lane (의도 분석, 경량 모델) ──
LLM_ROUTING_PRIMARY_PROVIDER=openai_compat
LLM_ROUTING_PRIMARY_MODEL=gemini-3.1-flash-lite-preview
LLM_ROUTING_PRIMARY_BASE_URL=https://api.cometapi.com
LLM_ROUTING_PRIMARY_API_KEY=${COMETAPI_KEY}
LLM_ROUTING_FALLBACK_PROVIDER=none

# ── Main Lane (답변 생성, 고성능 모델) ──
LLM_MAIN_PRIMARY_PROVIDER=openai_compat
LLM_MAIN_PRIMARY_MODEL=DeepSeek-V3.2-Exp-nothinking
LLM_MAIN_PRIMARY_BASE_URL=https://api.cometapi.com/v1
LLM_MAIN_PRIMARY_API_KEY=${COMETAPI_KEY}
LLM_MAIN_FALLBACK_PROVIDER=none

# 직접 Gemini Fallback (선택, 기본 비활성)
GEMINI_API_KEY=your_gemini_key
ALLOW_DIRECT_GEMINI_FALLBACK=false
```

### 5.4 TiDB 중앙화 운영

```env
MASAMONG_DB_BACKEND=tidb
MASAMONG_DB_STRICT_REMOTE_ONLY=true
MASAMONG_DB_HOST=gateway01.ap-northeast-1.prod.aws.tidbcloud.com
MASAMONG_DB_PORT=4000
MASAMONG_DB_NAME=masamong
MASAMONG_DB_USER=your_db_user
MASAMONG_DB_PASSWORD=your_db_password
MASAMONG_DB_SSL_CA=/etc/ssl/certs/ca-certificates.crt
MASAMONG_DB_SSL_VERIFY_IDENTITY=true

DISCORD_EMBEDDING_BACKEND=tidb
KAKAO_STORE_BACKEND=tidb
DISCORD_EMBEDDING_TIDB_TABLE=discord_chat_embeddings
KAKAO_TIDB_TABLE=kakao_chunks
```

`MASAMONG_DB_STRICT_REMOTE_ONLY=true` 시:
- DB 백엔드가 `tidb`가 아니면 시작 실패
- Discord/Kakao 저장소 강제 `tidb` 고정
- 로컬 `*.db` 파일 무시

### 5.5 TiDB 연결 안정화 권장값

```env
MASAMONG_DB_CONNECT_TIMEOUT=10
MASAMONG_DB_READ_TIMEOUT=30
MASAMONG_DB_WRITE_TIMEOUT=30
MASAMONG_DB_CONN_MAX_LIFETIME_SECONDS=600

RAG_ARCHIVE_RUN_ON_STARTUP=false
RAG_ARCHIVE_STARTUP_DELAY_SECONDS=120
```

### 5.6 웹 검색 설정

```env
# Linkup (권장)
LINKUP_API_KEY=your_linkup_api_key
LINKUP_BASE_URL=https://api.linkup.so/v1
WEB_SEARCH_PROVIDER=linkup
LINKUP_ENABLED=true
LINKUP_OUTPUT_TYPE=searchResults
LINKUP_MONTHLY_BUDGET_ENFORCED=true
LINKUP_MONTHLY_BUDGET_EUR=4.5

# DuckDuckGo (대체)
DDGS_ENABLED=true
```

### 5.7 외부 API 키

```env
KMA_API_KEY=your_kma_api_key           # 기상청
FINNHUB_API_KEY=your_finnhub_api_key   # 금융
KAKAO_API_KEY=your_kakao_api_key       # 장소 검색
KRX_API_KEY=your_krx_api_key           # 한국거래소
EXIM_API_KEY_KR=your_exim_api_key      # 환율
```

### 5.8 이미지 생성

```env
COMETAPI_IMAGE_ENABLED=true
COMETAPI_IMAGE_API_KEY=${COMETAPI_KEY}
COMETAPI_IMAGE_BASE_URL=https://api.cometapi.com
IMAGE_MODEL=gemini-3.1-flash-image
IMAGE_ASPECT_RATIO=1:1
IMAGE_USER_LIMIT=10                    # 유저당 6시간 최대 10장
IMAGE_GLOBAL_DAILY_LIMIT=50            # 전역 일일 50장
```

### 5.9 서버 전용 설정 파일

```env
EMB_CONFIG_PATH=/mnt/block-storage/masamong/tmp/server_config/emb_config.server.json
PROMPT_CONFIG_PATH=/mnt/block-storage/masamong/tmp/server_config/prompts.server.json
```

### 5.10 API Rate Limit / 안전장치

```env
# LLM 호출 제한
USER_COOLDOWN_SECONDS=3                        # 유저 쿨다운
USER_DAILY_LLM_LIMIT=200                       # 유저 일일 한도
GLOBAL_DAILY_LLM_LIMIT=5000                    # 전역 일일 한도
COMETAPI_RPM_LIMIT=40                          # 분당 한도
COMETAPI_RPD_LIMIT=3000                        # 일일 한도
AI_REQUEST_TIMEOUT=120                         # 요청 타임아웃(초)

# 의도 분석 LLM 호출 제어
INTENT_LLM_ENABLED=true
INTENT_LLM_ALWAYS_RUN=true
INTENT_LLM_RAG_STRONG_BYPASS=true
AUTO_WEB_SEARCH_COOLDOWN_SECONDS=90

# RAG / 메모리
AI_MEMORY_ENABLED=true
RAG_SIMILARITY_THRESHOLD=0.6
RAG_STRONG_SIMILARITY_THRESHOLD=0.72
RERANK_ENABLED=false            # Re-ranker (무거움, 선택)
USER_MEMORY_ENABLED=false       # 유저별 메모리 (기본 비활성)

# 알림
ENABLE_RAIN_NOTIFICATION=false
ENABLE_GREETING_NOTIFICATION=false
ENABLE_EARTHQUAKE_ALERT=true
```

---

## 6. 실행

### 6.1 로컬 실행

```bash
PYTHONPATH=. python main.py
```

### 6.2 screen으로 서버 백그라운드 실행

```bash
cd /mnt/block-storage/masamong
source venv/bin/activate
screen -S masamong
PYTHONPATH=. python main.py

# 분리: Ctrl+A, D
# 재접속: screen -r masamong
```

### 6.3 정상 실행 로그 확인 사항

```
✅ 메인 DB 백엔드: tidb
✅ Discord 메모리 저장소: tidb
✅ Kakao 저장소: tidb
✅ 데이터베이스 연결 완료: backend=tidb target=...
✅ Cog 로드 성공: ai_handler
✅ LLM 레인 구성: routing=..., main=...
✅ 봇 준비 완료
```

### 6.4 무시 가능한 경고

- `PyNaCl is not installed` → 음성 기능 미사용 시 무시
- `davey is not installed` → 음성 기능 미사용 시 무시
- `Both GOOGLE_API_KEY and GEMINI_API_KEY are set` → CometAPI 사용 시 무시
- `urllib3 NotOpenSSLWarning` → macOS 환경에서 무시

---

## 7. 명령어 레퍼런스

### 7.1 AI 대화
| 명령어 | 설명 |
|--------|------|
| `@마사몽 <메시지>` | 봇 멘션으로 대화 (서버) |
| DM 직접 메시지 | 1:1 대화 (DM, 멘션 불필요) |

### 7.2 일반 명령어 (`!` 프리픽스)
| 명령어 | 설명 |
|--------|------|
| `!도움`, `!help`, `!h` | 도움말 표시 |
| `!업데이트` | GitHub에서 코드 업데이트 (관리자) |
| `!이미지 <프롬프트>` | AI 이미지 생성 |

### 7.3 날씨
| 명령어 | 설명 |
|--------|------|
| `!날씨` | 기본 지역 날씨 |
| `!날씨 서울` | 특정 지역 날씨 |
| `!날씨 내일 부산` | 특정일 + 지역 |
| `!날씨 이번주 광주` | 주간 예보 |

### 7.4 운세 / 별자리
| 명령어 | 설명 |
|--------|------|
| `!운세` | 오늘의 운세 |
| `!운세 등록` | 생년월일 등록 |
| `!운세 상세` | 상세 운세 |
| `!운세 구독 HH:MM` | 매일 정시 운세 구독 |
| `!운세 구독취소` | 구독 취소 |
| `!운세 삭제` | 등록 정보 삭제 |
| `!이번달운세` | 월간 운세 |
| `!올해운세` | 연간 운세 |
| `!별자리` | 별자리 운세 |
| `!별자리 <별자리명>` | 특정 별자리 |
| `!별자리 순위` | 별자리 랭킹 |

### 7.5 커뮤니티
| 명령어 | 설명 |
|--------|------|
| `!요약` | 채널 대화 요약 |
| `!랭킹` | 서버 활동 랭킹 |
| `!투표 "주제" "항목1" "항목2"` | 투표 생성 |

### 7.6 슬래시 커맨드
| 명령어 | 설명 |
|--------|------|
| `/config set_ai` | AI 채널 활성화 |
| `/config channel` | 채널 설정 |
| `/persona view` | 페르소나 조회 |
| `/persona set` | 페르소나 설정 |

---

## 8. 운영 검증 스크립트

### 8.1 TiDB Smoke Test
```bash
PYTHONPATH=. python scripts/smoke_tidb_runtime.py --write-check
```

### 8.2 통합 헬스체크
```bash
PYTHONPATH=. python scripts/verify_runtime_health.py --backend tidb --write-check --strict
```
검증 항목: 메인 DB, 좌표 조회, 아카이빙, 임베딩, Discord/Kakao RAG, 프롬프트 주입, 쓰기/읽기/검색

### 8.3 구조화 메모리 재생성
```bash
PYTHONPATH=. python scripts/rebuild_structured_memories.py \
  --source-db 임시/remasamong.db \
  --target-db 임시/discord_embeddings.db \
  --clear
```

### 8.4 TiDB 데이터 마이그레이션
```bash
PYTHONPATH=. python scripts/migrate_latest_data_to_tidb.py --source-root 임시
# 옵션: --skip-main, --skip-discord, --skip-kakao, --truncate
```

---

## 9. 서버 업데이트 절차

```bash
cd /mnt/block-storage/masamong
source venv/bin/activate

git pull origin main
pip install -r requirements.txt
pip install -r requirements-cpu.txt

# smoke test
PYTHONPATH=. python scripts/smoke_tidb_runtime.py --write-check

# 봇 재시작
screen -r masamong
# Ctrl+C 로 기존 프로세스 종료 후
PYTHONPATH=. python main.py
```

---

## 10. 테스트

```bash
# 전체 테스트
PYTHONPATH=. pytest tests/ -v

# 특정 테스트
PYTHONPATH=. pytest tests/test_ai_handler_rag.py -v
PYTHONPATH=. pytest tests/test_hybrid_search.py -v
```

---

## 11. 트러블슈팅

### TiDB 연결 끊김 (`Lost connection to MySQL server during query`)
- 연결 수명 제한 확인: `MASAMONG_DB_CONN_MAX_LIFETIME_SECONDS=600`
- 최신 코드는 자동 재연결/재시도 로직 포함
- Free tier의 유휴 연결 종료(약 30분) 대응

### `PyMySQL가 필요합니다`
```bash
pip install -r requirements-cpu.txt
```

### 봇이 멈춘 것처럼 보임
- 임베딩 모델 로딩 중일 수 있음 (CPU 서버에서 수 초~수십 초)
- 첫 질의/검색 시 SentenceTransformer 초기 로드

### `GOOGLE_API_KEY`가 없음
- 필수 아님. Google Custom Search fallback만 비활성화됨
- TiDB, Gemini, Discord 메모리, Kakao 벡터 구동에 불필요

---

## 12. 의존성

### 공통 (`requirements.txt`)
```
discord.py>=2.7.1, aiosqlite, PyMySQL>=1.1.0, python-dotenv,
google-generativeai, google-genai, openai>=2.28.0,
requests, aiohttp, yfinance>=1.2.0,
korean-lunar-calendar, psutil, pytz, ephem,
matplotlib>=3.9.0, seaborn>=0.13.2,
ddgs, trafilatura, newspaper4k, nltk, lxml_html_clean
```

### CPU 서버 추가 (`requirements-cpu.txt`)
```
numpy>=1.24,<2.0, torch (CPU), sentence-transformers,
scikit-learn, PyMySQL>=1.1.0
```

### GPU 개발 추가 (`requirements-gpu.txt`)
```
numpy>=1.24,<2.0, torch (CUDA), sentence-transformers,
scikit-learn, PyMySQL>=1.1.0
```

### 임베딩 모델
- **Embedding**: `dragonkue/multilingual-e5-small-ko-v2` (한국어 최적화)
- **Query Rewriter**: `upskyy/e5-small-korean`
- **Re-ranker**: `BAAI/bge-reranker-v2-m3`

---

## 13. 문서

| 문서 | 내용 |
|------|------|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 시스템 아키텍처 상세 |
| [docs/QUICKSTART.md](docs/QUICKSTART.md) | 빠른 시작 가이드 |
| [docs/CHANGELOG.md](docs/CHANGELOG.md) | 변경 이력 |
| [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) | 기여 가이드 |

---

## 14. 라이선스

Private repository / 내부 운영 프로젝트.

---

## 15. 기술 스택 요약

| 계층 | 기술 |
|------|------|
| 봇 프레임워크 | discord.py >=2.7.1 |
| LLM 제공자 | CometAPI (OpenAI-compatible), Google Gemini |
| LLM 아키텍처 | Dual Lane (Routing + Main) with Primary/Fallback |
| 데이터베이스 | TiDB (운영), SQLite (개발) |
| 벡터 검색 | SentenceTransformers + TiDB VECTOR / 로컬 코사인 유사도 |
| 웹 검색 | Linkup API, DuckDuckGo |
| 금융 데이터 | Finnhub, yfinance, KRX API, 한국수출입은행 |
| 날씨 | 기상청(KMA) API |
| 이미지 생성 | CometAPI Gemini Image |
| 모니터링 | KST 로깅, Discord 채널 로그 전송 |

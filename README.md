# 마사몽 (Masamong)

Discord 서버용 AI 보조 봇입니다.  
멘션 기반 대화, 날씨/재난 정보, 웹 탐색 RAG, 이미지 생성, 운세, 커뮤니티 유틸리티를 제공합니다.

- 기본 언어: 한국어
- 런타임: Python 3.9+
- DB: SQLite (`database/remasamong.db`)
- 아키텍처: `discord.py` + Cog 모듈 + 로컬 RAG + 외부 API 툴

## 문서 언어
- 한국어 (이 문서)
- English: [docs/README.en.md](docs/README.en.md)
- 日本語: [docs/README.ja.md](docs/README.ja.md)

---

## 1. 핵심 기능

### AI 대화
- 서버: `@멘션`이 있는 메시지에만 응답
- DM: 멘션 없이 대화 가능 (사용량 제한 적용)
- 채널별 페르소나/규칙 적용 (`prompts.json`의 `channels`)
- 도구 자동 선택(LLM + 키워드 fallback)

### 외부 정보 탐색
- `search_news_rag` 기반 범용 탐색
- 뉴스뿐 아니라 웹/블로그/문서/커뮤니티 결과를 수집/요약
- 페이지 크롤링 실패 시 snippet/title 기반 fallback 요약
- 동일 질의 단기 캐시 및 Fast LLM 호출 예산 제한

### 날씨/재난
- 현재/단기/중기 예보
- `!날씨 이번주` 종합 요약
- 강수 알림, 아침/저녁 인사, 지진 알림 루프

### 금융/생활 도구
- 주식(yfinance 우선, KRX/Finnhub 경로 보유)
- 환율(수출입은행)
- 장소/웹/이미지 검색(카카오)
- 이미지 생성(CometAPI Gemini-compatible 이미지 엔드포인트)

### 운세/커뮤니티
- `!운세` 그룹(등록/구독/상세/삭제)
- 별자리 운세/순위
- 대화 요약(`!요약`) 증분 요약 + 컨텍스트 압축
- 활동 랭킹, 투표

---

## 2. 실제 동작 정책 (코드 기준)

- AI 준비 상태(`AIHandler.is_ready`)는 `GEMINI_API_KEY` 설정을 전제로 합니다.
  - CometAPI를 사용하더라도 Gemini 키가 없으면 AI 경로가 준비 상태가 되지 않습니다.
- 금융 질문은 운영 정책상 AI 라우팅에서 `search_news_rag`로 우선 처리되도록 구성되어 있습니다.
  - 기존 금융 툴 이름이 들어와도 내부에서 웹 탐색 경로로 리다이렉트될 수 있습니다.
- BM25는 현재 설정상 비활성 (`config.BM25_ENABLED = False`)입니다.

---

## 3. 프로젝트 구조

```text
masamong/
├─ main.py                     # 엔트리포인트, DB 연결, Cog 로딩, 메시지 라우팅
├─ config.py                   # 환경변수/config.json/기본값 로더
├─ prompts.json                # 채널별 페르소나/규칙 및 프롬프트
├─ emb_config.json             # RAG/임베딩 상세 설정
├─ cogs/
│  ├─ ai_handler.py            # 에이전트 메인 파이프라인
│  ├─ tools_cog.py             # 외부 API 툴
│  ├─ weather_cog.py           # 날씨 명령/알림 루프
│  ├─ fortune_cog.py           # 운세/별자리/구독
│  ├─ fun_cog.py               # !요약
│  ├─ activity_cog.py          # !랭킹
│  ├─ poll_cog.py              # !투표
│  ├─ settings_cog.py          # 슬래시 설정 커맨드
│  ├─ maintenance_cog.py       # 아카이빙/BM25 유지보수
│  ├─ events.py                # 이벤트 리스너
│  └─ help_cog.py              # 커스텀 도움말
├─ utils/
│  ├─ news_search.py           # 범용 웹 탐색 RAG 파이프라인
│  ├─ weather.py               # 기상청 API 래퍼
│  ├─ db.py                    # 레이트리밋/로그/보조 DB 유틸
│  ├─ hybrid_search.py         # 로컬 RAG 검색
│  ├─ embeddings.py            # 임베딩 저장/조회
│  └─ api_handlers/            # 카카오/금융/환율 등 API 핸들러
├─ database/
│  ├─ schema.sql               # DB 스키마
│  └─ init_db.py               # 수동 초기화 스크립트
├─ requirements.txt            # 공통 의존성
├─ requirements-cpu.txt        # 서버 CPU용 RAG 의존성
└─ requirements-gpu.txt        # 로컬 GPU용 RAG 의존성
```

---

## 4. 설치

### 4.1 사전 요구사항
- Python 3.9+
- pip / virtualenv
- Discord Bot Token
- Gemini API Key (필수)

### 4.2 가상환경 + 의존성

```bash
git clone <your-repo-url>
cd masamong

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

RAG 임베딩까지 쓸 경우:

```bash
# 서버 CPU
pip install -r requirements-cpu.txt

# 로컬 GPU
pip install -r requirements-gpu.txt
```

---

## 5. 설정

## 5.1 설정 로드 우선순위
`config.py` 기준:
1. 환경변수 (`.env` 포함)
2. `config.json`
3. 코드 기본값

### 5.2 최소 필수 `.env`

```env
DISCORD_BOT_TOKEN=...
GEMINI_API_KEY=...
```

### 5.3 권장 `.env` (자주 쓰는 값)

```env
# AI
USE_COMETAPI=true
COMETAPI_KEY=...
COMETAPI_BASE_URL=https://api.cometapi.com/v1
COMETAPI_MODEL=DeepSeek-V3.2-Exp-nothinking
FAST_MODEL_NAME=gemini-3.1-flash-lite-preview

# 외부 검색
DDGS_ENABLED=true
KAKAO_API_KEY=...
GOOGLE_API_KEY=...
GOOGLE_CX=...
SERPAPI_KEY=...

# 날씨/재난
KMA_API_KEY=...
# KMA 키가 없으면 GO_DATA_API_KEY_KR를 fallback으로 사용 가능
GO_DATA_API_KEY_KR=...

# 금융/환율
FINNHUB_API_KEY=...
EXIM_API_KEY_KR=...

# 이미지
COMETAPI_IMAGE_ENABLED=true
IMAGE_MODEL=gemini-3.1-flash-image
IMAGE_ASPECT_RATIO=1:1
IMAGE_USER_LIMIT=10
IMAGE_GLOBAL_DAILY_LIMIT=50
```

### 5.4 RAG/메모리 설정 파일
- `emb_config.json`
  - 임베딩 모델/디바이스/threshold
  - 윈도우 크기/stride
  - query rewrite/reranker 옵션
- 현재 기본 정책상 BM25는 비활성 상태

### 5.5 프롬프트/채널 페르소나
- `prompts.json`
  - `prompts`: 시스템 프롬프트 템플릿
  - `channels`: 채널별 `allowed/persona/rules`

---

## 6. 실행

```bash
python3 main.py
```

- 첫 실행 시 `main.py`에서 스키마 적용 및 필요한 테이블/컬럼 점검 수행
- `database/init_db.py`는 수동 초기화가 필요할 때만 실행

---

## 7. 명령어

모든 텍스트 명령어 prefix는 기본 `!`입니다.

### 일반
- `!도움`, `!help`, `!도움말`, `!h`
- `!이미지 <프롬프트>` (`image`, `img`, `그림`, `생성`)
- `!업데이트` (`update`, `패치노트`)
- `!delete_log` (`로그삭제`) - 관리자, 서버 전용

### 날씨
- `!날씨`
- `!날씨 서울`
- `!날씨 내일 부산`
- `!날씨 이번주 광주`

### 운세
- `!운세`
- `!운세 등록` (DM 권장)
- `!운세 상세`
- `!운세 구독 HH:MM`
- `!운세 구독취소`
- `!운세 삭제`
- `!구독 HH:MM` (`구독시간`, `알림시간`)
- `!이번달운세` (`이번달`)
- `!올해운세` (`올해`, `신년운세`)

### 별자리
- `!별자리`
- `!별자리 <별자리명>`
- `!별자리 순위`

### 커뮤니티
- `!요약` (`summarize`, `summary`, `3줄요약`, `sum`)
- `!랭킹` (`수다왕`, `ranking`)
- `!투표 "주제" "항목1" ...` (`poll`)

### 슬래시(설정)
- `/config set_ai`
- `/config channel`
- `/persona view`
- `/persona set`

참고: 슬래시 커맨드는 운영 환경에서 command tree sync가 선행되어야 표시됩니다.

---

## 8. 운영 안전장치

### 8.1 레이트리밋/쿨다운
- 사용자 쿨다운
- 사용자 일일 LLM 제한
- 글로벌 일일 LLM 제한
- DM 개별/전역 제한
- 이미지 생성 개별/전역 제한
- CometAPI RPM/RPD 제한 + 프롬프트/토큰 상한
- 웹탐색 Fast LLM 호출 예산 제한

### 8.2 검색/요약 비용 최적화
- `search_news_rag` 질의 결과 단기 캐시
- 후보 URL 수/요약 페이지 수 상한
- 문맥 길이 상한 (`WEB_RAG_CONTEXT_MAX_CHARS`)
- 요약 컨텍스트 압축(최근 대화 + 과거 샘플)

---

## 9. 데이터베이스

기본 DB 파일: `database/remasamong.db`

주요 테이블:
- `conversation_history`, `conversation_windows`, `conversation_history_archive`
- `guild_settings`, `user_activity`, `user_profiles`
- `api_call_log`, `system_counters`, `analytics_log`, `dm_usage_logs`, `locations`

스키마 원본: `database/schema.sql`

---

## 10. 배포/업데이트 (기존 서버 DB 보호)

요청하신 조건처럼 **기존 누적 DB 영향 최소화**를 기준으로 운영하세요.

### 권장 절차

```bash
# 1) 코드만 업데이트
git pull origin <branch>

# 2) 의존성 동기화 (필요 시)
source .venv/bin/activate
pip install -r requirements.txt
# RAG 사용 환경이면 CPU/GPU 파일 추가 설치

# 3) DB 백업 (강력 권장)
cp database/remasamong.db database/remasamong.db.bak.$(date +%Y%m%d_%H%M%S)

# 4) 봇 재시작
python3 main.py
```

### 주의
- 코드가 DB를 강제로 초기화하거나 삭제하는 동작은 기본 경로에 없습니다.
- `database/init_db.py`는 필요 시에만 수동 실행하세요.
- 운영 DB를 건드리지 않는 검증이 필요하면 `/tmp` 임시 DB로 별도 테스트 하네스를 돌리세요.

---

## 11. 테스트

### 기본 문법 점검

```bash
python3 -m compileall -q cogs utils main.py
```

### 제공 스크립트
- `scripts/test_all_features.py`: 외부 API 중심 종합 점검
- `scripts/test_context_aware.py`: 도구 선택/쿼리 정제 점검
- `tests/verify_fortune.py`: 운세 계산 로직 점검

실환경 API 검증 시에는 실제 키/네트워크 상태에 따라 결과가 달라질 수 있습니다.

---

## 12. 트러블슈팅

### 봇이 응답하지 않음
- `DISCORD_BOT_TOKEN`, `GEMINI_API_KEY` 설정 확인
- Discord Developer Portal에서 Message Content Intent 확인
- 채널 허용 설정(`prompts.json` 또는 DB `guild_settings.ai_allowed_channels`) 확인

### 날씨 API 실패
- `KMA_API_KEY` 유효성 확인
- 필요 시 `GO_DATA_API_KEY_KR` fallback 사용
- 기상청 API 자체 장애/지연 가능성 확인

### KRX 주식 조회 실패
- 실제 런타임 키는 `GO_DATA_API_KEY_KR` 경로를 확인
- 키 권한/호출 제한/엔드포인트 상태 점검

### 이미지 생성 실패
- `COMETAPI_KEY`/`COMETAPI_IMAGE_ENABLED` 확인
- 사용자/전역 이미지 제한 도달 여부 확인
- 안전 필터(NSFW) 차단 여부 확인

---

## 13. 추가 문서
- 아키텍처: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 퀵스타트: [docs/QUICKSTART.md](docs/QUICKSTART.md)
- 변경 이력: [docs/CHANGELOG.md](docs/CHANGELOG.md)
- 기여 가이드: [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md)

---

## 14. 라이선스

Private repository / 내부 운영 프로젝트.

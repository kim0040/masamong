# 마사몽 (Masamong)

마사몽은 Discord 서버에서 동작하는 한국어 중심 AI 보조 봇이다.  
멘션 기반 대화, 서버/유저 메모리, Kakao 대화 RAG, 날씨/재난/금융 도구, 운세, 커뮤니티 기능을 하나의 런타임으로 묶어 운영한다.

현재 운영 기준으로는 **메인 데이터와 메모리 저장소를 TiDB로 중앙화**했고, 로컬 SQLite/파일 저장소는 개발용 백업 또는 마이그레이션 소스로만 사용한다.

- 기본 언어: 한국어
- 런타임: Python 3.9+
- 프레임워크: `discord.py`
- 메인 DB: TiDB 또는 SQLite
- Discord 메모리 저장소: TiDB 또는 SQLite
- Kakao 저장소: TiDB 또는 로컬 `vectors.npy + metadata.json`

## 문서 언어
- 한국어 (이 문서)
- English: [docs/README.en.md](docs/README.en.md)
- 日本語: [docs/README.ja.md](docs/README.ja.md)

---

## 1. 현재 아키텍처 요약

### 메인 저장소
- `conversation_history`
- `conversation_windows`
- `conversation_history_archive`
- `guild_settings`
- `user_activity`
- `user_profiles`
- `locations`
- `api_call_log`
- `analytics_log`
- `dm_usage_logs`

### 메모리 저장소
- `discord_chat_embeddings`
  - 기존 Discord 임베딩 저장소
- `discord_memory_entries`
  - 구조화 메모리 저장소
  - `channel` / `user` scope 분리
  - 요약문과 원문 맥락을 함께 저장

### Kakao 저장소
- `kakao_chunks`
  - `room_key` 단위 저장
  - `VECTOR(384)` 기반 임베딩
  - `room1`, `room2` 등 서버/그룹별 데이터 분리

### 핵심 운영 방향
- 메인 운영 데이터는 TiDB `masamong` DB 기준
- Discord 구조화 메모리도 TiDB 기준
- Kakao 검색도 TiDB 기준
- BM25는 운영 정책상 비활성

---

## 2. 핵심 기능

### AI 대화
- 서버: 멘션 또는 허용된 AI 채널 정책에 따라 응답
- DM: 멘션 없이 대화 가능
- 채널별 페르소나/규칙 적용
- 대화 히스토리 저장
- 구조화 메모리 기반 회상

### 메모리 / RAG
- 최근 Discord 대화를 구조화 메모리로 축적
- 유저별 기억과 채널 공동 기억 분리
- Kakao 대화방 벡터 검색 지원
- 원문 로그 + 정제 메모리 병행 사용

### 외부 정보 도구
- 뉴스/웹 검색
- 장소 검색
- 날씨/재난
- 금융/환율
- 이미지 생성

### 커뮤니티 기능
- 운세 등록/조회/구독
- 대화 요약
- 활동 랭킹
- 투표
- 설정 슬래시 커맨드

---

## 3. 저장 구조 설명

### 3.1 원본 로그와 구조화 메모리의 차이

마사몽은 같은 대화를 두 층으로 다룬다.

- 원본 로그
  - 감사 추적과 문맥 복원용
  - `conversation_history`
- 구조화 메모리
  - 회상 검색 최적화용
  - `discord_memory_entries`

구조화 메모리는 다음 원칙을 따른다.

- `channel` 공동 기억 생성
- `user` 개인 기억 생성
- 요약과 키워드 저장
- 원문 맥락 별도 저장
- 임베딩은 요약만이 아니라 `요약 + 원문 맥락`을 함께 반영

이 구조 덕분에:
- 답변은 더 정돈되고
- 유저별 기억은 더 명확해지고
- 필요한 경우 원문 문맥도 복원 가능하다

### 3.2 Kakao 데이터

기존 Kakao 저장소는 로컬 디렉터리 기반이었다.

- `vectors.npy`
- `metadata.json`

현재 운영 구조에서는 `kakao_chunks`로 올려서 중앙화했다.  
질의 임베딩은 봇 프로세스가 만들고, 벡터 검색은 TiDB가 담당한다.

---

## 4. 프로젝트 구조

```text
masamong/
├─ main.py
├─ config.py
├─ prompts.json
├─ emb_config.json
├─ cogs/
│  ├─ ai_handler.py
│  ├─ weather_cog.py
│  ├─ tools_cog.py
│  ├─ fortune_cog.py
│  ├─ activity_cog.py
│  ├─ fun_cog.py
│  ├─ poll_cog.py
│  ├─ settings_cog.py
│  ├─ maintenance_cog.py
│  ├─ proactive_assistant.py
│  ├─ events.py
│  ├─ commands.py
│  └─ help_cog.py
├─ utils/
│  ├─ db.py
│  ├─ coords.py
│  ├─ embeddings.py
│  ├─ hybrid_search.py
│  ├─ memory_units.py
│  ├─ news_search.py
│  ├─ weather.py
│  └─ api_handlers/
├─ database/
│  ├─ compat_db.py
│  ├─ schema.sql
│  └─ schema_tidb.sql
├─ scripts/
│  ├─ migrate_latest_data_to_tidb.py
│  ├─ rebuild_structured_memories.py
│  ├─ smoke_tidb_runtime.py
│  ├─ verify_tidb_parity.py
│  └─ append_kakao_csv_to_room_store.py
├─ requirements.txt
├─ requirements-cpu.txt
└─ requirements-gpu.txt
```

---

## 5. 설치

### 5.1 사전 요구사항
- Python 3.9+
- `venv` 또는 `virtualenv`
- Discord Bot Token
- Gemini API Key

### 5.2 가상환경 생성

```bash
git clone <repo-url>
cd masamong

python3 -m venv venv
source venv/bin/activate
```

### 5.3 공통 의존성

```bash
python -m pip install -r requirements.txt
```

### 5.4 CPU 서버

```bash
python -m pip install -r requirements-cpu.txt
```

### 5.5 GPU 개발 환경

```bash
python -m pip install -r requirements-gpu.txt
```

---

## 6. 설정

## 6.1 설정 로드 우선순위
`config.py` 기준:
1. 환경변수 (`.env`)
2. `config.json`
3. 코드 기본값

### 6.2 최소 필수 `.env`

```env
DISCORD_BOT_TOKEN=...
COMETAPI_KEY=...
USE_COMETAPI=true
```

옵션:
- `ALLOW_DIRECT_GEMINI_FALLBACK=true`를 켠 경우에만 `GEMINI_API_KEY`가 실사용된다.
- 기본값은 `false`이며, 이때는 CometAPI만 사용한다.

### 6.3 TiDB 중앙화 운영 예시

```env
MASAMONG_DB_BACKEND=tidb
MASAMONG_DB_STRICT_REMOTE_ONLY=true
MASAMONG_DB_HOST=gateway01.ap-northeast-1.prod.aws.tidbcloud.com
MASAMONG_DB_PORT=4000
MASAMONG_DB_NAME=masamong
MASAMONG_DB_USER=...
MASAMONG_DB_PASSWORD=...
MASAMONG_DB_SSL_CA=/etc/ssl/certs/ca-certificates.crt
MASAMONG_DB_SSL_VERIFY_IDENTITY=true

DISCORD_EMBEDDING_BACKEND=tidb
KAKAO_STORE_BACKEND=tidb
DISCORD_EMBEDDING_TIDB_TABLE=discord_chat_embeddings
KAKAO_TIDB_TABLE=kakao_chunks
```

`MASAMONG_DB_STRICT_REMOTE_ONLY=true`를 켜면:
- `MASAMONG_DB_BACKEND`가 `tidb`가 아니면 시작 시 즉시 실패한다.
- Discord/Kakao 저장소를 강제로 `tidb`로 고정한다.
- 서버에 `database/*.db` 파일이 남아 있어도 운영 경로에서는 사용하지 않는다.

### 6.4 검색/외부 API 관련 예시

```env
USE_COMETAPI=true
COMETAPI_KEY=...
COMETAPI_BASE_URL=https://api.cometapi.com/v1
ALLOW_DIRECT_GEMINI_FALLBACK=false

# 의도 분석 LLM 호출 제어 (과호출 방지)
INTENT_LLM_ENABLED=true
INTENT_LLM_RAG_STRONG_BYPASS=true
AUTO_WEB_SEARCH_COOLDOWN_SECONDS=90
AUTO_WEB_SEARCH_ALLOW_SHORT_FOLLOWUP=false

KMA_API_KEY=...
FINNHUB_API_KEY=...
KAKAO_API_KEY=...
ENABLE_EARTHQUAKE_ALERT=true
EARTHQUAKE_CHECK_INTERVAL_MINUTES=1

GOOGLE_CX=...
# GOOGLE_API_KEY=...
```

주의:
- `GOOGLE_API_KEY`는 **Gemini 키가 아니다**
- `GOOGLE_API_KEY` + `GOOGLE_CX`는 Google Custom Search fallback용
- 없어도 핵심 TiDB/RAG 구동은 가능하다
- `AUTO_WEB_SEARCH_COOLDOWN_SECONDS`는 도구 계획이 없을 때의 자동 웹검색 fallback에만 적용된다 (명시적 웹검색 요청에는 미적용).

### 6.7 TiDB 연결 안정화 권장값

```env
MASAMONG_DB_CONNECT_TIMEOUT=10
MASAMONG_DB_READ_TIMEOUT=30
MASAMONG_DB_WRITE_TIMEOUT=30
MASAMONG_DB_CONN_MAX_LIFETIME_SECONDS=600

RAG_ARCHIVE_RUN_ON_STARTUP=false
RAG_ARCHIVE_STARTUP_DELAY_SECONDS=120
```

의미:
- TiDB free tier의 유휴 연결 종료(약 30분) 대응을 위해 연결을 주기적으로 교체한다.
- 부팅 직후 아카이빙 1회 실행을 건너뛰어 초기화 충돌 가능성을 낮춘다.

### 6.5 구조화 메모리 관련 `.env`

```env
STRUCTURED_MEMORY_QUERY_LIMIT=800
STRUCTURED_MEMORY_FALLBACK_QUERY_LIMIT=2000
STRUCTURED_MEMORY_SIMILARITY_THRESHOLD=0.5
LOCAL_EMBEDDING_LOCAL_FILES_ONLY=false
```

### 6.6 서버 전용 설정 파일

운영 서버에서는 Git 추적 파일 대신 별도 설정 파일을 쓰는 것을 권장한다.

- `EMB_CONFIG_PATH`
- `PROMPT_CONFIG_PATH`

예시:

```env
EMB_CONFIG_PATH=/mnt/block-storage/masamong/tmp/server_config/emb_config.server.json
PROMPT_CONFIG_PATH=/mnt/block-storage/masamong/tmp/server_config/prompts.server.json
```

---

## 7. `emb_config.server.json` 역할

이 파일은 검색/RAG 세부 정책을 담당한다.

- 임베딩 모델 이름
- CPU/GPU 선택
- normalize 여부
- similarity threshold
- query limit
- conversation window size / stride
- Kakao `room_key` 매핑

운영 기준 권장:
- `embedding_model_name`: `dragonkue/multilingual-e5-small-ko-v2`
- `embedding_device`: `cpu`
- `bm25_enabled`: `false`

---

## 8. `prompts.server.json` 역할

이 파일은 채널별 페르소나와 응답 규칙을 담당한다.

- 시스템 프롬프트 템플릿
- 채널 허용 여부
- 채널별 말투/규칙
- 툴 사용 가이드

운영에서 채널 설정을 Git 추적 파일에 두지 않으려면 이 파일로 분리하는 것이 맞다.

---

## 9. 실행

### 9.1 로컬/일반 실행

```bash
PYTHONPATH=. python main.py
```

### 9.2 `screen`으로 서버 실행

```bash
cd /mnt/block-storage/masamong
source venv/bin/activate
screen -S masamong
PYTHONPATH=. python main.py
```

분리:
- `Ctrl + A`, `D`

재접속:

```bash
screen -r masamong
```

### 9.3 실행 로그에서 정상으로 봐야 하는 항목

- 메인 DB 백엔드가 `tidb`로 표시됨
- Discord 메모리 저장소가 `tidb`
- Kakao 저장소가 `tidb`
- `데이터베이스 연결 완료: backend=tidb ...`
- Cog 로드 성공
- `봇 준비 완료`

### 9.4 음성 경고

다음 경고는 음성 기능을 안 쓰면 치명적이지 않다.

- `PyNaCl is not installed`
- `davey is not installed`

즉, 음성 기능이 필요 없으면 무시 가능하다.

---

## 10. 운영 검증

### 10.1 TiDB smoke test

```bash
PYTHONPATH=. python scripts/smoke_tidb_runtime.py --write-check
```

정상 예시:
- `conversation_history ...`
- `discord_rows ...`
- `discord_memory_rows ...`
- `kakao_rows ...`
- `guild_setting_write ...`
- `user_profile_write ...`
- `discord_write ...`
- `discord_memory_write ...`

### 10.2 통합 운영 헬스체크

```bash
PYTHONPATH=. python scripts/verify_runtime_health.py --backend tidb --write-check --strict
```

이 스크립트는 다음을 한 번에 검증한다.

- 메인 DB 핵심 테이블 row count
- 좌표 조회
- 아카이빙 루프 단일 실행
- 임베딩 사전검사(모델 로드/벡터 생성)
- Discord 임베딩/구조화 메모리 적재 상태
- Discord RAG 검색 회수 여부
- Kakao 벡터 검색 회수 여부
- 프롬프트 주입(RAG/도구/질문 섹션) 확인
- 테스트용 합성 데이터 쓰기/읽기/검색/정리

### 10.3 구조화 메모리 재생성

로컬/운영 로그에서 구조화 메모리를 다시 만들 때:

```bash
PYTHONPATH=. python scripts/rebuild_structured_memories.py \
  --source-db 임시/remasamong.db \
  --target-db 임시/discord_embeddings.db \
  --clear
```

### 10.4 TiDB 적재

```bash
PYTHONPATH=. python scripts/migrate_latest_data_to_tidb.py --source-root 임시
```

옵션:
- `--skip-main`
- `--skip-discord`
- `--skip-kakao`
- `--truncate`

### 10.5 Kakao CSV 추가 적재

```bash
PYTHONPATH=. python scripts/append_kakao_csv_to_room_store.py ...
```

이 스크립트는 기존 Kakao room 데이터에 새 CSV를 append하는 용도다.

---

## 11. 데이터베이스

### 11.1 SQLite 개발용 파일
- `database/remasamong.db`
- `database/discord_embeddings.db`
- `data/.../vectors.npy`

### 11.2 TiDB 운영 테이블
- `guild_settings`
- `user_activity`
- `conversation_history`
- `conversation_windows`
- `conversation_history_archive`
- `system_counters`
- `api_call_log`
- `analytics_log`
- `user_preferences`
- `locations`
- `user_profiles`
- `dm_usage_logs`
- `discord_chat_embeddings`
- `discord_memory_entries`
- `kakao_chunks`

### 11.3 현재 운영 정책
- BM25 비활성
- 메인 DB와 메모리는 TiDB 중앙화
- Kakao 벡터도 TiDB 기준

---

## 12. 명령어

### 일반
- `!도움`
- `!help`
- `!도움말`
- `!h`
- `!업데이트`
- `!이미지 <프롬프트>`

### 날씨
- `!날씨`
- `!날씨 서울`
- `!날씨 내일 부산`
- `!날씨 이번주 광주`

### 운세
- `!운세`
- `!운세 등록`
- `!운세 상세`
- `!운세 구독 HH:MM`
- `!운세 구독취소`
- `!운세 삭제`
- `!이번달운세`
- `!올해운세`

### 별자리
- `!별자리`
- `!별자리 <별자리명>`
- `!별자리 순위`

### 커뮤니티
- `!요약`
- `!랭킹`
- `!투표 "주제" "항목1" ...`

### 슬래시
- `/config set_ai`
- `/config channel`
- `/persona view`
- `/persona set`

---

## 13. 서버 업데이트 절차

```bash
cd /mnt/block-storage/masamong
source venv/bin/activate
git pull origin main
python -m pip install -r requirements.txt
python -m pip install -r requirements-cpu.txt
PYTHONPATH=. python scripts/smoke_tidb_runtime.py --write-check
```

통과 후:

```bash
screen -r masamong
# 기존 프로세스 종료 후
PYTHONPATH=. python main.py
```

---

## 14. 트러블슈팅

### `PyMySQL가 필요합니다`
- `requirements-cpu.txt` 또는 `requirements-gpu.txt` 재설치

```bash
python -m pip install -r requirements-cpu.txt
```

### `Discord 임베딩 TiDB 초기화 완료`는 나오는데 봇이 멈춘 것 같음
- 임베딩 모델 로딩 중일 수 있다
- CPU 서버에서는 첫 질의/첫 검색 시 수 초 이상 걸릴 수 있다

### `Lost connection to MySQL server during query`
- 오래 살아 있는 연결 재사용 문제일 수 있다
- 최신 코드에서는 TiDB 연결 자동 재연결/재시도 로직이 포함되어 있다
- 아래 값을 `.env`에 명시하면 안정성이 높다:

```env
MASAMONG_DB_CONN_MAX_LIFETIME_SECONDS=600
MASAMONG_DB_CONNECT_TIMEOUT=10
MASAMONG_DB_READ_TIMEOUT=30
MASAMONG_DB_WRITE_TIMEOUT=30
RAG_ARCHIVE_RUN_ON_STARTUP=false
RAG_ARCHIVE_STARTUP_DELAY_SECONDS=120
```

### `GOOGLE_API_KEY`가 없는데 문제인가
- 필수 아님
- Google Custom Search fallback만 비활성화됨
- TiDB, Gemini, Discord 메모리, Kakao 벡터 구동에는 직접 필수 아님

### `PyNaCl is not installed`
- 음성 기능 미사용이면 무시 가능

---

## 15. 테스트 / 검증 스크립트

- `scripts/smoke_tidb_runtime.py`
- `scripts/verify_runtime_health.py`
- `scripts/migrate_latest_data_to_tidb.py`
- `scripts/rebuild_structured_memories.py`
- `scripts/verify_tidb_parity.py`
- `scripts/append_kakao_csv_to_room_store.py`
- `tests/verify_fortune.py`

---

## 16. 추가 문서

- 아키텍처: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 퀵스타트: [docs/QUICKSTART.md](docs/QUICKSTART.md)
- 변경 이력: [docs/CHANGELOG.md](docs/CHANGELOG.md)
- 기여 가이드: [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md)

---

## 17. 라이선스

Private repository / 내부 운영 프로젝트.

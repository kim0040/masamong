# 🤖 마사몽 Discord 에이전트

마사몽은 Discord 서버에서 실시간 날씨·금융 데이터와 Google Gemini 기반 AI 대화를 동시에 제공하는 복합형 봇입니다. `cogs` 구조로 기능을 모듈화했고, 주요 업무 흐름은 2-Step 에이전트(의도 분석 → 도구 실행 → 답변 생성)로 구성되어 있습니다.

## 프로젝트 개요
- Discord.py 2.x 기반의 비동기 Discord 봇
- Google Gemini(Lite/Flash) + 맞춤 도구 묶음을 활용한 2단계 에이전트 구조
- 기상청(OpenAPI)·Finnhub·Kakao 등 핵심 외부 API 연동
- SentenceTransformer 기반 로컬 임베딩 + SQLite RAG 저장소
- SQLite + aiosqlite로 사용자 활동·대화 내역·API 호출 제한을 관리
- 구조화된 로깅(`logger_config.py`)과 Discord 임베드 로그 지원

## 주요 기능
- `AIHandler`: Gemini 모델 호출, 대화 기록(RAG), 도구 실행 파이프라인
- `ToolsCog`: 날씨/환율/주식/Kakao 검색 등 외부 API에 대한 통합 인터페이스
- `WeatherCog`: 기상청(KMA) 날씨 조회와 비/눈 알림, 인사 알림 루프
- `ActivityCog`: 사용자 메시지 누적 → `!랭킹` 명령으로 활동 순위 안내
- `ProactiveAssistant`: 키워드 기반 능동 제안 (옵션, 기본 비활성화)
- 기타 Cog: 유틸리티 명령(`!delete_log`), 투표, 재미 요소 등

## Discord 사용 가이드
- **AI 호출 (`@마사몽`)**: 멘션하면 의도 분석 → 도구 실행 → 응답 생성 파이프라인이 동작합니다. 대화 맥락을 일부 기억하므로 "아까 말한 그거?" 같은 추억팔이도 가능합니다.
- **바로 쓸 수 있는 질문**
-  - `📈` 주식: "애플 주가 얼마야?", "삼성전자 오늘 주가 알려줘"
  - `💱` 환율: "달러 환율 알려줘", "엔화 환율은?"
  - `📰` 뉴스: "엔비디아 관련 뉴스 3개만 찾아줘" *(미국 주식 대상)*
  - `☀️` 날씨: "서울 오늘 날씨 어때?", "부산 내일 날씨 알려줘" (지역 없으면 기본값은 `광양`)
  - `📍` 장소: "광양 맛집 추천해줘", "여수 가볼만한 곳 알려줘"
  - `🎮` 게임: "평점 높은 RPG 게임 추천해줘", "최신 게임 뭐 나왔어?"
  - `⏰` 시간: "지금 몇 시야?"
  - `🧠` 기억: "아까 내가 뭐랬더라?"처럼 묻으면 최근 대화를 기반으로 답변 가능 (로컬 RAG)
- **간단 명령어 (`!`)**
  - `!랭킹`, `!수다왕`: 서버 활동량 Top 5를 확인하고 AI 멘트를 받을 수 있습니다.
  - `!투표 "질문" "항목1" "항목2"`: 즉석 투표를 생성합니다.
  - `!운세`: 마사몽이 오늘의 운세를 츤데레 감성으로 알려줍니다.
  - `!요약`: 최근 대화를 3줄 요약으로 정리합니다.
- **캐릭터성**: 기본 페르소나는 츤데레. 가끔 투덜거려도 도움을 주려는 마음은 진심입니다.

## 코드 구조
```
masamong/
├── main.py                 # 봇 엔트리포인트 및 Cog 로딩
├── config.py               # 환경 변수/설정 로딩 유틸리티
├── logger_config.py        # 콘솔·파일·Discord 로그 핸들러
├── cogs/                   # 기능 모듈 (AI, 날씨, 활동 통계 등)
├── utils/                  # API 래퍼, DB/HTTP 유틸리티, 초기 데이터
├── database/               # SQLite 초기화 스크립트 및 스키마
├── tests/                  # pytest 기반 단위 테스트
└── requirements.txt        # 의존성 목록
```

## 준비 사항
- Python 3.9 이상 (3.11 권장)
- Git, SQLite3
- Discord 봇 토큰과 필수 API 키 (Gemini, 기상청 등)
- macOS/Linux 환경에서의 `screen` 사용 가능 여부

## 설치 절차
1. **소스 코드 가져오기 및 가상환경 구성**
   ```bash
   git clone https://github.com/kim0040/masamong.git
   cd masamong
   python3 -m venv venv
   source venv/bin/activate  # Windows는 venv\Scripts\activate
   ```
2. **의존성 설치**
   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```
3. **환경 변수/설정 파일 준비**
   ```bash
   cp .env.example .env               # 없으면 새로 작성
   cp config.json.example config.json
   cp emb_config.json.example emb_config.json
   ```
4. **필요한 키와 값 채우기**
   - `.env` 또는 실제 환경 변수에 필수 키를 입력합니다.
   - `config.json`은 `.env` 값이 없을 때 참조되는 보조 설정입니다.

## 핵심 환경 변수
`config.py`는 다음 순서로 값을 찾습니다: (1) OS 환경 변수 → (2) `.env` → (3) `config.json`. 주요 키:

- `DISCORD_BOT_TOKEN`: Discord 봇 토큰 (필수)
- `GEMINI_API_KEY`: Google Gemini API 키 (필수)
- `GOOGLE_API_KEY`, `GOOGLE_CX`: Google Custom Search (웹 검색 1순위)
- `SERPAPI_KEY`: SerpAPI 키 (웹 검색 2순위)
- `KAKAO_API_KEY`: Kakao 로컬/웹 검색 (폴백)
- `KMA_API_KEY`: 기상청 API 키 (국내 날씨)
- `KMA_BASE_URL`: (선택) 동네예보 API 기본 URL. 기본값은 기상청 API 허브입니다.
- `KMA_ALERT_BASE_URL`: (선택) 기상특보 API 기본 URL. 기본값은 기상청 API 허브, 공공데이터 포털 키를 쓰면 `https://apis.data.go.kr/1360000/WthrWrnInfoService` 같은 기존 URL로 변경하세요.
- `EXIM_API_KEY_KR`: 한국수출입은행 환율 API 키
- `FINNHUB_API_KEY`: 미국/해외 주식 조회용 키
- `ENABLE_PROACTIVE_KEYWORD_HINTS`: `true`로 설정하면 키워드 기반 자동 제안을 다시 활성화할 수 있습니다.

임베딩/RAG 관련 경로 및 모델 설정은 `emb_config.json`에서 관리합니다. 기본 예시 파일(`emb_config.json.example`)을 참고해 서버/채널별 저장소 경로나 SentenceTransformer 모델명을 조정하세요.

필요한 값을 모두 채운 뒤 저장하세요. 민감 정보는 깃에 커밋하지 않도록 주의합니다.

## 임베딩 설정 (`emb_config.json`)
- SentenceTransformer 모델과 Discord/Kakao 임베딩 DB 경로를 분리 관리합니다.
- 저장소 경로를 변경하면 재시작 시 자동으로 SQLite 파일과 테이블이 생성됩니다.

```json
{
  "embedding_model_name": "BM-K/KoSimCSE-roberta",
  "discord_db_path": "database/discord_embeddings.db",
  "kakao_db_path": "database/kakao_embeddings.db",
  "embedding_device": "cpu",
  "normalize_embeddings": true,
  "query_limit": 200,
  "kakao_servers": [
    { "server_id": "kakao_room_01", "db_path": "database/kakao_room_01_embeddings.db", "label": "가족방" },
    { "server_id": "kakao_room_02", "db_path": "database/kakao_room_02_embeddings.db", "label": "친구방" }
  ]
}
```

> `embedding_device`는 GPU 사용 시 `"cuda"`, CPU만 사용할 경우 `"cpu"`로 유지하세요. `normalize_embeddings`를 `true`로 두면 저장된 벡터가 코사인 유사도를 바로 계산할 수 있도록 정규화됩니다.
> `kakao_servers` 배열은 카카오 채팅방별 RAG DB 경로를 미리 선언하는 용도로, `server_id` 값은 Kakao 세션 식별자 또는 디스코드 길드/채널 ID 등 매칭하고 싶은 식별자로 자유롭게 지정할 수 있습니다. RAG 검색 시 먼저 채널 ID → 길드 ID 순으로 매칭을 시도하며, 어느 항목과도 맞지 않으면 `kakao_db_path`가 폴백으로 사용됩니다. DB 파일에는 최소한 `message`(텍스트)와 `embedding`(float32 벡터 BLOB) 컬럼이 존재해야 하며, `timestamp`/`speaker` 컬럼이 있을 경우 자동으로 함께 활용됩니다.

## 데이터베이스 초기화
최초 실행 전 SQLite 스키마를 생성합니다.
```bash
python database/init_db.py
```
- `database/remasamong.db`가 생성되며, 필수 테이블과 카운터가 준비됩니다.
- `utils/initial_data.py`가 위치 좌표를 시드합니다.
- 임베딩 전용 DB(`discord_embeddings.db` 등)는 메시지 저장 시 자동 생성됩니다.

## 실행 (screen 기반 운영)
운영 환경에서 `screen` 세션을 사용해 봇을 띄울 때는 다음 절차를 따릅니다.

1. 새 세션 생성 및 진입
   ```bash
   screen -S masamong-bot
   ```
2. 가상환경 활성화 후 봇 실행
   ```bash
   cd /path/to/masamong
   source venv/bin/activate
   python main.py
   ```
3. 세션 분리 (백그라운드 실행)
   - `Ctrl + A` → `D`
   - 이후에도 프로세스는 계속 동작합니다.
4. 실행 중인 세션 목록 확인: `screen -ls`
5. 다시 접속: `screen -r masamong-bot`
6. 종료 방법
   - 세션 안에서 `Ctrl + C`로 봇을 중지한 뒤 `exit`
   - 불필요해진 세션은 `screen -S masamong-bot -X quit`으로 제거

> 자동 재실행이 필요하면 `screen` 대신 `systemd`나 `pm2` 등을 사용할 수 있지만, 현재 저장소는 `screen` 기반 운영 시나리오를 기본으로 합니다.

## 로그 & 모니터링
- 애플리케이션 로그: `discord_logs.txt`
- 에러/경고 로그(JSON): `error_logs.txt`
- Discord 채널 로그: 서버 내 `#logs` 채널 (권한 필요)
- 실시간 확인이 필요하면 `screen -r`로 세션에 재접속하여 콘솔 출력을 확인하세요.

## 테스트 실행
단위 테스트는 pytest로 구성되어 있습니다. 가상환경에서 실행하세요.
```bash
pytest
pytest tests/test_weather_handler.py::TestWeatherCog  # 특정 테스트 예시
```
일부 테스트는 외부 API 호출을 모킹하므로 네트워크 없이도 동작합니다.

## 운영 팁 & 문제 해결
- **Gemini 키 누락**: `config.GEMINI_API_KEY`가 비어 있으면 AI 응답이 동작하지 않습니다. 봇 시작 시 경고 로그를 확인하세요.
- **기상청 API 오류**: 키가 없거나 일일 호출 제한을 넘으면 `config.MSG_WEATHER_*` 메시지가 출력됩니다. 호출 제한은 `config.KMA_API_DAILY_CALL_LIMIT`로 조정 가능합니다.
- **웹 검색 실패**: Google CSE → SerpAPI → Kakao 순으로 폴백합니다. 모든 키가 만료되면 빈 결과가 반환될 수 있습니다.
- **데이터베이스 문제**: `database/init_db.py`를 재실행하거나 `database/remasamong.db` 파일 권한을 확인하세요.
- **로그 정리**: `!delete_log` 명령은 관리자만 사용할 수 있으며 `discord_logs.txt`를 비웁니다.

## 라이선스
MIT License. 세부 내용은 `LICENSE` 파일을 참조하세요.

---
문의나 개선 제안은 `masamong_improvement_request.md`에 기록하거나 이슈를 등록해 주세요. 즐거운 운영 되세요! 🚀

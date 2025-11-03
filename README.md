# 🤖 마사몽 Discord 에이전트

마사몽은 Discord 서버에서 실시간 날씨·금융 데이터와 Google Gemini 기반 AI 대화를 동시에 제공하는 복합형 봇입니다. `cogs` 구조로 기능을 모듈화했고, 주요 업무 흐름은 2-Step 에이전트(의도 분석 → 도구 실행 → 답변 생성)로 구성되어 있습니다.

## 프로젝트 개요
- Discord.py 2.x 기반의 비동기 Discord 봇
- Google Gemini(Lite/Flash) + 맞춤 도구 묶음을 활용한 2단계 에이전트 구조
- Reciprocal Rank Fusion(RRF) 하이브리드 검색 + 선택적 Cross-Encoder 리랭커
- Lite(Thinking) JSON 응답 → Flash 단일 승급으로 비용을 관리하는 라우팅 규칙
- 슬라이딩 윈도우(6/stride 3) 기반 대화 맥락과 의미/키워드 가중 결합 RAG 파이프라인
- 기상청(OpenAPI)·Finnhub·Kakao 등 핵심 외부 API 연동
- SentenceTransformer 기반 로컬 임베딩 + SQLite RAG 저장소
- SQLite + aiosqlite로 사용자 활동·대화 내역·API 호출 제한을 관리
- 구조화된 로깅(`logger_config.py`)과 Discord 임베드 로그 지원

## 주요 기능
- `AIHandler`: Gemini 모델 호출, Thinking/Flash 라우팅, 도구 실행 파이프라인
- `Thinking 라우팅`: Lite 모델이 JSON으로 초안/도구 계획/셀프 스코어를 작성하고 필요할 때만 Flash를 1회 호출합니다.
- `대화 윈도우 저장`: 최근 6개의 메시지를 묶어 ±3 메시지 이웃까지 한 번에 RAG 컨텍스트로 제공합니다.
- `ToolsCog`: 날씨/환율/주식/Kakao 검색 등 외부 API에 대한 통합 인터페이스
- `WeatherCog`: 기상청(KMA) 날씨 조회와 비/눈 알림, 인사 알림 루프
- `ActivityCog`: 사용자 메시지 누적 → `!랭킹` 명령으로 활동 순위 안내
- `ProactiveAssistant`: 키워드 기반 능동 제안 (옵션, 기본 비활성화)
- 기타 Cog: 유틸리티 명령(`!delete_log`), 투표, 재미 요소 등

## Discord 사용 가이드
- **AI 호출 (`@마사몽`)**: 멘션이 확인되면 의도 분석 → 도구 실행 → 응답 생성 파이프라인이 동작합니다. 멘션이 없다면 어떤 경우에도 답변하거나 API를 호출하지 않습니다.
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
├── config.py               # 환경 변수·임베딩·프롬프트 설정 로더
├── logger_config.py        # 콘솔·파일·Discord 로그 핸들러
├── cogs/                   # 기능 모듈 (AI, 날씨, 활동 통계 등)
├── utils/                  # API 래퍼, DB/HTTP 유틸리티, 초기 데이터
├── database/               # SQLite 초기화 스크립트 및 하이브리드 색인
├── tests/                  # pytest 기반 단위 테스트
└── requirements.txt        # 의존성 목록
```

## 프롬프트 & 멘션 관리
- 모든 시스템/페르소나 프롬프트는 `prompts.json`(또는 `PROMPT_CONFIG_PATH` 환경변수로 지정한 JSON·YAML)에서 로드하며 저장소에는 포함하지 않습니다.
- 로드된 프롬프트에는 자동으로 **“사용자가 봇을 멘션한 메시지에만 응답한다”**는 가드 문구가 추가됩니다.
- `CHANNEL_AI_CONFIG` 역시 이 파일을 기반으로 재구성되므로, 응답을 허용할 채널은 `allowed: true`로 명시해야 합니다.
- `<@봇ID>`, `<@!봇ID>`, `@봇닉네임` 등 다양한 멘션 표기를 모두 인식하며, 멘션이 없으면 Gemini 호출을 포함한 어떤 처리도 실행되지 않습니다.

```jsonc
{
  "prompts": {
    "lite_system_prompt": "…",          // Lite(의도 분석) 시스템 프롬프트
    "agent_system_prompt": "…",         // 메인 답변용 프롬프트
    "web_fallback_prompt": "…"          // 웹 검색만 가능할 때 사용
  },
  "channels": {
    "912210558122598450": {
      "allowed": true,
      "persona": "…",                   // 채널 전용 페르소나
      "rules": "…"                      // 채널 전용 규칙
    }
  }
}
```

> `prompts.json`은 `.gitignore`에 포함되어 있으므로 운영 서버에만 배포하세요.

## 준비 사항
- Python 3.9 이상 (3.11 권장)
- Git, SQLite3
- Discord 봇 토큰과 필수 API 키 (Gemini, 기상청 등)
- macOS/Linux 환경에서의 `screen` 사용 가능 여부
- CPU-only 환경을 기본 가정하며, Flash 승급 비율을 1회 이하로 유지하도록 설계되어 있습니다.

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
   # (선택) RAG/임베딩 기능이 필요하면 추가 패키지를 설치합니다.
   pip install numpy==1.26.4 sentence-transformers==2.7.0
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
   - `prompts.json`을 작성해 채널별 페르소나·규칙·시스템 프롬프트를 정의합니다.

## 가상환경 초기화 & 재설치 절차
1. (선택) 실행 중인 봇이 있다면 `screen`/`tmux` 세션에서 `Ctrl+C`로 종료합니다.
2. 가상환경이 활성화돼 있다면 `deactivate`로 빠져나옵니다.
3. 기존 가상환경 폴더 삭제:
   ```bash
   rm -rf venv
   ```
4. 새 가상환경 생성 및 활성화:
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # Windows는 venv\Scripts\activate
   ```
5. 최신 pip로 올린 뒤 필요한 패키지를 다시 설치합니다.
   ```bash
   pip install --upgrade pip
   pip install --no-cache-dir -r requirements.txt
   # (필요 시) pip install --no-cache-dir numpy==1.26.4 sentence-transformers==2.7.0
   ```
   `--no-cache-dir` 옵션을 붙이면 남은 디스크 공간이 매우 적을 때 pip 캐시를 쓰지 않아 설치 용량을 조금 아낄 수 있습니다.
6. 설치가 끝나면 빠르게 검증합니다.
   ```bash
   python -m pip check  # 의존성 충돌 여부 확인
   python main.py       # Discord 토큰이 맞다면 봇이 실행됩니다.
   ```

## 핵심 환경 변수
`config.py`는 다음 순서로 값을 찾습니다: (1) OS 환경 변수 → (2) `.env` → (3) `config.json`. 자주 쓰는 키는 아래 표를 참고하세요.

| 키 | 설명 | 기본값 |
| --- | --- | --- |
| `DISCORD_BOT_TOKEN` | Discord 봇 토큰 | 필수 |
| `GEMINI_API_KEY` | Google Gemini API 키 | 필수 |
| `GOOGLE_API_KEY`, `GOOGLE_CX` | Google Custom Search | 선택 |
| `SERPAPI_KEY` | SerpAPI 키 (웹 검색 2순위) | 선택 |
| `KAKAO_API_KEY` | Kakao 로컬/웹 검색 | 선택 |
| `KMA_API_KEY` | 기상청 API 키 | 선택 |
| `FINNHUB_API_KEY` | 미국/해외 주식 조회 | 선택 |
| `ENABLE_PROACTIVE_KEYWORD_HINTS` | 키워드 기반 능동 응답 | `false` |
| `SEARCH_QUERY_EXPANSION_ENABLED` | Thinking 단계 쿼리 확장 | `true` |
| `SEARCH_CHUNKING_ENABLED` | RAG 청킹 사용 여부 | `false` |
| `RERANK_ENABLED` | Cross-Encoder 리랭킹 | `false` |
| `AI_MEMORY_ENABLED` | 로컬 임베딩/RAG | `true` |
| `CONVERSATION_WINDOW_SIZE` | 대화 윈도우 크기 | `6` |
| `CONVERSATION_WINDOW_STRIDE` | 대화 윈도우 stride | `3` |
| `CONVERSATION_NEIGHBOR_RADIUS` | 인접 대화 half-window | `3` |
| `DISABLE_VERBOSE_THINKING_OUTPUT` | Thinking JSON 전체 로그 | `true` |

그 외 `KMA_BASE_URL`, `KMA_ALERT_BASE_URL`, `EXIM_API_KEY_KR` 등 세부 API 엔드포인트 역시 환경 변수로 조정할 수 있습니다.

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

## 저사양·CPU 전용 서버 운영 팁
- `pip install -r requirements.txt`만 설치하면 봇 실행에 필요한 최소 패키지만 들어갑니다. RAG/임베딩이 필요 없으면 `numpy`/`sentence-transformers`는 설치하지 않아도 됩니다.
- 메모리가 부족하거나 SentenceTransformer 모델을 설치하고 싶지 않다면 `.env` 또는 `config.json`에 `"AI_MEMORY_ENABLED": false`를 추가해 대화 임베딩 기능을 완전히 끌 수 있습니다.
- 임베딩을 쓰더라도 `emb_config.json`의 `"embedding_device"` 값을 `"cpu"`로 두면 GPU 없이도 동작합니다. 단, 최초 모델 다운로드는 수백 MB 이상이므로 디스크 용량을 반드시 확인하세요.
- 이미 설치된 임베딩 모델/데이터베이스가 부담된다면 `database/discord_embeddings.db` 등 임베딩 관련 DB 파일을 삭제해 공간을 확보하고 필요 시 다시 생성하세요.
- `screen` 대신 `tmux`/`systemd`를 쓰면 메모리 사용량이 더 줄어드는 것은 아니지만, 비정상 종료 시 자동 재시작을 설정하는 데 도움이 됩니다.

## 데이터베이스 초기화
최초 실행 전 SQLite 스키마와 BM25 인덱스를 준비합니다.
```bash
python database/init_db.py
python database/init_bm25.py   # (선택) 기존 대화 로그를 FTS5 인덱스로 재구축
```
- `database/remasamong.db`가 생성되며, 필수 테이블과 카운터가 준비됩니다.
- `utils/initial_data.py`가 위치 좌표를 시드합니다.
- 임베딩 전용 DB(`discord_embeddings.db` 등)는 메시지 저장 시 자동 생성됩니다.
- `conversation_windows` 테이블에는 메시지 6개 단위의 슬라이딩 묶음이 저장되어 RAG에서 즉시 재사용됩니다.
- BM25 하이브리드 검색을 활용하려면 `init_bm25.py`를 주기적으로 실행해 FTS 인덱스를 최신 상태로 유지하세요.

## RAG 검색 고도화 운용 가이드
1. **Thinking 단계 쿼리 확장**: 사용자의 현재 질문과 직전 사용자/봇 발화를 조합해 시드 쿼리를 만들고, 필요하면 Gemini로 패러프레이즈를 1회 생성합니다.
2. **의미·키워드 하이브리드**: SentenceTransformer 임베딩 상위 `RAG_EMBEDDING_TOP_N`과 BM25 상위 `RAG_BM25_TOP_N`을 불러와 0.55/0.45 가중치로 `combined_score`를 계산합니다.
3. **대화 윈도우 재구성**: 각 후보는 저장된 슬라이딩 윈도우(`conversation_windows`)나 ±`CONVERSATION_NEIGHBOR_RADIUS` 이웃 메시지를 묶어 `[speaker][time] text` 형식으로 정리됩니다.
4. **선택적 리랭킹**: `RERANK_ENABLED=true`라면 Cross-Encoder가 상위 후보만 재평가하고, torch/transformers가 설치되어 있지 않으면 자동으로 생략됩니다.
5. **Thinking JSON**: Lite 모델은 위 RAG 블록을 바탕으로 `{analysis, tool_plan, draft, self_score, needs_flash}`를 채운 JSON을 반드시 반환합니다. self_score가 낮거나 위험 판단 시 Flash를 한 번만 호출합니다.
6. **운영 팁**: `emb_config.json`의 `hybrid_top_k`, `bm25_top_n`, `rrf_constant`, `query_rewrite_variants` 등을 조정해 성향을 튜닝하고, 인덱스/임베딩 DB는 정기적으로 백업하세요.

## Thinking/Flash 라우팅 규칙
- **Thinking(Flash Lite)** 단계는 다음 JSON 형식을 준수합니다.
  ```json
  {
    "analysis": "요약/의도 분석",
    "tool_plan": [{"tool_name": "search_for_place", "parameters": {...}}],
    "draft": "반말 초안",
    "self_score": {"accuracy": 0.92, "completeness": 0.85, "risk": 0.10, "overall": 0.90},
    "needs_flash": false
  }
  ```
- `self_score.overall < 0.75`, `risk > 0.6`, 금융/법률/의료/정책 등 고위험 질의, 예상 토큰 > 1200, 근거 충돌 등이 감지되면 Flash가 1회 호출됩니다.
- 도구 실행이 실패하면 자동으로 `web_search`가 폴백으로 추가되며, 강한 RAG 컨텍스트(`combined_score >= RAG_STRONG_SIMILARITY_THRESHOLD`)에서는 중복 웹 검색을 제거합니다.
- Thinking 초안이 충분히 신뢰할 수 있으면 Flash 없이 Lite 응답만으로 최종 답변을 반환합니다.

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

## 디버그 설정
- `RAG_DEBUG_ENABLED`: RAG 후보/유사도 정보를 서버 로그에만 남깁니다. 채팅에는 절대 노출되지 않습니다.
- `AI_DEBUG_ENABLED`: 멘션 게이트 판단, Gemini 프롬프트·응답, 도구 실행/폴백 과정을 세부 로그로 기록합니다.
- `AI_DEBUG_LOG_MAX_LEN`: 디버그 로그에 표시할 프롬프트/응답 미리보기 길이 (기본 400자)를 조정합니다.

## 테스트 실행
단위 테스트는 pytest로 구성되어 있습니다. 가상환경에서 실행하세요.
```bash
pytest                  # huggingface_hub 미설치 시 test_download.py에서 오류가 발생할 수 있습니다.
pytest tests            # tests/ 디렉터리만 실행하면 huggingface 의존 테스트가 제외됩니다.
pytest tests/test_ai_handler_mentions.py      # 멘션 게이트 확인
pytest tests/test_hybrid_search.py            # 하이브리드 검색 검증
```
일부 테스트는 외부 API를 모킹하므로 네트워크 없이도 동작합니다.

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

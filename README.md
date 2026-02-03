# 마사몽 Discord 봇

한국어 | [English](docs/README.en.md) | [日本語](docs/README.ja.md)

마사몽은 Discord 서버에서 **멘션 기반 AI 대화**와 **생활형 도구(날씨/주식/환율/장소/이미지 생성)**를 제공하는 봇입니다.
이 문서는 **코드 기준으로 실제 동작하는 기능과 구조**를 기술 문서 스타일로 정리했습니다.

**Quick Start**
1. Python 3.9+ 설치
2. 의존성 설치
```
python -m pip install -r requirements.txt
```
3. 환경 변수 설정
- `.env` 또는 `config.json` 사용
- 설정 로드 순서: **환경 변수 → `config.json` → 기본값**
4. 실행
```
python main.py
```

**동작 개요**
- 서버 채널: `@마사몽` 멘션이 있는 메시지만 AI가 처리합니다.
- DM: 멘션 없이 대화 가능하지만 **5시간당 30회 + 전역 하루 100회 제한**이 적용됩니다.
- 응답 생성: **CometAPI(선택)** → **Gemini(폴백)** 순서로 시도합니다.
- `GEMINI_API_KEY`가 없으면 **AI 파이프라인이 시작되지 않습니다.**

**메시지 처리 흐름 (텍스트 다이어그램)**
```
[Discord Message]
  ├─ if startswith('!') → 명령어 처리 후 종료
  └─ else
      ├─ 멘션 검사 (Guild: 필수, DM: 생략)
      ├─ 키워드 기반 도구 감지
      ├─ 도구 실행 (날씨/주식/장소/이미지 생성)
      ├─ RAG 컨텍스트 검색 (선택)
      ├─ 프롬프트 구성
      └─ LLM 응답 생성 (CometAPI → Gemini 폴백)
```

**핵심 파이프라인 상세**

**1) 메시지 라우팅**
1. `main.py`의 `on_message`가 모든 메시지를 수신합니다.
2. `!` 프리픽스가 있으면 명령어 처리로 분기합니다.
3. 명령어가 아니면 `AIHandler.process_agent_message`로 전달합니다.
4. 길드 메시지는 멘션 검사 후 처리하며, DM은 멘션 검사 없이 처리합니다.

**2) 도구 감지 및 실행**
- Lite 모델 없이 **키워드 매칭**으로 도구를 선택합니다.
- 감지된 도구는 `ToolsCog`에서 실행됩니다.
- 도구 결과는 **프롬프트 최상단 정보 블록**으로 포함됩니다.
- 날씨 요청은 단일 도구로 즉시 처리됩니다.

**3) LLM 선택과 폴백**
- CometAPI가 활성화되어 있으면 먼저 사용합니다.
- CometAPI 실패 또는 비활성화 시 Gemini로 폴백합니다.
- Gemini API 키가 없으면 AI 파이프라인은 준비 상태가 아닙니다.

**4) RAG(기억) 파이프라인**
1. 대화는 `conversation_history`에 저장됩니다.
2. 일정 메시지가 모이면 **윈도우 단위 요약**을 생성합니다.
3. 요약 텍스트를 임베딩하여 `discord_embeddings.db`에 저장합니다.
4. 질문이 들어오면 임베딩/BM25 하이브리드 검색을 수행합니다.
5. `emb_config.json`에서 임베딩/BM25/쿼리 확장/리랭커를 제어합니다.

**5) 웹 검색 자동 판단**
- “최근/뉴스/방법/왜” 등 키워드가 포함되고 RAG 점수가 약할 때만 실행됩니다.
- Google CSE가 우선이며 실패 시 Kakao 검색으로 폴백합니다.
- 일일 검색 제한을 초과하면 자동 검색은 비활성화됩니다.

**6) 백그라운드 작업**
- 강수 알림: 단기 예보 강수 확률 기반 알림
- 아침/저녁 인사: 지정 시각에 날씨 요약 포함 메시지 전송
- 지진 알림: 국내 영향권 규모 4.0+ 지진 발생 시 알림

**기능별 의존성 요약**
- AI 대화: `GEMINI_API_KEY` 필수, CometAPI는 선택
- 이미지 생성: `COMETAPI_KEY` 필수 + `COMETAPI_IMAGE_ENABLED=true`
- 날씨: `KMA_API_KEY` 필요 (기상청)
- 환율: `EXIM_API_KEY_KR` 필요 (한국수출입은행)
- 장소/웹/이미지 검색: `KAKAO_API_KEY` 필요 (카카오)
- 웹 검색(자동): `GOOGLE_API_KEY` + `GOOGLE_CX` 우선, 실패 시 Kakao 폴백
- 주식(기본): `USE_YFINANCE=true` + CometAPI 티커 추출
- 주식(대안): `USE_YFINANCE=false` 시 KRX/Finnhub 사용
- 운세/별자리: CometAPI 기반 (Gemini 폴백 없음)
- RAG(임베딩): `numpy`, `sentence-transformers` 필요
- 점성술 상세: `ephem` 설치 시 반영

**아키텍처 구성 요소**
| 영역 | 주요 모듈 | 역할 |
| --- | --- | --- |
| 엔트리포인트 | `main.py` | 봇 초기화, Cog 로딩, 메시지 라우팅 |
| AI 파이프라인 | `cogs/ai_handler.py` | 도구 감지, RAG, LLM 호출, 응답 생성 |
| 도구 모음 | `cogs/tools_cog.py` | 날씨/주식/환율/장소/웹/이미지 도구 |
| 날씨/알림 | `cogs/weather_cog.py` | 날씨 조회, 강수/인사/지진 알림 |
| 운세/별자리 | `cogs/fortune_cog.py` | 운세 등록/구독/별자리 |
| 명령어 | `cogs/commands.py`, `cogs/fun_cog.py` | 일반 명령어/요약/이미지 |
| 랭킹 | `cogs/activity_cog.py` | 활동 기록 및 랭킹 |
| 투표 | `cogs/poll_cog.py` | 투표 생성 |
| 설정 | `cogs/settings_cog.py` | 슬래시 커맨드 설정 저장 |
| 유지보수 | `cogs/maintenance_cog.py` | 아카이빙, BM25 재구축 |
| 임베딩/RAG | `utils/embeddings.py`, `utils/hybrid_search.py` | 임베딩 저장, 검색 파이프라인 |

**데이터 저장소**
- SQLite 메인 DB: `database/remasamong.db`
- 임베딩 DB: `database/discord_embeddings.db` (옵션)
- 주요 테이블: `conversation_history`, `conversation_windows`, `user_activity`, `user_profiles`, `api_call_log`

**설정과 우선순위**
- 설정 로드: 환경 변수 → `config.json` → 기본값
- AI 허용 채널: `prompts.json`의 `channels.allowed` 또는 `DEFAULT_AI_CHANNELS`
- `/config channel`은 DB에 저장되며, **AI 응답 허용 로직에는 직접 반영되지 않습니다.**

**명령어 요약**
- `!도움` / `!도움말` / `!h`: 도움말
- `!날씨`: 날씨 조회
- `!요약`: 최근 대화 요약 (서버 전용)
- `!랭킹`: 활동 랭킹 (서버 전용)
- `!투표`: 투표 생성 (서버 전용)
- `!이미지`: 이미지 생성 (서버 전용)
- `!운세`, `!별자리`: 운세/별자리
- `!업데이트`: 업데이트 안내
- `!delete_log`: 로그 삭제 (관리자 전용)
- `!debug`: 디버그 (봇 오너 전용)

**운영 시 유의사항**
- Gemini API 키가 없으면 AI 기능이 비활성화됩니다.
- 주식 조회는 yfinance 기본이며, CometAPI 티커 추출에 실패하면 조회가 실패할 수 있습니다.
- 이미지 생성은 유저/전역 제한이 적용됩니다.
- DM은 사용량 제한이 강하게 적용됩니다.


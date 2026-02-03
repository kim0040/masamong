# 마사몽 Discord 봇

한국어 | [English](docs/README.en.md) | [日本語](docs/README.ja.md)

마사몽은 Discord 서버에서 **멘션 기반 AI 대화**와 **생활형 도구(날씨/주식/환율/장소/이미지 생성)**를 제공하는 봇입니다.
이 문서는 **코드 기준으로 실제 동작하는 기능만** 정리했습니다.

**핵심 기능**
- 서버 채널: `@마사몽` 멘션이 있을 때만 AI 응답
- DM: 멘션 없이 대화 가능 (DM 사용량 제한 적용)
- 키워드 기반 도구 자동 실행: 날씨, 주식, 장소, 이미지 생성
- 최신/뉴스/방법형 질문은 조건에 따라 자동 웹 검색
- 명령어: 날씨, 운세, 별자리, 요약, 랭킹, 투표, 이미지 생성, 도움말
- 주기 알림: 비/눈 예보, 아침/저녁 인사, 지진(국내 영향권 규모 4.0+)
- 대화 기록 저장 + 선택적 RAG(임베딩/BM25)

**무엇을 할 수 있나 (예시)**
```
@마사몽 오늘 서울 날씨 알려줘
@마사몽 삼성전자 주가 알려줘
@마사몽 홍대 맛집 추천해줘
@마사몽 최신 테슬라 뉴스 알려줘
@마사몽 사이버펑크 서울 야경 그려줘

!날씨 이번주 부산 날씨
!운세
!운세 등록
!운세 구독 07:30
!별자리 물병자리
!요약
!랭킹
!투표 "점심 메뉴" "피자" "라멘" "국밥"
!이미지 별이 가득한 밤하늘
```

**AI 대화 규칙**
- 서버 채널은 `@마사몽` 멘션이 필수입니다.
- AI 응답 허용 채널은 `prompts.json`의 `channels.allowed` 또는 환경 변수 `DEFAULT_AI_CHANNELS`로 제한됩니다.
- DM은 멘션 없이 대화 가능하지만 **5시간당 30회 + 전역 하루 100회 제한**이 적용됩니다.
- 응답 생성은 CometAPI(선택) → Gemini(폴백) 순서로 동작합니다.
- Gemini API 키가 없으면 **AI 기능 전체가 비활성화**됩니다.

**자동 도구 실행 기준 (요약)**
- 날씨: 날씨/기온/비/눈/우산 등 키워드 감지
- 주식: 주가/주식/시세 등 키워드 감지
- 장소: 맛집/카페/추천/근처 등 키워드 감지
- 이미지 생성: “그려줘/생성해줘/이미지 만들어” 등 키워드 감지
- 웹 검색: “최근/뉴스/현재/방법/왜” 등 + RAG 약함 + 일일 한도 내일 때 자동 수행

**명령어 레퍼런스**
- `!도움` / `!도움말` / `!h`: 전체 명령어 안내
- `!날씨 [지역/날짜]`: 오늘/내일/모레/이번주 날씨 (서버/DM)
- `!요약`: 최근 대화 요약 (서버 전용)
- `!랭킹`: 서버 활동 랭킹 (서버 전용)
- `!투표 "주제" "항목1" "항목2" ...`: 투표 생성 (서버 전용)
- `!이미지 <설명>`: 이미지 생성 (서버 전용)
- `!업데이트`: 업데이트 안내(정적 메시지)
- `!delete_log`: 로그 파일 삭제 (관리자 전용, 서버 전용)
- `!debug status`, `!debug reset_dm <user_id>`: 봇 오너 전용

**운세/별자리 명령어**
- `!운세`: 오늘 운세 (서버=요약, DM=상세)
- `!운세 상세`: DM에서 상세 운세
- `!운세 등록`: 생년월일/시간/성별/출생지 등록 (DM 전용)
- `!운세 구독 HH:MM`: 매일 운세 브리핑 구독 (DM 전용)
- `!운세 구독취소`: 구독 해제
- `!구독 HH:MM`: `!운세 구독`의 별칭 (DM 전용)
- `!이번달운세`, `!올해운세`: 월/년 운세 (DM 전용, 일일 3회 제한)
- `!별자리`: 내 별자리 운세 (등록 정보 있으면 자동, 없으면 안내)
- `!별자리 <이름>`: 특정 별자리 운세
- `!별자리 순위`: 오늘의 12별자리 랭킹

**알림 기능 (백그라운드 작업)**
- 비/눈 예보 알림: 강수 확률 임계값 이상일 때 채널 알림
- 아침/저녁 인사: 지정 시간에 날씨 요약 포함 메시지 전송
- 지진 알림: 국내 영향권 규모 4.0+ 지진 발생 시 안내

**기능별 API/의존성 요약**
- AI 대화: `GEMINI_API_KEY` 필수, CometAPI는 선택
- 이미지 생성: `COMETAPI_KEY` 필수 + `COMETAPI_IMAGE_ENABLED=true`
- 날씨: `KMA_API_KEY` 필요 (기상청)
- 환율: `EXIM_API_KEY_KR` 필요 (한국수출입은행)
- 장소/웹/이미지 검색: `KAKAO_API_KEY` 필요 (카카오)
- 웹 검색(자동): `GOOGLE_API_KEY` + `GOOGLE_CX` 우선, 실패 시 카카오 검색 폴백
- 주식(기본): `USE_YFINANCE=true` + CometAPI 티커 추출 필요
- 주식(대안): `USE_YFINANCE=false` 시 KRX(공공데이터포털) + Finnhub 사용
- 운세/별자리: CometAPI 기반 (Gemini 폴백 없음)
- RAG(임베딩): `numpy`, `sentence-transformers` 설치 필요
- 점성술: `ephem` 설치 시 상세 천체 배치 반영

**설치 및 실행**
1. Python 3.9+ 설치
2. 의존성 설치
```
python -m pip install -r requirements.txt
```
3. 환경 변수 설정
- `.env` 또는 `config.json`에 키를 설정합니다.
- 환경 변수 → `config.json` 순서로 읽습니다.
4. 실행
```
python main.py
```

**주요 환경 변수**
- `DISCORD_BOT_TOKEN`: 봇 토큰 (필수)
- `GEMINI_API_KEY`: Gemini API 키 (AI 기능 필수)
- `COMETAPI_KEY`: CometAPI 키 (선택)
- `COMETAPI_BASE_URL`: 기본값 `https://api.cometapi.com/v1`
- `COMETAPI_MODEL`: 기본값 `DeepSeek-V3.2-Exp-nothinking`
- `USE_COMETAPI`: 기본값 `true`
- `KMA_API_KEY`: 기상청 API 키
- `KAKAO_API_KEY`: 카카오 API 키
- `GOOGLE_API_KEY`, `GOOGLE_CX`: Google CSE 키
- `EXIM_API_KEY_KR`: 한국수출입은행 환율 API 키
- `GO_DATA_API_KEY_KR`: 공공데이터포털(KRX) 키
- `FINNHUB_API_KEY`: Finnhub 키
- `DEFAULT_AI_CHANNELS`: AI 허용 채널 ID 목록(쉼표 구분)
- `EMB_CONFIG_PATH`: 기본값 `emb_config.json`
- `PROMPT_CONFIG_PATH`: 기본값 `prompts.json`

**설정 파일**
- `prompts.json`: 채널별 AI 페르소나/규칙 및 허용 채널 설정
- `emb_config.json`: RAG(임베딩/BM25/쿼리 확장 등) 설정
- `config.py`: 전체 기본값 및 제한/쿨다운 설정

**프로젝트 구조 (요약)**
- `main.py`: 봇 엔트리포인트 및 Cog 로딩
- `cogs/`: 기능 단위 모듈 (AI, 날씨, 운세, 투표 등)
- `utils/`: API 핸들러, RAG/임베딩, 포맷터
- `database/`: DB 스키마 및 마이그레이션
- `prompts.json`, `emb_config.json`: AI/검색 설정 파일

**데이터 저장**
- SQLite DB: `database/remasamong.db`
- 저장 항목: 대화 기록, 활동 랭킹, 운세 사용자 프로필, API 호출 로그
- 임베딩 DB: `database/discord_embeddings.db` (옵션)

**자주 묻는 문제**
- AI가 응답하지 않음: `GEMINI_API_KEY` 설정 여부와 채널 허용 설정을 확인하세요.
- 날씨/장소/환율이 안 나옴: 해당 API 키가 필요합니다.
- 주식 조회 실패: yfinance 모드에서 CometAPI 티커 추출이 실패했을 수 있습니다. `USE_YFINANCE=false`로 전환 후 KRX/Finnhub 키를 설정하세요.
- 이미지 생성 실패: `COMETAPI_KEY` 및 이미지 생성 제한을 확인하세요.

---

필요한 내용이 더 있으면 알려줘. 실제 동작 기준으로 문서를 계속 업데이트할게.

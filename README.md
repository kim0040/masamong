# 마사몽 AI 에이전트 v3.5: 실시간 여행 어시스턴트

마사몽 3.5는 단순한 정보 검색 봇을 넘어, 사용자의 여행 계획에 생동감을 불어넣는 **'현지 전문가'** AI 에이전트입니다. "다음 주 도쿄 가는데 뭐하지?" 라는 막연한 질문에, 날씨, 인기 명소, 그리고 현지에서 열리는 이벤트까지 종합하여 "마침 다음 주에만 열리는 재즈 페스티벌이 있고, 요즘 현지인들에게 가장 인기 있는 전망대는 여기예요." 와 같이 살아있는 정보를 제공합니다.

## 🤖 봇의 핵심 아키텍처: 2-Step Agent & API Mashup

마사몽 3.5는 효율성과 정확성을 극대화하기 위해 두 가지 핵심 전략을 사용합니다.

### 1. 2-Step Agent (의도 분석 → 최종 답변)
- **1단계 (Triage & Intent):** 경량 LLM(`gemini-2.5-flash-lite`)이 사용자의 질문을 먼저 분석하여, 간단한 대화인지, 아니면 복잡한 정보 조회가 필요한 '도구 사용'인지 판단합니다.
- **2단계 (Execution & Synthesis):** 도구 사용이 필요할 경우, `ToolsCog`가 필요한 API를 호출하여 데이터를 수집합니다. 그 후, 더 강력한 LLM(`gemini-2.5-flash`)이 수집된 모든 정보를 바탕으로 최종 답변을 생성합니다.

### 2. API Mashup (지능형 라우팅)
고비용의 단일 API 대신, 각 분야 최고의 무료 API를 지능적으로 조합합니다. `get_travel_recommendation`과 같은 '메타-도구'는 내부적으로 다음과 같이 동작합니다.
1.  **Geocoding (`Nominatim`):** '도쿄'라는 지명을 위도/경도와 국가 코드로 변환합니다.
2.  **Intelligent Routing:** 국가 코드가 'kr'이면 한국 기상청(KMA) API를, 그 외에는 `OpenWeatherMap` API를 호출하여 날씨 정보를 가져옵니다.
3.  **Data Aggregation:** `Foursquare` (명소), `Ticketmaster` (이벤트) API를 동시에 호출하여 추가 정보를 수집합니다.
4.  **Final Response:** 수집된 모든 데이터를 '엄격한 프롬프트'와 함께 LLM에 전달하여, 정보 왜곡 없는 정확한 요약 답변을 생성합니다.

## ✨ 주요 기능 및 사용법

- **실시간 여행 정보**
  - `@마사몽 다음 주 파리 날씨랑 가볼만한 곳 알려줘`
  - `@마사몽 서울에서 지금 하고 있는 콘서트 있어?`

- **금융 정보**
  - `@마사몽 애플 주가를 원화로 알려줘.` (해외 주식 + 환율)
  - `@마사몽 삼성전자 최신 뉴스 3개만 보여줘.` (국내 주식 + 뉴스)

- **기타**
  - `@마사몽 재밌는 스팀 게임 추천해줘`
  - `!랭킹`, `!투표` 등 기존 명령어

## ⚙️ 설치 및 설정

### 1. 필수 API 키 목록
봇의 모든 기능을 사용하려면 아래 API들의 키가 필요합니다. 각 키는 `.env` 파일에 환경 변수로 설정해야 합니다.

- **필수:**
  - `DISCORD_BOT_TOKEN`
  - `GEMINI_API_KEY`
- **여행 어시스턴트 기능:**
  - `OPENWEATHERMAP_API_KEY`
  - `FOURSQUARE_API_KEY`
  - `TICKETMASTER_API_KEY`
- **금융 기능:**
  - `FINNHUB_API_KEY` (해외 주식)
  - `GO_DATA_API_KEY_KR` (국내 주식, KRX)
  - `EXIM_API_KEY_KR` (환율)
- **기타 기능:**
  - `KMA_API_KEY` (한국 날씨)
  - `RAWG_API_KEY` (게임 추천)
  - `KAKAO_API_KEY` (장소 검색)

### 2. 설치 과정 (Ubuntu)

```bash
# 1. 시스템 패키지 업데이트 및 파이썬 설치
sudo apt update
sudo apt install python3 python3-pip python3-venv -y

# 2. 저장소 복제
git clone <저장소_URL>
cd <프로젝트_디렉토리>

# 3. 파이썬 가상환경 생성 및 활성화
python3 -m venv venv
source venv/bin/activate

# 4. 필수 라이브러리 설치
pip install -r requirements.txt

# 5. 환경 변수 설정
# .env.example 파일을 .env 로 복사한 후, 자신의 API 키 값으로 채워주세요.
cp .env.example .env
nano .env  # nano 에디터 또는 원하는 편집기로 .env 파일 수정

# 6. 데이터베이스 초기화 (최초 1회만 실행)
python3 database/init_db.py

# 7. 봇 실행
python3 main.py
```

### 3. 설치 과정 (Windows)

```powershell
# 1. Python 설치
# https://python.org 에서 최신 버전을 다운로드하여 설치합니다.
# 설치 과정에서 "Add Python to PATH" 옵션을 반드시 체크해주세요.

# 2. 저장소 복제
git clone <저장소_URL>
cd <프로젝트_디렉토리>

# 3. 파이썬 가상환경 생성 및 활성화
python -m venv venv
.\\venv\\Scripts\\activate

# 4. 필수 라이브러리 설치
pip install -r requirements.txt

# 5. 환경 변수 설정
# .env.example 파일을 .env 로 복사한 후, 자신의 API 키 값으로 채워주세요.
copy .env.example .env
notepad .env  # 메모장 또는 원하는 편집기로 .env 파일 수정

# 6. 데이터베이스 초기화 (최초 1회만 실행)
python database/init_db.py

# 7. 봇 실행
python main.py
```

# 마사몽 AI 에이전트 3.0: 다중 도구 지능형 어시스턴트

마사몽 3.0은 단순한 대화 봇을 넘어, 사용자의 복합적인 자연어 질문을 이해하고, 스스로 행동 계획을 수립하며, 다양한 외부 도구(API)를 사용하여 정확한 정보를 찾아내는 고성능 AI 에이전트입니다. '마사몽'이라는 독특한 페르소나를 유지하면서, 서버 멤버들에게 실제 세상의 데이터를 기반으로 한 유용한 정보를 제공하는 것을 목표로 합니다.

## 🤖 봇의 핵심 아키텍처: Plan-and-Execute

마사몽 3.0은 최신 AI 에이전트 기술인 **'계획 및 실행(Plan-and-Execute)'** 모델을 기반으로 동작합니다. 이 아키텍처는 복잡한 문제를 해결하기 위해 다음과 같은 3단계 과정을 거칩니다.

### 1. 계획 (Planning)
사용자가 `@마사몽 애플 주가를 원화로 알려줘`와 같이 복합적인 질문을 하면, 봇은 먼저 경량 LLM(`gemini-2.5-flash-lite`)을 사용하여 **어떤 도구를 어떤 순서로 사용해야 할지**에 대한 행동 계획을 수립합니다. 이 계획은 다음과 같은 JSON 형식으로 생성됩니다.

```json
{
  "plan": [
    {
      "tool_to_use": "get_stock_price",
      "parameters": { "stock_name": "AAPL" }
    },
    {
      "tool_to_use": "get_krw_exchange_rate",
      "parameters": { "currency_code": "USD" }
    }
  ]
}
```

### 2. 실행 (Execution)
봇은 생성된 JSON 계획을 받아 각 단계를 순차적으로 실행합니다.
- **동적 도구 호출**: `get_stock_price`와 같은 도구 이름을 기반으로, `ToolsCog`에 구현된 해당 함수를 동적으로 호출합니다.
- **API 연동**: 각 도구 함수는 `utils/api_handlers/`에 구현된 API 핸들러를 통해 외부 서비스(Finnhub, 한국수출입은행 등)와 통신하여 필요한 데이터를 가져옵니다.
- **컨텍스트 관리**: 첫 번째 단계(주가 조회)의 결과(예: `{ "price": 170.5 }`)는 실행 컨텍스트에 저장되어, 다음 단계에서 필요할 경우 사용될 수 있습니다.

### 3. 종합 (Synthesis)
모든 계획 단계가 실행된 후, 봇은 수집된 모든 데이터(예: 주가, 환율 정보)를 종합합니다. 그리고 더 강력한 LLM(`gemini-2.5-flash`)을 사용하여 이 데이터를 기반으로 사용자의 원래 질문에 대한 자연스럽고 유용한 최종 답변을 생성합니다.

> "애플 주가 찾고 환율까지 보느라 좀 귀찮았는데... 지금 애플(AAPL)은 170.5달러고, 원화로는 대충 230,175원 정도네. 됐냐?"

이러한 분리된 접근 방식은 저사양 서버에서도 효율적으로 작동하며, 복잡한 요청에 대해 더 정확하고 신뢰성 높은 답변을 제공합니다.

## ✨ 주요 기능 및 사용법

### 1. 일반 사용자 기능

- **AI 에이전트 상호작용**
  `@마사몽`으로 봇을 직접 호출하여 다양한 질문을 할 수 있습니다. 이제 봇은 여러 정보를 조합해야 하는 복합적인 질문도 처리할 수 있습니다.
  - `@마사몽 애플 주가를 원화로 알려줘.` (해외 주식 + 환율)
  - `@마사몽 페이커 최근 롤 전적 찾아봐.` (LoL 전적 조회)
  - `@마사몽 강남역 근처 맛집 추천해줘.` (장소 검색)
  - `@마사몽 삼성전자 뉴스 3개만 요약해줘.` (국내 주식 + 뉴스 요약 - *향후 확장 가능*)
  - `@마사몽 어제 우리가 얘기했던 LLM 최적화 방안 다시 설명해줄래?` (기존 RAG 기반 대화 기억)

- **기존 명령어**
  - `!랭킹` 또는 `!수다왕`: 서버 내 메시지 작성 빈도를 기준으로 '수다왕' 랭킹을 보여줍니다.
  - `!투표 "질문" "항목1" "항목2"`: 간단한 주제에 대해 투표를 생성합니다.

### 2. 서버 관리자 기능 (Slash Commands)
서버 관리자는 슬래시(`/`) 명령어를 통해 봇의 작동 방식을 서버별로 제어할 수 있습니다. (`/config set_ai`, `/config channel`, `/persona set`, `/persona view`)

## ⚙️ 설치 및 설정

### 1. 사전 요구 사항

- Python 3.11 이상
- Discord 봇 토큰
- **Google Gemini API 키**
- (선택) 기상청 공공데이터포털 API 키
- (선택) **Riot Games API 키**
- (선택) **Finnhub API 키**
- (선택) **Kakao Developers REST API 키**
- (선택) **공공데이터포털(KRX) API 키**
- (선택) **한국수출입은행 API 키**

### 2. 설치 과정

```bash
# 1. 저장소 복제
git clone <저장소_URL>
cd <프로젝트_디렉토리>

# 2. 파이썬 가상환경 생성 및 활성화
python3 -m venv venv
source venv/bin/activate

# 3. 필수 라이브러리 설치
pip install -r requirements.txt

# 4. 환경 변수 설정
# .env.example 파일을 .env 로 복사한 후, 아래 내용을 자신의 키 값으로 채워주세요.
# DISCORD_BOT_TOKEN="YOUR_DISCORD_BOT_TOKEN"
# GEMINI_API_KEY="YOUR_GEMINI_API_KEY"
# KMA_API_KEY="YOUR_KMA_API_KEY"
# RIOT_API_KEY="YOUR_RIOT_API_KEY"
# FINNHUB_API_KEY="YOUR_FINNHUB_API_KEY"
# KAKAO_API_KEY="YOUR_KAKAO_API_KEY"
# GO_DATA_API_KEY_KR="YOUR_GO_DATA_API_KEY_KR"
# EXIM_API_KEY_KR="YOUR_EXIM_API_KEY_KR"

# 5. 데이터베이스 초기화 (최초 1회만 실행)
python3 database/init_db.py

# 6. 봇 실행
python3 main.py
```

## 🔧 안정성 및 확장성

- **안정적인 비동기 처리**: 모든 데이터베이스 상호작용은 `aiosqlite`를 사용하여 비동기적으로 처리되며, 외부 API 호출은 별도의 스레드에서 실행되어 봇의 메인 이벤트 루프를 막지 않습니다.
- **중앙화된 로깅 시스템**: 모든 중요한 이벤트와 오류는 콘솔, 로그 파일, 그리고 각 서버의 'logs' 채널에 동시에 기록되어 운영 및 디버깅 편의성을 극대화합니다.
- **모듈화된 도구 설계**: 새로운 기능을 추가하고 싶을 경우, `utils/api_handlers/`에 새 API 핸들러를 만들고 `cogs/tools_cog.py`에 해당 도구를 등록한 뒤, `config.py`의 플래너 프롬프트에 도구 설명을 추가하는 것만으로 간단하게 확장할 수 있습니다.

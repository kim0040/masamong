# 마사몽 AI 챗봇 2.0: 지능형 대화 플랫폼

마사몽은 단순한 명령어 봇을 넘어, 서버의 모든 대화를 기억하고, 문맥을 깊이 있게 이해하며, 사용자의 자연어 명령을 지능적으로 수행하는 AI 대화 플랫폼입니다. '마사몽'이라는 독특한 페르소나를 바탕으로 서버 멤버들과 진정한 유대감을 형성하고, 커뮤니티에 활기를 불어넣는 것을 목표로 합니다.

## 🤖 봇의 핵심 기능

마사몽 2.0은 저사양 서버 환경에서도 최상의 성능을 발휘할 수 있도록 설계된, 다음과 같은 최신 AI 아키텍처를 기반으로 동작합니다.

- **영속적 대화 기억 (RAG)**
  봇은 더 이상 대화 내용을 잊어버리지 않습니다. 모든 상호작용은 `SQLite` 데이터베이스에 영구적으로 저장됩니다. **검색 증강 생성(RAG)** 기술을 통해, 봇은 이 방대한 대화 기록 속에서 현재 대화와 가장 관련 높은 '기억'을 실시간으로 찾아내어, 깊이 있고 사실에 기반한 답변을 생성합니다.

- **지능형 기능 라우팅 (Function Calling)**
  딱딱한 명령어는 이제 필수가 아닙니다. 봇의 모든 기능(`날씨`, `요약`, `투표` 등)은 AI가 이해할 수 있는 하나의 '도구(Tool)'로 정의되어 있습니다. 사용자가 자연어로 무언가를 요청하면, Gemini AI가 그 의도를 파악하여 가장 적절한 도구를 스스로 선택하고 실행하여 그 결과를 바탕으로 답변합니다.

- **서버별 동적 페르소나**
  `!페르소나` 명령어를 통해 서버 관리자는 자신의 서버에만 적용되는 AI의 역할, 말투, 정체성을 직접 설정할 수 있습니다. 설정된 페르소나는 데이터베이스에 저장되어 봇의 모든 응답에 반영됩니다.

- **자발적 대화 참여**
  봇은 `@멘션`으로 호출될 때만 응답하는 수동적인 존재가 아닙니다. 대화의 흐름을 지켜보다가 자신과 관련된 키워드가 나오면, '눈치 보는 AI'의 판단을 거쳐 자연스럽게 대화에 참여합니다.

## ✨ 주요 기능 및 사용법

마사몽은 정보 제공, 엔터테인먼트, 커뮤니티 활성화 등 다양한 기능을 **자연어**로 사용하거나, 명확한 행동을 원할 때 **레거시 명령어**로 사용할 수 있습니다.

### 1. 지능형 AI 채팅 (자연어 상호작용)

`@마사몽`으로 봇을 직접 호출하여 질문하거나, 대화 중에 `마사몽`, `봇` 등의 키워드를 언급하여 봇의 참여를 유도할 수 있습니다.

- **일반 대화:** `@마사몽 심심한데 재밌는 얘기 해줘`
- **정보 검색 (RAG):** `@마사몽 어제 우리가 얘기했던 LLM 최적화 방안 다시 설명해줄래?`
- **기능 실행 (Function Calling):**
    - `@마사몽 내일 서울 날씨 어때?`
    - `@마사몽 오늘 내 운세 좀 봐줘`
    - `@마사몽 방금 무슨 얘기했는지 3줄로 요약해줄래?`
    - `@마사몽 "오늘 점심 메뉴는?" 이걸로 "짜장면"이랑 "짬뽕" 투표 열어줘`

### 2. 레거시 명령어

이전 버전과의 호환성을 위해 일부 `!` 명령어를 지원합니다.

- **활동 랭킹:** `!랭킹` 또는 `!수다왕`
- **간단한 투표:** `!투표 "질문" "항목1" "항목2"`
- **운세/요약:** `!운세`, `!요약`

### 3. 서버 관리 (슬래시 커맨드)

서버 관리자는 슬래시 커맨드(`/`)를 사용하여 봇의 행동을 제어할 수 있습니다.

- **페르소나 설정:**
    - `/persona set [새로운 페르소나]`: 이 서버의 AI 페르소나를 새로 설정합니다.
    - `/persona view`: 현재 서버에 설정된 AI 페르소나를 확인합니다.

## 🛠️ 기술 스택

- **Core Framework:** `discord.py`
- **AI Engine:** Google `Gemini 2.5 Flash` via `google-generativeai`
- **RAG & Embeddings:** `sentence-transformers`
- **Database:** `SQLite`
- **Async DB Driver:** `aiosqlite`
- **Vector Search:** `sqlite-vss`

## ⚙️ 설치 및 실행

### 1. 요구 사항
- Python 3.11 이상
- Discord Bot Token
- Google Gemini API Key
- [기상청 공공데이터포털](https://www.data.go.kr/data/15057682/openapi.do) API 키

### 2. 설치 과정
```bash
# 1. 저장소 복제
git clone https://github.com/your-repo/masamong-2.0.git
cd masamong-2.0

# 2. 파이썬 가상환경 생성 및 활성화
# Windows: python -m venv venv && .\venv\Scripts\activate
# macOS/Linux: python3 -m venv venv && source venv/bin/activate
python3 -m venv venv
source venv/bin/activate

# 3. 필수 라이브러리 설치
pip install -r requirements.txt

# 4. .env 파일 설정
# .env.example 파일을 복사하여 .env 파일을 만들고 아래 내용을 채웁니다.
cp .env.example .env
# nano .env 또는 다른 편집기로 아래 값을 입력
# DISCORD_BOT_TOKEN="YOUR_DISCORD_BOT_TOKEN"
# GEMINI_API_KEY="YOUR_GEMINI_API_KEY"
# KMA_API_KEY="YOUR_KMA_API_KEY"

# 5. 데이터베이스 초기화 (최초 1회만 실행)
python database/init_db.py

# 6. 봇 실행
python main.py
```
> **참고:** `sentence-transformers` 라이브러리는 최초 실행 시 사전 훈련된 모델을 다운로드하므로, 초기 구동에 다소 시간이 걸릴 수 있습니다.

## 📁 프로젝트 구조

- `main.py`: 🤖 봇의 생명주기를 관리하는 메인 실행 파일.
- `config.py`: 🧠 봇의 기본 페르소나, API 제한 등 핵심 설정을 담은 파일.
- `cogs/`: 🧩 각 기능별 모듈(Cog)이 들어있는 폴더.
    - `ai_handler.py`: Gemini AI와의 모든 통신, RAG, 함수 호출을 전담하는 핵심 두뇌.
    - `events.py`: 👂 디스코드의 모든 이벤트를 감지하고 `AIHandler`에 전달하는 귀.
    - `weather_cog.py`: 🌦️ 날씨 조회 '도구'와 주기적 알림 기능.
    - `fun_cog.py`: 🎉 운세, 요약 '도구' 기능.
    - `poll_cog.py`: 🗳️ 투표 생성 '도구' 기능.
    - `activity_cog.py`: 📊 사용자 활동을 DB에 기록하고 랭킹을 제공하는 기능.
    - `settings_cog.py`: ⚙️ 서버 관리용 슬래시 커맨드 기능.
- `database/`: 💾 데이터베이스 관련 파일.
    - `remasamong.db`: 모든 데이터가 저장되는 SQLite 데이터베이스 파일.
    - `schema.sql`: 데이터베이스의 구조 설계도.
    - `init_db.py`: 설계도를 바탕으로 DB 파일을 생성하는 스크립트.
- `utils.py`: 🔧 여러 모듈에서 공통으로 사용하는 유틸리티 함수.
- `requirements.txt`: 📜 실행에 필요한 라이브러리 목록.

# 🤖 마사몽 AI 에이전트 v5.2: 지능형 실시간 어시스턴트

[![Python](https://img.shields.io/badge/Python-3.9%2B-blue.svg)](https://python.org)
[![Discord.py](https://img.shields.io/badge/Discord.py-2.3%2B-green.svg)](https://discordpy.readthedocs.io)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Status](https://img.shields.io/badge/Status-Production%20Ready-brightgreen.svg)]()

마사몽 5.2는 단순한 Q&A 봇을 넘어 실시간으로 데이터를 수집·분석하는 **지능형 웹 검색 에이전트**입니다. 2025년 버전에서는 Google Search Grounding를 자동으로 감지하고, Google/SerpAPI/Kakao 검색 스택과 긴밀하게 연동되도록 개선했습니다.

## 🔄 최신 변경 사항 (2025)
- Google Search Grounding 도구 버전에 따라 자동으로 초기화하며, 실패 시 내부 웹 검색 스택으로 즉시 폴백합니다.
- `requirements.txt`를 최신 코드에서 사용하는 패키지 목록으로 갱신했습니다.
- README 전면 개편: 로컬/서버 배포 절차, 환경 변수, 트러블슈팅 및 운영 팁 추가.

## 🌟 주요 특징
- **🧠 2-Step Agent 아키텍처**: `gemini-2.5-flash-lite`로 의도를 판단하고, `gemini-2.5-flash`가 도구 결과를 종합해 답변합니다.
- **🛠️ 하이브리드 API Mashup**: Google Custom Search → SerpAPI → Kakao의 다중 검색망, 기상청+OpenWeatherMap, 한국수출입은행+Finnhub+KRX 등 분야별 최고 API를 결합합니다.
- **🧠 메모리 & RAG**: 최근 대화를 벡터로 저장하여 맥락을 유지하고, 최대 3개의 관련 히스토리를 재활용합니다.
- **🛡️ 안정성**: 구조화된 JSON 로그, API 속도 제한 모니터링, 예외 발생 시 자동 폴백.

## 📦 필수 파이썬 패키지
`pip install -r requirements.txt` 명령으로 아래 라이브러리가 설치됩니다.

```
aiohttp
aiosqlite
discord.py
google-generativeai
numpy
python-dotenv
pytz
requests
```

## 🚀 설치 가이드

### 1. 필수 요구사항
- Python 3.9 이상 (3.11 권장)
- SQLite 3 (Ubuntu 기본 포함)
- Discord 봇 토큰, Google Gemini API 키 (필수)
- Google Custom Search API 키 / SerpAPI 키 / Kakao REST API 키 (선택이지만 권장)

### 2. 로컬 개발 환경 (macOS · Windows)
```bash
# 소스 클론 및 가상환경 생성
git clone https://github.com/kim0040/masamong.git
cd masamong
python3 -m venv venv
source venv/bin/activate  # Windows는 venv\Scripts\activate

# 패키지 설치 및 데이터베이스 초기화
pip install --upgrade pip
pip install -r requirements.txt
python database/init_db.py
```
환경 변수 및 API 키 설정은 아래 "⚙️ 환경 설정" 섹션을 참고하세요. 준비가 끝났다면 `python main.py`로 봇을 실행합니다.

### 3. 그릭(Greek) Ubuntu 서버 배포 (Ubuntu 22.04 LTS 기준)
> 운영 중인 "그릭" 서버에서 사용 중인 환경을 기준으로 서술했습니다.

1. **시스템 준비**
   ```bash
   sudo apt update && sudo apt upgrade -y
   sudo apt install python3.11 python3.11-venv python3-pip git build-essential -y
   ```
2. **서비스 전용 계정 생성 (선택)**
   ```bash
   sudo adduser --disabled-password --gecos "" masamong
   sudo usermod -aG sudo masamong
   sudo su - masamong
   ```
3. **소스 배포 및 가상환경 구성**
   ```bash
   git clone https://github.com/kim0040/masamong.git
   cd masamong
   python3.11 -m venv venv
   source venv/bin/activate
   pip install --upgrade pip
   pip install -r requirements.txt
   ```
4. **환경 파일 & 설정 복사**
   ```bash
   cp .env.example .env            # 필요 시 sed/vi로 API 키 입력
   cp config.json.example config.json
   ```
5. **데이터베이스 초기화 및 사전 점검**
   ```bash
   python database/init_db.py
   python -m compileall cogs utils  # 문법 오류 빠르게 확인
   ```
6. **(선택) systemd 서비스 등록**
   `/etc/systemd/system/masamong.service` 예시:
   ```ini
   [Unit]
   Description=Masamong Discord Agent
   After=network.target

   [Service]
   Type=simple
   User=masamong
   WorkingDirectory=/home/masamong/masamong
   ExecStart=/home/masamong/masamong/venv/bin/python main.py
   Restart=on-failure
   Environment="PYTHONUNBUFFERED=1"

   [Install]
   WantedBy=multi-user.target
   ```
   적용:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now masamong.service
   journalctl -u masamong.service -f
   ```
7. **로그 위치**
   - 일반 로그: `discord_logs.txt`
   - 에러 로그 (JSON): `error_logs.txt`
   - 시스템 로그: `journalctl -u masamong.service`

## ⚙️ 환경 설정

### 1. `.env` / 환경 변수
`.env.example`를 복사해 다음 값을 채웁니다.

```
DISCORD_BOT_TOKEN=YOUR_DISCORD_TOKEN
GEMINI_API_KEY=YOUR_GEMINI_API_KEY
GOOGLE_API_KEY=...
GOOGLE_CX=...
SERPAPI_KEY=...
KAKAO_API_KEY=...
FINNHUB_API_KEY=...
OPENWEATHERMAP_API_KEY=...
```

환경 변수는 `.env`, `config.json`, 실제 OS 환경변수 순서로 읽습니다. 민감한 키는 `.env`에 보관하고 Git에 커밋하지 마세요.

### 2. `config.json`
`config.json.example`을 복사해 편집합니다. 주요 항목은 아래와 같습니다.

| 서비스 | 키 | 비고 |
| --- | --- | --- |
| Discord | `DISCORD_BOT_TOKEN` | 봇 실행 필수 |
| Google Gemini | `GEMINI_API_KEY` | LLM 호출 필수 |
| Google Custom Search | `GOOGLE_API_KEY`, `GOOGLE_CX` | Grounding 실패 시 1순위 폴백 |
| SerpAPI | `SERPAPI_KEY` | Google CSE 없이도 검색 가능 |
| Kakao Search | `KAKAO_API_KEY` | 마지막 폴백 |
| 기상청(KMA) | `KMA_API_KEY` | 국내 상세 날씨 |
| OpenWeatherMap | `OPENWEATHERMAP_API_KEY` | 해외 날씨 |
| Finnhub | `FINNHUB_API_KEY` | 해외 주식/뉴스 |

## ▶️ 실행 및 모니터링
```bash
source venv/bin/activate
python main.py
```
- 봇이 정상 가동되면 콘솔과 `discord_logs.txt`에 구조화된 로그가 남습니다.
- 테스트 메시지는 Discord에서 `@마사몽 상태 어때?` 같이 간단한 질문으로 확인하세요.

## 🛠️ 트러블슈팅
- **Google Search Grounding이 동작하지 않을 때**
  - `pip install --upgrade google-generativeai` (권장 버전 ≥ 0.7).
  - `error_logs.txt`에서 `Google Grounding 도구` 관련 경고를 확인하세요. 감지 실패 시 자동으로 SerpAPI/Kakao 검색을 사용합니다.
  - `config.py`에서 `GEMINI_API_KEY`가 비어 있으면 Grounding이 비활성화됩니다.
- **웹 검색 결과가 비어 있을 때**
  - `config.json`에 `GOOGLE_API_KEY`와 `GOOGLE_CX`가 설정되어 있는지 확인.
  - SerpAPI 잔량이 남아 있는지 확인 (무료 플랜은 월 100회).
  - Kakao API는 한국어 쿼리에 적합합니다. 영어 쿼리 실패 시 Google CSE 설정을 권장합니다.
- **데이터베이스 오류**
  - `database/init_db.py`를 다시 실행해 스키마를 재생성.
  - SQLite 파일 권한(`database/remasamong.db`)을 확인합니다.

## 💬 사용 예시
```
@마사몽 안녕?
→ 안녕! 오늘은 뭐 하고 지낼 거야? 🌤️

@마사몽 아이폰 17 출시일 루머 정리해줘
→ (Google Grounding 또는 폴백 검색 결과를 요약)

@마사몽 다음 주 파리 날씨 알려줘
→ 파리 좌표 조회 → OpenWeatherMap 조회 → 깔끔한 리포트

@마사몽 애플 주가를 원화로 알려줘
→ Finnhub 주가 + 환율 + 요약
```

## 🏗️ 아키텍처 개요
```
masamong/
├── main.py                 # Discord 봇 진입점
├── config.py               # 환경 변수 및 설정 로딩
├── cogs/
│   ├── ai_handler.py       # 2-Step Agent, 도구 실행, Grounding 폴백
│   ├── tools_cog.py        # Google/SerpAPI/Kakao 등 외부 도구 모음
│   └── weather_cog.py      # 국내/해외 날씨 처리
├── utils/
│   ├── api_handlers/       # API 래퍼 (Finnhub, Kakao, EXIM, KMA 등)
│   ├── db.py               # SQLite + 레이트 리밋 관리
│   └── http.py             # TLS/세션 유틸리티
└── database/
    ├── schema.sql          # DB 스키마
    └── init_db.py          # 초기화 스크립트
```

---

**마사몽과 함께 더 스마트한 Discord 서버를 운영해보세요!** 🚀

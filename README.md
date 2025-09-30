# 🤖 마사몽 AI 에이전트 v5.2: 지능형 실시간 어시스턴트

[![Python](https://img.shields.io/badge/Python-3.9+-blue.svg)](https://python.org)
[![Discord.py](https://img.shields.io/badge/Discord.py-2.3+-green.svg)](https://discordpy.readthedocs.io)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Status](https://img.shields.io/badge/Status-Production%20Ready-brightgreen.svg)]()

마사몽 5.2는 단순한 정보 검색 봇을 넘어, 사용자의 질문에 가장 정확한 최신 정보를 제공하는 **'지능형 웹 검색 에이전트'**로 한 단계 더 발전했습니다. "대한민국 최신 경제 동향 알려줘" 와 같은 실시간 정보가 필요한 질문에, Google 검색 결과를 분석하고 요약하여 신뢰도 높은 답변을 제공합니다.

## 🌟 주요 특징

### 🧠 2-Step Agent 아키텍처
- **1단계 (Triage & Intent)**: 경량 LLM(`gemini-2.5-flash-lite`)이 사용자 의도를 분석하고 간단한 대화는 직접 처리합니다.
- **2단계 (Execution & Synthesis)**: 복잡한 요청은 도구를 사용하여 데이터를 수집한 후, 강력한 LLM(`gemini-2.5-flash`)이 최종 답변을 생성합니다.

### 🛠️ 하이브리드 API Mashup 시스템
고비용의 단일 API 대신, 각 분야 최고의 무료/유료 API를 지능적으로 조합하고 우선순위에 따라 폴백(fallback)합니다.
- **웹 검색**: **Google Custom Search** → **SerpAPI** → Kakao Web Search
- **지리 정보**: Nominatim (OpenStreetMap)
- **날씨**: 기상청(KMA) + OpenWeatherMap
- **금융**: 한국수출입은행 + Finnhub + KRX

### 🎯 능동적 비서 기능
- **잠재적 의도 파악**: "다음 달에 일본 여행 가려고" → "엔화 환율 정보를 알려드릴까요?"
- **개인화된 알림**: 환율, 날씨, 주식 등 사용자 설정 기반 알림
- **맥락 기억**: 대화 내용을 기억하여 "아까 말했던 그 게임" 같은 모호한 질문에도 답변합니다.

### 🛡️ 엔터프라이즈급 안정성
- **완전한 예외 처리**: 모든 API 호출에 대한 방어벽을 구축합니다.
- **자동 복구**: API 장애 시에도 봇이 중단되지 않습니다.
- **상세한 로깅**: JSON 형식의 구조화된 로그로 문제 추적을 용이하게 합니다.

## 🚀 빠른 시작

### 1. 필수 요구사항
- Python 3.9 이상
- Discord 봇 토큰
- Google Gemini API 키

### 2. 설치 (Ubuntu 20.04+ 기준)

**1. 시스템 준비**

```bash
# 시스템 패키지 목록을 최신 상태로 업데이트합니다.
sudo apt update && sudo apt upgrade -y

# Python 3.11, 가상 환경 도구, pip, git을 설치합니다.
# (Python 3.9 이상이면 되지만, 3.11을 권장합니다.)
sudo apt install python3.11 python3.11-venv python3-pip git -y
```

**2. 프로젝트 클론 및 설정**

```bash
# 원하는 위치에 프로젝트 소스 코드를 클론합니다.
git clone https://github.com/kim0040/masamong.git
cd masamong

# 'venv'라는 이름의 가상 환경을 생성합니다.
python3.11 -m venv venv

# 가상 환경을 활성화합니다. (터미널 프롬프트 앞에 (venv)가 표시됩니다.)
source venv/bin/activate

# pip를 최신 버전으로 업그레이드합니다.
pip install --upgrade pip

# requirements.txt 파일에 명시된 모든 파이썬 라이브러리를 설치합니다.
pip install -r requirements.txt
```

**3. 환경 변수 및 API 키 설정**

`config.json.example` 파일을 `config.json`으로 복사하고, 텍스트 편집기로 열어 각 API 서비스에서 발급받은 키를 입력하고 저장합니다. `DISCORD_BOT_TOKEN`과 `GEMINI_API_KEY`는 봇의 핵심 기능을 위해 **반드시** 필요합니다.

**4. 데이터베이스 초기화**

```bash
# database/schema.sql 파일의 내용에 따라 SQLite 데이터베이스 파일을 생성합니다.
# 이 명령어는 봇을 처음 설정할 때 한 번만 실행하면 됩니다.
python database/init_db.py
```

**5. 봇 실행**

```bash
# 모든 설정이 완료되었습니다! 봇을 실행합니다.
python main.py
```

### 3. API 키 설정 가이드

`config.json` 파일에 다음 API 키들을 설정하세요.

| 서비스 | `config.json` 키 | 발급처 | 무료 할당량 | 필수도 |
|---|---|---|---|---|
| **Discord Bot** | `DISCORD_BOT_TOKEN` | [Discord Developer Portal](https://discord.com/developers/applications) | 무제한 | **필수** |
| **Google Gemini** | `GEMINI_API_KEY` | [Google AI Studio](https://aistudio.google.com/app/apikey) | 15 RPM | **필수** |
| **Google Search**| `GOOGLE_API_KEY`, `GOOGLE_CX` | [Google Custom Search](https://developers.google.com/custom-search/v1/overview) | 100/일 | **권장** |
| **SerpAPI** | `SERPAPI_KEY` | [SerpAPI](https://serpapi.com/) | 100/월 | 선택 |
| **기상청 API** | `KMA_API_KEY` | [API 허브](https://apihub.kma.go.kr/) | 10,000/일 | 권장 |
| **OpenWeatherMap**| `OPENWEATHERMAP_API_KEY` | [OpenWeatherMap](https://openweathermap.org/api) | 1,000/일 | 권장 |
| **Finnhub** | `FINNHUB_API_KEY` | [Stock API](https://finnhub.io/) | 60/분 | 선택 |
| **Kakao** | `KAKAO_API_KEY` | [Kakao Developers](https://developers.kakao.com/) | 다양 | 선택 |

> **웹 검색 우선순위**: `Google Custom Search` -> `SerpAPI` -> `Kakao Web Search` 순으로 자동 폴백됩니다. 안정적인 서비스를 위해 **Google Custom Search API 키 설정**을 권장합니다.

## 📖 사용법

### 일반 대화 및 웹 검색
```
@마사몽 안녕!
→ 안녕! 오늘 날씨가 좋은데 나가서 산책이라도 할까? 🌤️

@마사몽 아이폰 17 출시일 루머 정리해줘
→ (Google/SerpAPI 검색 결과를 바탕으로 최신 루머를 요약하여 답변)
```

### 여행 정보 조회
```
@마사몽 다음 주 파리 날씨랑 가볼만한 곳 알려줘
→ 파리 여행 정보를 종합적으로 제공 (날씨 + 명소 + 이벤트)
```

### 금융 정보
```
@마사몽 애플 주가를 원화로 알려줘
→ 현재 주가 + 환율 변환 + 뉴스 정보
```

### 명령어 목록
`!` 접두사를 사용하여 다음 명령어들을 사용할 수 있습니다. (자세한 내용은 `!help` 명령어로 확인)

## 🏗️ 아키텍처

### 핵심 컴포넌트

```
masamong/
├── main.py                 # 봇 진입점
├── config.py              # 설정 관리
├── cogs/                  # Discord Cog 모듈들
│   ├── ai_handler.py      # AI 대화 처리
│   ├── tools_cog.py       # API 도구 모음 (Google/SerpAPI/Kakao 웹 검색 포함)
│   └── ...
├── utils/                 # 유틸리티 모듈들
│   ├── api_handlers/      # 개별 API 핸들러
│   └── ...
└── database/              # 데이터베이스
    ├── schema.sql         # 스키마 정의
    └── init_db.py         # 초기화 스크립트
```

---

**마사몽과 함께 더 스마트한 디스코드 서버를 만들어보세요!** 🚀

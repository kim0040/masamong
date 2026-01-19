# 🚀 마사몽 빠른 시작 가이드

5분 안에 마사몽을 실행해보세요!

## ⚡ 빠른 설치 (최소 구성)

### 1. 저장소 클론
```bash
git clone https://github.com/kim0040/masamong.git
cd masamong
```

### 2. 가상환경 및 의존성 설치
```bash
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. 필수 설정 파일 생성

#### .env 파일
```bash
cat > .env << 'EOF'
# 필수
DISCORD_BOT_TOKEN=your_discord_bot_token_here
GEMINI_API_KEY=your_gemini_api_key_here

# 선택 (기능 제한)
AI_MEMORY_ENABLED=false
RERANK_ENABLED=false
EOF
```

#### prompts.json 파일
```bash
cp prompts.json.example prompts.json
```

**중요**: `prompts.json`에서 `YOUR_CHANNEL_ID_HERE`를 실제 Discord 채널 ID로 변경하세요!

### 4. 데이터베이스 초기화
```bash
python3 database/init_db.py
```

### 5. 봇 실행
```bash
python3 main.py
```

## ✅ 실행 확인

1. 봇이 온라인 상태인지 Discord에서 확인
2. 설정한 채널에서 테스트:
   ```
   @마사몽 안녕?
   ```
3. 봇이 응답하면 성공! 🎉

## 🔑 Discord 봇 토큰 받기

1. [Discord Developer Portal](https://discord.com/developers/applications) 접속
2. "New Application" 클릭
3. 왼쪽 메뉴에서 "Bot" 선택
4. "Reset Token" → 토큰 복사
5. `.env` 파일의 `DISCORD_BOT_TOKEN`에 붙여넣기

## 🤖 Gemini API 키 받기

1. [Google AI Studio](https://aistudio.google.com/app/apikey) 접속
2. "Create API Key" 클릭
3. 키 복사
4. `.env` 파일의 `GEMINI_API_KEY`에 붙여넣기

## 🆔 Discord 채널 ID 찾기

1. Discord 설정 → 고급 → "개발자 모드" 활성화
2. 채널 우클릭 → "채널 ID 복사"
3. `prompts.json`의 `YOUR_CHANNEL_ID_HERE` 부분을 복사한 ID로 교체

## 🎯 기본 명령어

| 명령어 | 설명 |
|--------|------|
| `@마사몽 안녕?` | AI와 대화 |
| `@마사몽 서울 날씨` | 날씨 조회 |
| `!랭킹` | 활동 순위 |
| `!운세` | 오늘의 운세 |

## ⚙️ 추가 기능 활성화

### RAG 메모리 기능
```bash
# .env에 추가
AI_MEMORY_ENABLED=true
```

### 날씨 기능
```bash
# .env에 추가
KMA_API_KEY=your_kma_api_key
```

### 주식 정보
```bash
# .env에 추가
FINNHUB_API_KEY=your_finnhub_key
```

## 🐛 문제 해결

### 봇이 시작되지 않음
```bash
# 토큰 확인
python3 -c "import config; print('✅ OK' if config.TOKEN else '❌ TOKEN 없음')"
```

### 봇이 응답하지 않음
1. 채널이 `prompts.json`에 `allowed: true`로 설정되었는지 확인
2. 봇을 **멘션**했는지 확인 (`@마사몽`)
3. 봇에게 메시지 읽기 권한이 있는지 확인

### 모듈 오류
```bash
# 의존성 재설치
pip install --force-reinstall -r requirements.txt
```

## 📚 다음 단계

- [전체 문서 읽기](README.md)
- [아키텍처 이해하기](ARCHITECTURE.md)
- [환경 변수 설정](README.md#환경-변수)
- [기여하기](CONTRIBUTING.md)

## 💬 도움이 필요하신가요?

- 📘 [상세 README](README.md)
- 🐛 [GitHub Issues](https://github.com/kim0040/masamong/issues)

---

**팁**: 저사양 서버에서 실행 시 `AI_MEMORY_ENABLED=false`로 설정하면 메모리 사용량이 크게 줄어듭니다!

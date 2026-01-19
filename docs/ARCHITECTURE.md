# 마사몽 아키텍처 문서

## 시스템 개요

마사몽은 모듈식 아키텍처를 가진 Discord 봇으로, AI 에이전트, RAG 시스템, 외부 API 통합을 결합합니다.

## 핵심 설계 원칙

### 1. 2단계 에이전트 패턴

**목표**: 비용 효율적이면서도 정확한 AI 응답 생성

```
사용자 쿼리 
    ↓
[1단계: Gemini Lite - 의도 분석]
    ├─ JSON 구조 생성 (tool_plan, draft, self_score)
    ├─ 필요한 도구 식별
    └─ 자기 평가 (confidence score)
    ↓
[도구 실행]
    ├─ 날씨 API
    ├─ 금융 API
    ├─ 검색 API
    └─ RAG 검색
    ↓
[2단계: 승급 판단]
    ├─ self_score >= 0.75 → Lite 응답 사용 (80% 케이스)
    └─ self_score < 0.75 → Flash 호출 (20% 케이스)
    ↓
최종 응답
```

**비용 절감 효과**:
- Lite 모델: ~1/10 비용
- Flash 승급률: ~20%
- 전체 비용 절감: ~70%

### 2. 하이브리드 RAG

**문제**: 단일 검색 방식의 한계
- 의미 검색만: 키워드 정확도 부족
- 키워드 검색만: 의미 파악 불가

**해결**: BM25 + Embedding 결합

```python
# 가중치
embedding_weight = 0.55  # 의미 기반
bm25_weight = 0.45       # 키워드 기반

# 최종 점수
combined_score = (similarity * 0.55) + (bm25_score * 0.45)
```

### 3. 멘션 게이트 패턴

**목표**: 리소스 낭비 방지 및 개인정보 보호

모든 메시지를 처리하면:
- ❌ 불필요한 API 호출
- ❌ 개인 대화 노출 위험
- ❌ 높은 비용

멘션만 처리하면:
- ✅ 명시적 요청만 응답
- ✅ API 비용 절감
- ✅ 프라이버시 보호

## 모듈 구조

### Cog 아키텍처

```
main.py (봇 엔트리포인트)
    │
    ├─ AIHandler (AI 에이전트 핵심)
    │   ├─ 멘션 검증
    │   ├─ RAG 검색
    │   ├─ Gemini 호출
    │   └─ 응답 생성
    │
    ├─ ToolsCog (외부 API 통합)
    │   ├─ get_weather()
    │   ├─ get_us_stock_info()
    │   ├─ search_for_place()
    │   └─ web_search()
    │
    ├─ WeatherCog (날씨 기능)
    │   ├─ 위치 기반 조회
    │   └─ 정기 알림
    │
    ├─ ActivityCog (활동 추적)
    │   └─ 랭킹 시스템
    │
    └─ 기타 Cogs
        ├─ EventsCog (이벤트 핸들러)
        ├─ SettingsCog (서버 설정)
        └─ FunCog (재미 요소)
```

### 의존성 주입

Cog 간 통신은 의존성 주입 패턴으로 처리:

```python
# main.py에서
tools_cog = await bot.get_cog("ToolsCog")
ai_handler.tools_cog = tools_cog  # 주입
```

**장점**:
- 느슨한 결합
- 테스트 가능성
- 모듈 독립성

## 데이터 레이어

### 데이터베이스 구조

```
remasamong.db (메인 DB)
├─ conversation_history      # 모든 대화 기록
├─ conversation_windows       # 슬라이딩 윈도우 캐시
├─ user_activity             # 활동 추적
├─ guild_settings            # 서버 설정
├─ api_call_log              # API 호출 로그
├─ system_counters           # 시스템 카운터
└─ locations                 # 날씨 격자 좌표

discord_embeddings.db (임베딩 DB)
└─ discord_chat_embeddings   # 벡터 + 메타데이터

kakao_embeddings.db (카카오 DB)
└─ kakao_message_embeddings  # 외부 데이터 소스
```

### 대화 윈도우 캐싱

**목적**: RAG 성능 최적화

일반적인 방식:
```sql
-- 매번 ±3 메시지 조회 (느림)
SELECT * FROM conversation_history 
WHERE message_id BETWEEN (target_id - 3) AND (target_id + 3)
```

마사몽 방식:
```sql
-- 미리 계산된 윈도우 조회 (빠름)
SELECT messages_json FROM conversation_windows 
WHERE start_message_id <= target_id 
  AND end_message_id >= target_id
```

**성능 향상**: 3~5배

## RAG 파이프라인 상세

### 1. 쿼리 전처리

```python
# 입력: "서울 날씨"
query = "서울 날씨"
recent_messages = ["어제 비 왔어", "오늘은 어떨까"]

# 1단계: 컨텍스트 결합
seed_query = "서울 날씨 어제 비 왔어 오늘은 어떨까"

# 2단계: 쿼리 확장 (Gemini)
variants = [
    "서울 날씨",
    "서울 날씨 어제 비 왔어 오늘은 어떨까",
    "서울의 현재 기상 정보",  # 생성된 변형
]
```

### 2. 병렬 검색

```python
# 각 변형마다 BM25 + 임베딩 동시 실행
for variant in variants:
    # 병렬로
    embedding_results = await embedding_search(variant, top_n=8)
    bm25_results = await bm25_search(variant, top_n=8)
```

### 3. RRF (Reciprocal Rank Fusion)

```python
def calculate_rrf_score(rank: int, k: int = 60) -> float:
    return 1.0 / (k + rank)

# 예시
# 임베딩 rank 1 → rrf_score = 1/(60+1) = 0.0164
# BM25 rank 3 → rrf_score = 1/(60+3) = 0.0159
```

### 4. 가중 결합

```python
# 후보가 두 검색에서 모두 나타난 경우
combined_score = (
    similarity * 0.55 +        # 의미 유사도
    bm25_normalized * 0.45     # 키워드 매칭
)
```

### 5. 리랭킹 (선택)

```python
if RERANK_ENABLED:
    # Cross-Encoder로 정밀 평가
    reranked = cross_encoder.rank(query, candidates)
    return reranked[:top_k]
```

## Gemini 통신 프로토콜

### Lite 모델 (의도 분석)

**입력 프롬프트 구조**:
```json
{
  "system": "lite_system_prompt + MENTION_GUARD",
  "context": {
    "rag_results": [...],
    "recent_messages": [...],
    "channel_persona": "츤데레",
    "rules": "반말 사용, ..."
  },
  "user_query": "서울 날씨 알려줘"
}
```

**출력 JSON 구조**:
```json
{
  "analysis": "사용자가 서울 날씨 정보를 요청함",
  "tool_plan": [
    {
      "tool_name": "get_weather",
      "parameters": {"location": "서울"}
    }
  ],
  "draft": "서울 날씨? 지금 확인해볼게~",
  "self_score": {
    "accuracy": 0.95,
    "completeness": 0.90,
    "risk": 0.10,
    "overall": 0.92
  },
  "needs_flash": false
}
```

### Flash 승급 조건

```python
needs_flash = (
    self_score.overall < 0.75 or
    self_score.risk > 0.6 or
    is_high_risk_topic(query) or  # 금융, 의료, 법률
    estimated_tokens > 1200 or
    has_conflicting_evidence()
)
```

## 성능 최적화 전략

### 1. 캐싱 계층

```
레벨 1: Python 메모리 캐시
├─ 임베딩 모델 (_MODEL 전역 변수)
├─ Gemini 클라이언트
└─ 설정 객체

레벨 2: SQLite 캐시
├─ conversation_windows (미리 계산)
├─ BM25 FTS5 인덱스
└─ 임베딩 벡터

레벨 3: 디스크
└─ HuggingFace 모델 캐시 (~/.cache)
```

### 2. 비동기 처리

**메시지 임베딩**:
```python
# 메인 스레드 블로킹 방지
asyncio.create_task(
    self._create_and_save_embedding(message)
)
```

**병렬 API 호출**:
```python
# 여러 API 동시 호출
results = await asyncio.gather(
    get_weather(),
    get_stock_info(),
    web_search(),
    return_exceptions=True
)
```

### 3. 인덱싱 최적화

```sql
-- conversation_windows 복합 인덱스
CREATE INDEX idx_conversation_windows_channel 
ON conversation_windows (channel_id, anchor_timestamp DESC);

-- 유니크 제약으로 중복 방지
CREATE UNIQUE INDEX idx_conversation_windows_span 
ON conversation_windows (channel_id, start_message_id, end_message_id);
```

## 에러 처리 패턴

### 계층적 폴백

```python
try:
    # 1순위: Primary API
    result = await google_search(query)
except APIError:
    try:
        # 2순위: Fallback API
        result = await serpapi_search(query)
    except APIError:
        try:
            # 3순위: Alternative API
            result = await kakao_search(query)
        except APIError:
            # 최종 폴백: 에러 메시지
            result = "검색 서비스를 사용할 수 없습니다"
```

### 도구 실행 실패 처리

```python
# 도구 실행 실패 시 자동으로 웹 검색 추가
if tool_execution_failed:
    tool_plan.append({
        "tool_name": "web_search",
        "parameters": {"query": original_query}
    })
```

## 확장 가능성

### 새 Cog 추가

```python
# cogs/my_new_cog.py
class MyNewCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    
    @commands.command()
    async def my_command(self, ctx):
        await ctx.send("Hello!")

# main.py
await bot.load_extension("cogs.my_new_cog")
```

### 새 도구 추가

```python
# cogs/tools_cog.py
async def my_new_tool(self, param1: str) -> dict:
    """새로운 도구 설명"""
    result = await some_api_call(param1)
    return {"result": result}

# AIHandler가 자동으로 발견하여 사용 가능
```

### 새 임베딩 소스 추가

```python
# emb_config.json
{
  "kakao_servers": [
    {
      "server_id": "new_source_123",
      "db_path": "database/new_source_embeddings.db",
      "label": "새 데이터 소스"
    }
  ]
}
```

## 보안 고려사항

### 1. 멘션 게이트

- 모든 프롬프트에 자동 추가되는 멘션 정책
- 코드 레벨에서도 이중 확인

### 2. API 키 관리

```python
# ❌ 하드코딩 금지
GEMINI_API_KEY = "AIza..."

# ✅ 환경 변수 사용
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
```

### 3. Rate Limiting

```python
# API 호출 제한 (DB 기반)
async def check_rate_limit(api_type: str) -> bool:
    recent_calls = await db.count_recent_calls(
        api_type, 
        window_minutes=60
    )
    return recent_calls < config.RPM_LIMIT
```

### 4. 입력 검증

```python
# 사용자 입력 sanitization
cleaned_query = re.sub(r'[<>\"\'`]', '', user_query)
```

## 모니터링 및 관찰성

### 로깅 계층

```
1. 콘솔 로그 (INFO 이상)
   ├─ 봇 시작/종료
   ├─ Cog 로드
   └─ 주요 이벤트

2. 파일 로그 (DEBUG 이상)
   ├─ discord_logs.txt (일반)
   └─ error_logs.txt (에러)

3. Discord 임베드 로그
   └─ #logs 채널 (설정 시)

4. DB 분석 로그
   └─ analytics_log 테이블
```

### 메트릭 수집

```python
# analytics_log 테이블
{
  "event_type": "AI_INTERACTION",
  "details": {
    "model_used": "lite",  # or "flash"
    "rag_hits": 3,
    "latency_ms": 1250,
    "tools_used": ["get_weather"],
    "self_score": 0.92
  }
}
```

## 배포 고려사항

### 저사양 서버

**권장 사양**:
- CPU: 2 Core
- RAM: 2GB
- Disk: 5GB

**최적화 설정**:
```env
AI_MEMORY_ENABLED=false
RERANK_ENABLED=false
SEARCH_CHUNKING_ENABLED=false
CONVERSATION_WINDOW_SIZE=3
```

### 고성능 서버

**권장 사양**:
- CPU: 4+ Core
- RAM: 8GB+
- Disk: 20GB+
- GPU: Optional (CUDA 11.8+)

**최적화 설정**:
```env
AI_MEMORY_ENABLED=true
RERANK_ENABLED=true
SEARCH_CHUNKING_ENABLED=true
LOCAL_EMBEDDING_DEVICE=cuda  # GPU 사용
BM25_AUTO_REBUILD_ENABLED=true
```

## 레퍼런스

- [Discord.py 문서](https://discordpy.readthedocs.io/)
- [Google Gemini API](https://ai.google.dev/)
- [SentenceTransformers](https://www.sbert.net/)
- [SQLite FTS5](https://www.sqlite.org/fts5.html)

---

*마지막 업데이트: 2026-01-19*

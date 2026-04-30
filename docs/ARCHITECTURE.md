# 마사몽 아키텍처 문서

> **참고**: 더 자세한 UML 분석은 [UML_SPEC.md](UML_SPEC.md)를 참조하세요.

## 시스템 개요

마사몽은 모듈식 아키텍처를 가진 Discord 봇으로, AI 에이전트, RAG 시스템, 외부 API 통합을 결합합니다.

---

## 시스템 컨텍스트 다이어그램

```mermaid
graph TB
    subgraph Users["👤 사용자"]
        GU["서버 유저<br/>@멘션 필수"]
        DM["DM 유저<br/>5h/30회 제한"]
        AD["관리자<br/>!업데이트, !debug"]
    end

    subgraph Discord["Discord 플랫폼"]
        Gateway["Discord Gateway<br/>WebSocket + HTTP"]
    end

    subgraph BotProcess["🤖 마사몽 Bot Process"]
        Entry["main.py<br/>ReMasamongBot"]
    end

    subgraph ExternalAPIs["🌐 외부 API"]
        LLM["CometAPI / Gemini<br/>LLM Inference"]
        KMA_["KMA (기상청)<br/>날씨/지진"]
        Finance_["Finnhub / yfinance / KRX<br/>금융 데이터"]
        Web_["Linkup / DuckDuckGo<br/>웹 검색"]
        Place_["Kakao Local<br/>장소 검색"]
    end

    subgraph Storage["💾 저장소"]
        TiDB["TiDB Cloud<br/>(운영)"]
        SQLiteDB["SQLite<br/>(개발)"]
        HF["HuggingFace Cache<br/>임베딩 모델"]
    end

    GU --> Gateway
    DM --> Gateway
    AD --> Gateway
    Gateway <-->|"WebSocket"| Entry
    Entry --> LLM
    Entry --> KMA_
    Entry --> Finance_
    Entry --> Web_
    Entry --> Place_
    Entry --> TiDB
    Entry --> SQLiteDB
    Entry --> HF
```

---

## 핵심 설계 원칙

### 1. 3단계 AI 파이프라인 (2026-04 기준)

```mermaid
flowchart TB
    Input["👤 사용자 메시지"] --> Valid[검증<br/>멘션/채널/잠금]

    Valid --> Step1["🔍 Step 1: 의도 분석<br/>IntentAnalyzer<br/><i>키워드 휴리스틱 + LLM</i>"]

    Step1 -->|"도구 필요 없음"| RAG
    Step1 -->|"도구 실행 계획"| Step2["🛠️ Step 2: 도구 실행<br/>ToolsCog"]

    subgraph Step2Detail[" "]
        direction LR
        W["날씨<br/>KMA"]
        F["금융<br/>Finnhub/yfinance"]
        S["웹 검색<br/>Linkup/DDG"]
        P["장소<br/>Kakao"]
        I["이미지<br/>CometAPI"]
    end

    Step2 --> Step2Detail
    Step2Detail --> RAG["🧠 RAG 컨텍스트 검색<br/>HybridSearchEngine<br/><i>임베딩 + BM25 + RRF</i>"]

    RAG --> Step3["✍️ Step 3: 응답 생성<br/>LLMClient (Main Lane)<br/><i>DeepSeek-V3.2-Exp</i>"]

    Step3 --> Output["💬 Discord 응답<br/><i>페르소나 + 이모지 적용</i>"]

    style Valid fill:#ffecb3,stroke:#f57c00
    style Step1 fill:#e1f5fe,stroke:#0288d1
    style Step2 fill:#f3e5f5,stroke:#7b1fa2
    style RAG fill:#e8f5e9,stroke:#388e3c
    style Step3 fill:#fff3e0,stroke:#e65100
    style Output fill:#c8e6c9,stroke:#2e7d32
```

### 2. 듀얼 레인 LLM 라우팅

```mermaid
graph TB
    subgraph Routing["Routing Lane (의도 분석)"]
        direction TB
        RP1["Primary: gemini-3.1-flash-lite<br/><i>(CometAPI)</i>"]
        RF1["Fallback: gemini-2.5-flash<br/><i>(CometAPI)</i>"]
        RD1["Direct Gemini<br/><i>(선택적)</i>"]
        RP1 -->|"fail"| RF1
        RF1 -->|"fail"| RD1
    end

    subgraph Main["Main Lane (응답 생성)"]
        direction TB
        MP1["Primary: DeepSeek-V3.2-Exp<br/><i>(CometAPI)</i>"]
        MF1["Fallback: DeepSeek-R1<br/><i>(CometAPI)</i>"]
        MD1["Direct Gemini<br/><i>(선택적)</i>"]
        MP1 -->|"fail"| MF1
        MF1 -->|"fail"| MD1
    end

    Caller["LLMClient"] --> Routing
    Caller --> Main

    style RP1 fill:#e3f2fd,stroke:#1565c0
    style MP1 fill:#fff8e1,stroke:#f57f17
    style RF1 fill:#e3f2fd,stroke:#90caf9
    style MF1 fill:#fff8e1,stroke:#ffb74d
```

**LLM 호출 시퀀스**:

```mermaid
sequenceDiagram
    participant Caller as AIHandler
    participant Client as LLMClient
    participant Primary as CometAPI Primary
    participant Fallback as CometAPI Fallback
    participant Gemini as Gemini Direct

    Caller->>Client: call_routing_llm(prompt, system)
    Client->>Client: _check_rate_limit(RPM/RPD)

    Client->>Primary: chat.completions.create()
    alt 성공
        Primary-->>Client: {choices: [{message: {content: "..."}}]}
        Client->>Client: _filter_prompt_leak()
        Client-->>Caller: parsed response
    else 실패
        Primary-->>Client: Exception
        Client->>Fallback: chat.completions.create()
        alt 성공
            Fallback-->>Client: response
            Client-->>Caller: parsed response
        else 실패
            opt ALLOW_DIRECT_GEMINI_FALLBACK=true
                Client->>Gemini: generate_content()
                Gemini-->>Client: response
                Client-->>Caller: parsed response
            end
        end
    end
```

### 3. 하이브리드 RAG

**문제**: 단일 검색 방식의 한계
- 의미 검색만: 키워드 정확도 부족
- 키워드 검색만: 의미 파악 불가

**해결**: BM25 + Embedding 결합

```mermaid
flowchart LR
    Query["사용자 쿼리"] --> QE["Query Expansion<br/>query_rewriter<br/><i>변형 생성</i>"]

    QE --> Parallel

    subgraph Parallel["병렬 검색"]
        direction TB
        Emb["🔍 임베딩 검색<br/>코사인 유사도<br/><i>top_n=8</i>"]
        BM["📝 BM25 검색<br/>키워드 매칭<br/><i>top_n=8</i>"]
    end

    Emb --> RRF["🔄 RRF 융합<br/>RRF score = 1/(k+rank)<br/>k=60"]
    BM --> RRF

    RRF --> Weighted["⚖️ 가중 결합<br/>embedding: 0.55<br/>bm25: 0.45"]

    Weighted --> RerankOpt{"Reranker<br/>활성화?"}

    RerankOpt -->|"yes"| Rerank["🎯 Cross-Encoder<br/>BAAI/bge-reranker-v2-m3"]
    RerankOpt -->|"no"| Results["📋 최종 결과"]

    Rerank --> Results

    style Query fill:#e1f5fe,stroke:#0288d1
    style RRF fill:#fff3e0,stroke:#e65100
    style Results fill:#c8e6c9,stroke:#2e7d32
```

```python
# 가중치
embedding_weight = 0.55  # 의미 기반
bm25_weight = 0.45       # 키워드 기반

# 최종 점수
combined_score = (similarity * 0.55) + (bm25_score * 0.45)
```

### 4. 멘션 게이트 패턴

**목표**: 리소스 낭비 방지 및 개인정보 보호

모든 메시지를 처리하면:
- ❌ 불필요한 API 호출
- ❌ 개인 대화 노출 위험
- ❌ 높은 비용

멘션만 처리하면:
- ✅ 명시적 요청만 응답
- ✅ API 비용 절감
- ✅ 프라이버시 보호

---

## 모듈 구조

### Cog 아키텍처

```mermaid
graph TB
    Bot["ReMasamongBot<br/>main.py"] --> CogLoad["Cog 로드<br/>setup_hook()"]

    CogLoad -->|"순서 1-13"| Cogs

    subgraph Cogs["Cog 레이어"]
        direction TB
        WC["WeatherCog<br/>날씨 명령어 + 알림"]
        TC["ToolsCog<br/>외부 API 도구"]
        EV["EventsCog<br/>길드/멤버 이벤트"]
        CM["Commands<br/>관리자 명령어"]
        AI["AIHandler<br/>AI 파이프라인 (핵심)"]
        FC["FunCog<br/>요약/유틸"]
        AC["ActivityCog<br/>활동/랭킹"]
        PC["PollCog<br/>투표"]
        SC["SettingsCog<br/>슬래시 설정"]
        MC["MaintenanceCog<br/>아카이빙"]
        PA["ProactiveAssistant<br/>선제적 참여"]
        FC2["FortuneCog<br/>운세/별자리"]
        HC["HelpCog<br/>도움말"]
    end

    CogLoad --> DepInject["의존성 주입"]

    DepInject -->|"LLMClient.db"| AI
    DepInject -->|"IntentAnalyzer.db"| AI
    DepInject -->|"RAGManager.db"| AI
    DepInject -->|"AIHandler → ActivityCog"| AC
    DepInject -->|"AIHandler → FunCog"| FC

    AI -->|"도구 위임"| TC
    AI -->|"도구 위임"| WC

    style AI fill:#fff3e0,stroke:#e65100,stroke-width:3px
    style TC fill:#f3e5f5,stroke:#7b1fa2
    style Bot fill:#e1f5fe,stroke:#0288d1
```

### 컴포넌트 의존성 관계

```mermaid
graph TB
    subgraph Core["핵심 컴포넌트"]
        AIHandler["AIHandler<br/><i>파이프라인 컨트롤러</i>"]
    end

    subgraph LLMLayer["LLM 레이어"]
        LLMClient["LLMClient<br/><i>레인 라우팅, Rate Limit</i>"]
        IntentAnalyzer["IntentAnalyzer<br/><i>의도 분석, 도구 계획</i>"]
    end

    subgraph RAGLayer["RAG 레이어"]
        RAGManager["RAGManager<br/><i>메모리 관리</i>"]
        HybridSearch["HybridSearchEngine<br/><i>임베딩+BM25+RRF</i>"]
        QueryRewriter["QueryRewriter"]
        Reranker["Reranker<br/><i>Cross-Encoder</i>"]
    end

    subgraph StoreLayer["저장소 레이어"]
        DiscordStore["DiscordEmbeddingStore"]
        KakaoStore["KakaoEmbeddingStore"]
        CompatDB["CompatDB<br/><i>TiDB/SQLite</i>"]
        BM25Idx["BM25IndexManager<br/><i>(비활성)</i>"]
    end

    subgraph ToolLayer["도구 레이어"]
        ToolsCog["ToolsCog"]
        Weather["weather.py"]
        LinkupSearch["linkup_search.py"]
        NewsSearch["news_search.py<br/>(DuckDuckGo)"]
        FinanceAPIs["api_handlers/<br/>finnhub, yfinance, krx"]
    end

    AIHandler --> LLMClient
    AIHandler --> IntentAnalyzer
    AIHandler --> RAGManager
    AIHandler --> HybridSearch
    AIHandler --> ToolsCog

    IntentAnalyzer --> LLMClient : "Routing Lane"

    RAGManager --> DiscordStore
    RAGManager --> CompatDB

    HybridSearch --> DiscordStore
    HybridSearch --> KakaoStore
    HybridSearch --> BM25Idx
    HybridSearch --> QueryRewriter
    HybridSearch --> Reranker

    ToolsCog --> Weather
    ToolsCog --> LinkupSearch
    ToolsCog --> NewsSearch
    ToolsCog --> FinanceAPIs
```

---

## 메시지 처리 상세 시퀀스

```mermaid
sequenceDiagram
    actor User as 👤 유저
    participant Discord as Discord
    participant Bot as ReMasamongBot
    participant Activity as ActivityCog
    participant AI as AIHandler
    participant Intent as IntentAnalyzer
    participant LLMR as LLMClient<br/>(Routing)
    participant Tools as ToolsCog
    participant RAG as RAGManager
    participant LLMM as LLMClient<br/>(Main)

    User->>Discord: "@마사몽 오늘 서울 날씨랑 애플 주가 알려줘"
    Discord->>Bot: on_message(message)

    Note over Bot: 1. 봇 메시지 무시
    Note over Bot: 2. ActivityCog 기록

    Bot->>Activity: record_message(message)
    Activity-->>Bot: done

    Note over Bot: 3. ! 프리픽스 체크 → 아님

    Bot->>AI: add_message_to_history(message)
    AI-->>Bot: 저장 완료

    Note over Bot: 4. 검증: AI 준비, 채널 허용, 멘션 유효, 사용자 잠금 해제

    Bot->>AI: process_agent_message(message)

    Note over AI: 5. 의도 분석
    AI->>Intent: analyze(query, context, history)

    Intent->>Intent: _detect_by_keywords(query)
    Note over Intent: 날씨 키워드 O<br/>주식 키워드 O

    Intent->>LLMR: call_routing_llm(분석 프롬프트)
    LLMR-->>Intent: {<br/>  analysis: "...",<br/>  tool_plan: [<br/>    {tool: "weather", params: {location: "서울"}},<br/>    {tool: "stock_us", params: {ticker: "AAPL"}}<br/>  ],<br/>  draft: "...",<br/>  self_score: {overall: 0.92}<br/>}

    Intent-->>AI: intent_result + tool_plan

    Note over AI: 6. 도구 실행 → ToolsCog 위임

    par 날씨 조회
        AI->>Tools: get_weather(location="서울")
        Tools-->>AI: {temp: 22°C, sky: "맑음", ...}
    and 주식 조회
        AI->>Tools: get_us_stock_info("AAPL")
        Tools-->>AI: {price: $182.63, change: +1.2%}
    end

    Note over AI: 7. RAG 컨텍스트 검색

    AI->>RAG: search(query, channel_id, scope)
    RAG-->>AI: RAG context (관련 대화 기억)

    Note over AI: 8. 응답 생성

    AI->>LLMM: call_main_llm(<br/>  system_prompt + persona,<br/>  tool_results + RAG_context + history<br/>)
    LLMM-->>AI: "서울은 맑고 22°C, 애플은 $182.63 (+1.2%) 🍎"

    Note over AI: 9. 응답 전송

    AI->>Discord: reply("서울은 맑고 22°C, 애플은 $182.63 (+1.2%) 🍎")

    Note over AI: 10. 임베딩 비동기 저장
    AI->>AI: asyncio.create_task(save_embedding)
```

---

## 데이터 레이어

### 데이터베이스 구조

```mermaid
erDiagram
    conversation_history {
        int message_id PK
        int guild_id
        int channel_id
        int user_id
        text user_name
        text content
        boolean is_bot
        text created_at
        blob embedding
    }

    conversation_windows {
        int window_id PK
        int guild_id
        int channel_id
        int start_message_id
        int end_message_id
        int message_count
        text messages_json
        text anchor_timestamp
        text created_at
    }

    guild_settings {
        int guild_id PK
        boolean ai_enabled
        text ai_allowed_channels
        float proactive_response_probability
        int proactive_response_cooldown
        text persona_text
        text created_at
    }

    user_profiles {
        int user_id PK
        text birth_date
        text birth_time
        text gender
        boolean is_lunar
        boolean subscription_active
        text subscription_time
        text birth_place
    }

    user_activity {
        int user_id PK
        int guild_id PK
        int message_count
        text last_active_at
    }

    user_activity_log {
        int message_id PK
        int guild_id
        int channel_id
        int user_id
        text created_at
    }

    discord_memory_entries {
        int id PK
        text memory_id UK
        text anchor_message_id
        text server_id
        text channel_id
        text owner_user_id
        text memory_scope
        text memory_type
        text summary_text
        text memory_text
        text raw_context
        blob embedding
    }

    api_call_log {
        int id PK
        text api_type
        text called_at
    }

    linkup_usage_log {
        int id PK
        text used_at
        text endpoint
        text depth
        boolean render_js
        float cost_eur
    }

    system_counters {
        text counter_name PK
        int counter_value
        text last_reset_at
    }

    conversation_history ||--o{ conversation_windows : "forms windows"
    conversation_history ||--o{ user_activity_log : "tracks activity"
    conversation_windows ||--o{ discord_memory_entries : "summarized into"
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

---

## RAG 파이프라인 상세

### 1. 쿼리 전처리

```python
# 입력: "서울 날씨"
query = "서울 날씨"
recent_messages = ["어제 비 왔어", "오늘은 어떨까"]

# 1단계: 컨텍스트 결합
seed_query = "서울 날씨 어제 비 왔어 오늘은 어떨까"

# 2단계: 쿼리 확장
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

---

## 백그라운드 태스크 아키텍처

```mermaid
sequenceDiagram
    participant WC as WeatherCog
    participant KMA as KMA API
    participant AI as AIHandler
    participant Discord as Discord
    participant MC as MaintenanceCog

    loop 10분 간격
        WC->>KMA: 초단기예보 조회
        KMA-->>WC: 강수 데이터
        alt 강수 예보 감지
            WC->>AI: 날씨 요약 요청
            AI-->>WC: 알림 메시지
            WC->>Discord: 강수 알림 전송
        end
    end

    loop 아침/저녁
        WC->>KMA: 당일 날씨 요약
        KMA-->>WC: 날씨 데이터
        WC->>AI: 인사말 + 날씨 생성
        AI-->>WC: 인사 메시지
        WC->>Discord: 인사 전송
    end

    loop 1시간 간격
        MC->>MC: archive_old_messages()
        Note over MC: 7일 이상 지난 메시지<br/>conversation_history → archive
    end
```

---

## Gemini 통신 프로토콜

### 의도 분석 프롬프트 구조

**입력 프롬프트 구조**:
```json
{
  "system": "routing_system_prompt + MENTION_GUARD",
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

---

## 성능 최적화 전략

### 1. 캐싱 계층

```mermaid
graph TB
    subgraph L1["Level 1: Python 메모리"]
        M1["임베딩 모델 (_MODEL)"]
        M2["LLM 클라이언트 인스턴스"]
        M3["설정 객체"]
    end

    subgraph L2["Level 2: SQLite/TiDB"]
        DB1["conversation_windows<br/><i>미리 계산된 윈도우</i>"]
        DB2["BM25 FTS5 인덱스"]
        DB3["임베딩 벡터"]
    end

    subgraph L3["Level 3: 디스크"]
        HF1["HuggingFace 모델 캐시<br/><i>~/.cache/huggingface</i>"]
        HF2["yfinance 캐시"]
    end

    L1 --> L2 --> L3
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

---

## 에러 처리 패턴

### 계층적 폴백

```mermaid
flowchart LR
    Try1["1순위: Primary LLM<br/><i>CometAPI</i>"]
    Try1 -->|"fail"| Try2["2순위: Fallback LLM<br/><i>CometAPI</i>"]
    Try2 -->|"fail"| Try3["3순위: Gemini Direct<br/><i>(선택적)</i>"]
    Try3 -->|"fail"| Error["에러 응답<br/>AI 서비스 이용 불가"]

    style Try1 fill:#c8e6c9,stroke:#2e7d32
    style Try2 fill:#fff9c4,stroke:#f9a825
    style Try3 fill:#ffecb3,stroke:#f57c00
    style Error fill:#ffcdd2,stroke:#c62828
```

### 웹 검색 폴백 체인

```mermaid
flowchart LR
    L["Linkup 검색<br/><i>(주력)</i>"] -->|"fail"| D["DuckDuckGo 검색<br/><i>(대체)</i>"]
    D -->|"fail"| F["도구 없는 일반 응답<br/><i>(최종 폴백)</i>"]

    style L fill:#c8e6c9,stroke:#2e7d32
    style D fill:#fff9c4,stroke:#f9a825
    style F fill:#ffecb3,stroke:#f57c00
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

---

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

# main.py → cog_list에 추가
await bot.load_extension("cogs.my_new_cog")
```

### 새 도구 추가

```python
# cogs/tools_cog.py
async def my_new_tool(self, param1: str) -> dict:
    """새로운 도구 설명"""
    result = await some_api_call(param1)
    return {"result": result}

# IntentAnalyzer > keyword sets에 키워드 추가
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

---

## 배포 아키텍처

```mermaid
graph TB
    subgraph DevEnv["🖥️ 개발 환경 (macOS)"]
        Dev["GPU Workstation<br/>CUDA 11.8"]
    end

    subgraph ProdServer["☁️ 운영 서버 (Linux CPU)"]
        Screen["screen 세션"]
        Bot["Bot Process<br/>main.py"]
        VENV["Python venv"]
    end

    subgraph Cloud["☁️ 클라우드"]
        TiDB["TiDB Cloud<br/>ap-northeast-1"]
        LLM_API["CometAPI"]
    end

    Dev -->|"git push"| Repo["GitHub"]
    ProdServer -->|"git pull"| Repo
    Screen --> Bot
    VENV --> Bot
    Bot -->|"PyMySQL :4000"| TiDB
    Bot -->|"HTTPS"| LLM_API

    subgraph Block["/mnt/block-storage/masamong/"]
        Code["app code"]
        Configs["tmp/server_config/"]
        Logs["logs/"]
    end

    Bot --> Block
```

---

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

---

## 모니터링 및 관찰성

### 로깅 계층

```mermaid
graph TB
    subgraph L1["Level 1: Console"]
        C["INFO 이상<br/>봇 시작/종료, Cog 로드, 주요 이벤트"]
    end

    subgraph L2["Level 2: File"]
        F1["discord_logs.txt<br/>DEBUG 이상"]
        F2["error_logs.txt<br/>에러 전용"]
    end

    subgraph L3["Level 3: Discord"]
        D["#logs 채널<br/>Discord 임베드 로그"]
    end

    subgraph L4["Level 4: DB"]
        DB["analytics_log<br/>운영 지표"]
    end

    L1 --> L2 --> L3 --> L4
```

### 메트릭 수집

```python
# analytics_log 테이블
{
  "event_type": "AI_INTERACTION",
  "details": {
    "model_used": "DeepSeek-V3.2-Exp",
    "rag_hits": 3,
    "latency_ms": 1250,
    "tools_used": ["get_weather"],
    "self_score": 0.92
  }
}
```

---

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

---

## 레퍼런스

| 문서 | 내용 |
|------|------|
| [UML_SPEC.md](UML_SPEC.md) | 🆕 UML 다이어그램 상세 분석 |
| [README.md](../README.md) | 프로젝트 메인 문서 |
| [QUICKSTART.md](QUICKSTART.md) | 빠른 시작 가이드 |
| [Discord.py](https://discordpy.readthedocs.io/) | Discord.py 공식 문서 |
| [Google Gemini API](https://ai.google.dev/) | Gemini API |
| [SentenceTransformers](https://www.sbert.net/) | 임베딩 모델 |
| [SQLite FTS5](https://www.sqlite.org/fts5.html) | 전문 검색 |

---

*마지막 업데이트: 2026-04-30*

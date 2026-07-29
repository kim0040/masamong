# 마사몽 아키텍처 문서

> **참고**: 더 자세한 UML 분석은 [UML_SPEC.md](UML_SPEC.ko.md)를 참조하세요.

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

### 1. 3단계 AI 파이프라인

```mermaid
flowchart TB
    Input["👤 사용자 메시지"] --> Valid[검증<br/>멘션/채널/잠금]

    Valid --> Step1["🔍 Step 1: 의미 라우팅<br/>IntentAnalyzer<br/><i>도구 + 기억 필요 + 선택적 digest</i>"]

    Step1 -->|"장기기억 필요"| RAG
    Step1 -->|"장기기억 불필요"| Step2
    Step1 -->|"도구 실행 계획"| Step2["🛠️ Step 2: 도구 실행<br/>ToolsCog"]

    subgraph Step2Detail[" "]
        direction LR
        W["날씨<br/>KMA"]
        S["웹·금융·장소 검색<br/>Linkup"]
        I["이미지<br/>CometAPI"]
    end

    Step2 --> Step2Detail
    Step2Detail --> Merge["🧩 컨텍스트 조립<br/><i>digest + 최신 원문 + 선택 RAG</i>"]
    RAG --> Merge

    Merge --> Step3["✍️ Step 3: 응답 생성<br/>LLMClient (Main Lane)<br/><i>deepseek-v4-flash</i>"]

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
flowchart TB
    subgraph Routing["Routing Lane (의도 분석)"]
        direction TB
        RP1["Primary: gpt-5.4-nano<br/><i>(CometAPI)</i>"]
        RE["장애 시 제한된 키워드 fallback"]
        RP1 -->|"provider/JSON fail"| RE
    end

    subgraph Main["Main Lane (응답 생성)"]
        direction TB
        MP1["Primary: deepseek-v4-flash<br/><i>(CometAPI)</i>"]
        ME["Bounded timeout / 명시적 실패"]
        MP1 -->|"fail"| ME
    end

    Caller["LLMClient"] --> Routing
    Caller --> Main

    style RP1 fill:#e3f2fd,stroke:#1565c0
    style MP1 fill:#fff8e1,stroke:#f57f17
    style RE fill:#ffebee,stroke:#c62828
    style ME fill:#ffebee,stroke:#c62828
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
flowchart TB
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
flowchart TB
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
        FinanceAPIs["api_handlers<br/>finnhub, yfinance, krx"]
    end

    AIHandler --> LLMClient
    AIHandler --> IntentAnalyzer
    AIHandler --> RAGManager
    AIHandler --> HybridSearch
    AIHandler --> ToolsCog

    IntentAnalyzer -->|"Routing Lane"| LLMClient

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

    Note over AI: 5. Discord history 한 번 조회
    AI->>Intent: route_tools(query, history)
    Intent->>LLMR: call_routing_lane_target(도구 계약 + 최근 대화)
    LLMR-->>Intent: {intent, needs_memory, needs_fortune_context, context_digest, tools}
    Note over Intent: 이름만 있는 신원 질문은 scoped memory 우선 후조건
    Intent-->>AI: ToolRoutingDecision

    Note over AI: 6. 도구 실행 → ToolsCog 위임

    AI->>Tools: get_weather_forecast(location="서울", day_offset=0)
    Tools-->>AI: 기상청 근거 데이터

    opt needs_memory=true
        Note over AI: 7. 오래된 기억만 선택 검색
        AI->>RAG: search(query, channel_id, user_id)
        Note over RAG: 관련도 gate + 겹치는 원문 블록 제거 + 최대 3개
        RAG-->>AI: 다양화된 관련 장기 기억
    end

    Note over AI: 8. 응답 생성

    AI->>LLMM: call_main_llm(<br/>persona + tool results + digest<br/>+ 최신 원문 + 선택 RAG<br/>+ 요청 시에만 동의된 운세)
    LLMM-->>AI: Discord 규격 최종 응답

    Note over AI: 9. 응답 전송

    AI->>Discord: reply(정규화된 응답)

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

## 의미 라우터 통신 계약

### 의도 분석 프롬프트 구조

**입력 프롬프트 구조**:
```json
도구 계약, KST 시각, 화자가 표시된 최근 대화, 현재 요청을 전달한다. 대화가 설정된
문자 임계치를 넘은 경우에만 최신 원문보다 앞선 구간을 `압축할 오래된 대화`로 함께
전달한다. RAG와 서버 페르소나는 도구 선택 입력에 넣지 않는다.
```

**출력 JSON 구조**:
```json
{
  "intent": "서울 날씨 확인",
  "needs_memory": false,
  "requires_external_evidence": true,
  "context_digest": "",
  "tools": [
    {
      "tool": "get_weather_forecast",
      "params": {"location": "서울", "day_offset": 0}
    }
  ]
}
```

`requires_external_evidence=true`이면 실행 가능한 외부 자료가 하나도 없을 때 최종
모델을 호출하지 않고 명시적 확인 실패로 종료한다. 라우터가 시장 브리핑의 도구를
누락해도 `get_market_snapshot`과 `web_search`를 최대 한 번씩 보정하며, 최신 외부
사실은 장기기억의 과거 답변과 섞지 않는다. 이는 일반 키워드 라우팅이 아니라 라우터
오판 시 허위 사실 생성을 막는 실행 후조건이다.

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

### 제한된 공급자 실패 처리

```mermaid
flowchart LR
    Try1["Primary LLM<br/><i>CometAPI</i>"]
    Try1 -->|"routing fail"| RouteFallback["제한된 로컬 라우팅 fallback"]
    Try1 -->|"main fail"| Error["명시적 오류 응답<br/>무한 재시도 없음"]

    style Try1 fill:#c8e6c9,stroke:#2e7d32
    style RouteFallback fill:#fff9c4,stroke:#f9a825
    style Error fill:#ffcdd2,stroke:#c62828
```

### 웹 검색 폴백 체인

```mermaid
flowchart LR
    L["Linkup 검색<br/><i>(주력)</i>"] -->|"fail"| D["DuckDuckGo 검색<br/><i>(대체)</i>"]
    D -->|"fail"| F["명시적 확인 실패<br/><i>추측 금지</i>"]

    style L fill:#c8e6c9,stroke:#2e7d32
    style D fill:#fff9c4,stroke:#f9a825
    style F fill:#ffecb3,stroke:#f57c00
```

### 도구 실행 실패 처리

```python
# 도구 실패는 결과에 명시한다.
# 외부 근거가 필수인 요청은 성공 자료가 없으면 main 모델을 호출하지 않는다.
# 시장 수치는 구조화된 지수 스냅샷과 최종 문장의 숫자를 다시 대조한다.
tool_results.append({"tool": tool_name, "error": public_error})
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

# IntentAnalyzer의 routing tool contract와 allowlist에 이름/파라미터를 추가
# 정상 선택은 routing LLM이 의미적으로 수행하고 keyword는 장애 fallback만 수정
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
    "model_used": "deepseek-v4-flash",
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
| [UML_SPEC.md](UML_SPEC.ko.md) | 🆕 UML 다이어그램 상세 분석 |
| [README.md](../README.md) | 프로젝트 메인 문서 |
| [QUICKSTART.md](QUICKSTART.md) | 빠른 시작 가이드 |
| [Discord.py](https://discordpy.readthedocs.io/) | Discord.py 공식 문서 |
| [Google Gemini API](https://ai.google.dev/) | Gemini API |
| [SentenceTransformers](https://www.sbert.net/) | 임베딩 모델 |
| [SQLite FTS5](https://www.sqlite.org/fts5.html) | 전문 검색 |

---

*마지막 업데이트: 2026-04-30*

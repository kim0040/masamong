# 마사몽 UML 상세 분석 문서

> **버전**: 2.0.0 | **언어**: Python 3.9+ | **작성일**: 2026-04-30

본 문서는 마사몽 Discord 봇의 소프트웨어 아키텍처를 UML 표기법과 Mermaid 다이어그램으로 상세하게 분석한 기술 문서입니다.

---

## 목차

1. [시스템 컨텍스트 다이어그램 (C4 Level 1)](#1-시스템-컨텍스트-다이어그램-c4-level-1)
2. [컨테이너 다이어그램 (C4 Level 2)](#2-컨테이너-다이어그램-c4-level-2)
3. [컴포넌트 다이어그램](#3-컴포넌트-다이어그램)
4. [클래스 다이어그램](#4-클래스-다이어그램)
5. [시퀀스 다이어그램](#5-시퀀스-다이어그램)
6. [액티비티 다이어그램](#6-액티비티-다이어그램)
7. [상태 다이어그램](#7-상태-다이어그램)
8. [배포 다이어그램](#8-배포-다이어그램)
9. [ER 다이어그램](#9-er-entity-relationship-다이어그램)

---

## 1. 시스템 컨텍스트 다이어그램 (C4 Level 1)

```mermaid
graph TB
    subgraph External["🌐 외부 시스템"]
        Discord["Discord API<br/>메시지, 이벤트, 슬래시 커맨드"]
        CometAPI["CometAPI<br/>LLM Inference<br/>(OpenAI-compatible)"]
        Gemini["Google Gemini<br/>Fallback LLM"]
        KMA["기상청 KMA API<br/>날씨/지진 정보"]
        Finnhub["Finnhub API<br/>미국 주식"]
        yfinance["yfinance<br/>주식 시세 (캐시)"]
        KRX["한국거래소 KRX<br/>국내 주식"]
        Exim["한국수출입은행<br/>환율 정보"]
        Linkup["Linkup API<br/>웹 검색"]
        Kakao["Kakao Local API<br/>장소 검색"]
    end

    subgraph Users["👤 사용자"]
        GuildUser["서버 유저<br/>(@멘션 필수)"]
        DMUser["DM 유저<br/>(멘션 불필요, 5h/30회 제한)"]
        Admin["관리자<br/>(!업데이트, !debug)"]
    end

    subgraph System["🤖 마사몽 봇 시스템"]
        Masamong["마사몽 Discord Bot<br/>v2.0.0"]
    end

    subgraph Storage["💾 저장소"]
        TiDB["TiDB Cloud<br/>(운영)"]
        SQLite["SQLite<br/>(개발)"]
    end

    GuildUser -->|"@마사몽 메시지"| Discord
    DMUser -->|"DM"| Discord
    Admin -->|"관리 명령어"| Discord
    Discord -->|"Gateway Events"| Masamong
    Masamong -->|"API 응답"| Discord

    Masamong -->|"LLM 호출"| CometAPI
    Masamong -->|"Fallback LLM"| Gemini
    Masamong -->|"날씨 조회"| KMA
    Masamong -->|"주식 조회"| Finnhub
    Masamong -->|"주식 (캐시)"| yfinance
    Masamong -->|"국내 주식"| KRX
    Masamong -->|"환율 조회"| Exim
    Masamong -->|"웹 검색"| Linkup
    Masamong -->|"장소 검색"| Kakao

    Masamong -->|"CRUD"| TiDB
    Masamong -->|"CRUD"| SQLite
```

---

## 2. 컨테이너 다이어그램 (C4 Level 2)

```mermaid
graph TB
    subgraph DiscordPlatform["Discord 플랫폼"]
        Guilds["서버 (Guilds)"]
        DMs["DM"]
    end

    subgraph BotProcess["🐍 마사몽 Bot Process (Python)"]
        direction TB

        Entrypoint["main.py<br/>ReMasamongBot"]
        Config["config.py<br/>설정 로드"]
        Logger["logger_config.py<br/>KST 로깅"]

        subgraph CogLayer["Cog 확장 레이어"]
            AIHandler["AIHandler<br/>AI 파이프라인"]
            ToolsCog["ToolsCog<br/>외부 도구"]
            WeatherCog["WeatherCog<br/>날씨/알림"]
            FortuneCog["FortuneCog<br/>운세"]
            ActivityCog["ActivityCog<br/>활동/랭킹"]
            FunCog["FunCog<br/>요약/유틸"]
            PollCog["PollCog<br/>투표"]
            SettingsCog["SettingsCog<br/>슬래시 설정"]
            EventsCog["EventsCog<br/>이벤트"]
            Maintenance["MaintenanceCog<br/>백그라운드"]
            Proactive["ProactiveAssistant<br/>선제적 참여"]
            HelpCog["HelpCog<br/>도움말"]
        end

        subgraph UtilsLayer["유틸리티 레이어"]
            LLMClient["LLMClient<br/>LLM 레인 라우팅"]
            IntentAnalyzer["IntentAnalyzer<br/>의도 분석"]
            RAGManager["RAGManager<br/>메모리 관리"]
            Embeddings["embeddings.py<br/>벡터 저장소"]
            HybridSearch["hybrid_search.py<br/>하이브리드 검색"]
            LinkupSearch["linkup_search.py<br/>웹 검색"]
            Weather["weather.py<br/>KMA 클라이언트"]
            Fortune["fortune.py<br/>운세 계산"]
        end

        subgraph ApiHandlers["API 핸들러"]
            FinnhubClient["finnhub.py"]
            YFinanceHandler["yfinance_handler.py"]
            KRXClient["krx.py"]
            ExchangeRate["exchange_rate.py"]
            KakaoClient["kakao.py"]
        end

        subgraph DBLayer["데이터베이스 레이어"]
            CompatDB["compat_db.py<br/>TiDB/SQLite 어댑터"]
            SchemaSQL["schema.sql"]
            SchemaTiDB["schema_tidb.sql"]
        end

        Entrypoint --> Config
        Entrypoint --> Logger
        Entrypoint --> CogLayer
        CogLayer --> UtilsLayer
        UtilsLayer --> ApiHandlers
        CogLayer --> DBLayer
        UtilsLayer --> DBLayer
    end

    subgraph ExternalAPIs["외부 API 서비스"]
        Comet["CometAPI"]
        GCP["Google Gemini"]
        KMAAPI["KMA"]
        FinnAPI["Finnhub"]
        Yahoo["yfinance"]
        KRXAPI["KRX"]
        EximAPI["EximBank"]
        LinkupAPI["Linkup"]
        KakaoAPI["Kakao Local"]
    end

    subgraph DataStores["데이터 저장소"]
        TiDBCloud["TiDB Cloud<br/>aws ap-northeast-1"]
        LocalSQLite["SQLite<br/>(로컬 파일)"]
        HuggingFace["HuggingFace Cache<br/>(~/.cache)"]
    end

    Guilds -->|"Gateway"| Entrypoint
    DMs -->|"Gateway"| Entrypoint

    LLMClient -->|"Inference"| Comet
    LLMClient -->|"Fallback"| GCP
    Weather -->|"날씨"| KMAAPI
    ApiHandlers -->|"금융"| FinnAPI
    ApiHandlers -->|"금융"| Yahoo
    ApiHandlers -->|"금융"| KRXAPI
    ApiHandlers -->|"금융"| EximAPI
    LinkupSearch -->|"검색"| LinkupAPI
    ApiHandlers -->|"장소"| KakaoAPI

    DBLayer -->|"운영"| TiDBCloud
    DBLayer -->|"개발"| LocalSQLite
    Embeddings -->|"모델"| HuggingFace
```

---

## 3. 컴포넌트 다이어그램

```mermaid
graph TB
    subgraph MainModule["main.py"]
        Bot["ReMasamongBot<br/>(commands.Bot)"]
        OnMessage["on_message()<br/>메시지 라우터"]
        SetupHook["setup_hook()<br/>DB + Cog 초기화"]
    end

    subgraph AIHandlerMod["cogs/ai_handler.py"]
        ProcessAgent["process_agent_message()<br/>AI 파이프라인 진입점"]
        MentionCheck["_message_has_valid_mention()<br/>멘션 검증"]
        AddHistory["add_message_to_history()<br/>대화 기록 저장"]
    end

    subgraph IntentModule["utils/intent_analyzer.py"]
        Analyze["analyze()<br/>의도 분석"]
        KeywordMatch["_detect_by_keywords()<br/>키워드 매칭"]
        LLMAnalyze["_analyze_with_llm()<br/>LLM 의도 분석"]
        ToolPlan["_build_tool_plan()<br/>도구 실행 계획"]
    end

    subgraph LLMModule["utils/llm_client.py"]
        LaneRouter["get_lane_targets()<br/>레인 타깃 선택"]
        PrimaryCall["call_primary()<br/>Primary 호출"]
        FallbackCall["call_fallback()<br/>Fallback 호출"]
        RateLimiter["_check_rate_limit()<br/>Rate Limit"]
        PromptFilter["_filter_prompt_leak()<br/>프롬프트 누출 방지"]
    end

    subgraph RAGModule["utils/rag_manager.py"]
        StoreMsg["store_message()<br/>메시지 저장"]
        BuildWindow["_build_window()<br/>윈도우 생성"]
        GenMemory["_generate_memory()<br/>구조화 메모리"]
        SearchRAG["search()<br/>RAG 검색"]
    end

    subgraph SearchModule["utils/hybrid_search.py"]
        EmbSearch["_embedding_search()<br/>임베딩 검색"]
        BM25Search["_bm25_search()<br/>BM25 검색"]
        RRF["_rrf_fusion()<br/>RRF 융합"]
        Rerank["_rerank()<br/>재순위화"]
    end

    subgraph ToolsModule["cogs/tools_cog.py"]
        WeatherTool["get_weather()"]
        StockTool["get_stock_info()"]
        WebSearch["web_search()"]
        PlaceSearch["search_for_place()"]
        ImageGen["generate_image()"]
    end

    subgraph DBModule["database/compat_db.py"]
        TiDBAdapter["TiDBConnection<br/>(PyMySQL 기반)"]
        SQLiteAdapter["aiosqlite<br/>(네이티브)"]
    end

    OnMessage --> ProcessAgent
    ProcessAgent --> MentionCheck
    ProcessAgent --> Analyze
    ProcessAgent --> SearchRAG
    ProcessAgent --> PrimaryCall
    
    Analyze --> KeywordMatch
    Analyze --> LLMAnalyze
    Analyze --> ToolPlan

    ToolPlan --> ToolsModule

    PrimaryCall --> LaneRouter
    PrimaryCall --> RateLimiter
    PrimaryCall --> PromptFilter
    LaneRouter --> FallbackCall

    SearchRAG --> EmbSearch
    SearchRAG --> BM25Search
    EmbSearch --> RRF
    BM25Search --> RRF
    RRF --> Rerank

    StoreMsg --> BuildWindow
    BuildWindow --> GenMemory

    Bot --> TiDBAdapter
    Bot --> SQLiteAdapter
```

---

## 4. 클래스 다이어그램

### 4.1 핵심 클래스 구조

```mermaid
classDiagram
    class ReMasamongBot {
        +Connection db
        +str db_path
        +set locked_users
        +_migrate_db()
        +_table_exists(table_name) bool
        +setup_hook()
        +on_message(message)
        +close()
    }

    class AIHandler {
        +Bot bot
        +LLMClient llm_client
        +IntentAnalyzer intent_analyzer
        +RAGManager rag_manager
        +DiscordEmbeddingStore discord_store
        +KakaoEmbeddingStore kakao_store
        +HybridSearchEngine search_engine
        +BM25IndexManager bm25_manager
        +Reranker reranker
        +ToolsCog tools_cog
        +bool is_ready
        +process_agent_message(message)
        +_message_has_valid_mention(message) bool
        +add_message_to_history(message)
        +_execute_tool_plan(plan) dict
        +_generate_response(context) str
        +_save_embedding(message)
    }

    class LLMClient {
        +Connection _db
        +dict _openai_clients
        +dict _gemini_compat_clients
        +bool use_cometapi
        +bool gemini_configured
        +bool debug_enabled
        +get_lane_targets(lane) list
        +can_use_direct_gemini() bool
        +call_routing_llm(prompt, system) dict
        +call_main_llm(prompt, system, history) str
        +_get_openai_client(base_url, key) AsyncOpenAI
        +_get_gemini_compat_client(base_url, key)
        +_check_rate_limit() bool
        +_filter_prompt_leak(text) str
    }

    class IntentAnalyzer {
        +Connection db
        +LLMClient llm_client
        +float auto_search_cooldown
        +analyze(query, context, history) dict
        +_detect_by_keywords(query) dict
        +_analyze_with_llm(query, context) dict
        +_build_tool_plan(intent) list
        +_needs_web_search(query, rag_score) bool
    }

    class RAGManager {
        +Connection db
        +DiscordEmbeddingStore discord_store
        +str embedding_model_name
        +int window_size
        +int stride
        +store_message(message)
        +_build_window(channel_id) bool
        +_generate_memory(window) dict
        +search(query, channel_id, scope) list
        +_compute_embedding(text) list
        +archive_old_messages(cutoff_days) int
    }

    class HybridSearchEngine {
        +SentenceTransformer model
        +float similarity_threshold
        +float strong_threshold
        +bool chunking_enabled
        +QueryRewriter rewriter
        +Reranker reranker
        +search(query, channel_id, top_n) list
        +_embedding_search(embedding, top_n) list
        +_bm25_search(tokens, top_n) list
        +_rrf_fusion(emb_results, bm25_results) list
        +_rerank(query, candidates) list
    }

    class ToolsCog {
        +Bot bot
        +AIHandler ai_handler
        +get_weather(location, date) dict
        +get_us_stock_info(ticker) dict
        +get_kr_stock_info(name) dict
        +get_exchange_rate(currencies) dict
        +search_for_place(query) dict
        +web_search(query) dict
        +generate_image(prompt) dict
        +linkup_search(query, depth) dict
    }

    class TiDBConnection {
        +PyMySQL connection
        +str backend
        +execute(query, params) cursor
        +executemany(query, params)
        +executescript(script)
        +commit()
        +close()
    }

    class DiscordEmbeddingStore {
        +Connection db
        +str table_name
        +str backend
        +store_embedding(message_id, embedding, metadata)
        +search(embedding, filters, top_n) list
        +delete(message_id)
    }

    class KakaoEmbeddingStore {
        +Connection db
        +str table_name
        +str backend
        +search(embedding, room_key, top_n) list
        +store_chunk(chunk_id, room_key, embedding, metadata)
    }

    ReMasamongBot "1" --> "1..*" AIHandler : loads as Cog
    ReMasamongBot "1" --> "1" TiDBConnection : db
    AIHandler "1" --> "1" LLMClient
    AIHandler "1" --> "1" IntentAnalyzer
    AIHandler "1" --> "1" RAGManager
    AIHandler "1" --> "1" HybridSearchEngine
    AIHandler "1" --> "0..1" ToolsCog : injected
    AIHandler "1" --> "1" DiscordEmbeddingStore
    AIHandler "1" --> "1" KakaoEmbeddingStore
    IntentAnalyzer "1" --> "1" LLMClient : uses routing lane
    RAGManager "1" --> "1" DiscordEmbeddingStore
    HybridSearchEngine "1" --> "1" DiscordEmbeddingStore
    HybridSearchEngine "1" --> "1" KakaoEmbeddingStore
```

### 4.2 Cog 의존성 관계

```mermaid
classDiagram
    class AIHandler {
        +process_agent_message()
    }
    class ToolsCog {
        +get_weather()
        +get_stock_info()
        +web_search()
    }
    class WeatherCog {
        +check_rain_alerts()
        +send_greeting()
        +check_earthquake()
    }
    class FortuneCog {
        +calculate_fortune()
        +send_morning_briefing()
    }
    class ActivityCog {
        +record_message()
        +get_ranking()
        -ai_handler
    }
    class FunCog {
        +summarize_channel()
        -ai_handler
    }
    class PollCog {
        +create_poll()
    }
    class SettingsCog {
        +set_ai()
        +set_persona()
    }
    class MaintenanceCog {
        +archive_task()
    }

    AIHandler --> ToolsCog : 도구 실행 위임
    ActivityCog --> AIHandler : injected (랭킹 연동)
    FunCog --> AIHandler : injected (요약 연동)
    WeatherCog --> ToolsCog : 날씨 도구
```

---

## 5. 시퀀스 다이어그램

### 5.1 전체 메시지 처리 흐름

```mermaid
sequenceDiagram
    actor User as 👤 유저
    participant Discord as Discord Gateway
    participant Bot as ReMasamongBot
    participant Activity as ActivityCog
    participant AI as AIHandler
    participant Intent as IntentAnalyzer
    participant Routing as LLMClient<br/>(Routing Lane)
    participant Tools as ToolsCog
    participant RAG as RAGManager
    participant Main as LLMClient<br/>(Main Lane)

    User->>Discord: 메시지 전송
    Discord->>Bot: on_message(message)

    alt 봇 메시지
        Bot-->>Bot: return (무시)
    else 사용자 메시지
        Bot->>Activity: record_message(message)
        Activity-->>Bot: 활동 기록 완료

        alt ! 커맨드
            Bot->>Bot: process_commands(message)
        else AI 메시지
            Bot->>AI: add_message_to_history(message)
            AI-->>Bot: 기록 저장 완료

            alt AI 준비 안됨
                Bot-->>Bot: return
            else 채널 비허용
                Bot-->>Bot: return
            else 멘션 없음 (서버)
                Bot-->>Bot: return
            else 사용자 잠금
                Bot-->>Bot: return
            end

            Bot->>AI: process_agent_message(message)

            AI->>Intent: analyze(query, context, history)
            Intent->>Routing: call_routing_llm(prompt, system)
            Routing-->>Intent: analysis JSON (tool_plan, draft, self_score)
            Intent-->>AI: intent analysis result

            alt 도구 필요
                loop 각 도구
                    AI->>Tools: execute_tool(tool_name, params)
                    Tools-->>AI: tool_result
                end
            end

            opt RAG 검색 필요
                AI->>RAG: search(query, channel_id)
                RAG-->>AI: RAG context
            end

            AI->>Main: call_main_llm(prompt, system, tool_results, rag_context)
            Main-->>AI: 최종 응답 텍스트

            AI->>Discord: reply(message, response)
        end
    end
```

### 5.2 듀얼 레인 LLM 라우팅 흐름

```mermaid
sequenceDiagram
    actor Caller as 호출자
    participant Client as LLMClient
    participant Config as config.py
    participant CometAPIPrimary as CometAPI<br/>(Primary)
    participant CometAPIFallback as CometAPI<br/>(Fallback)
    participant GeminiDirect as Gemini<br/>(직접 호출)

    Caller->>Client: call_routing_llm(prompt, system)
    Client->>Config: get_lane_targets("routing")
    Config-->>Client: [primary, fallback] targets

    Client->>Client: _check_rate_limit()
    alt Rate Limit 초과
        Client-->>Caller: Error: Rate limited
    end

    Client->>CometAPIPrimary: chat.completions.create(model, messages)
    alt Primary 성공
        CometAPIPrimary-->>Client: response
        Client->>Client: _filter_prompt_leak(response)
        Client-->>Caller: parsed JSON response
    else Primary 실패
        Client->>Client: log warning

        alt Fallback 설정됨
            Client->>CometAPIFallback: chat.completions.create(model, messages)
            alt Fallback 성공
                CometAPIFallback-->>Client: response
                Client-->>Caller: parsed JSON response
            else Fallback 실패
                alt 직접 Gemini 허용
                    Client->>GeminiDirect: generate_content()
                    GeminiDirect-->>Client: response
                    Client-->>Caller: parsed response
                else
                    Client-->>Caller: Error: All lanes failed
                end
            end
        else
            Client-->>Caller: Error: Primary failed, no fallback
        end
    end
```

### 5.3 RAG 검색 파이프라인

```mermaid
sequenceDiagram
    actor User as 👤 유저
    participant AI as AIHandler
    participant RAG as RAGManager
    participant Hybrid as HybridSearchEngine
    participant Emb as EmbeddingSearch
    participant BM25 as BM25Search
    participant Rewriter as QueryRewriter
    participant Reranker as Reranker

    User->>AI: 메시지 ("서울 날씨 어때?")
    AI->>RAG: search(query, channel_id, scope="channel")
    RAG->>Hybrid: search(query, channel_id, top_n=8)
    
    Hybrid->>Rewriter: expand_query(query)
    Rewriter-->>Hybrid: [원본, 변형1, 변형2]

    par 임베딩 검색 (병렬)
        Hybrid->>Emb: search(embedding, top_n=8)
        Emb-->>Hybrid: embedding_results (Top 8)
    and BM25 검색 (병렬)
        Hybrid->>BM25: search(tokens, top_n=8)
        BM25-->>Hybrid: bm25_results (Top 8)
    end

    Hybrid->>Hybrid: _rrf_fusion(emb_results, bm25_results)
    Note over Hybrid: RRF score = 1/(k + rank)<br/>k=60, embedding_weight=0.55<br/>bm25_weight=0.45

    opt Rerank 활성화
        Hybrid->>Reranker: rerank(query, fused_candidates)
        Reranker-->>Hybrid: reranked (Cross-Encoder)
    end

    Hybrid-->>RAG: 최종 검색 결과 (Top 5)
    RAG-->>AI: RAG context

    opt RAG 점수 낮고 검색어 감지
        AI->>AI: _needs_web_search(query, rag_score)
        alt 웹 검색 필요
            AI->>AI: Linkup / DuckDuckGo 검색 추가
        end
    end
```

### 5.4 외부 도구 실행 흐름

```mermaid
sequenceDiagram
    actor User as 👤 유저
    participant AI as AIHandler
    participant Intent as IntentAnalyzer
    participant Tools as ToolsCog
    participant Weather as weather.py
    participant KMA as 기상청 KMA
    participant Finance as Finnhub/ yfinance
    participant Search as Linkup/ DDG
    participant Image as CometAPI Image

    User->>AI: "애플 주가랑 내일 서울 날씨 알려줘"
    AI->>Intent: analyze(query)

    Intent->>Intent: _detect_by_keywords()
    Note over Intent: 키워드 매칭 결과:<br/>- weather: "서울", "날씨", "내일"<br/>- stock_us: "애플", "주가"

    Intent-->>AI: tool_plan: [<br/>  {tool: "weather", params: {location: "서울", date: "내일"}},<br/>  {tool: "stock_us", params: {ticker: "AAPL"}}<br/>]

    par 날씨 도구 실행
        AI->>Tools: get_weather(location="서울", date="내일")
        Tools->>Weather: get_weather_forecast("서울", "내일")
        Weather->>Weather: coords.convert_to_grid("서울")
        Weather->>KMA: VilageFcstInfoService API
        KMA-->>Weather: 기온, 강수확률, 하늘상태
        Weather-->>Tools: formatted weather data
        Tools-->>AI: weather_result
    and 주식 도구 실행
        AI->>Tools: get_us_stock_info("AAPL")
        Tools->>Finance: get_stock_data("AAPL")
        Finance-->>Tools: {price, change, volume, news}
        Tools-->>AI: stock_result
    end

    AI->>AI: 결과 취합 → Main Lane LLM 프롬프트 구성
    AI-->>User: "애플 현재 $182.63 (+1.2%)<br/>내일 서울: 맑음, 최저 12°C / 최고 22°C"
```

### 5.5 배경 알림 루프 (WeatherCog)

```mermaid
sequenceDiagram
    participant Cog as WeatherCog
    participant KMA as 기상청 KMA
    participant Discord as Discord
    participant AI as AIHandler

    loop 10분 간격
        Cog->>Cog: _check_rain_alert()

        Cog->>KMA: 초단기예보 조회 (등록된 모든 지역)
        KMA-->>Cog: 강수 확률, 강수 형태, 예상 강수량

        alt 강수 예보 감지
            Cog->>AI: 날씨 요약 생성 요청
            AI-->>Cog: 강수 알림 메시지
            Cog->>Discord: 알림 채널에 메시지 전송
        end
    end

    loop 아침/저녁
        Cog->>Cog: _send_greeting()

        alt 설정된 시간
            Cog->>KMA: 당일 날씨 요약 조회
            KMA-->>Cog: 날씨 데이터
            Cog->>AI: 인사말 + 날씨 요약 생성
            AI-->>Cog: 인사 메시지
            Cog->>Discord: 인사 채널에 전송
        end
    end

    loop 지진 모니터링
        Cog->>KMA: 지진 통보문 조회
        KMA-->>Cog: 지진 목록

        alt 국내 M≥4.0 신규 지진
            Cog->>Discord: 지진 알림 (응급)
        end
    end
```

---

## 6. 액티비티 다이어그램

### 6.1 메시지 처리 활동 흐름

```mermaid
flowchart TD
    Start([메시지 수신]) --> CheckBot{작성자가 봇?}
    CheckBot -->|Yes| End([종료])
    CheckBot -->|No| RecordActivity[활동 기록<br/>ActivityCog.record_message]

    RecordActivity --> CheckCmd{! 프리픽스?}
    CheckCmd -->|Yes| ProcessCmd[process_commands<br/>명령어 처리]
    ProcessCmd --> End

    CheckCmd -->|No| AddHistory[대화 기록 저장<br/>add_message_to_history]

    AddHistory --> CheckAIReady{AI 준비 완료?}
    CheckAIReady -->|No| End
    CheckAIReady -->|Yes| CheckChannel{채널 허용?<br/>DM은 자동 통과}
    CheckChannel -->|No| End
    CheckChannel -->|Yes| CheckMention{멘션 검증<br/>서버: @멘션 필수<br/>DM: 불필요}
    CheckMention -->|실패| End

    CheckMention -->|통과| CheckLock{사용자 잠금?<br/>(대화형 커맨드 중)}
    CheckLock -->|Yes| End
    CheckLock -->|No| ValidateInput[입력 검증 및 정제<br/>text_cleaner]

    ValidateInput --> IntentAnalysis[1단계: 의도 분석<br/>IntentAnalyzer.analyze]

    IntentAnalysis --> HasTools{도구 필요?}

    HasTools -->|Yes| ExecuteTools[2단계: 도구 실행<br/>ToolsCog 호출]
    ExecuteTools --> CollectResults[도구 결과 수집]

    HasTools -->|No| RAGSearch[RAG 컨텍스트 검색<br/>RAGManager.search]
    CollectResults --> RAGSearch

    RAGSearch --> CheckRAGScore{RAG 점수 낮음<br/>+ 검색 필요어?}

    CheckRAGScore -->|Yes| WebSearch[자동 웹 검색<br/>Linkup / DuckDuckGo]
    CheckRAGScore -->|No| BuildPrompt[프롬프트 구성<br/>페르소나 + 도구결과 + RAG]

    WebSearch --> BuildPrompt

    BuildPrompt --> GenResponse[3단계: 응답 생성<br/>Main Lane LLM 호출]

    GenResponse --> CheckSuccess{생성 성공?}
    CheckSuccess -->|No| GenFallback[Fallback LLM 시도]
    GenFallback --> CheckSuccess

    CheckSuccess -->|Yes| FormatResponse[응답 포맷팅<br/>data_formatters]
    FormatResponse --> SendMessage[Discord 메시지 전송]

    SendMessage --> SaveEmbedding[임베딩 비동기 저장<br/>asyncio.create_task]
    SaveEmbedding --> End
```

### 6.2 의도 분석 상세 활동

```mermaid
flowchart TD
    Start([의도 분석 시작]) --> KeywordMatch[키워드 기반 1차 분석]
    
    KeywordMatch --> CheckWeather{날씨 키워드?}
    CheckWeather -->|Yes| MarkWeather[weather 플래그 설정]
    CheckWeather -->|No| CheckStock{주식 키워드?}
    
    CheckStock -->|Yes| ClassifyStock{국내/해외?}
    ClassifyStock -->|해외| MarkUS[stock_us 플래그]
    ClassifyStock -->|국내| MarkKR[stock_kr 플래그]
    
    CheckStock -->|No| CheckExchange{환율 키워드?}
    CheckExchange -->|Yes| MarkExchange[exchange 플래그]
    CheckExchange -->|No| CheckFinance{금융 의도 힌트?}
    
    CheckFinance -->|Yes| MarkFinance[finance 플래그]
    CheckFinance -->|No| CheckPlace{장소 키워드?}
    
    CheckPlace -->|Yes| MarkPlace[place 플래그]
    CheckPlace -->|No| CheckWebSearch{웹 검색어?<br/>최신/뉴스/방법/왜}
    
    CheckWebSearch -->|Yes| MarkWeb[web_search 플래그]
    CheckWebSearch -->|No| CheckImage{이미지 키워드?}
    
    CheckImage -->|Yes| MarkImage[image_gen 플래그]
    CheckImage -->|No| CheckGeneral

    MarkWeather --> LLMAnalysis
    MarkUS --> LLMAnalysis
    MarkKR --> LLMAnalysis
    MarkExchange --> LLMAnalysis
    MarkFinance --> LLMAnalysis
    MarkPlace --> LLMAnalysis
    MarkWeb --> LLMAnalysis
    MarkImage --> LLMAnalysis

    subgraph LLMAnalysis["LLM 의도 분석 (Routing Lane)"]
        CheckGeneral[일반 대화 감지] --> BuildPrompt2[분석 프롬프트 구성]
        BuildPrompt2 --> CallLLM[LLM 호출<br/>gemini-3.1-flash-lite]
        CallLLM --> ParseJSON[JSON 파싱<br/>tool_plan, draft, self_score]
        ParseJSON --> ValidateScore{self_score 검증}
    end

    ValidateScore -->|통과| MergeIntent[키워드 + LLM 결과 병합]
    ValidateScore -->|실패| RetryLLM[재시도 / 키워드만 사용]
    RetryLLM --> MergeIntent

    MergeIntent --> BuildPlan[도구 실행 계획 생성]
    BuildPlan --> End([분석 완료])
```

---

## 7. 상태 다이어그램

### 7.1 봇 라이프사이클

```mermaid
stateDiagram-v2
    [*] --> Initializing: asyncio.run(main())
    
    Initializing --> ConfigLoading: main.py 실행
    ConfigLoading --> TokenCheck: config.py 로드
    
    TokenCheck --> TokenError: TOKEN 없음
    TokenError --> [*]: sys.exit(1)
    
    TokenCheck --> BotCreation: ReMasamongBot 생성
    BotCreation --> DBConnect: setup_hook() 진입
    
    DBConnect --> DBError: 연결 실패
    DBError --> [*]: 종료
    
    DBConnect --> Migration: _migrate_db()
    Migration --> CogLoading: Cog 순차 로드
    
    state CogLoading {
        [*] --> LoadWeather: weather_cog
        LoadWeather --> LoadTools: tools_cog
        LoadTools --> LoadEvents: events
        LoadEvents --> LoadCommands: commands
        LoadCommands --> LoadAI: ai_handler
        LoadAI --> LoadFun: fun_cog
        LoadFun --> LoadActivity: activity_cog
        LoadActivity --> LoadPoll: poll_cog
        LoadPoll --> LoadSettings: settings_cog
        LoadSettings --> LoadMaint: maintenance_cog
        LoadMaint --> LoadProactive: proactive_assistant
        LoadProactive --> LoadFortune: fortune_cog
        LoadFortune --> LoadHelp: help_cog
        LoadHelp --> [*]
    }

    CogLoading --> DepInjection: 의존성 주입
    DepInjection --> Ready: bot.start(token)
    
    Ready --> Running: Discord Gateway 연결 완료
    
    state Running {
        [*] --> Listening: 이벤트 대기
        Listening --> Processing: on_message 수신
        Processing --> Listening: 응답 전송 완료
        
        Listening --> BackgroundTasks: 백그라운드 태스크
        BackgroundTasks --> Listening: 알림/아카이빙
        
        Listening --> Reconnecting: 연결 끊김 감지
        Reconnecting --> Listening: 재연결 성공
    }

    Running --> ShuttingDown: KeyboardInterrupt / 오류
    ShuttingDown --> DBClose: bot.close()
    DBClose --> [*]: 프로세스 종료
```

### 7.2 AI 처리 상태

```mermaid
stateDiagram-v2
    [*] --> Idle: 대기 중
    
    Idle --> Validating: 메시지 수신
    Validating --> Idle: 검증 실패 (멘션/채널/잠금)
    
    Validating --> Analyzing: 검증 통과
    Analyzing --> ToolExecuting: 도구 필요
    Analyzing --> RAGSearching: 일반 대화
    
    ToolExecuting --> ToolError: 도구 실행 실패
    ToolError --> ToolExecuting: Fallback 도구
    
    ToolExecuting --> RAGSearching: 도구 결과 수집 완료
    
    RAGSearching --> WebSearching: RAG 부족 + 검색어 감지
    WebSearching --> Generating: 웹 검색 결과 추가
    
    RAGSearching --> Generating: RAG 컨텍스트 주입
    
    Generating --> LLMError: LLM 호출 실패
    LLMError --> Generating: Fallback LLM
    LLMError --> Idle: 모든 레인 실패
    
    Generating --> Responding: 응답 생성 완료
    Responding --> Storing: 임베딩 비동기 저장
    Storing --> Idle: 처리 완료
```

---

## 8. 배포 다이어그램

```mermaid
graph TB
    subgraph DevEnv["🖥️ 개발 환경 (macOS)"]
        DevMachine["GPU Workstation<br/>Python 3.9+<br/>CUDA 11.8<br/>SQLLite"]
        DevCache["HuggingFace Cache<br/>~/.cache/huggingface"]
        GitRepo["GitHub Repository<br/>kim0040/masamong"]
    end

    subgraph ProdServer["☁️ 운영 서버 (Linux CPU)"]
        Screen["screen 세션<br/>masamong"]
        Venv["Python venv<br/>Python 3.9+"]
        BotProcess["마사몽 Bot Process<br/>main.py"]
        LocalFS["로컬 파일시스템<br/>/mnt/block-storage"]
        
        subgraph FSStructure["저장소 구조"]
            AppCode["masamong/"]
            ServerConfig["tmp/server_config/<br/>prompts.server.json<br/>emb_config.server.json"]
            Logs["로그 파일<br/>discord_logs.txt<br/>error_logs.txt"]
        end
    end

    subgraph CloudServices["☁️ 클라우드 서비스"]
        TiDBCloud["TiDB Cloud<br/>ap-northeast-1.aws<br/>Free Tier / Dedicated"]
        CometAPI["CometAPI<br/>LLM Inference"]
        KMA_API["기상청 KMA API<br/>공공데이터포털"]
        LinkupAPI["Linkup API<br/>Web Search"]
    end

    DevMachine -->|"git push"| GitRepo
    ProdServer -->|"git pull"| GitRepo
    
    Screen --> BotProcess
    Venv --> BotProcess
    BotProcess --> LocalFS
    BotProcess --> ServerConfig
    BotProcess --> Logs

    BotProcess -->|"PyMySQL :4000"| TiDBCloud
    BotProcess -->|"HTTPS"| CometAPI
    BotProcess -->|"HTTPS"| KMA_API
    BotProcess -->|"HTTPS"| LinkupAPI

    DevCache -->|"SentenceTransformers<br/>dragonkue/multilingual-e5-small-ko-v2"| BotProcess
```

---

## 9. ER (Entity-Relationship) 다이어그램

```mermaid
erDiagram
    guild_settings {
        bigint guild_id PK "서버 ID"
        boolean ai_enabled "AI 활성화 여부"
        text ai_allowed_channels "허용 채널 JSON"
        float proactive_response_probability "선제 응답 확률"
        int proactive_response_cooldown "선제 응답 쿨다운(초)"
        text persona_text "커스텀 페르소나"
        text created_at "생성 시간"
        text updated_at "수정 시간"
    }

    user_profiles {
        bigint user_id PK "사용자 ID"
        text birth_date "생년월일 (YYYY-MM-DD)"
        text birth_time "출생 시간 (HH:MM)"
        text gender "성별 (M/F)"
        boolean is_lunar "음력 여부"
        boolean subscription_active "운세 구독 여부"
        text subscription_time "구독 발송 시간"
        text pending_payload "미리 생성된 브리핑"
        text last_fortune_sent "마지막 운세 발송일"
        text last_fortune_content "마지막 운세 내용"
        text birth_place "출생 지역"
        text created_at "생성 시간"
    }

    user_activity {
        bigint user_id PK "사용자 ID"
        bigint guild_id PK "서버 ID"
        int message_count "메시지 수"
        text last_active_at "마지막 활동 시간"
    }

    user_activity_log {
        bigint message_id PK "메시지 ID"
        bigint guild_id "서버 ID"
        bigint channel_id "채널 ID"
        bigint user_id "사용자 ID"
        text created_at "생성 시간"
    }

    conversation_history {
        bigint message_id PK "메시지 ID"
        bigint guild_id "서버 ID"
        bigint channel_id "채널 ID"
        bigint user_id "사용자 ID"
        text user_name "사용자 이름"
        text content "메시지 내용"
        boolean is_bot "봇 여부"
        text created_at "생성 시간"
        blob embedding "임베딩 벡터"
    }

    conversation_windows {
        bigint window_id PK "윈도우 ID (AUTO)"
        bigint guild_id "서버 ID"
        bigint channel_id "채널 ID"
        bigint start_message_id "시작 메시지 ID"
        bigint end_message_id "종료 메시지 ID"
        int message_count "메시지 수"
        text messages_json "메시지 JSON"
        text anchor_timestamp "기준 타임스탬프"
        text created_at "생성 시간"
    }

    conversation_history_archive {
        bigint message_id PK "메시지 ID"
        bigint guild_id "서버 ID"
        bigint channel_id "채널 ID"
        bigint user_id "사용자 ID"
        text user_name "사용자 이름"
        text content "메시지 내용"
        boolean is_bot "봇 여부"
        text created_at "생성 시간"
        blob embedding "임베딩 벡터"
    }

    discord_memory_entries {
        bigint id PK "엔트리 ID (AUTO)"
        text memory_id UK "메모리 UUID"
        text anchor_message_id "기준 메시지 ID"
        text server_id "서버 ID"
        text channel_id "채널 ID"
        text owner_user_id "소유 유저 ID"
        text owner_user_name "소유 유저 이름"
        text memory_scope "스코프 (channel/user)"
        text memory_type "메모리 타입"
        text summary_text "요약 텍스트"
        text memory_text "메모리 본문"
        text raw_context "원본 컨텍스트"
        text source_message_ids "소스 메시지 ID 목록"
        text speaker_names "발화자 목록"
        text keyword_json "키워드 JSON"
        text timestamp "타임스탬프"
        blob embedding "임베딩 벡터"
    }

    kakao_chunks {
        bigint id PK "청크 ID (AUTO)"
        text room_key "방 키"
        text source_room_label "방 라벨"
        bigint chunk_id "청크 번호"
        bigint session_id "세션 ID"
        text start_date "시작 날짜"
        int message_count "메시지 수"
        text summary "요약"
        text text_long "전체 텍스트"
        vector embedding "Vector(384)"
    }

    locations {
        text name PK "지역명"
        int nx "격자 X 좌표"
        int ny "격자 Y 좌표"
    }

    api_call_log {
        bigint id PK "로그 ID (AUTO)"
        text api_type "API 타입"
        text called_at "호출 시간"
    }

    system_counters {
        text counter_name PK "카운터명"
        bigint counter_value "카운터 값"
        text last_reset_at "마지막 리셋 시간"
    }

    analytics_log {
        bigint log_id PK "로그 ID (AUTO)"
        text log_timestamp "로그 시간"
        text event_type "이벤트 타입"
        text guild_id "서버 ID"
        text user_id "사용자 ID"
        text details "상세 정보 JSON"
    }

    linkup_usage_log {
        bigint id PK "로그 ID (AUTO)"
        text used_at "사용 시간"
        text endpoint "엔드포인트 (search/fetch)"
        text depth "검색 깊이"
        boolean render_js "JS 렌더링 여부"
        float cost_eur "비용 (EUR)"
    }

    dm_usage_logs {
        bigint user_id PK "사용자 ID"
        int usage_count "사용 횟수"
        text window_start_at "윈도우 시작 시간"
        text reset_at "리셋 예정 시간"
    }

    user_preferences {
        bigint user_id PK "사용자 ID"
        text preference_type PK "설정 타입"
        text preference_value "설정 값"
        text updated_at "수정 시간"
    }

    discord_chat_embeddings {
        bigint id PK "엔트리 ID (AUTO)"
        text message_id UK "메시지 ID"
        text server_id "서버 ID"
        text channel_id "채널 ID"
        text user_id "사용자 ID"
        text user_name "사용자 이름"
        text message "메시지 내용"
        text timestamp "타임스탬프"
        blob embedding "임베딩 벡터"
    }

    %% Relations
    guild_settings ||--o{ user_activity : "has members in"
    user_profiles ||--o| user_preferences : "has"
    conversation_history ||--o{ conversation_windows : "forms"
    conversation_history ||--o{ conversation_history_archive : "archived to"
    conversation_history ||--o{ user_activity_log : "tracks"
    conversation_windows ||--o{ discord_memory_entries : "summarized into"
    conversation_history ||--o{ discord_chat_embeddings : "embedded as"
```

---

## 부록: 주요 데이터 흐름 요약

```mermaid
flowchart LR
    subgraph Input["📥 입력"]
        DiscordMsg["Discord Message"]
        SlashCmd["Slash Command"]
        TextCmd["! Command"]
    end

    subgraph Pipeline["⚙️ 파이프라인"]
        direction TB
        A[Message Router<br/>on_message] --> B[Intent<br/>Analysis]
        B --> C[Tool<br/>Execution]
        C --> D[Response<br/>Generation]
    end

    subgraph Knowledge["🧠 지식"]
        RAGStore["RAG<br/>conversation_history<br/>discord_memory_entries"]
        KakaoStore["Kakao<br/>kakao_chunks"]
        WebResults["Web Search<br/>Linkup / DDG"]
    end

    subgraph Output["📤 출력"]
        TextResponse["텍스트 응답"]
        ImageResponse["이미지 생성"]
        ChartResponse["차트 (matplotlib)"]
    end

    subgraph Monitoring["📊 모니터링"]
        APILog["api_call_log"]
        Analytics["analytics_log"]
        DiscordLog["Discord #logs 채널"]
    end

    Input --> Pipeline
    Pipeline --> Knowledge
    Knowledge --> Pipeline
    Pipeline --> Output
    Pipeline --> Monitoring
```

---

> **문서 업데이트**: 마지막 갱신 2026-04-30  
> **참조**: 이 문서는 [ARCHITECTURE.md](ARCHITECTURE.md), [README.md](../README.md)와 함께 읽는 것을 권장합니다.

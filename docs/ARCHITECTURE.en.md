# Masamong Architecture Document

> **See also**: [UML_SPEC.md](UML_SPEC.md) for detailed UML diagrams and technical analysis.

## System Overview

Masamong is a modular Discord bot combining an AI agent, RAG system, and external API integrations.

---

## System Context Diagram

```mermaid
graph TB
    subgraph Users["👤 Users"]
        GU["Guild User<br/>@mention required"]
        DM["DM User<br/>5h / 30 calls limit"]
        AD["Admin<br/>!update, !debug"]
    end

    subgraph Discord["Discord Platform"]
        Gateway["Discord Gateway<br/>WebSocket + HTTP"]
    end

    subgraph BotProcess["🤖 Masamong Bot Process"]
        Entry["main.py<br/>ReMasamongBot"]
    end

    subgraph ExternalAPIs["🌐 External APIs"]
        LLM["CometAPI / Gemini<br/>LLM Inference"]
        KMA_["KMA (기상청)<br/>Weather / Earthquake"]
        Finance_["Finnhub / yfinance / KRX<br/>Financial Data"]
        Web_["Linkup / DuckDuckGo<br/>Web Search"]
        Place_["Kakao Local<br/>Place Search"]
    end

    subgraph Storage["💾 Storage"]
        TiDB["TiDB Cloud<br/>(Production)"]
        SQLiteDB["SQLite<br/>(Development)"]
        HF["HuggingFace Cache<br/>Embedding Models"]
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

## Core Design Principles

### 1. 3-Stage AI Pipeline

```mermaid
flowchart TB
    Input["👤 User Message"] --> Valid["Validation<br/>mention / channel / lock"]

    Valid --> Step1["🔍 Stage 1: Intent Analysis<br/>IntentAnalyzer<br/><i>Keyword Heuristics + LLM</i>"]

    Step1 -->|"no tools needed"| RAG
    Step1 -->|"tool plan"| Step2["🛠️ Stage 2: Tool Execution<br/>ToolsCog"]

    subgraph Step2Detail[" "]
        direction LR
        W["Weather<br/>KMA"]
        F["Finance<br/>Finnhub/yfinance"]
        S["Web Search<br/>Linkup/DDG"]
        P["Place<br/>Kakao"]
        I["Image<br/>CometAPI"]
    end

    Step2 --> Step2Detail
    Step2Detail --> RAG["🧠 RAG Context Search<br/>HybridSearchEngine<br/><i>Embedding + BM25 + RRF</i>"]

    RAG --> Step3["✍️ Stage 3: Response Generation<br/>LLMClient (Main Lane)<br/><i>DeepSeek-V3.2-Exp</i>"]

    Step3 --> Output["💬 Discord Reply<br/><i>Persona + Emoji applied</i>"]

    style Valid fill:#ffecb3,stroke:#f57c00
    style Step1 fill:#e1f5fe,stroke:#0288d1
    style Step2 fill:#f3e5f5,stroke:#7b1fa2
    style RAG fill:#e8f5e9,stroke:#388e3c
    style Step3 fill:#fff3e0,stroke:#e65100
    style Output fill:#c8e6c9,stroke:#2e7d32
```

### 2. Dual-Lane LLM Routing

```mermaid
flowchart TB
    subgraph Routing["Routing Lane (Intent Analysis)"]
        direction TB
        RP1["Primary: gemini-3.1-flash-lite<br/><i>(CometAPI)</i>"]
        RF1["Fallback: gemini-2.5-flash<br/><i>(CometAPI)</i>"]
        RD1["Direct Gemini<br/><i>(optional)</i>"]
        RP1 -->|"fail"| RF1
        RF1 -->|"fail"| RD1
    end

    subgraph Main["Main Lane (Response Generation)"]
        direction TB
        MP1["Primary: DeepSeek-V3.2-Exp<br/><i>(CometAPI)</i>"]
        MF1["Fallback: DeepSeek-R1<br/><i>(CometAPI)</i>"]
        MD1["Direct Gemini<br/><i>(optional)</i>"]
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

**LLM Call Sequence**:

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
    alt success
        Primary-->>Client: {choices: [{message: {content: "..."}}]}
        Client->>Client: _filter_prompt_leak()
        Client-->>Caller: parsed response
    else failure
        Primary-->>Client: Exception
        Client->>Fallback: chat.completions.create()
        alt success
            Fallback-->>Client: response
            Client-->>Caller: parsed response
        else failure
            opt ALLOW_DIRECT_GEMINI_FALLBACK=true
                Client->>Gemini: generate_content()
                Gemini-->>Client: response
                Client-->>Caller: parsed response
            end
        end
    end
```

### 3. Hybrid RAG

```mermaid
flowchart LR
    Query["User Query"] --> QE["Query Expansion<br/>query_rewriter<br/><i>variant generation</i>"]

    QE --> Parallel

    subgraph Parallel["Parallel Search"]
        direction TB
        Emb["🔍 Embedding Search<br/>cosine similarity<br/><i>top_n=8</i>"]
        BM["📝 BM25 Search<br/>keyword matching<br/><i>top_n=8</i>"]
    end

    Emb --> RRF["🔄 RRF Fusion<br/>score = 1/(k+rank)<br/>k=60"]
    BM --> RRF

    RRF --> Weighted["⚖️ Weighted Combination<br/>embedding: 0.55<br/>bm25: 0.45"]

    Weighted --> RerankOpt{"Reranker<br/>enabled?"}

    RerankOpt -->|"yes"| Rerank["🎯 Cross-Encoder<br/>BAAI/bge-reranker-v2-m3"]
    RerankOpt -->|"no"| Results["📋 Final Results"]

    Rerank --> Results

    style Query fill:#e1f5fe,stroke:#0288d1
    style RRF fill:#fff3e0,stroke:#e65100
    style Results fill:#c8e6c9,stroke:#2e7d32
```

### 4. Mention Gate Pattern

**Goal**: Prevent resource waste and protect privacy.

Processing all messages would mean:
- ❌ Unnecessary API calls
- ❌ Risk of exposing private conversations
- ❌ High costs

Processing mentions only ensures:
- ✅ Respond only to explicit requests
- ✅ Reduced API costs
- ✅ Privacy protection

---

## Module Structure

### Cog Architecture

```mermaid
flowchart TB
    Bot["ReMasamongBot<br/>main.py"] --> CogLoad["Cog Loading<br/>setup_hook()"]

    CogLoad -->|"ordered 1-13"| Cogs

    subgraph Cogs["Cog Layer"]
        direction TB
        WC["WeatherCog<br/>Weather + alerts"]
        TC["ToolsCog<br/>External API tools"]
        EV["EventsCog<br/>Guild/member events"]
        CM["Commands<br/>Admin commands"]
        AI["AIHandler<br/>AI pipeline (core)"]
        FC["FunCog<br/>Summary / utils"]
        AC["ActivityCog<br/>Activity / ranking"]
        PC["PollCog<br/>Polls"]
        SC["SettingsCog<br/>Slash commands"]
        MC["MaintenanceCog<br/>Archiving"]
        PA["ProactiveAssistant<br/>Proactive participation"]
        FC2["FortuneCog<br/>Fortune / zodiac"]
        HC["HelpCog<br/>Help"]
    end

    CogLoad --> DepInject["Dependency Injection"]

    DepInject -->|"LLMClient.db"| AI
    DepInject -->|"IntentAnalyzer.db"| AI
    DepInject -->|"RAGManager.db"| AI
    DepInject -->|"AIHandler → ActivityCog"| AC
    DepInject -->|"AIHandler → FunCog"| FC

    AI -->|"tool delegation"| TC
    AI -->|"tool delegation"| WC

    style AI fill:#fff3e0,stroke:#e65100,stroke-width:3px
    style TC fill:#f3e5f5,stroke:#7b1fa2
    style Bot fill:#e1f5fe,stroke:#0288d1
```

### Component Dependency

```mermaid
flowchart TB
    subgraph Core["Core Components"]
        AIHandler["AIHandler<br/><i>Pipeline Controller</i>"]
    end

    subgraph LLMLayer["LLM Layer"]
        LLMClient["LLMClient<br/><i>Lane Routing, Rate Limit</i>"]
        IntentAnalyzer["IntentAnalyzer<br/><i>Intent Analysis, Tool Planning</i>"]
    end

    subgraph RAGLayer["RAG Layer"]
        RAGManager["RAGManager<br/><i>Memory Management</i>"]
        HybridSearch["HybridSearchEngine<br/><i>Embedding + BM25 + RRF</i>"]
        QueryRewriter["QueryRewriter"]
        Reranker["Reranker<br/><i>Cross-Encoder</i>"]
    end

    subgraph StoreLayer["Storage Layer"]
        DiscordStore["DiscordEmbeddingStore"]
        KakaoStore["KakaoEmbeddingStore"]
        CompatDB["CompatDB<br/><i>TiDB/SQLite</i>"]
        BM25Idx["BM25IndexManager<br/><i>(inactive)</i>"]
    end

    subgraph ToolLayer["Tool Layer"]
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

## Message Processing Sequence

```mermaid
sequenceDiagram
    actor User as 👤 User
    participant Discord as Discord
    participant Bot as ReMasamongBot
    participant Activity as ActivityCog
    participant AI as AIHandler
    participant Intent as IntentAnalyzer
    participant LLMR as LLMClient<br/>(Routing)
    participant Tools as ToolsCog
    participant RAG as RAGManager
    participant LLMM as LLMClient<br/>(Main)

    User->>Discord: "@Masamong weather in Seoul and Apple stock"
    Discord->>Bot: on_message(message)

    Note over Bot: 1. Ignore bot messages
    Note over Bot: 2. Record via ActivityCog

    Bot->>Activity: record_message(message)
    Activity-->>Bot: done

    Note over Bot: 3. Check for ! prefix → not command

    Bot->>AI: add_message_to_history(message)
    AI-->>Bot: saved

    Note over Bot: 4. Validate: AI ready, channel allowed, mention valid, user unlocked

    Bot->>AI: process_agent_message(message)

    Note over AI: 5. Intent Analysis
    AI->>Intent: analyze(query, context, history)

    Intent->>Intent: _detect_by_keywords(query)
    Note over Intent: weather keywords: ✅<br/>stock keywords: ✅

    Intent->>LLMR: call_routing_llm(analysis prompt)
    LLMR-->>Intent: {analysis, tool_plan, draft, self_score}

    Intent-->>AI: intent_result + tool_plan

    Note over AI: 6. Tool Execution → delegate to ToolsCog

    par Weather query
        AI->>Tools: get_weather(location="Seoul")
        Tools-->>AI: {temp: 22°C, sky: "clear"}
    and Stock query
        AI->>Tools: get_us_stock_info("AAPL")
        Tools-->>AI: {price: $182.63, change: +1.2%}
    end

    Note over AI: 7. RAG Context Search

    AI->>RAG: search(query, channel_id, scope)
    RAG-->>AI: RAG context (relevant conversation memory)

    Note over AI: 8. Response Generation

    AI->>LLMM: call_main_llm(<br/>  system + persona,<br/>  tool_results + RAG + history<br/>)
    LLMM-->>AI: "Seoul is clear and 22°C. Apple at $182.63 (+1.2%)"

    Note over AI: 9. Send Response

    AI->>Discord: reply("Seoul is clear and 22°C. Apple at $182.63 (+1.2%)")

    Note over AI: 10. Async embedding save
    AI->>AI: asyncio.create_task(save_embedding)
```

---

## Data Layer

### Database Structure

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
    }

    user_profiles {
        int user_id PK
        text birth_date
        text birth_time
        text gender
        boolean is_lunar
        boolean subscription_active
        text subscription_time
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
        text server_id
        text channel_id
        text memory_scope
        text memory_type
        text summary_text
        text memory_text
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

### Conversation Window Caching

**Goal**: Optimize RAG performance

Naive approach:
```sql
-- Query ±3 messages every time (slow)
SELECT * FROM conversation_history 
WHERE message_id BETWEEN (target_id - 3) AND (target_id + 3)
```

Masamong approach:
```sql
-- Query pre-computed windows (fast)
SELECT messages_json FROM conversation_windows 
WHERE start_message_id <= target_id 
  AND end_message_id >= target_id
```

**Performance improvement**: 3~5x

---

## RAG Pipeline Details

### 1. Query Preprocessing

```python
# Input: "weather Seoul"
query = "weather Seoul"
recent_messages = ["it rained yesterday", "what about today"]

# Step 1: Context combination
seed_query = "weather Seoul it rained yesterday what about today"

# Step 2: Query expansion
variants = [
    "weather Seoul",
    "weather Seoul it rained yesterday what about today",
    "current weather information for Seoul",  # generated variant
]
```

### 2. Parallel Search

```python
# Run BM25 + embedding simultaneously for each variant
for variant in variants:
    embedding_results = await embedding_search(variant, top_n=8)
    bm25_results = await bm25_search(variant, top_n=8)
```

### 3. RRF (Reciprocal Rank Fusion)

```python
def calculate_rrf_score(rank: int, k: int = 60) -> float:
    return 1.0 / (k + rank)

# Example
# embedding rank 1 → rrf_score = 1/(60+1) = 0.0164
# BM25 rank 3 → rrf_score = 1/(60+3) = 0.0159
```

### 4. Weighted Combination

```python
# When a candidate appears in both searches
combined_score = (
    similarity * 0.55 +        # semantic similarity
    bm25_normalized * 0.45     # keyword matching
)
```

### 5. Re-ranking (Optional)

```python
if RERANK_ENABLED:
    # Cross-Encoder for precision
    reranked = cross_encoder.rank(query, candidates)
    return reranked[:top_k]
```

---

## Background Tasks

```mermaid
sequenceDiagram
    participant WC as WeatherCog
    participant KMA as KMA API
    participant AI as AIHandler
    participant Discord as Discord
    participant MC as MaintenanceCog

    loop Every 10 min
        WC->>KMA: Fetch ultra-short-term forecast
        KMA-->>WC: precipitation data
        alt Precipitation detected
            WC->>AI: Request weather summary
            AI-->>WC: Alert message
            WC->>Discord: Send precipitation alert
        end
    end

    loop Morning / Evening
        WC->>KMA: Fetch daily weather summary
        KMA-->>WC: weather data
        WC->>AI: Generate greeting + weather
        AI-->>WC: Greeting message
        WC->>Discord: Send greeting
    end

    loop Every 1 hour
        MC->>MC: archive_old_messages()
        Note over MC: Messages older than 7 days<br/>conversation_history → archive
    end
```

---

## Performance Optimization

### 1. Caching Layers

```mermaid
graph TB
    subgraph L1["Level 1: Python Memory"]
        M1["Embedding Model (_MODEL)"]
        M2["LLM Client Instances"]
        M3["Config Objects"]
    end

    subgraph L2["Level 2: SQLite / TiDB"]
        DB1["conversation_windows<br/><i>pre-computed</i>"]
        DB2["BM25 FTS5 Index"]
        DB3["Embedding Vectors"]
    end

    subgraph L3["Level 3: Disk"]
        HF1["HuggingFace Model Cache<br/><i>~/.cache/huggingface</i>"]
        HF2["yfinance Cache"]
    end

    L1 --> L2 --> L3
```

### 2. Async Processing

**Message embedding**:
```python
# Prevent main thread blocking
asyncio.create_task(
    self._create_and_save_embedding(message)
)
```

**Parallel API calls**:
```python
# Concurrent API calls
results = await asyncio.gather(
    get_weather(),
    get_stock_info(),
    web_search(),
    return_exceptions=True
)
```

### 3. Index Optimization

```sql
-- conversation_windows composite index
CREATE INDEX idx_conversation_windows_channel 
ON conversation_windows (channel_id, anchor_timestamp DESC);

-- Unique constraint prevents duplicates
CREATE UNIQUE INDEX idx_conversation_windows_span 
ON conversation_windows (channel_id, start_message_id, end_message_id);
```

---

## Error Handling Patterns

### Layered Fallback

```mermaid
flowchart LR
    Try1["1st: Primary LLM<br/><i>CometAPI</i>"]
    Try1 -->|"fail"| Try2["2nd: Fallback LLM<br/><i>CometAPI</i>"]
    Try2 -->|"fail"| Try3["3rd: Gemini Direct<br/><i>(optional)</i>"]
    Try3 -->|"fail"| Error["Error Response<br/>AI service unavailable"]

    style Try1 fill:#c8e6c9,stroke:#2e7d32
    style Try2 fill:#fff9c4,stroke:#f9a825
    style Try3 fill:#ffecb3,stroke:#f57c00
    style Error fill:#ffcdd2,stroke:#c62828
```

### Web Search Fallback Chain

```mermaid
flowchart LR
    L["Linkup Search<br/><i>(primary)</i>"] -->|"fail"| D["DuckDuckGo Search<br/><i>(fallback)</i>"]
    D -->|"fail"| F["Plain response without tools<br/><i>(final fallback)</i>"]

    style L fill:#c8e6c9,stroke:#2e7d32
    style D fill:#fff9c4,stroke:#f9a825
    style F fill:#ffecb3,stroke:#f57c00
```

### Tool Execution Failure

```python
# Auto-add web search on tool failure
if tool_execution_failed:
    tool_plan.append({
        "tool_name": "web_search",
        "parameters": {"query": original_query}
    })
```

---

## Extensibility

### Adding a New Cog

```python
# cogs/my_new_cog.py
class MyNewCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    
    @commands.command()
    async def my_command(self, ctx):
        await ctx.send("Hello!")

# main.py → add to cog_list
await bot.load_extension("cogs.my_new_cog")
```

### Adding a New Tool

```python
# cogs/tools_cog.py
async def my_new_tool(self, param1: str) -> dict:
    """New tool description"""
    result = await some_api_call(param1)
    return {"result": result}

# IntentAnalyzer → add keywords to keyword sets
# AIHandler auto-discovers and uses the tool
```

### Adding a New Embedding Source

```python
# emb_config.json
{
  "kakao_servers": [
    {
      "server_id": "new_source_123",
      "db_path": "database/new_source_embeddings.db",
      "label": "New Data Source"
    }
  ]
}
```

---

## Deployment Architecture

```mermaid
graph TB
    subgraph DevEnv["🖥️ Development (macOS)"]
        Dev["GPU Workstation<br/>CUDA 11.8"]
    end

    subgraph ProdServer["☁️ Production Server (Linux CPU)"]
        Screen["screen session"]
        Bot["Bot Process<br/>main.py"]
        VENV["Python venv"]
    end

    subgraph Cloud["☁️ Cloud Services"]
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

## Security Considerations

### 1. Mention Gate

- Auto-injected mention policy in all prompts
- Double-checked at code level

### 2. API Key Management

```python
# ❌ No hardcoding
GEMINI_API_KEY = "AIza..."

# ✅ Environment variables
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
```

### 3. Rate Limiting

```python
# DB-based API call limit
async def check_rate_limit(api_type: str) -> bool:
    recent_calls = await db.count_recent_calls(
        api_type, 
        window_minutes=60
    )
    return recent_calls < config.RPM_LIMIT
```

### 4. Input Validation

```python
# User input sanitization
cleaned_query = re.sub(r'[<>\"\'`]', '', user_query)
```

---

## Monitoring & Observability

### Logging Layers

```mermaid
graph TB
    subgraph L1["Level 1: Console"]
        C["INFO+<br/>Bot start/stop, Cog loading, key events"]
    end

    subgraph L2["Level 2: File"]
        F1["discord_logs.txt<br/>DEBUG+"]
        F2["error_logs.txt<br/>Errors only"]
    end

    subgraph L3["Level 3: Discord"]
        D["#logs channel<br/>Discord embed logs"]
    end

    subgraph L4["Level 4: DB"]
        DB["analytics_log<br/>Operational metrics"]
    end

    L1 --> L2 --> L3 --> L4
```

### Metrics Collection

```python
# analytics_log table
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

## Deployment Considerations

### Low-end Server

**Recommended specs**:
- CPU: 2 Core
- RAM: 2 GB
- Disk: 5 GB

**Optimization**:
```env
AI_MEMORY_ENABLED=false
RERANK_ENABLED=false
SEARCH_CHUNKING_ENABLED=false
CONVERSATION_WINDOW_SIZE=3
```

### High-performance Server

**Recommended specs**:
- CPU: 4+ Core
- RAM: 8 GB+
- Disk: 20 GB+
- GPU: Optional (CUDA 11.8+)

**Optimization**:
```env
AI_MEMORY_ENABLED=true
RERANK_ENABLED=true
SEARCH_CHUNKING_ENABLED=true
LOCAL_EMBEDDING_DEVICE=cuda
BM25_AUTO_REBUILD_ENABLED=true
```

---

## References

| Document | Content |
|----------|---------|
| [UML_SPEC.md](UML_SPEC.md) | Detailed UML diagrams & technical analysis |
| [README.en.md](README.en.md) | English project overview |
| [QUICKSTART.md](QUICKSTART.md) | 5-minute quick start guide |
| [Discord.py](https://discordpy.readthedocs.io/) | Discord.py official docs |
| [Google Gemini API](https://ai.google.dev/) | Gemini API |
| [SentenceTransformers](https://www.sbert.net/) | Embedding models |
| [SQLite FTS5](https://www.sqlite.org/fts5.html) | Full-text search |

---

*Last updated: 2026-04-30*

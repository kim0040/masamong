# Masamong UML Specification

These diagrams represent the current implementation. BM25 is intentionally
absent from the production runtime; retrieval is semantic. For feature use,
see the [Korean user guide](README.ko.md); for the component-level explanation,
see [Architecture](ARCHITECTURE.en.md).

## Components

```mermaid
flowchart TB
    Discord["Discord Gateway / REST"]
    Bot["MasamongBot"]
    AI["AIHandler"]
    Intent["IntentAnalyzer"]
    LLM["LLMClient"]
    Tools["ToolsCog"]
    Health["ToolHealthRegistry"]
    RAG["RAGManager"]
    Search["HybridSearchEngine<br/>semantic production path"]
    Feature["Privacy / Weather / Fortune / Notice Cogs"]
    DB["TiDBConnection"]
    TiDB[("Profile TiDB")]
    External["KMA / Search / Market / CometAPI"]
    Batch["School and transfer one-shots"]
    Local[("Masamo-only notice stores")]

    Discord --> Bot
    Bot --> AI
    Bot --> Feature
    AI --> Intent
    AI --> LLM
    AI --> Tools
    AI --> RAG
    Tools --> Health
    Tools --> External
    RAG --> Search
    RAG --> DB
    LLM --> DB
    Feature --> DB
    DB --> TiDB
    Batch --> Local
    Feature --> Local
```

## Core classes

```mermaid
classDiagram
    class MasamongBot {
        +db
        +setup_hook()
        +on_ready()
        +close()
    }
    class AIHandler {
        +process_agent_message(message)
        -_route_tools(query, history)
        -_get_rag_context(...)
        -_execute_tool(...)
        -_compose_main_prompt(...)
    }
    class IntentAnalyzer {
        +analyze(query, history)
        -_sanitize_tool_plan(...)
        -_detect_tools_by_keyword(query)
    }
    class LLMClient {
        +generate_content(...)
        +fast_generate_text(...)
        -_run_bounded_provider_call(...)
        -_reserve_request_budget(...)
    }
    class ToolsCog {
        +execute_tool(...)
        +execute_guarded(...)
        +result_has_external_evidence(...)
    }
    class ToolHealthRegistry {
        +begin_attempt(provider)
        +record_success(provider)
        +record_failure(provider)
        +abandon_attempt(provider)
    }
    class RAGManager {
        +add_message_to_history(message)
        +search_memory(...)
        +close()
    }
    class HybridSearchEngine {
        +search(...)
        -_embedding_candidates(...)
        -_gate_by_relevance(...)
        -_dedupe_overlapping_entries(...)
    }
    class TiDBConnection {
        +execute(sql, params)
        +commit()
        +rollback()
        -_enter_transaction_gate(...)
        -_rollback_abandoned_transaction(...)
    }

    MasamongBot --> AIHandler
    AIHandler --> IntentAnalyzer
    AIHandler --> LLMClient
    AIHandler --> ToolsCog
    AIHandler --> RAGManager
    ToolsCog --> ToolHealthRegistry
    RAGManager --> HybridSearchEngine
    RAGManager --> TiDBConnection
    LLMClient --> TiDBConnection
```

## Conversation

```mermaid
sequenceDiagram
    actor User
    participant Discord
    participant AI
    participant Intent
    participant RAG
    participant Tools
    participant LLM

    User->>Discord: mention or DM
    Discord->>AI: process_agent_message
    AI->>AI: cooldown, spam, DM quota
    AI->>Discord: progress message
    AI->>Discord: read recent history once
    AI->>Intent: query + selected history
    Intent->>LLM: one routing call
    LLM-->>Intent: structured decision
    opt explicit or shallow memory
        AI->>RAG: scoped semantic retrieval
        RAG-->>AI: gated blocks
    end
    loop normalized plan, maximum 3
        AI->>Tools: execute
        Tools-->>AI: evidence or error
    end
    alt direct evidence renderer
        AI->>AI: deterministic rendering
    else synthesis
        AI->>LLM: bounded main call
        LLM-->>AI: response
    end
    AI->>Discord: normalize, edit, split
```

## Retrieval

```mermaid
sequenceDiagram
    participant AI
    participant Search
    participant DiscordStore
    participant KakaoStore
    participant Reranker

    AI->>Search: query + guild/channel/user scope
    Search->>Search: variants; shallow uses one
    par Discord
        Search->>DiscordStore: vector or bounded compatibility scan
        DiscordStore-->>Search: candidates
    and mapped Kakao
        Search->>KakaoStore: scoped query
        KakaoStore-->>Search: candidates
    end
    Search->>Search: identity alignment
    Search->>Search: absolute semantic gate
    Search->>Search: relative floor and overlap dedupe
    opt enabled
        Search->>Reranker: reorder gated candidates
    end
    Search-->>AI: maximum 3 blocks
```

## Evidence tool

```mermaid
sequenceDiagram
    participant AI
    participant Tools
    participant Health
    participant Provider
    participant LLM

    AI->>Tools: requested tool
    Tools->>Health: begin_attempt
    alt circuit open
        Health-->>Tools: reject
        Tools-->>AI: unavailable error
    else admitted
        Tools->>Provider: bounded request
        alt evidence success
            Provider-->>Tools: payload
            Tools->>Health: record_success
            Tools-->>AI: evidence
        else failure/cancellation
            Tools->>Health: record_failure / abandon_attempt
            Tools-->>AI: error
        end
    end
    alt external fact without evidence
        AI->>AI: block unsupported claim
    else verified
        AI->>LLM: evidence-bound synthesis
    end
```

## LLM state

```mermaid
stateDiagram-v2
    [*] --> Budget
    Budget --> Blocked: quota/storage failure
    Budget --> Admission: reserved
    Admission --> Saturated: wait timeout
    Admission --> Calling: slot acquired
    Calling --> Success
    Calling --> Failed
    Calling --> TimedOut
    Failed --> Fallback: configured and non-timeout
    Fallback --> Success
    Fallback --> Failed
    TimedOut --> [*]: no overlapping fallback
    Saturated --> [*]
    Blocked --> [*]
    Success --> [*]
    Failed --> [*]
```

## TiDB transaction ownership

```mermaid
sequenceDiagram
    participant A as Task A
    participant DB
    participant B as Task B
    participant TiDB

    A->>DB: first write
    DB->>DB: gate owner = A
    DB->>TiDB: execute
    B->>DB: query
    Note over B,DB: waits outside A transaction
    A->>DB: further write
    alt normal
        A->>DB: commit
        DB->>TiDB: COMMIT
    else cancellation/exit
        DB->>TiDB: automatic ROLLBACK
    end
    DB-->>B: gate released
```

## Consent interaction

```mermaid
sequenceDiagram
    actor User
    participant Discord
    participant View
    participant DB
    participant Feature

    User->>Discord: press consent
    Discord->>View: interaction
    View->>Discord: defer ephemeral response
    View->>DB: record current policy consent
    DB-->>View: commit
    View->>Discord: follow-up
    opt original action
        View->>Feature: resume once
    end
```

## School notice

```mermaid
sequenceDiagram
    actor User
    participant Cog
    participant Parser
    participant Batch as 05:00 one-shot
    participant Site
    participant Store

    User->>Cog: natural-language profile
    Cog->>Parser: bounded canonicalization
    Parser-->>Cog: draft
    Cog-->>User: confirmation
    User->>Cog: confirm, revise, or cancel
    opt confirmed
        Cog->>Store: versioned profile
        Cog->>Batch: one-school initial run
    end
    Batch->>Store: active school sources
    Batch->>Site: public list pages
    Batch->>Site: candidate details
    Batch->>Store: revisioned digest
    Cog->>Store: due + snapshot validation
    opt relevant items
        Cog-->>User: configured-time DM
    end
```

## Earthquake edit

```mermaid
sequenceDiagram
    participant Loop as 60-second KMA loop
    participant DB
    participant Discord

    Loop->>DB: load watermark
    Loop->>Loop: fetch new notices
    Loop->>DB: save watermark before send
    alt new incident
        Loop->>Discord: formal alert
        Loop->>DB: save message ID
    else same incident window
        Loop->>DB: load message ID
        Loop->>Discord: PATCH original
    end
    Note over Loop,Discord: permission/timeouts never create duplicates
```

## Deployment

```mermaid
flowchart LR
    Git["Release SHA"] --> Current["/srv/masamong/current"]
    EnvM["masamo.env"] --> SM["masamong-masamo.service"]
    EnvG["general.env"] --> SG["masamong-general.service"]
    Current --> SM
    Current --> SG
    SM --> DBM[("TiDB masamong")]
    SG --> DBG[("TiDB masamong_general")]
    T1["05:00 school timer"] --> B1["school one-shot"]
    T2["05:35 transfer timer"] --> B2["transfer one-shot"]
    B1 --> LM[("Masamo notice store")]
    B2 --> LT[("Masamo transfer store")]
```

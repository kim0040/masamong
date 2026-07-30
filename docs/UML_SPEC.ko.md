# 마사몽 UML 명세

이 문서는 현재 구현의 구조와 핵심 시퀀스를 Mermaid로 표현합니다. 운영 원격 경로에는
BM25 구성요소가 없으며 의미 임베딩 검색만 표시합니다. 기능을 사용하려면
[사용자 가이드](README.ko.md)를, 구성 요소의 설명을 읽으려면
[아키텍처](ARCHITECTURE.ko.md)를 참고할 수 있습니다.

## 전체 컴포넌트

```mermaid
flowchart TB
    Discord["Discord Gateway / REST"]
    Main["MasamongBot<br/>main.py"]
    Events["Events Cog"]
    AI["AIHandler"]
    Intent["IntentAnalyzer"]
    LLM["LLMClient"]
    Tools["ToolsCog"]
    Health["ToolHealthRegistry"]
    RAG["RAGManager"]
    Search["HybridSearchEngine<br/>semantic-only in production"]
    Privacy["PrivacyCog"]
    Feature["Weather / Fortune / School / Transfer / Fun Cogs"]
    DB["TiDBConnection"]
    TiDB[("Profile TiDB")]
    KMA["KMA"]
    SearchAPI["Linkup / legacy search"]
    Market["Market providers"]
    Comet["CometAPI"]
    Notice["School / transfer one-shot jobs"]
    Local[("Masamo-only notice stores")]

    Discord --> Main
    Main --> Events
    Main --> AI
    Main --> Privacy
    Main --> Feature
    Events --> AI
    AI --> Intent
    AI --> Tools
    AI --> RAG
    AI --> LLM
    Intent --> LLM
    Tools --> Health
    Tools --> KMA
    Tools --> SearchAPI
    Tools --> Market
    Tools --> Comet
    RAG --> Search
    RAG --> DB
    Privacy --> DB
    Feature --> DB
    DB --> TiDB
    Notice --> Local
    Feature --> Local
```

## 핵심 클래스

```mermaid
classDiagram
    class MasamongBot {
        +db
        +setup_hook()
        +on_ready()
        +close()
        +is_ai_channel_allowed(guild_id, channel_id)
    }
    class AIHandler {
        +process_agent_message(message)
        +should_proactively_respond(message)
        -_route_tools(query, history)
        -_get_rag_context(...)
        -_execute_tool(...)
        -_compose_main_prompt(...)
    }
    class IntentAnalyzer {
        +analyze(query, history)
        -_sanitize_tool_plan(...)
        -_detect_tools_by_keyword(query)
        -_mark_auto_web_search_used(message)
    }
    class LLMClient {
        +generate_content(system, user, log_extra)
        +fast_generate_text(prompt, model, log_extra)
        +safe_generate_content(model, prompt, log_extra)
        -_run_bounded_provider_call(factory, lane, log_extra)
        -_reserve_request_budget(log_extra, feature)
    }
    class ToolsCog {
        +execute_tool(name, parameters, context)
        +execute_guarded(provider, operation)
        +result_has_external_evidence(name, result)
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
        -_update_conversation_windows(message)
    }
    class HybridSearchEngine {
        +search(query, guild_id, channel_id, user_id, deep_search)
        -_embedding_candidates(...)
        -_gate_by_relevance(query, entries, deep_search)
        -_dedupe_overlapping_entries(entries)
    }
    class TiDBConnection {
        +execute(sql, params)
        +executemany(sql, values)
        +commit()
        +rollback()
        +close()
        -_enter_transaction_gate(starts_transaction)
        -_rollback_abandoned_transaction(owner)
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

## 일반 대화 시퀀스

```mermaid
sequenceDiagram
    actor User
    participant Discord
    participant AI as AIHandler
    participant Intent as IntentAnalyzer
    participant RAG as RAGManager
    participant Tools
    participant LLM

    User->>Discord: @마사몽 질문 또는 DM
    Discord->>AI: process_agent_message
    AI->>AI: cooldown / spam / DM quota
    AI->>Discord: 진행 상태 메시지
    AI->>Discord: 최근 기록 1회 조회
    AI->>Intent: query + selected history
    Intent->>LLM: routing lane 1회
    LLM-->>Intent: JSON decision
    Intent-->>AI: tool plan + memory flags + digest
    opt memory requested or shallow fallback
        AI->>RAG: scoped semantic search
        RAG-->>AI: gated memory blocks
    end
    loop sanitized tool plan, max 3
        AI->>Tools: execute tool
        Tools-->>AI: structured result or error
    end
    alt verified direct renderer is sufficient
        AI->>AI: render evidence directly
    else synthesis required
        AI->>LLM: bounded main lane
        LLM-->>AI: final text
    end
    AI->>Discord: edit/split normalized response
```

## RAG 시퀀스

```mermaid
sequenceDiagram
    participant AI
    participant Search as HybridSearchEngine
    participant DiscordStore
    participant KakaoStore
    participant Reranker

    AI->>Search: search(query, guild, channel, user, deep)
    Search->>Search: build variants<br/>shallow = original only
    par scoped Discord candidates
        Search->>DiscordStore: vector query or bounded scan
        DiscordStore-->>Search: semantic candidates
    and allowed Kakao candidates
        Search->>KakaoStore: mapped-server query
        KakaoStore-->>Search: semantic candidates
    end
    Search->>Search: identity/scope alignment
    Search->>Search: absolute gate 0.61 or 0.58
    Search->>Search: relative floor + overlap dedupe
    opt reranker enabled
        Search->>Reranker: rerank gated candidates
        Reranker-->>Search: reordered candidates
    end
    Search-->>AI: at most 3 blocks
```

## 외부 사실 확인 시퀀스

```mermaid
sequenceDiagram
    participant AI
    participant Tools
    participant Health as ToolHealthRegistry
    participant Provider
    participant MainLLM

    AI->>Tools: execute requested provider
    Tools->>Health: begin_attempt
    alt circuit open
        Health-->>Tools: reject
        Tools-->>AI: explicit unavailable error
    else admitted
        Tools->>Provider: bounded network request
        alt success with evidence
            Provider-->>Tools: payload
            Tools->>Health: record_success
            Tools-->>AI: evidence result
        else failure or cancellation
            Provider-->>Tools: error
            Tools->>Health: record_failure / abandon_attempt
            Tools-->>AI: normalized error
        end
    end
    alt no verified evidence for external fact
        AI-->>AI: block unsupported answer
    else evidence exists
        AI->>MainLLM: evidence-bound synthesis
        MainLLM-->>AI: response
    end
```

## LLM 호출 상태

```mermaid
stateDiagram-v2
    [*] --> Budget
    Budget --> Blocked: quota store error or limit
    Budget --> Admission: reserved
    Admission --> Saturated: slot timeout
    Admission --> Calling: semaphore acquired
    Calling --> Success: valid response
    Calling --> Failed: provider error
    Calling --> TimedOut: call timeout
    Failed --> Fallback: non-timeout and fallback configured
    Fallback --> Success
    Fallback --> Failed
    TimedOut --> [*]: no overlapping fallback
    Saturated --> [*]
    Blocked --> [*]
    Success --> [*]
    Failed --> [*]
```

## TiDB 트랜잭션 소유권

```mermaid
sequenceDiagram
    participant A as Discord Task A
    participant DB as TiDBConnection
    participant B as Discord Task B
    participant TiDB

    A->>DB: first write
    DB->>DB: acquire transaction gate<br/>owner = Task A
    DB->>TiDB: execute
    B->>DB: SELECT or write
    Note over B,DB: waits; cannot enter A transaction
    A->>DB: second write
    DB->>TiDB: execute
    alt normal
        A->>DB: commit
        DB->>TiDB: COMMIT
    else A cancelled/exits
        DB->>TiDB: automatic ROLLBACK
    end
    DB-->>B: release gate
```

## 개인정보 동의 버튼

```mermaid
sequenceDiagram
    actor User
    participant Discord
    participant View as ConsentView
    participant DB
    participant Feature

    User->>Discord: 동의합니다 클릭
    Discord->>View: interaction
    View->>Discord: defer(ephemeral)
    View->>DB: current policy consent UPSERT/history
    DB-->>View: commit success
    View->>Discord: follow-up confirmation
    opt original action callback
        View->>Feature: resume once
    end
```

## 학교 공지 등록·수집·전달

```mermaid
sequenceDiagram
    actor User
    participant Cog as SchoolNoticeCog
    participant Consent
    participant Router as Profile parser/LLM
    participant Batch as 05:00 one-shot
    participant Site as Public school site
    participant Store as Masamo notice store

    User->>Cog: 자연어 학교·과정·학년·관심사
    Cog->>Consent: current scope check
    Cog->>Router: bounded canonicalization
    Router-->>Cog: canonical draft
    Cog-->>User: 이해한 값 확인
    User->>Cog: 맞아 / 수정 / 취소
    alt confirmed
        Cog->>Store: profile version/hash save
        Cog->>Batch: one-school initial subprocess
    end
    Note over Batch: daily 05:00, sequential, bounded
    Batch->>Store: active school source IDs only
    Batch->>Site: list pages without user data
    Batch->>Site: candidate detail pages
    Batch->>Store: revisioned per-user digest
    Cog->>Store: due digest + profile snapshot validation
    alt relevant items
        Cog-->>User: configured-time DM
    else empty
        Cog-->>Cog: send nothing
    end
```

## 지진 편집

```mermaid
sequenceDiagram
    participant Loop as 60s KMA loop
    participant DB
    participant Discord

    Loop->>DB: load occurrence watermark
    Loop->>Loop: fetch and sort new KMA notices
    Loop->>DB: persist new watermark before send
    alt new incident
        Loop->>Discord: send formal incident message
        Loop->>DB: persist channel message ID
    else same time/distance incident
        Loop->>DB: load original message ID
        Loop->>Discord: PATCH original message
    end
    Note over Loop,Discord: timeout/permission errors do not send duplicates
```

## 배포 구조

```mermaid
flowchart LR
    Git["Git release SHA"] --> Release["/srv/masamong/releases/SHA"]
    Release --> Current["/srv/masamong/current symlink"]
    EnvM["/etc/masamong/masamo.env"] --> ServiceM["masamong-masamo.service"]
    EnvG["/etc/masamong/general.env"] --> ServiceG["masamong-general.service"]
    Current --> ServiceM
    Current --> ServiceG
    ServiceM --> DBM[("TiDB masamong")]
    ServiceG --> DBG[("TiDB masamong_general")]
    TimerS["05:00 school timer"] --> BatchS["school one-shot"]
    TimerT["05:35 transfer timer"] --> BatchT["transfer one-shot"]
    BatchS --> LocalM[("/var/lib/masamong/masamo/notice")]
    BatchT --> LocalT[("/var/lib/masamong/masamo/transfer_notice")]
```

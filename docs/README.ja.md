# マサモン Discord ボット

[한국어](../README.md) | [English](README.en.md) | 日本語

マサモンは Discord サーバーで **メンションベースのAI会話** と **ユーティリティツール（天気/株価/為替/場所検索/画像生成）** を提供するボットです。
この文書は **現在のコードに基づく実際の動作と構造** を技術文書として整理しています。

---

## システムコンテキスト図

```mermaid
graph TB
    subgraph Users["👤 ユーザー"]
        GU["サーバーユーザー<br/>@mention 必須"]
        DM["DM ユーザー<br/>5h/30回 制限"]
    end

    subgraph Discord["Discord プラットフォーム"]
        GW["Gateway<br/>WebSocket + HTTP"]
    end

    subgraph Bot["🤖 マサモン Bot"]
        Main["ReMasamongBot<br/>Python 3.9+"]
    end

    subgraph LLM["🧠 LLM プロバイダ"]
        Comet["CometAPI<br/>DeepSeek-V3.2-Exp<br/>gemini-3.1-flash-lite"]
        Gemini["Google Gemini<br/><i>(オプション fallback)</i>"]
    end

    subgraph APIs["🌐 外部 API"]
        KMA_["KMA (気象庁)"]
        Fin["Finnhub/yfinance/KRX"]
        Web["Linkup/DuckDuckGo"]
        Place["Kakao Local"]
    end

    subgraph Store["💾 ストレージ"]
        TiDB["TiDB Cloud<br/>(本番)"]
        SQLiteDB["SQLite<br/>(開発)"]
    end

    GU --> GW
    DM --> GW
    GW <--> Main
    Main --> Comet
    Main --> Gemini
    Main --> KMA_
    Main --> Fin
    Main --> Web
    Main --> Place
    Main --> TiDB
    Main --> SQLiteDB
```

---

## メッセージ処理フロー

```mermaid
sequenceDiagram
    actor User as 👤 ユーザー
    participant Bot as ReMasamongBot
    participant AI as AIHandler
    participant Intent as IntentAnalyzer
    participant Tools as ToolsCog
    participant LLM as LLMClient

    User->>Bot: "@マサモン ソウルの天気は？"
    Bot->>Bot: 活動記録 + 履歴保存

    alt 検証失敗 (メンション/チャンネル/ロック)
        Bot-->>User: (応答なし)
    else 検証通過
        Bot->>AI: process_agent_message()

        AI->>Intent: analyze(query, context)
        Intent->>Intent: キーワードヒューリスティック
        Intent->>LLM: Routing Lane 呼び出し
        LLM-->>Intent: {tool_plan, draft, self_score}
        Intent-->>AI: 意図分析結果

        alt ツール必要
            AI->>Tools: execute_tool_plan()
            Tools-->>AI: ツール結果
        end

        AI->>AI: RAGコンテキスト検索

        AI->>LLM: Main Lane 呼び出し
        LLM-->>AI: 最終応答

        AI->>User: reply(応答)
    end
```

---

## 3ステージAIパイプライン

```mermaid
flowchart TB
    Input["👤 ユーザーメッセージ"] --> Valid["検証<br/>メンション/チャンネル/ロック"]

    Valid --> Stage1["🔍 Stage 1: 意図分析<br/>IntentAnalyzer<br/><i>キーワード + LLM (Routing Lane)</i>"]

    Stage1 --> Decision{ツール必要？}

    Decision -->|"yes"| Stage2["🛠️ Stage 2: ツール実行<br/>ToolsCog"]

    subgraph Tools["🛠️ ツール実行"]
        direction LR
        W["天気<br/>KMA"]
        F["金融<br/>Finnhub/yfinance"]
        S["Web検索<br/>Linkup/DDG"]
        P["場所<br/>Kakao"]
        I["画像<br/>CometAPI"]
    end

    Stage2 --> Tools
    Tools --> RAG["🧠 RAG検索<br/>HybridSearchEngine"]

    Decision -->|"no"| RAG

    RAG --> Stage3["✍️ Stage 3: 応答生成<br/>LLMClient (Main Lane)<br/><i>DeepSeek-V3.2-Exp</i>"]

    Stage3 --> Output["💬 Discord 返信"]

    style Stage1 fill:#e1f5fe,stroke:#0288d1
    style Stage2 fill:#f3e5f5,stroke:#7b1fa2
    style RAG fill:#e8f5e9,stroke:#388e3c
    style Stage3 fill:#fff3e0,stroke:#e65100
```

---

## デュアルレーンLLMルーティング

```mermaid
flowchart LR
    subgraph Routing["Routing Lane (意図分析)"]
        RP["Primary<br/>gemini-3.1-flash-lite"]
        RF["Fallback<br/>gemini-2.5-flash"]
        RP -->|"失敗"| RF
        RF -->|"失敗"| RD["Gemini Direct<br/><i>(オプション)</i>"]
    end

    subgraph Main["Main Lane (応答生成)"]
        MP["Primary<br/>DeepSeek-V3.2-Exp"]
        MF["Fallback<br/>DeepSeek-R1"]
        MP -->|"失敗"| MF
        MF -->|"失敗"| MD["Gemini Direct<br/><i>(オプション)</i>"]
    end

    Client["LLMClient"] --> Routing
    Client --> Main

    style RP fill:#e3f2fd,stroke:#1565c0
    style MP fill:#fff8e1,stroke:#f57f17
```

---

## ハイブリッドRAG検索

```mermaid
flowchart LR
    Query["検索クエリ"] --> QE["クエリ拡張<br/>query_rewriter"]

    QE --> Emb["埋め込み検索<br/>コサイン類似度<br/><i>top_n=8</i>"]
    QE --> BM["BM25検索<br/>キーワードマッチング<br/><i>top_n=8</i>"]

    Emb --> RRF["RRFフュージョン<br/>score = 1/(k+rank)<br/>k=60"]
    BM --> RRF

    RRF --> Weight["重み付き統合<br/>埋め込み: 0.55<br/>BM25: 0.45"]

    Weight --> Results["最終結果<br/>Top 5"]

    style RRF fill:#fff3e0,stroke:#e65100
    style Results fill:#c8e6c9,stroke:#2e7d32
```

---

**Quick Start**
1. Python 3.9+ を用意
2. 依存関係をインストール
```
python -m pip install -r requirements.txt
```
3. 環境変数を設定
- `.env` または `config.json` を使用
- 読み込み順序: **環境変数 → `config.json` → 既定値**
4. 実行
```
python main.py
```

**動作概要**
- サーバー: `@マサモン` メンションがある場合のみAIが応答。
- DM: メンション不要。ただし **5時間あたり30回 + 全体で1日100回** の制限あり。
- 応答生成: **CometAPI（既定）**、必要時のみ Gemini フォールバック。
- LLMプロバイダが1つ以上有効ならAIパイプラインは準備状態になります。

**パイプライン詳細**

**1) メッセージルーティング**
1. `main.py` が全メッセージを受信。
2. `!` コマンドは命令処理へ。
3. 非コマンドは `AIHandler.process_agent_message` に渡される。
4. Guildはメンション必須、DMは省略。

**2) ツール検出と実行**
- **キーワードマッチング + LLM分析**でツールを選択。
- 実行は `ToolsCog` が担当。
- ツール結果はプロンプト先頭に挿入。
- 天気リクエストは単一ツールで即時処理。

**3) LLM選択とフォールバック**
- CometAPIが有効なら先に利用。
- `ALLOW_DIRECT_GEMINI_FALLBACK=true` のときのみGeminiへフォールバック。
- CometAPIが有効ならGeminiキーは必須ではない。

**4) RAG（記憶）パイプライン**
1. メッセージは `conversation_history` に保存。
2. まとまったウィンドウ単位で要約生成。
3. 要約を埋め込み化し `discord_memory_entries` に保存。
4. 検索時は埋め込み/BM25ハイブリッド。
5. `emb_config.json` で制御。

**5) Web検索の自動判断**
- 「最新/ニュース/方法/なぜ」等のキーワード + RAGが弱い時に実行。
- Linkupを優先し、失敗時はDuckDuckGoへフォールバック。
- 月間予算制限でコスト管理。

**6) バックグラウンド処理**
- 雨/雪アラート
- 朝/夜の挨拶（天気要約付き）
- 国内影響圏 M4.0以上の地震アラート

**機能別依存関係**
- AI会話: `COMETAPI_KEY` 推奨、Geminiは任意フォールバック
- 画像生成: `COMETAPI_KEY` 必須 + `COMETAPI_IMAGE_ENABLED=true`
- 天気: `KMA_API_KEY`
- 為替: `EXIM_API_KEY_KR`
- 場所/検索: `KAKAO_API_KEY`
- Web検索: `LINKUP_API_KEY` (主)、DuckDuckGo (代替)
- 株価（既定）: `USE_YFINANCE=true` + CometAPIティッカー抽出
- 株価（代替）: `USE_YFINANCE=false` で KRX/Finnhub
- 運勢/星座: CometAPIのみ（Geminiフォールバックなし）
- RAG埋め込み: `numpy`, `sentence-transformers`

**アーキテクチャ構成**
| 領域 | モジュール | 役割 |
| --- | --- | --- |
| エントリポイント | `main.py` | 初期化、Cog読み込み、ルーティング |
| AIパイプライン | `cogs/ai_handler.py` | ツールルーティング、RAG、LLM呼び出し |
| LLMクライアント | `utils/llm_client.py` | レーンルーティング、Rate Limit |
| 意図分析 | `utils/intent_analyzer.py` | キーワード + LLM分析 |
| RAG管理 | `utils/rag_manager.py` | メモリ保存、ウィンドウ生成 |
| ハイブリッド検索 | `utils/hybrid_search.py` | 埋め込み + BM25 + RRF |
| ツール | `cogs/tools_cog.py` | 天気/株価/為替/場所/検索/画像 |
| 天気/通知 | `cogs/weather_cog.py` | 天気、雨/挨拶/地震通知 |
| 運勢/星座 | `cogs/fortune_cog.py` | 運勢と星座 |
| コマンド | `cogs/commands.py`, `cogs/fun_cog.py` | 汎用コマンド、要約 |
| ランキング | `cogs/activity_cog.py` | 活動記録/ランキング |
| 投票 | `cogs/poll_cog.py` | 投票作成 |
| 設定 | `cogs/settings_cog.py` | スラッシュ設定保存 |
| 保守 | `cogs/maintenance_cog.py` | アーカイブ/BM25再構築 |
| DBアダプター | `database/compat_db.py` | TiDB/SQLite統合 |

**データ保存**
- メインDB: TiDB (本番) / SQLite (開発)
- メモリストア: `discord_memory_entries` (TiDB/SQLite)
- Kakaoストア: `kakao_chunks` (TiDB/ローカル)
- 主要テーブル: `conversation_history`, `conversation_windows`, `user_activity`, `user_profiles`, `api_call_log`

**設定優先順位**
- 環境変数 → `config.json` → 既定値
- AI許可チャンネル: `prompts.json` または `DEFAULT_AI_CHANNELS`
- `/config channel` はDB保存のみで、AI許可ロジックには直接反映されません。

**コマンド概要**
- `!도움` / `!도움말` / `!h`: ヘルプ
- `!날씨`: 天気
- `!요약`: 要約（サーバーのみ）
- `!랭킹`: ランキング（サーバーのみ）
- `!투표`: 投票（サーバーのみ）
- `!이미지`: 画像生成（サーバーのみ）
- `!운세`, `!별자리`: 運勢/星座
- `!업데이트`: 更新情報
- `!delete_log`: ログ削除（管理者のみ）
- `!debug`: デバッグ（オーナーのみ）

**運用上の注意**
- CometAPIが主要LLMプロバイダ。Geminiキーはオプション。
- yfinanceモードの株価はCometAPIのティッカー抽出に依存します。
- 画像生成はユーザー/全体の制限があります。
- DMは厳格な使用制限が適用されます。

## 参考文献

| 文書 | 内容 |
|------|------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | 詳細システムアーキテクチャ |
| [UML_SPEC.md](UML_SPEC.md) | UML図と技術分析 |
| [QUICKSTART.md](QUICKSTART.md) | クイックスタートガイド |

# マサモン Discord ボット

<p align="center">
  <strong>韓国語中心のDiscord AIチャットボット</strong><br/>
  デュアルレーンLLM · 構造化メモリ(RAG) · 天気 · 金融 · Web検索 · 運勢 · 画像生成
</p>

<p align="center">
  <a href="README.ko.md">한국어</a> &nbsp;|&nbsp;
  <a href="README.en.md">English</a>
</p>

---

## 概要

マサモンはDiscordサーバーで動作する**韓国語中心のAIチャットボット**です。メンションベースの会話、構造化メモリ/RAG、Kakao Talkベクトル検索、天気/金融/Web検索ツール、運勢、画像生成、コミュニティ機能を単一ランタイムで統合しています。

- **言語**: Python 3.10+
- **フレームワーク**: `discord.py` >=2.7.1
- **LLM**: CometAPI (OpenAI互換) + Gemini (オプション fallback)
- **DB**: TiDB (本番) / SQLite (開発)
- **ライセンス**: MIT

---

## クイックスタート

### 前提条件
- Python 3.10+
- Discord Bot Token ([Developer Portal](https://discord.com/developers/applications))
- CometAPI Key (または Gemini API Key)

### インストール

```bash
git clone https://github.com/kim0040/masamong.git
cd masamong
```

**仮想環境の作成:**

| OS | コマンド |
|----|----------|
| **macOS / Linux** | `python3 -m venv venv && source venv/bin/activate` |
| **Windows (CMD)** | `python -m venv venv && venv\Scripts\activate.bat` |
| **Windows (PowerShell)** | `python -m venv venv && venv\Scripts\Activate.ps1` |

**依存関係のインストール:**

```bash
pip install -r requirements.txt
pip install -r requirements-cpu.txt   # CPUサーバー用 (RAG/埋め込み)
```

### 設定

```bash
cp .env.example .env
cp emb_config.example.json emb_config.json
cp prompts.example.json prompts.json
```

`.env`を編集してAPIキーを入力してください。**最低限必要な設定:**

```env
DISCORD_BOT_TOKEN=your_token_here
COMETAPI_KEY=your_cometapi_key
LLM_ROUTING_PRIMARY_PROVIDER=openai_compat
LLM_ROUTING_PRIMARY_MODEL=gpt-5.4-nano
LLM_ROUTING_PRIMARY_BASE_URL=https://api.cometapi.com/v1
LLM_ROUTING_PRIMARY_API_KEY=${COMETAPI_KEY}
LLM_MAIN_PRIMARY_PROVIDER=openai_compat
LLM_MAIN_PRIMARY_MODEL=deepseek-v4-flash
LLM_MAIN_PRIMARY_BASE_URL=https://api.cometapi.com/v1
LLM_MAIN_PRIMARY_API_KEY=${COMETAPI_KEY}
```

> **ヒント:** `python setup.py`を実行すると対話式セットアップウィザードが使えます。

### 実行

```bash
# macOS / Linux
PYTHONPATH=. python main.py

# Windows (CMD)
set PYTHONPATH=. && python main.py

# Windows (PowerShell)
$env:PYTHONPATH="."; python main.py
```

---

## 主な機能

| 機能 | 説明 |
|------|------|
| **AI会話** | `@マサモン` メンションでLLM応答 (チャンネル別ペルソナ) |
| **DM会話** | メンション不要の1:1会話 (5時間30回制限) |
| **メモリ / RAG** | スコープ分離した意味埋め込み + TiDBベクトル検索 |
| **天気** | KMA気象庁 リアルタイム/週間予報 + 地震通知 + `!날씨` |
| **金融** | 株式(US/KR)、為替 — Finnhub, yfinance, KRX, EximBank |
| **Web検索** | リアルタイム検索 — Linkup API (主) / DuckDuckGo (代替) |
| **画像生成** | `!이미지 <プロンプト>` — CometAPI Gemini Image |
| **運勢** | 日/月/年 運勢 + 星座 + 購読 |
| **学校のお知らせ** | DM限定、自然文で学校・学年・関心分野を確認後に登録 |
| **編入のお知らせ** | DM限定、TOEIC等の公認英語を使う20大学を購読 |
| **プライバシー** | 個人情報を使う機能は保存前にDiscordボタンで同意 |
| **ランキング** | サーバー活動ランキング (`!랭킹`) |
| **要約** | チャンネル会話要約 (`!요약`) |
| **投票** | `!투표 "テーマ" "項目1" "項目2"` |

---

## アーキテクチャ

マサモンは**3ステージデュアルレーンエージェントパイプライン**を使用します：

```
メッセージ → 意味ルーティング → 必要なツール → 選択的な長期記憶 → 応答生成
```

通常時のツール選択は固定キーワードではなくRouting Laneが会話の意味から決定します。
キーワード規則はプロバイダ障害時の限定fallbackだけです。直近の発話は原文のまま保ち、
長くなった同一リクエスト内の文脈は追加API呼び出しなしで短いdigestにし、さらに古い
内容が必要な質問だけRAGを検索します。

TiDB Cloud Starterでは`TIDB_STARTER_FREE_PLAN_MODE=true`を使用し、構造化メモリの
候補読み取りを384件、拡張読み取りを768件に制限します。無料枠は行5GiB・列5GiB・
月5,000万RUで、最終使用量はCloudの**Usage this month**を確認してください。
低スペックの本番サーバーではBM25/FTS5を構築・検索しません。
`BM25_AUTO_REBUILD_ENABLED=false`は明示プロファイルの必須条件です。

[📘 詳細アーキテクチャ (English)](ARCHITECTURE.en.md) &nbsp;|&nbsp; [📗 詳細アーキテクチャ (한국어)](ARCHITECTURE.ko.md)

[📐 UML仕様とダイアグラム](UML_SPEC.ko.md) — コンポーネント、クラス、シーケンス、状態、デプロイ図

---

## 技術スタック

| 層 | 技術 |
|----|------|
| Botフレームワーク | discord.py >=2.7.1 |
| LLMプロバイダ | CometAPI, Google Gemini |
| LLMアーキテクチャ | Dual Lane (Routing + Main) with Primary/Fallback |
| データベース | TiDB (本番), SQLite (開発) |
| ベクトル検索 | SentenceTransformers + TiDB VECTOR(384) |
| Web検索 | Linkup API, DuckDuckGo |
| 金融 | Finnhub, yfinance, KRX, EximBank |
| 天気 | KMA (韓国気象庁) |

---

## ライセンス

MIT License — 詳細は [LICENSE](../LICENSE) を参照してください。

---

## ドキュメント

| ドキュメント | 言語 | 内容 |
|-------------|------|------|
| [ARCHITECTURE.en.md](ARCHITECTURE.en.md) | English | 現行システムのアーキテクチャ |
| [ARCHITECTURE.md](ARCHITECTURE.ko.md) | 한국어 | 現行システムのアーキテクチャ |
| [UML_SPEC.md](UML_SPEC.ko.md) | 한국어 | 現行ランタイムのUMLとシーケンス |
| [README.en.md](README.en.md) | English | 英語ガイド |
| [README.ko.md](README.ko.md) | 한국어 | 韓国語README |
| [README.md](README.md) | 한국어 | 文書一覧 |

---

<p align="center">
  Made with 🐍 by <a href="https://github.com/kim0040">kim0040</a>
</p>

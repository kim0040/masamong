# マサモン Discord ボット

<p align="center">
  <strong>韓国語中心のDiscord AIチャットボット</strong><br/>
  デュアルレーンLLM · 構造化メモリ(RAG) · 天気 · 金融 · Web検索 · 運勢 · 画像生成
</p>

<p align="center">
  <a href="../../README.md">English</a> &nbsp;|&nbsp;
  <a href="README.ko.md">한국어</a>
</p>

---

## 概要

マサモンはDiscordサーバーで動作する**韓国語中心のAIチャットボット**です。メンションベースの会話、構造化メモリ/RAG、Kakao Talkベクトル検索、天気/金融/Web検索ツール、運勢、画像生成、コミュニティ機能を単一ランタイムで統合しています。

- **言語**: Python 3.9+
- **フレームワーク**: `discord.py` >=2.7.1
- **LLM**: CometAPI (OpenAI互換) + Gemini (オプション fallback)
- **DB**: TiDB (本番) / SQLite (開発)
- **ライセンス**: MIT

---

## クイックスタート

### 前提条件
- Python 3.9+
- Discord Bot Token ([Developer Portal](https://discord.com/developers/applications))
- CometAPI Key (または Gemini API Key)

### インストール

```bash
git clone https://github.com/kim0040/masamong.git
cd masamong

python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt
pip install -r requirements-cpu.txt
```

### 設定

```bash
cp .env.example .env
# .envを編集して実際のAPIキーを入力
```

**最小 `.env`:**
```env
DISCORD_BOT_TOKEN=your_token_here
COMETAPI_KEY=your_cometapi_key
COMETAPI_BASE_URL=https://api.cometapi.com/v1
USE_COMETAPI=true
```

### 実行

```bash
PYTHONPATH=. python main.py
```

---

## 主な機能

| 機能 | 説明 |
|------|------|
| **AI会話** | `@マサモン` メンションでLLM応答 (チャンネル別ペルソナ) |
| **DM会話** | メンション不要の1:1会話 (5時間30回制限) |
| **メモリ / RAG** | ハイブリッド検索 (埋め込み + BM25 + RRF) |
| **天気** | KMA気象庁 リアルタイム/週間予報 + 地震通知 + `!날씨` |
| **金融** | 株式(US/KR)、為替 — Finnhub, yfinance, KRX, EximBank |
| **Web検索** | リアルタイム検索 — Linkup API (主) / DuckDuckGo (代替) |
| **画像生成** | `!이미지 <プロンプト>` — CometAPI Gemini Image |
| **運勢** | 日/月/年 運勢 + 星座 + 購読 |
| **ランキング** | サーバー活動ランキング (`!랭킹`) |
| **要約** | チャンネル会話要約 (`!요약`) |
| **投票** | `!투표 "テーマ" "項目1" "項目2"` |

---

## アーキテクチャ

マサモンは**3ステージデュアルレーンエージェントパイプライン**を使用します：

```
メッセージ → 意図分析 (Routing Lane) → ツール実行 → RAG検索 → 応答生成 (Main Lane)
```

[📘 詳細アーキテクチャ (English)](ARCHITECTURE.en.md) &nbsp;|&nbsp; [📗 詳細アーキテクチャ (한국어)](ARCHITECTURE.md)

[📐 UML仕様とダイアグラム](UML_SPEC.md) — C4、コンポーネント、クラス、シーケンス、アクティビティ、状態、デプロイ、ER図 (全17種)

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

MIT License — 詳細は [LICENSE](../../LICENSE) を参照してください。

---

## ドキュメント

| ドキュメント | 言語 | 内容 |
|-------------|------|------|
| [ARCHITECTURE.en.md](ARCHITECTURE.en.md) | English | システムアーキテクチャ詳細 (15図) |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 한국어 | システムアーキテクチャ詳細 (15図) |
| [UML_SPEC.md](UML_SPEC.md) | 한국어 | UML分析 — C4, クラス, シーケンス, ER (17図) |
| [../README.md](../../README.md) | English | 英語README |
| [README.ko.md](README.ko.md) | 한국어 | 韓国語README |

---

<p align="center">
  Made with 🐍 by <a href="https://github.com/kim0040">kim0040</a>
</p>

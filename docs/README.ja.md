# マサモン Discord ボット

[한국어](../README.md) | [English](README.en.md) | 日本語

マサモンは Discord サーバーで **メンションベースのAI会話** と **生活系ツール（天気/株価/為替/場所検索/画像生成）** を提供するボットです。
この文書は **現在のコードで実際に動作する機能のみ** をまとめています。

**主な機能**
- サーバー: `@マサモン` メンションがある時だけAIが応答
- DM: メンション不要（DM利用制限あり）
- キーワードでツール自動実行: 天気/株価/場所/画像生成
- 「最新/ニュース/方法」系の質問は条件に応じて自動Web検索
- コマンド: 天気、運勢、星座、要約、ランキング、投票、画像生成、ヘルプ
- 定期通知: 雨/雪、朝夕あいさつ、地震（国内影響圏 M4.0以上）
- 会話履歴保存 + 任意のRAG（埋め込み/BM25）

**できること（例）**
```
@マサモン 明日のソウルの天気は？
@マサモン 삼성전자 株価教えて
@マサモン 弘大のおすすめご飯屋さん
@マサモン テスラの最新ニュース
@マサモン サイバーパンク風ソウル夜景を描いて

!날씨 부산 이번주 날씨
!운세
!운세 등록
!운세 구독 07:30
!별자리 물병자리
!요약
!랭킹
!투표 "ランチ" "ピザ" "ラーメン" "スープ"
!이미지 星がいっぱいの夜空
```

**AI会話ルール**
- サーバーでは `@マサモン` メンション必須です。
- 許可チャンネルは `prompts.json` の `channels.allowed` または `DEFAULT_AI_CHANNELS` で制限されます。
- DMはメンション不要ですが **5時間あたり30回 + 全体で1日100回** の制限があります。
- 応答生成: CometAPI（任意）→ Gemini フォールバック。
- `GEMINI_API_KEY` がない場合 **AI機能は無効** になります。

**自動ツール起動の目安**
- 天気: 天気/気温/雨/雪/傘 などのキーワード
- 株価: 価格/株/相場 などのキーワード
- 場所: 店/カフェ/近く/おすすめ などのキーワード
- 画像生成: 「描いて/生成して」などのキーワード
- Web検索: 「最新/ニュース/方法/なぜ」など + RAG弱い + 日次上限内

**コマンド一覧**
- `!도움` / `!도움말` / `!h`: ヘルプ
- `!날씨 [地域/日付]`: 天気（サーバー/DM）
- `!요약`: 最近の会話要約（サーバーのみ）
- `!랭킹`: 活動ランキング（サーバーのみ）
- `!투표 "テーマ" "選択肢1" ...`: 投票作成（サーバーのみ）
- `!이미지 <説明>`: 画像生成（サーバーのみ）
- `!업데이트`: 更新案内（固定メッセージ）
- `!delete_log`: ログ削除（管理者のみ/サーバーのみ）
- `!debug status`, `!debug reset_dm <user_id>`: ボットオーナー専用

**運勢/星座コマンド**
- `!운세`: 今日の運勢（サーバー=要約、DM=詳細）
- `!운세 상세`: DMで詳細運勢
- `!운세 등록`: 生年月日等の登録（DMのみ）
- `!운세 구독 HH:MM`: 毎日運勢ブリーフィング（DMのみ）
- `!운세 구독취소`: 購読解除
- `!구독 HH:MM`: `!운세 구독` の別名（DMのみ）
- `!이번달운세`, `!올해운세`: 月/年運勢（DMのみ、1日3回制限）
- `!별자리`: 自分の星座運勢（登録情報があれば自動）
- `!별자리 <名前>`: 特定の星座運勢
- `!별자리 순위`: 今日の12星座ランキング

**バックグラウンド通知**
- 降水確率が閾値以上の雨/雪アラート
- 朝/夜のあいさつ（天気要約付き）
- 国内影響圏 M4.0以上の地震アラート

**機能別 API/依存関係**
- AI会話: `GEMINI_API_KEY` 必須、CometAPIは任意
- 画像生成: `COMETAPI_KEY` 必須 + `COMETAPI_IMAGE_ENABLED=true`
- 天気: `KMA_API_KEY`（韓国気象庁）
- 為替: `EXIM_API_KEY_KR`（韓国輸出入銀行）
- 場所/検索/画像検索: `KAKAO_API_KEY`
- Web検索（自動）: `GOOGLE_API_KEY` + `GOOGLE_CX`、失敗時はKakaoフォールバック
- 株価（基本）: `USE_YFINANCE=true` + CometAPIでティッカー抽出
- 株価（代替）: `USE_YFINANCE=false` で KRX/Finnhub
- 運勢/星座: CometAPIのみ（Geminiフォールバックなし）
- RAG埋め込み: `numpy`, `sentence-transformers`
- 占星術詳細: `ephem` インストール時のみ

**インストール & 実行**
1. Python 3.9+ を用意
2. 依存関係のインストール
```
python -m pip install -r requirements.txt
```
3. 環境変数の設定
- `.env` または `config.json` を使用
- 読み込み順: 環境変数 → `config.json`
4. 実行
```
python main.py
```

**主な環境変数**
- `DISCORD_BOT_TOKEN`: ボットトークン（必須）
- `GEMINI_API_KEY`: Gemini API キー（AI必須）
- `COMETAPI_KEY`: CometAPI キー（任意）
- `COMETAPI_BASE_URL`: 既定 `https://api.cometapi.com/v1`
- `COMETAPI_MODEL`: 既定 `DeepSeek-V3.2-Exp-nothinking`
- `USE_COMETAPI`: 既定 `true`
- `KMA_API_KEY`: 気象庁API
- `KAKAO_API_KEY`: Kakao API
- `GOOGLE_API_KEY`, `GOOGLE_CX`: Google CSE
- `EXIM_API_KEY_KR`: 為替API
- `GO_DATA_API_KEY_KR`: 公共データ(KRX)
- `FINNHUB_API_KEY`: Finnhub
- `DEFAULT_AI_CHANNELS`: 許可チャンネルID（カンマ区切り）
- `EMB_CONFIG_PATH`: 既定 `emb_config.json`
- `PROMPT_CONFIG_PATH`: 既定 `prompts.json`

**設定ファイル**
- `prompts.json`: チャンネル別ペルソナ/ルール/許可設定
- `emb_config.json`: RAG設定（埋め込み/BM25/拡張）
- `config.py`: デフォルト値と制限

**プロジェクト構成（要約）**
- `main.py`: エントリポイント
- `cogs/`: 機能モジュール（AI/天気/運勢/投票など）
- `utils/`: APIハンドラ/RAG/埋め込み
- `database/`: スキーマ/マイグレーション
- `prompts.json`, `emb_config.json`: AI/検索設定

**データ保存**
- SQLite DB: `database/remasamong.db`
- 保存内容: 会話履歴、活動ランキング、運勢プロファイル、APIログ
- 埋め込みDB: `database/discord_embeddings.db`（任意）

**トラブルシュート**
- AIが応答しない: `GEMINI_API_KEY` と許可チャンネルを確認
- 天気/場所/為替が出ない: APIキー不足
- 株価が失敗: yfinanceはCometAPIでティッカー抽出が必要。`USE_YFINANCE=false` にして KRX/Finnhub を利用
- 画像生成失敗: `COMETAPI_KEY` と生成回数制限を確認

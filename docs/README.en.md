# Masamong Discord Bot

[한국어](../README.md) | English | [日本語](README.ja.md)

Masamong is a Discord bot that provides **mention-based AI chat** and **daily utility tools (weather, stocks, exchange rates, places, image generation)**.
This document describes **only the features that actually work in the current code**.

**Key Features**
- Server channels: AI replies only when you `@` mention the bot
- DM: chat without mention (DM usage limits apply)
- Keyword-based tool triggers: weather, stock, places, image generation
- Automatic web search for “latest/news/how-to” type queries (when conditions match)
- Commands: weather, fortune, zodiac, summary, ranking, poll, image generation, help
- Scheduled alerts: rain/snow, morning/evening greetings, earthquakes (domestic, M≥4.0)
- Conversation history + optional RAG (embeddings/BM25)

**What You Can Do (Examples)**
```
@Masamong What’s the weather in Seoul tomorrow?
@Masamong Samsung Electronics stock price
@Masamong Recommend restaurants near Hongdae
@Masamong Latest Tesla news
@Masamong Draw a cyberpunk Seoul night view

!weather 부산 이번주 날씨
!운세
!운세 등록
!운세 구독 07:30
!별자리 물병자리
!요약
!랭킹
!투표 "Lunch menu" "Pizza" "Ramen" "Soup"
!이미지 A starry night sky
```

**AI Chat Rules**
- Server channels require `@Masamong` mention.
- Allowed channels are defined in `prompts.json` (`channels.allowed`) or `DEFAULT_AI_CHANNELS` env var.
- DM is allowed without mention but limited to **30 messages per 5 hours + 100 global DMs per day**.
- Response generation: CometAPI (optional) → Gemini fallback.
- Without `GEMINI_API_KEY`, **all AI features are disabled**.

**Auto Tool Triggers (Summary)**
- Weather: keywords like weather/temperature/rain/snow/umbrella
- Stock: keywords like price/stock/quote
- Places: keywords like restaurant/cafe/nearby/recommend
- Image generation: keywords like “draw/generate image”
- Web search: keywords like “latest/news/how/why” + weak RAG + within daily quota

**Command Reference**
- `!도움` / `!도움말` / `!h`: help
- `!날씨 [location/date]`: weather (server/DM)
- `!요약`: recent chat summary (server only)
- `!랭킹`: activity ranking (server only)
- `!투표 "topic" "choice1" ...`: create a poll (server only)
- `!이미지 <prompt>`: generate an image (server only)
- `!업데이트`: static update notice
- `!delete_log`: delete log file (admin only, server only)
- `!debug status`, `!debug reset_dm <user_id>`: bot owner only

**Fortune / Zodiac Commands**
- `!운세`: today’s fortune (summary in server, detailed in DM)
- `!운세 상세`: detailed fortune in DM
- `!운세 등록`: register birth info (DM only)
- `!운세 구독 HH:MM`: daily fortune briefing (DM only)
- `!운세 구독취소`: unsubscribe
- `!구독 HH:MM`: alias for `!운세 구독` (DM only)
- `!이번달운세`, `!올해운세`: monthly/yearly fortune (DM only, 3/day limit)
- `!별자리`: your zodiac fortune (auto if registered)
- `!별자리 <name>`: specific zodiac sign fortune
- `!별자리 순위`: daily zodiac ranking

**Background Alerts**
- Rain/snow alerts above precipitation threshold
- Morning/evening greetings with weather summary
- Earthquake alerts for domestic M≥4.0 events

**API/Dependency Requirements by Feature**
- AI chat: `GEMINI_API_KEY` required, CometAPI optional
- Image generation: `COMETAPI_KEY` required + `COMETAPI_IMAGE_ENABLED=true`
- Weather: `KMA_API_KEY` (KMA)
- Exchange rates: `EXIM_API_KEY_KR` (Korea Eximbank)
- Place/Web/Image search: `KAKAO_API_KEY`
- Web search (auto): `GOOGLE_API_KEY` + `GOOGLE_CX`, Kakao fallback
- Stocks (default): `USE_YFINANCE=true` + CometAPI ticker extraction
- Stocks (alternative): set `USE_YFINANCE=false` and use KRX/Finnhub keys
- Fortune/Zodiac: CometAPI only (no Gemini fallback)
- RAG embeddings: `numpy`, `sentence-transformers`
- Astrology details: `ephem` installed

**Install & Run**
1. Install Python 3.9+
2. Install dependencies
```
python -m pip install -r requirements.txt
```
3. Set environment variables
- Use `.env` or `config.json`.
- Load order: env vars → `config.json`.
4. Run
```
python main.py
```

**Key Environment Variables**
- `DISCORD_BOT_TOKEN`: bot token (required)
- `GEMINI_API_KEY`: Gemini API key (required for AI)
- `COMETAPI_KEY`: CometAPI key (optional)
- `COMETAPI_BASE_URL`: default `https://api.cometapi.com/v1`
- `COMETAPI_MODEL`: default `DeepSeek-V3.2-Exp-nothinking`
- `USE_COMETAPI`: default `true`
- `KMA_API_KEY`: KMA weather API
- `KAKAO_API_KEY`: Kakao API
- `GOOGLE_API_KEY`, `GOOGLE_CX`: Google CSE
- `EXIM_API_KEY_KR`: Eximbank exchange rate API
- `GO_DATA_API_KEY_KR`: Public data portal (KRX)
- `FINNHUB_API_KEY`: Finnhub
- `DEFAULT_AI_CHANNELS`: comma-separated channel IDs
- `EMB_CONFIG_PATH`: default `emb_config.json`
- `PROMPT_CONFIG_PATH`: default `prompts.json`

**Config Files**
- `prompts.json`: per-channel persona/rules and allowlist
- `emb_config.json`: RAG (embeddings/BM25/query expansion)
- `config.py`: defaults and rate limits

**Project Structure (Summary)**
- `main.py`: entrypoint and Cog loading
- `cogs/`: feature modules (AI, weather, fortune, poll, etc.)
- `utils/`: API handlers, RAG/embeddings
- `database/`: schema and migrations
- `prompts.json`, `emb_config.json`: AI/search settings

**Data Storage**
- SQLite DB: `database/remasamong.db`
- Stores: chat history, activity counts, fortune profiles, API logs
- Embedding DB: `database/discord_embeddings.db` (optional)

**Troubleshooting**
- No AI replies: check `GEMINI_API_KEY` and allowed channels.
- Weather/place/exchange not working: missing API keys.
- Stock lookup fails: yfinance mode needs CometAPI ticker extraction; try `USE_YFINANCE=false` with KRX/Finnhub keys.
- Image generation fails: verify `COMETAPI_KEY` and image limits.

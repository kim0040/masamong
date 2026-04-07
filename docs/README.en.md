# Masamong Discord Bot

[한국어](../README.md) | English | [日本語](README.ja.md)

Masamong is a Discord bot that provides **mention-based AI chat** and **utility tools (weather, stocks, exchange rates, places, image generation)**.
This document describes **the actual behavior and architecture in the current code**.

**Quick Start**
1. Install Python 3.9+
2. Install dependencies
```
python -m pip install -r requirements.txt
```
3. Configure environment variables
- Use `.env` or `config.json`
- Load order: **env vars → `config.json` → defaults**
4. Run
```
python main.py
```

**Behavior Overview**
- Guild channels: AI responds only when the bot is mentioned.
- DM: chat without mention, with **30 messages per 5 hours + 100 global DMs per day** limits.
- Response generation: **CometAPI (default)**, optional direct Gemini fallback.
- AI pipeline is ready when at least one LLM provider is available.

**Message Flow (Text Diagram)**
```
[Discord Message]
  ├─ if startswith('!') → command handler
  └─ else
      ├─ mention check (Guild required, DM skipped)
      ├─ keyword tool detect
      ├─ tool execution
      ├─ RAG context search (optional)
      ├─ prompt compose
      └─ LLM response (CometAPI, optional Gemini fallback)
```

**Pipeline Details**

**1) Message Routing**
1. `main.py` receives all messages.
2. Command messages are processed and stop there.
3. Non-command messages are passed to `AIHandler.process_agent_message`.
4. Guild messages require mention; DM does not.

**2) Tool Detection and Execution**
- Tools are selected by **keyword matching** (no lite model).
- Tools are executed in `ToolsCog`.
- Tool results are placed at the top of the prompt.
- Weather requests are handled by a single tool call.

**3) LLM Selection and Fallback**
- CometAPI is tried first when enabled.
- Gemini fallback is used only when `ALLOW_DIRECT_GEMINI_FALLBACK=true`.
- Gemini is optional if CometAPI is configured.

**4) RAG (Memory) Pipeline**
1. Messages are stored in `conversation_history`.
2. Window summaries are created after a message window fills.
3. Summaries are embedded and stored in `discord_embeddings.db`.
4. Query time uses embedding/BM25 hybrid search.
5. `emb_config.json` controls embedding/BM25/query expansion/reranker.

**5) Auto Web Search**
- Triggered only when “latest/news/how/why” keywords appear and RAG is weak.
- Google CSE is preferred; Kakao search is used as fallback.
- Daily quota disables auto search when exceeded.

**6) Background Tasks**
- Rain/snow alerts based on short-term precipitation probability.
- Morning/evening greetings with weather summary.
- Earthquake alerts for domestic M≥4.0 events.

**Dependencies by Feature**
- AI chat: `COMETAPI_KEY` recommended, Gemini optional fallback
- Image generation: `COMETAPI_KEY` required + `COMETAPI_IMAGE_ENABLED=true`
- Weather: `KMA_API_KEY`
- Exchange rates: `EXIM_API_KEY_KR`
- Place/web/image search: `KAKAO_API_KEY`
- Auto web search: `GOOGLE_API_KEY` + `GOOGLE_CX` (Kakao fallback)
- Stocks (default): `USE_YFINANCE=true` + CometAPI ticker extraction
- Stocks (alternative): `USE_YFINANCE=false` with KRX/Finnhub keys
- Fortune/Zodiac: CometAPI only (no Gemini fallback)
- RAG embeddings: `numpy`, `sentence-transformers`
- Astrology details: `ephem` optional

**Architecture Components**
| Area | Modules | Responsibility |
| --- | --- | --- |
| Entrypoint | `main.py` | Bot init, Cog loading, message routing |
| AI pipeline | `cogs/ai_handler.py` | Tool routing, RAG, LLM calls |
| Tools | `cogs/tools_cog.py` | Weather/stocks/exchange/places/web/image |
| Weather/alerts | `cogs/weather_cog.py` | Weather + rain/greeting/earthquake alerts |
| Fortune/Zodiac | `cogs/fortune_cog.py` | Fortune and zodiac features |
| Commands | `cogs/commands.py`, `cogs/fun_cog.py` | Utility commands and summary |
| Ranking | `cogs/activity_cog.py` | Activity tracking and ranking |
| Poll | `cogs/poll_cog.py` | Poll creation |
| Settings | `cogs/settings_cog.py` | Slash command config storage |
| Maintenance | `cogs/maintenance_cog.py` | Archiving, BM25 rebuild |
| RAG | `utils/embeddings.py`, `utils/hybrid_search.py` | Embeddings and search |

**Data Storage**
- SQLite main DB: `database/remasamong.db`
- Embedding DB: `database/discord_embeddings.db` (optional)
- Key tables: `conversation_history`, `conversation_windows`, `user_activity`, `user_profiles`, `api_call_log`

**Config Priority**
- Load order: env vars → `config.json` → defaults
- AI allowlist: `prompts.json` (`channels.allowed`) or `DEFAULT_AI_CHANNELS`
- `/config channel` writes DB but is **not used for AI allowlist** in the current pipeline.

**Command Summary**
- `!도움` / `!도움말` / `!h`: help
- `!날씨`: weather
- `!요약`: chat summary (guild only)
- `!랭킹`: activity ranking (guild only)
- `!투표`: poll (guild only)
- `!이미지`: image generation (guild only)
- `!운세`, `!별자리`: fortune/zodiac
- `!업데이트`: update info
- `!delete_log`: delete log (admin only)
- `!debug`: debug (owner only)

**Operational Notes**
- Without Gemini API key, AI features are disabled.
- Stock lookup in yfinance mode depends on CometAPI ticker extraction.
- Image generation is rate-limited per user and globally.
- DM usage is strictly rate-limited.

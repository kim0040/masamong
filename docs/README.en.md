# Masamong user and operator guide

[한국어](README.ko.md) | English | [日本語](README.ja.md)

Masamong is a Korean-first Discord assistant for conversation, scoped long-term
memory, KMA weather and disaster information, source-backed web and market
lookups, image generation, fortunes, and private school/transfer notices.

The repository runs two editions from one release:

- `masamo`: the existing community instance and its existing TiDB data
- `general`: a clean instance with separate identity, data, prompts, state, and
  writable paths

They must never share a bot token, database, service, prompt file, embedding
store, admin list, log, or scheduler ownership.

## Quick start

Requirements are Python 3.10+, a Discord bot token, and one configured LLM
endpoint.

```bash
git clone https://github.com/kim0040/masamong.git
cd masamong
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
cp prompts.example.json prompts.json
cp emb_config.example.json emb_config.json
python main.py
```

Install `requirements-cpu.txt` when local embedding/RAG is enabled. Production
must use an explicit profile outside the release directory:

```bash
MASAMONG_ENV_FILE=/etc/masamong/masamo.env PYTHONPATH=. .venv/bin/python main.py
```

Explicit profiles fail closed when their identity, database, TLS settings,
required Cogs, writable paths, or resource limits are inconsistent.

## Discord entry points

| Entry point | Behavior |
|---|---|
| `@Masamong <message>` | Guild conversation; mention required |
| DM | Private conversation without a mention |
| `!메뉴` | Private button menu; chosen public actions reply to the channel |
| `!도움` | Full command reference |
| `!날씨 [place] [date]` | KMA observation, forecast, and active warnings |
| `!이미지 <description>` | Generate exactly one image |
| `!요약` | Summarize recent channel conversation |
| `!운세` | Fortune dashboard; reusable profile requires consent |
| `!공지` | School notice dashboard, DM only |
| `!편입` | Transfer notice subscription, DM only |
| `!개인정보` | Consent status and withdrawal |
| `!관리`, `!초대` | Super-admin only and private |

School profiles are entered in natural Korean and shown back for confirmation
before they are stored. Registration triggers one bounded initial collection;
subsequent collection is sequential at 05:00 KST. Delivery defaults to 09:00
KST and is configurable per subscriber. Only matching public notices are sent.

## Conversation pipeline

One user turn follows a bounded pipeline:

```text
Discord message
  -> instance/guild/channel/privacy validation
  -> semantic routing
  -> at most three planned tool calls
  -> selective scoped memory lookup
  -> one final response
```

The routing lane uses `gpt-5.4-nano` in the current Masamo profile and the main
lane uses `deepseek-v4-flash`. Keyword rules are an outage fallback, not the
normal tool-selection mechanism. Long context is compacted inside the routing
response, so compaction does not create another LLM request.

Current or niche factual questions are evidence-gated. If a required lookup
fails, Masamong says it could not verify the answer instead of inventing market
numbers, events, or sources. Provider calls have finite timeouts, finite retry
targets, bounded concurrency, circuit breakers, and global/feature/guild/user/DM
budgets.

## Memory and RAG

Recent conversation remains verbatim. Older useful facts become structured,
embedded memories. Retrieval is isolated by instance, DM/guild, channel, and
user scope and is subject to an absolute and relative relevance gate. The
normal production path uses semantic embeddings and TiDB vector search.

BM25/FTS5 is intentionally disabled. The low-spec production host neither
builds nor queries a BM25 index, and an explicit profile is rejected unless
`BM25_AUTO_REBUILD_ENABLED=false`.

The Masamo database already contains accumulated memories. Deployments are
forward-only and non-destructive: existing BLOB embeddings remain intact while
the compatible TiDB `VECTOR(384)` copy is used when enabled. No routine deploy
deletes conversation or memory rows.

## External tools

- KMA: observation, nowcast, forecasts, warnings, earthquakes, and typhoons
- Finance: Finnhub/yfinance/KRX/EximBank with market-evidence validation
- Web/news: Linkup with a bounded fallback and visible source links
- Places: Kakao Local
- Images: CometAPI Gemini-native `generateContent`

Source details are revealed by the newspaper reaction and hidden again when the
reaction is removed. Earthquake notices use formal shared wording; events in the
same sequence update one Discord message instead of producing an endless stream.
Server-specific personas are applied only inside that server.

## Privacy and administration

Normal server conversation stays available. Reusable fortune, school, and
transfer profiles require explicit feature-level consent in DM before storage.
Discord IDs or personal school-profile fields are never sent to school sites.

Only IDs listed in the current profile's `MASAMONG_SUPERADMIN_USER_IDS` can see
the admin menu. Discord server administrator status alone grants no bot-control
authority. The UI exposes only safe guild/channel enable switches and invite
help; model, database, collection schedule, and persona internals are not
editable from Discord.

## Low-spec production profile

```dotenv
MASAMONG_CPU_THREADS=1
MASAMONG_EXECUTOR_WORKERS=1
AI_MAX_CONCURRENT_PROCESSING=1
LLM_MAX_CONCURRENT_CALLS=1
LLM_CALL_TIMEOUT_SECONDS=120
EMBEDDING_MAX_CONCURRENCY=1
TIDB_STARTER_FREE_PLAN_MODE=true
TOKENIZERS_PARALLELISM=false
BM25_AUTO_REBUILD_ENABLED=false
```

TiDB uses one adapter connection per process. SQL packet access and logical
write transactions are serialized separately so another Discord task cannot
accidentally commit or roll back someone else's write.

## Verification

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q .
.venv/bin/python scripts/verify_bang_commands.py
.venv/bin/python scripts/audit_tracked_secrets.py --secret-env .env
```

Production audits are read-only and use a stale-read transaction that physically
rejects writes:

```bash
.venv/bin/python scripts/inspect_runtime_readonly.py \
  --expected-profile masamo --expected-db masamong
.venv/bin/python scripts/audit_tidb_free_plan_readonly.py \
  --expected-profile masamo --expected-db masamong
```

Live smoke scripts can spend real provider calls. Read each script's `--help`
and call budget before running it.

## Further documentation

- [Architecture](ARCHITECTURE.en.md)
- [UML and runtime sequences](UML_SPEC.en.md)
- [Instance separation](INSTANCE_SEPARATION.ko.md)
- [Deployment and rollback](../DEPLOYMENT.md)
- [Operational settings](SETTINGS_GUIDE.md)
- [School notices](SCHOOL_NOTICE.ko.md)
- [Transfer notices](TRANSFER_NOTICE.ko.md)
- [Measured RAG analysis](RAG_ANALYSIS.ko.md)
- [Non-destructive memory migration](MEMORY_INDEX_MIGRATION.ko.md)

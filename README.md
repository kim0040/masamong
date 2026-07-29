# Masamong

Masamong is a Korean-first Discord assistant with AI chat, bounded external-tool
use, optional conversation memory, weather and finance lookup, image generation,
fortune readings, community utilities, and an optional personalized school-notice
service.

[한국어 제품·사용 설명서](docs/README.ko.md) ·
[General/Masamo separation](docs/INSTANCE_SEPARATION.ko.md) ·
[Operations and deployment](DEPLOYMENT.md) ·
[School-notice integration](docs/SCHOOL_NOTICE_INTEGRATION_PLAN.ko.md) ·
[Transfer-notice subscriptions](docs/TRANSFER_NOTICE.ko.md) ·
[Memory-index migration](docs/MEMORY_INDEX_MIGRATION.ko.md)

## Runtime model

- Python 3.10+ and `discord.py` 2.7.1+
- TiDB in production; SQLite for development and isolated tests
- OpenAI-compatible and Gemini LLM providers with primary/fallback lanes
- Optional local embedding/RAG stack from `requirements-cpu.txt`
- One shared code release, with physically isolated `general` and `masamo`
  runtime profiles

The two production editions are not forks:

| Boundary | Masamo | General |
|---|---|---|
| Purpose | Existing community deployment | New clean deployment |
| Discord app/token | Existing app and token | Separate app and token |
| Database | Existing TiDB `masamong` | New TiDB `masamong_general` |
| Memory | Existing Discord and Kakao data | Discord only; no Masamo data |
| Configuration | Dedicated env/config/prompt/embedding files | Separate dedicated files |
| Administration | Masamo-only superadmin env and Masamo DB registrations | Separate General superadmin env and General DB registrations |
| Logs/service | Dedicated paths and service | Separate paths and service |
| School notices | Owns the existing rollout, schema, state, and 05:00 timer | Disabled by default; may later use only General-owned paths and state |
| Transfer notices | Owns its subscription tables, public snapshot, and 05:35 timer | Disabled by default; never shares Masamo state |

Never point both profiles at the same token, database, DB account, writable path,
prompt file, embedding store, administrator list, log, or service. The existing
Masamo database keeps its current name and data; it is not renamed, copied into
General, or rebuilt.

## Features

- AI conversation in configured guild channels and DMs
- Fast local routing for obvious small talk/tool requests; optional LLM intent
  routing for ambiguous requests
- Web search with normalized source URLs, same-query singleflight, a bounded
  non-JavaScript fetch path, and one final answer-model call; verified sources
  are rendered directly in the Discord response
- KMA observation, six-hour nowcast, short/mid-range forecast, active warning,
  earthquake, typhoon/impact outlook, finance, exchange-rate, web/news, and
  image tools
- Discord and optional Kakao memory with hybrid retrieval
- Daily/monthly/yearly fortune, zodiac, and persistent morning briefing
- Activity ranking, restart-safe incremental conversation summaries, polls,
  localization, and guild persona settings
- Optional school-notice personalization for registered users and supported
  schools
- Optional DM-only subscription to 20 official transfer-admissions notice
  sources, with bounded list/detail-page extraction and TOEIC/public-English
  caveats
- Three-level administration: Discord server managers are limited to their own
  guild settings, registered instance admins can inspect only their profile
  runtime, and env-pinned superadmins alone can register admins or create invite
  links

Database personas are keyed and cached by `guild_id`; static deployments bind
personas to Discord's globally unique `channel_id`. Normal chat, creative command
responses, and routine AI-written notices resolve only the destination
guild/channel persona. Conversation and RAG reads additionally require the same
guild and channel. Shared emergency notices such as earthquake alerts never use
a guild persona or an LLM: every destination receives the same fixed, formal
KMA-based message.

All external LLM calls share bounded concurrency, queue-acquisition timeout,
per-call timeout, provider rate limits, prompt/output caps, and finite
primary/fallback attempts. A single AI turn can plan at most three tool calls.
Fortune generation and school collection also use explicit finite attempt and
runtime limits; neither scheduler retries indefinitely.

Discord component callbacks acknowledge the interaction before remote DB or API
work. Views, modals, and slash commands share a private terminal error response,
so an internal exception does not leave only Discord's “application did not
respond” banner. Menu, consent, and notice-feedback clicks do not call an LLM;
they use only Discord and the bounded database operation required by that action.

Each logical LLM request is reserved before provider access against global,
feature, guild-or-DM, user, and DM-user minute/day budgets. Web searches use
separate global, guild/DM, and user budgets in addition to Linkup's persisted
monthly EUR ceiling. Image attempts are reserved before the provider against
rolling user, daily guild, and daily global limits. These checks fail closed
when the usage store is unavailable, preventing an error loop from becoming an
unbounded provider loop.

Rate-limit checks query only their indexed one-minute/day windows and do not
prune historical `api_call_log` rows. Operational history therefore remains
available unless an explicitly documented user deletion command targets its own
feature-derived records.

Conversation context labels every speaker and marks the current requester.
Follow-up search expansion uses only that requester's earlier turns, so one
member's subject cannot silently become another member's query. Retrieved web
content is treated as untrusted evidence rather than instructions. The search
result and tool context feed the common response path, which calls the main
answer model once instead of creating a preliminary search answer and then
calling the answer model again.

Optional NumPy, SentenceTransformer, Transformers, Torch, and model construction
run in executor threads on first use, including their heavy import phase. A load
failure enters a bounded cooldown instead of retrying on every message, so model
startup cannot block the Discord heartbeat or create a tight reload loop.

The earthquake monitor checks every 60 seconds, but it cannot precede KMA's
publication time. It shows both occurrence and KMA publication time. Its
persistent watermark is advanced before delivery, and the first deployment
records the newest already-published event without broadcasting it, so a restart
does not replay an earlier earthquake or aftershock. Events within the configured
time window and epicentral radius are displayed as one probable sequence:
the first alert creates a Discord message, and later events edit that same
message with the total count, maximum magnitude, and latest follow-ups. A distant
or later independent earthquake starts a new message. This grouping is clearly
labelled as an automatic display aid, not an official aftershock determination.
Message IDs are persisted in `system_counters`, so editing continues after a bot
restart.

## Privacy boundary

Ordinary Discord conversation and information supplied by Discord servers retain
their existing behavior. Separately collected, reusable personal profiles require
purpose-specific consent:

- `fortune`: Discord user ID and birth date, plus optional birth time, gender, and
  birthplace
- `school_notice`: required Discord user ID, school, degree program, and
  undergraduate grade; only user-supplied campus/department/status/topics/time
  and notice feedback are optional
- `transfer_notice`: Discord user ID, selected university IDs, and subscription
  state only; no TOEIC score, education history, intended major, real name, or
  contact details

Consent is requested in DM and is recorded only after the same user presses the
consent button. Current consent and append-only consent events are stored
separately.

```text
!개인정보
!개인정보 동의 운세
!개인정보 동의 학교공지
!개인정보 동의 편입공지
!개인정보 철회 운세
!개인정보 철회 학교공지
!개인정보 철회 편입공지
```

Withdrawal stops future profile use, personalization, and automatic delivery but
preserves stored data and settings for a possible later re-consent. Explicit
deletion is separate:

```text
!운세 삭제
!공지 삭제
!편입 삭제
```

Those deletion commands remove the corresponding feature profile and derived
personalization state and withdraw consent. They do not delete ordinary Discord
conversation or server records. Consent/audit events remain as the processing
history.

## Discord menu, context, and memory

In a guild, the prefix command itself posts a small launcher because Discord
prefix messages cannot be ephemeral. Pressing **Open my menu** returns the full
dashboard only to the caller. The dashboard does not flatten every command into
one screen: it first shows **School/Transfer, AI/Search, Weather/Disaster,
Fortune, Community, Personal settings, Server administration, and Help**. A
selection replaces the view with only that category's actions. School and
transfer dashboards and personal settings are visibly disabled in a guild and
point to DM; no personal setting or result is read for the public launcher.
Weather, image, poll, ranking, summary, fortune, and notification-time actions
can be run from their category, including modal input where arguments are
required. Each member must run their own `!메뉴`, and another member cannot use
its button.

`!요약` persists only its per-guild/per-channel anchor and bounded summary in
`channel_summary_state`. This lets the next summary continue after a restart
without reprocessing the whole channel. The table is additive, and unrelated
conversation, embedding, profile, and delivery rows are neither replaced nor
deleted.

Ordinary AI turns use a separate, non-persistent three-level context: the newest
turns verbatim, an on-demand digest of the older portion of the same bounded
Discord history, and long-term RAG only when the semantic router says older
memory is required. Tool routing is semantic in the normal path; keyword rules
exist only as a provider-outage fallback. The digest is returned by the same
routing call, so it does not add another LLM request.

Structured-memory embeddings use a lean `independent summary + speaker-labelled
evidence` passage. Memory type, speakers, date, and keywords remain separate
metadata instead of being repeated into the E5 vector text. The reproducible
synthetic retrieval check is:

New structured units use explicit `guild`, `guild_user`, `dm`, and `dm_user`
scopes. Guild-wide facts can be recalled across channels only inside the same
guild, while a current member's facts are additionally retrieved from that
guild's user scope. DM units remain tied to that user's DM channel. Legacy
channel/user units stay readable without being rewritten. The response prompt
forbids mentioning retrieved preferences or events unless they materially help
the current request. Image prompts use the same relevant-only rule and never
infer sensitive traits or a person's appearance when the conversation did not
provide them.

```bash
<venv>/bin/python scripts/evaluate_memory_retrieval_offline.py
```

## Administration boundary

`MASAMONG_SUPERADMIN_USER_IDS` belongs to one runtime profile and is never copied
between Masamo and General. The real Discord user ID stays only in the protected
runtime env; tracked examples use a placeholder. General intentionally starts
with an empty, separately chosen list. Discord members with **Manage Server** or the guild owner can use
`/config` and `/persona`, but every read/write is keyed only to that guild.

Registered instance admins live in `bot_admin_accounts` with `instance_name` in
the primary key. A superadmin manages them in bot DM with `!관리 추가`, `!관리
제거`, and `!관리 목록`; removal disables the row instead of deleting it.
Registered instance admins do not inherit Discord server permissions. `!관리`
opens a caller-only panel in a guild, while `!초대` and the invite button are
superadmin-only and deliver the OAuth link privately.

## School-notice behavior

The always-on Discord process never imports and runs the crawler in its event
loop. The vendored `school_notice` package runs in a separate bounded process:
once for the registering user immediately after first confirmation, and later
from the `05:00` systemd one-shot. Both paths publish validated JSON digests; the
bot handles onboarding, status, delivery, and feedback.

This is public-HTML crawling, not a university API integration. The fetcher reads
version-controlled public list/detail URLs with source-specific CSS selectors,
robots/host/redirect/request-size guards, no cookie jar, and no proxy inheritance.
It never sends a Discord ID, student profile, user input, or interest data to a
school site. Image-only notices remain linked candidates but are explicitly
labelled as requiring the original image/attachment to be checked.

The user flow is:

1. In DM, the user opens `!메뉴`/`!공지`, presses the setup button, or simply says
   “전북대 소프트웨어공학과 3학년 공지를 오전 9시에 알려줘.”
2. Masamong first uses its bounded local parser. Clear input makes no profile-LLM
   call. Only unresolved input may call the routing primary once for that
   interpretation attempt, with no provider fallback or retry; the whole session is
   capped at three provider calls by default.
3. The user confirms, corrects the summary in natural language, or cancels.
4. Nothing is stored until confirmation. New profiles default to `09:00` KST, and
   the user can choose another delivery time.
5. On a genuinely new profile, a one-user, one-thread, no-LLM process immediately
   checks only that school's sources. It has a finite timeout and at most one
   retry for a batch-lock collision.
6. The `05:00` KST batch later selects sources only for consented, enabled,
   registered profiles. It does not crawl every school.
7. Normally the 05:00 digest is delivered later that day at the user's selected
   time. After an outage, delivery considers only the newest valid result from the
   previous three days and never falls back behind a newer successful batch. If no
   relevant notice exists, no automatic DM is sent.

Long automatic digests are sent in bounded pages. Each successfully sent revision
is recorded immediately, and any remaining page continues on a later one-minute
scheduler tick without consuming a failure attempt for the successful page.

The versioned catalog currently covers 14 universities and 17 source IDs:
Jeonbuk, Seoul National, Pusan National, Korea, Jeonju, Sungkyunkwan, Gachon,
Soongsil, Chonnam National, Sunchon National, Myongji, Konkuk, Kookmin, and
Hanyang. Korea University has both a general academic board and a scoped computer
science board. Known department, campus, and degree mismatches are excluded before
crawling; scoped-board results are also hidden when the required affiliation is
unknown. General university boards remain available with the minimum profile. A
school is usable only when the separately deployed core has matching, validated
source definitions.

The collection core is vendored in `school_notice/` and released with the bot.
Masamo owns the current school-notice rollout and its isolated writable paths.
General starts with the feature disabled and must never point at Masamo's school
tables, core database, digests, or timer. The current wrapper guarantees
“consented, enabled, registered profiles only”; it does not crawl all catalogued
schools.

## Transfer-admissions notices

Transfer notices are a separate DM-only subscription service. A user opens
`!편입`, explicitly consents, and selects one to twenty universities. The public
collector checks official list pages sequentially from `05:35` KST. After the
first non-delivery baseline it fetches only bounded new, changed, and latest
detail-page candidates per source. It extracts a short evidence-based summary
and key dates without receiving a Discord ID or subscriber profile. The bot
reads the bounded JSON result and DMs only active subscribers whose selected
source has a genuine new item or meaningful list-title revision, at that
subscriber's selected time (default `09:00` KST). Detail text supports the
summary, but detail-only fingerprint changes never create a notification:
official sites frequently change view counts and shared navigation without
changing the notice.

The first successful collection for every source is a non-delivery baseline.
Per-user `(source, external ID, revision)` delivery records prevent replay after a
restart. Failed DMs have finite attempts and a durable payload, while cancellation,
selection changes, or consent withdrawal invalidate older retries. A malformed
retry payload is terminal rather than looped forever.

The catalog is exactly 20 official sources. “TOEIC/public English” means the
school has a relevant year or recruiting unit where public-English evidence may
matter; it does not mean every department uses TOEIC. Rules can change yearly, so
every alert directs the user to the final official guide. Each batch records
per-school `healthy`, `degraded`, or `failed` status. `robots.txt` restrictions,
WAFs, and official maintenance are not bypassed, and the dashboard reports the
latest healthy-source count instead of silently presenting an incomplete run as
fully successful.

School and transfer features are unavailable in guild channels, including their
menu buttons and natural-language entry points. The guild path only explains how
to continue in DM and performs no personal-profile read or write.

## Quick start

```bash
git clone https://github.com/kim0040/masamong.git
cd masamong
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Install the CPU-only memory stack only when local embedding/RAG is enabled:

```bash
python -m pip install -r requirements-cpu.txt
```

For local development, copy the root examples and fill in a separate development
token and database. For production, do not reuse the root `.env`; install a strict
profile file based on `profiles/general.env.example` or by preserving and
augmenting the existing Masamo environment. Select it from outside the process:

```bash
MASAMONG_ENV_FILE=/absolute/path/to/profile.env \
  PYTHONPATH=. .venv/bin/python main.py
```

Explicit profiles fail closed when their profile/instance identity, bot user ID,
database identity, TLS settings, required files, required Cogs, or resource limits
do not match.

## Common user commands

| Command | Behavior |
|---|---|
| `@Masamong <message>` | AI conversation in an allowed guild channel |
| DM message | Private AI conversation, subject to DM and LLM limits |
| `!메뉴`, `!도움` | Hierarchical category menu and complete text help; guild details are caller-only and DM-only actions are disabled |
| `!날씨 [지역] [날짜]` | KMA observation, six-hour outlook, forecast, and active warnings |
| `!이미지 <prompt>` | `gemini-3.1-flash-lite-image` generation with relevant memory and user/guild/global quota guards |
| `!운세`, `!운세 상세` | Daily summary or detailed fortune |
| `!운세 등록` | Consent-gated DM registration |
| `!운세 구독 HH:MM` | Persistent morning briefing |
| `!이번달운세`, `!올해운세` | Detailed monthly/yearly readings |
| `!별자리` | Zodiac information/ranking |
| `!공지` | Open the school dashboard; `!공지 1` views digest page 1 |
| `!공지 등록 <natural language>` | Confirm-before-save school onboarding |
| `!공지 수정 <natural language>` | Confirm-before-save profile correction |
| `!공지 정보` | Show the saved profile and delivery state |
| `!공지 상태` | Show the latest registered-school collection status |
| `!공지 시간 HH:MM` | Set a per-user delivery time |
| `!공지 중지`, `!공지 재개` | Pause/resume school processing and delivery |
| `!편입` | Open the DM-only 20-school selection dashboard |
| `!편입 최근`, `!편입 상태` | Show recent official notices or subscription state |
| `!편입 구독취소`, `!편입 재개` | Pause/resume without deleting university choices |
| `!편입 삭제` | Delete choices and delivery state, then withdraw consent |
| `!랭킹`, `!요약`, `!투표 ...` | Community utilities |
| `/config`, `/persona` | Guild AI policy and persona |
| `!관리` | Caller-only administration panel at the authorized server/profile scope |
| `!초대` | Superadmin-only private bot invite button |

Some commands and schedulers are feature-flagged or permission-restricted.

## Resource and API safety

Low-spec deployments should explicitly set, not merely inherit:

```dotenv
MASAMONG_CPU_THREADS=1
MASAMONG_EXECUTOR_WORKERS=1
AI_MAX_CONCURRENT_PROCESSING=1
AI_QUEUE_WAIT_TIMEOUT_SECONDS=5
LLM_MAX_CONCURRENT_CALLS=1
LLM_ACQUIRE_TIMEOUT_SECONDS=10
LLM_CALL_TIMEOUT_SECONDS=120
EMBEDDING_MAX_CONCURRENCY=1
RAG_MAX_BACKGROUND_TASKS=2
RAG_MAX_TRACKED_WINDOWS=64
MASAMONG_DISCORD_MAX_MESSAGES=100
TIDB_STARTER_FREE_PLAN_MODE=true
TIDB_STARTER_USAGE_WARNING_RATIO=0.8
STRUCTURED_MEMORY_QUERY_LIMIT=384
STRUCTURED_MEMORY_FALLBACK_QUERY_LIMIT=768
LINKUP_FETCH_RENDER_JS=false
LINKUP_FETCH_JS_RETRY_ENABLED=true
TOKENIZERS_PARALLELISM=false
```

Preserve the actual existing Masamo values during its profile cutover. Start
General with memory and recurring jobs disabled, measure combined CPU/RSS, and
enable only the features that fit the host.

For TiDB Cloud Starter, free-plan mode caps the structured-memory BLOB candidate
set even if an env file requests a larger value. It does not delete or rewrite
existing data. The official free allowance is 5 GiB row storage, 5 GiB columnar
storage, and 50 million RUs per month. The Cloud **Usage this month** panel is
authoritative because SQL-side RU history can omit the current day and network
egress. Run the bounded, non-identifying audit after deployments and during
monthly operations:

```bash
MASAMONG_ENV_FILE=/etc/masamong/masamo.env \
  <venv>/bin/python scripts/audit_tidb_free_plan_readonly.py \
  --expected-profile masamo --expected-db masamong
```

## Verification

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q .
.venv/bin/python -m pip check
.venv/bin/python scripts/verify_bang_commands.py
.venv/bin/python scripts/audit_tracked_secrets.py --secret-env .env
.venv/bin/python scripts/audit_tidb_free_plan_readonly.py \
  --expected-profile masamo --expected-db masamong
.venv/bin/python -m school_notice live-check \
  --details-per-source 2 --max-requests 96 \
  --output-dir /tmp/masamong-school-livecheck
.venv/bin/python scripts/run_transfer_notice_batch.py \
  --source-config transfer_notice/sources.json \
  --database /tmp/masamong-transfer/core.db \
  --output-dir /tmp/masamong-transfer/out \
  --lock-file /tmp/masamong-transfer/batch.lock \
  --max-retries 0 \
  --max-details-per-source 3 \
  --min-request-interval-seconds 0.35
```

The two API smoke scripts are deliberately excluded from the offline suite.
`scripts/smoke_linkup_live.py` reserves at most one standard Linkup search, and
`scripts/smoke_llm_quality_live.py` makes at most one primary answer-model call.
Run them only as an explicit billable production-key check.

The live check performs real public HTML requests, so it is an explicit operator
action rather than part of the offline unit suite. Review every source's list
contract, detail contract, and body-quality status; a selector failure must not
be reported as “no new notices.”

Validate real General and Masamo profile files offline before either service is
started:

```bash
.venv/bin/python scripts/validate_profile_separation.py \
  /etc/masamong/masamo.env \
  /etc/masamong/general.env
```

See [DEPLOYMENT.md](DEPLOYMENT.md) for read-only production fingerprints,
additive privacy/school/transfer/summary-state schema migrations, immutable
release metadata, controlled restart, 05:00/05:35 timers, verification,
post-deploy observation, and rollback. Memory provenance/vector
changes follow the non-destructive shadow strategy in
[docs/MEMORY_INDEX_MIGRATION.ko.md](docs/MEMORY_INDEX_MIGRATION.ko.md).

## Repository map

```text
main.py                         runtime, schema verification, Cog loading
config.py                       strict profile and resource configuration
cogs/                           Discord features
utils/                          LLM, RAG, privacy, weather, school contracts
database/                       SQLite and TiDB schemas/adapters
profiles/                       isolated profile examples and school catalog
school_notice/                  vendored bounded collection/analysis core
transfer_notice/                bounded 20-source public list/detail collector
scripts/                        read-only audits, additive migrations, one-shot jobs
deploy/systemd/                 school/transfer batch service and timer templates
tests/                          functional, contract, safety, and resource tests
docs/                           architecture and operations documentation
```

## License

MIT

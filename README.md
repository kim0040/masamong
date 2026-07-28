# Masamong

Masamong is a Korean-first Discord assistant with AI chat, bounded external-tool
use, optional conversation memory, weather and finance lookup, image generation,
fortune readings, community utilities, and an optional personalized school-notice
service.

[한국어 제품·사용 설명서](docs/README.ko.md) ·
[General/Masamo separation](docs/INSTANCE_SEPARATION.ko.md) ·
[Operations and deployment](DEPLOYMENT.md) ·
[School-notice integration](docs/SCHOOL_NOTICE_INTEGRATION_PLAN.ko.md) ·
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
| Logs/service | Dedicated paths and service | Separate paths and service |
| School notices | Owns the existing rollout, schema, state, and 23:00 timer | Disabled by default; may later use only General-owned paths and state |

Never point both profiles at the same token, database, DB account, writable path,
prompt file, embedding store, log, or service. The existing Masamo database keeps
its current name and data; it is not renamed, copied into General, or rebuilt.

## Features

- AI conversation in configured guild channels and DMs
- Fast local routing for obvious small talk/tool requests; optional LLM intent
  routing for ambiguous requests
- KMA observation, six-hour nowcast, short/mid-range forecast, active warning,
  earthquake, typhoon/impact outlook, finance, exchange-rate, web/news, and
  image tools
- Discord and optional Kakao memory with hybrid retrieval
- Daily/monthly/yearly fortune, zodiac, and persistent morning briefing
- Activity ranking, conversation summary, polls, localization, and guild persona
  settings
- Optional school-notice personalization for registered users and supported
  schools

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

The earthquake monitor checks every 30 seconds, but it cannot precede KMA's
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
- `school_notice`: school/student profile, delivery preferences, and notice
  feedback

Consent is requested in DM and is recorded only after the same user presses the
consent button. Current consent and append-only consent events are stored
separately.

```text
!개인정보
!개인정보 동의 운세
!개인정보 동의 학교공지
!개인정보 철회 운세
!개인정보 철회 학교공지
```

Withdrawal stops future profile use, personalization, and automatic delivery but
preserves stored data and settings for a possible later re-consent. Explicit
deletion is separate:

```text
!운세 삭제
!공지 삭제
```

Those deletion commands remove the corresponding feature profile and derived
personalization state and withdraw consent. They do not delete ordinary Discord
conversation or server records. Consent/audit events remain as the processing
history.

## School-notice behavior

The Discord bot process does not crawl websites. The vendored `school_notice`
package runs in a separate bounded one-shot systemd process and publishes
validated JSON digests; the always-on bot handles onboarding, delivery, and
feedback.

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
5. The `23:00` KST batch selects sources only for consented, enabled, registered
   profiles. It does not crawl every school.
6. Normally the resulting digest is delivered the following day at that user's
   time. After an outage, delivery considers only the newest valid result from the
   previous three days and never falls back behind a newer successful batch. If no
   relevant notice exists, no automatic DM is sent.

Long automatic digests are sent in bounded pages. Each successfully sent revision
is recorded immediately, and any remaining page continues on a later one-minute
scheduler tick without consuming a failure attempt for the successful page.

The versioned catalog currently covers 14 universities and 16 source IDs:
Jeonbuk, Seoul National, Pusan National, Korea, Jeonju, Sungkyunkwan, Gachon,
Soongsil, Chonnam National, Sunchon National, Myongji, Konkuk, Kookmin, and
Hanyang. A school is usable only when the separately deployed core has matching,
validated source definitions.

The collection core is vendored in `school_notice/` and released with the bot.
Masamo owns the current school-notice rollout and its isolated writable paths.
General starts with the feature disabled and must never point at Masamo's school
tables, core database, digests, or timer. The current wrapper guarantees
“consented, enabled, registered profiles only”; it does not crawl all catalogued
schools.

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
| `!메뉴`, `!도움` | Unified feature dashboard and detailed help |
| `!날씨 [지역] [날짜]` | KMA observation, six-hour outlook, forecast, and active warnings |
| `!이미지 <prompt>` | Image generation with user/global quota guards |
| `!운세`, `!운세 상세` | Daily summary or detailed fortune |
| `!운세 등록` | Consent-gated DM registration |
| `!운세 구독 HH:MM` | Persistent morning briefing |
| `!이번달운세`, `!올해운세` | Detailed monthly/yearly readings |
| `!별자리` | Zodiac information/ranking |
| `!공지` | Open the school dashboard; `!공지 1` views digest page 1 |
| `!공지 등록 <natural language>` | Confirm-before-save school onboarding |
| `!공지 수정 <natural language>` | Confirm-before-save profile correction |
| `!공지 정보` | Show the saved profile and delivery state |
| `!공지 시간 HH:MM` | Set a per-user delivery time |
| `!공지 중지`, `!공지 재개` | Pause/resume school processing and delivery |
| `!랭킹`, `!요약`, `!투표 ...` | Community utilities |
| `/config`, `/persona` | Guild AI policy and persona |

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
TOKENIZERS_PARALLELISM=false
```

Preserve the actual existing Masamo values during its profile cutover. Start
General with memory and recurring jobs disabled, measure combined CPU/RSS, and
enable only the features that fit the host.

## Verification

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q .
.venv/bin/python -m pip check
```

Validate real General and Masamo profile files offline before either service is
started:

```bash
.venv/bin/python scripts/validate_profile_separation.py \
  /etc/masamong/masamo.env \
  /etc/masamong/general.env
```

See [DEPLOYMENT.md](DEPLOYMENT.md) for read-only production fingerprints,
additive privacy/school schema migrations, controlled restart, 23:00 timer
verification, post-deploy observation, and rollback. Memory provenance/vector
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
scripts/                        read-only audits, additive migrations, one-shot jobs
deploy/systemd/                 school batch service/timer templates
tests/                          functional, contract, safety, and resource tests
docs/                           architecture and operations documentation
```

## License

MIT

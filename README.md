# Masamong (마사몽)

A Korean-first Discord bot that chats, remembers, and quietly watches the things
you'd otherwise have to check yourself — the weather, your school's notice board,
transfer-admission announcements.

It talks like a friend, not like a form. Ask it something in a channel and it
answers; ask it about a conversation from three months ago and it goes looking.

**[한국어 사용 설명서 →](docs/README.ko.md)**

```
!메뉴          기능을 버튼으로 골라보기
!날씨 전주      기상청 실황·예보·특보
!공지          내 학교 맞춤 공지
!운세          오늘의 운세
@마사몽 안녕    그냥 말 걸기
```

---

## What it does

**Conversation.** Mention it in an allowed channel, or DM it directly. It routes
each turn semantically — deciding whether to call a tool, search the web, or dig
through long-term memory — and answers once. A single turn plans at most three
tool calls.

**Memory.** Conversations are summarized into structured memories and embedded
locally, so "우리 저번에 정한 여행 날짜 언제였지" has somewhere to look. Memory
is scoped: guild-wide facts stay in that guild, DM facts stay in that DM, and
one member's private memories are never surfaced to another.

**Tools.** KMA weather (observation, six-hour nowcast, short/mid-range forecast,
active warnings, earthquakes, typhoons), stocks and exchange rates, web and news
search with visible sources, and image generation.

**School notices.** Tell it your school, program, and year in plain Korean. Every
morning it checks only your school's public boards and DMs you what's actually
relevant, with the reason it picked each notice. 14 universities, 17 boards.

**Transfer-admission notices.** A DM-only subscription to 20 official admissions
offices. It tells you when something new is posted, and always points you back to
the official guide.

**Fortune, polls, rankings, channel summaries.** The usual community things.

## Requirements

- Python 3.10+
- `discord.py` 2.7.1+
- SQLite for development, TiDB for production
- An OpenAI-compatible or Gemini LLM endpoint
- Optional: `requirements-cpu.txt` for the local embedding/RAG stack

## Quick start

```bash
git clone https://github.com/kim0040/masamong.git
cd masamong
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env        # fill in a bot token and an LLM key
python main.py
```

For memory and RAG, also install the CPU stack:

```bash
python -m pip install -r requirements-cpu.txt
```

For production, don't reuse the root `.env`. Install a profile file and select it
from outside the process:

```bash
MASAMONG_ENV_FILE=/etc/masamong/masamo.env PYTHONPATH=. .venv/bin/python main.py
```

Profiles fail closed: if the profile name, bot user ID, database identity, TLS
settings, required files, required Cogs, or resource limits don't match what the
file declares, the process refuses to start rather than running against the wrong
database. See [Instance separation](docs/INSTANCE_SEPARATION.ko.md).

## Commands

| Command | What it does |
|---|---|
| `@마사몽 <말>` / DM | Talk to it |
| `!메뉴`, `!도움` | Button menu / full command list |
| `!날씨 [지역] [날짜]` | KMA observation, forecast, active warnings |
| `!이미지 <설명>` | Generate one image |
| `!요약` | Summarize recent channel conversation |
| `!공지` | School notice dashboard (DM) |
| `!공지 등록 <자연어>` | Register a school, confirm before saving |
| `!공지 시간 HH:MM` | Set your delivery time |
| `!편입` | Transfer-notice subscription (DM) |
| `!운세`, `!운세 등록`, `!운세 구독 HH:MM` | Fortune and morning briefing |
| `!별자리` | Zodiac readings and ranking |
| `!랭킹`, `!투표 ...` | Activity ranking, polls |
| `!개인정보` | Consent status, withdrawal |
| `!관리`, `!초대` | Admin-only, private |

Some commands are feature-flagged or DM-only.

## Two editions, one codebase

Masamong runs as two physically isolated profiles from a single release:
`masamo` (the existing community deployment) and `general` (a clean deployment).
They never share a token, database, writable path, prompt file, embedding store,
admin list, log, or service.

| | Masamo | General |
|---|---|---|
| Database | existing TiDB `masamong` | TiDB `masamong_general` |
| Memory | Discord + Kakao history | Discord only |
| School/transfer notices | owns the rollout | disabled by default |

The full boundary is documented in
[INSTANCE_SEPARATION.ko.md](docs/INSTANCE_SEPARATION.ko.md).

## Privacy

Ordinary Discord conversation behaves as you'd expect. Reusable personal
profiles — fortune, school notices, transfer notices — each require their own
explicit consent, requested in DM and recorded only after you press the button.

```bash
!개인정보                    # see what you've consented to
!개인정보 동의 학교공지        # consent to one feature
!개인정보 철회 학교공지        # stop using it, keep the data
!공지 삭제                   # delete the data and withdraw
```

School crawling never sends your Discord ID, department, year, or interests to a
school website. Consent and withdrawal events are kept as processing history.

## Running it safely

Every external LLM call goes through bounded concurrency, per-call timeouts,
provider rate limits, and finite retry counts. Requests are reserved against
global, per-feature, per-guild, per-user, and per-DM budgets *before* the
provider is touched, and these checks fail closed — an error loop can't become an
unbounded billing loop.

Questions that require current or niche external facts are fail-closed: the bot
must obtain a successful source-backed tool result before it can answer. A failed
lookup produces an explicit “not verified” response instead of an ungrounded
guess or a promise to search later. Market briefs additionally batch-check the
latest available KOSPI/KOSDAQ or US index session and reject material numbers
that do not appear in the tool evidence.

On a low-spec host, set these explicitly rather than inheriting defaults:

```dotenv
MASAMONG_CPU_THREADS=1
MASAMONG_EXECUTOR_WORKERS=1
AI_MAX_CONCURRENT_PROCESSING=1
LLM_MAX_CONCURRENT_CALLS=1
LLM_CALL_TIMEOUT_SECONDS=120
EMBEDDING_MAX_CONCURRENCY=1
TIDB_STARTER_FREE_PLAN_MODE=true
TOKENIZERS_PARALLELISM=false
```

## Development

```bash
.venv/bin/python -m pytest -q                        # full offline suite
.venv/bin/python -m compileall -q .
.venv/bin/python scripts/verify_bang_commands.py     # command surface
.venv/bin/python scripts/audit_tracked_secrets.py --secret-env .env
```

Read-only production audits (they cannot write — TiDB stale-read transactions
reject it):

```bash
.venv/bin/python scripts/inspect_runtime_readonly.py --expected-profile masamo --expected-db masamong
.venv/bin/python scripts/audit_tidb_free_plan_readonly.py --expected-profile masamo --expected-db masamong
.venv/bin/python scripts/audit_memory_quality_readonly.py --expected-profile masamo --expected-db masamong
```

Live smoke scripts (`scripts/smoke_*.py`) each spend at most one real API call
and are deliberately excluded from the offline suite.

## Documentation

| Doc | What's in it |
|---|---|
| [README.ko.md](docs/README.ko.md) · [ja](docs/README.ja.md) | 사용 설명서 — 기능별 사용법 |
| [ARCHITECTURE.ko.md](docs/ARCHITECTURE.ko.md) · [en](docs/ARCHITECTURE.en.md) | Internals: layers, data flow, module boundaries |
| [UML_SPEC.ko.md](docs/UML_SPEC.ko.md) · [en](docs/UML_SPEC.en.md) | Class/sequence diagrams |
| [INSTANCE_SEPARATION.ko.md](docs/INSTANCE_SEPARATION.ko.md) | Masamo/General boundary and cutover |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Migrations, systemd timers, rollback |
| [SETTINGS_GUIDE.md](docs/SETTINGS_GUIDE.md) | Every configuration key |
| [SCHOOL_NOTICE.ko.md](docs/SCHOOL_NOTICE.ko.md) | School-notice design and contracts |
| [TRANSFER_NOTICE.ko.md](docs/TRANSFER_NOTICE.ko.md) | Transfer-notice design |
| [RAG_ANALYSIS.ko.md](docs/RAG_ANALYSIS.ko.md) | Memory retrieval: what was measured and why it failed |
| [RAG_IMPROVEMENT_PLAN.ko.md](docs/RAG_IMPROVEMENT_PLAN.ko.md) | Staged plan to fix it |
| [MEMORY_INDEX_MIGRATION.ko.md](docs/MEMORY_INDEX_MIGRATION.ko.md) | Non-destructive embedding re-index |

## Repository layout

```text
main.py              runtime, schema verification, Cog loading
config.py            profile resolution and resource limits
cogs/                Discord features
utils/               LLM, RAG, privacy, weather, contracts
database/            SQLite and TiDB schemas/adapters
profiles/            profile examples and the school catalog
school_notice/       vendored school collection core
transfer_notice/     transfer-admissions collector
scripts/             read-only audits, migrations, one-shot jobs
deploy/systemd/      batch service and timer templates
tests/               offline tests
docs/                architecture and operations
```

## License

MIT

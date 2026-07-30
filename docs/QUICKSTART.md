# 마사몽 빠른 시작

이 문서는 새 로컬 개발 인스턴스를 실행하고 기본 동작을 확인할 수 있는 최소 절차입니다.
기존 Masamo 운영 DB에는 연결하지 않으며, 개발용 SQLite 프로필로 시작합니다. 기능 사용법은
[사용자 가이드](README.ko.md), 전체 문서는 [문서 허브](README.md)에서 확인할 수 있습니다.

## 1. 준비

- Python 3.10 이상
- Discord Bot Token
- CometAPI 등 OpenAI 호환 LLM 키

```bash
git clone https://github.com/kim0040/masamong.git
cd masamong
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

로컬 임베딩/RAG까지 사용할 때만 CPU 패키지를 추가한다.

```bash
python -m pip install -r requirements-cpu.txt
```

## 2. 설정 파일

```bash
cp .env.example .env
cp prompts.example.json prompts.json
cp emb_config.example.json emb_config.json
```

`.env`에서 최소한 다음 값을 채운다.

```dotenv
DISCORD_BOT_TOKEN=replace_me
COMETAPI_KEY=replace_me

LLM_ROUTING_PRIMARY_PROVIDER=openai_compat
LLM_ROUTING_PRIMARY_MODEL=gpt-5.4-nano
LLM_ROUTING_PRIMARY_BASE_URL=https://api.cometapi.com/v1
LLM_ROUTING_PRIMARY_API_KEY=${COMETAPI_KEY}

LLM_MAIN_PRIMARY_PROVIDER=openai_compat
LLM_MAIN_PRIMARY_MODEL=deepseek-v4-flash
LLM_MAIN_PRIMARY_BASE_URL=https://api.cometapi.com/v1
LLM_MAIN_PRIMARY_API_KEY=${COMETAPI_KEY}

MASAMONG_DB_BACKEND=sqlite
BM25_AUTO_REBUILD_ENABLED=false
```

개발용 SQLite 파일과 운영 TiDB는 별개다. 운영 프로필을 실수로 재사용하지 않는다.
봇 토큰·API 키·DB 암호는 `.env`, `/etc/masamong/*.env` 밖으로 복사하거나 Git에
추가하지 않는다.

## 3. 사전 점검과 실행

```bash
.venv/bin/python scripts/verify_bang_commands.py
.venv/bin/python main.py
```

Discord에서 다음을 확인한다.

```text
@마사몽 안녕
!메뉴
!날씨 전주
```

서버 대화는 허용된 채널에서 봇 멘션이 필요하다. DM 대화는 멘션이 필요 없다.
학교·편입 기능은 DM 전용이고, 재사용 개인정보를 저장하기 전에 동의 버튼이 나온다.

## 4. 운영 프로필

기존 Masamo는 루트 `.env`가 아니라 외부 프로필로 실행한다.

```bash
MASAMONG_ENV_FILE=/etc/masamong/masamo.env \
PYTHONPATH=. \
.venv/bin/python scripts/validate_profile_separation.py \
  /etc/masamong/masamo.env \
  /etc/masamong/general.env
```

```bash
MASAMONG_ENV_FILE=/etc/masamong/masamo.env \
PYTHONPATH=. \
.venv/bin/python main.py
```

명시적 운영 프로필은 봇 ID, DB, TLS, 경로, 필수 Cog, scheduler 소유권, CPU/동시성
한도가 맞지 않으면 기동하지 않는다. 저사양 서버에서는 다음을 반드시 명시한다.

```dotenv
MASAMONG_CPU_THREADS=1
MASAMONG_EXECUTOR_WORKERS=1
AI_MAX_CONCURRENT_PROCESSING=1
AI_QUEUE_MAX_SIZE=8
LLM_MAX_CONCURRENT_CALLS=1
EMBEDDING_MAX_CONCURRENCY=1
TIDB_STARTER_FREE_PLAN_MODE=true
TOKENIZERS_PARALLELISM=false
BM25_AUTO_REBUILD_ENABLED=false
```

BM25/FTS5는 운영 서버에서 만들거나 조회하지 않는다. 기존 기억은 임베딩 의미 검색과
TiDB 벡터 경로를 사용한다.

## 5. 회귀 테스트

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q .
.venv/bin/python scripts/audit_tracked_secrets.py --secret-env .env
```

실제 API를 쓰는 `scripts/smoke_*.py`는 오프라인 테스트에 포함되지 않는다. 실행 전
각 스크립트의 `--help`, 최대 호출 수, 예상 비용을 확인한다.

운영 DB 점검은 쓰기가 거부되는 read-only 스크립트만 사용한다.

```bash
.venv/bin/python scripts/inspect_runtime_readonly.py \
  --expected-profile masamo --expected-db masamong
.venv/bin/python scripts/audit_tidb_free_plan_readonly.py \
  --expected-profile masamo --expected-db masamong
```

자세한 내용은 [설정 가이드](SETTINGS_GUIDE.md),
[인스턴스 분리](INSTANCE_SEPARATION.ko.md),
[배포와 롤백](DEPLOYMENT.md)에서 확인할 수 있습니다.

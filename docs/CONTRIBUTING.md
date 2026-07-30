# 마사몽 기여 가이드

마사몽은 기존 Masamo 운영 데이터와 새 General 인스턴스를 한 코드베이스에서
관리합니다. 변경을 제안하거나 구현할 때는 기능 동작뿐 아니라 인스턴스·개인정보·비용
경계도 함께 지킬 수 있어야 합니다. 제품과 운영 문서의 시작점은
[문서 허브](README.md)에서 확인할 수 있습니다.

## 개발 환경

```bash
git clone https://github.com/kim0040/masamong.git
cd masamong
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
cp prompts.example.json prompts.json
cp emb_config.example.json emb_config.json
```

로컬 임베딩 테스트가 필요할 때만 `requirements-cpu.txt`를 추가한다. 개발은 SQLite
프로필을 사용하고, `/etc/masamong/masamo.env` 또는 운영 DB 자격 증명을 복사하지 않는다.

## 반드시 지킬 경계

- 기존 DB 행을 삭제·초기화·덮어쓰는 마이그레이션을 기본 동작으로 만들지 않는다.
- 모든 데이터·캐시·프롬프트 조회는 `instance_name`, DM/guild, channel, user
  스코프를 유지한다.
- 학교·편입·운세의 재사용 개인정보는 저장 전 기능별 명시 동의를 확인한다.
- 학교 웹 요청에는 Discord ID, 학과, 학년, 관심사 같은 사용자 정보를 보내지 않는다.
- 외부 사실·시장 수치는 성공한 도구 근거가 없으면 생성하지 않는다.
- LLM·이미지·웹 검색 호출은 timeout, 유한 retry, 동시성, 계층형 할당량을 거친다.
- 운영 저사양 프로필에서는 BM25/FTS5를 만들거나 조회하지 않는다.
- 비밀키, 토큰, DB 주소·계정·암호, 실제 사용자 개인정보를 Git에 기록하지 않는다.

## 구현 원칙

- Discord 상호작용은 먼저 acknowledge/defer하고, 후속 오류도 사용자에게 설명한다.
- 백그라운드 작업은 한 tick의 처리량과 시간을 제한하고, 재시작 중복 전송을 DB
  상태로 막는다.
- TiDB 쓰기는 성공 시 commit, 실패 시 rollback한다. 예외를 기록만 하고 같은 연결을
  계속 사용하지 않는다.
- 블로킹 SDK는 `asyncio.to_thread`, 외부 비동기 호출은 명시적 timeout을 사용한다.
- 메모리·cooldown·singleflight 캐시는 최대 크기나 TTL이 있어야 한다.
- 서버 말투는 해당 guild에서만 적용하고, 재난 공통 메시지는 형식적 문구를 사용한다.

## 테스트

기능 수정에는 정상·실패·시간초과·재시작/중복·스코프 분리 시나리오를 함께 추가한다.
실제 API 호출은 오프라인 단위 테스트에서 mock한다.

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q main.py config.py cogs utils database \
  scripts school_notice transfer_notice tests
.venv/bin/python scripts/verify_bang_commands.py
.venv/bin/python scripts/audit_tracked_secrets.py --secret-env .env
git diff --check
```

`scripts/smoke_*.py`는 실제 provider 비용을 쓸 수 있다. 실행 전 `--help`, 최대 호출
수, 대상 프로필을 확인한다. 운영 DB 확인은 다음 read-only 스크립트만 사용한다.

```bash
.venv/bin/python scripts/inspect_runtime_readonly.py \
  --expected-profile masamo --expected-db masamong
.venv/bin/python scripts/audit_tidb_free_plan_readonly.py \
  --expected-profile masamo --expected-db masamong
```

## 새 기능 추가

1. 기존 Cog·서비스와 중복되는 책임이 없는지 확인한다.
2. 명령어라면 DM 전용 여부, 메뉴 노출, 권한 없는 사용자에게 숨길 항목을 정한다.
3. DB 변경은 additive DDL과 기존 행 수 전후 검증을 사용한다.
4. 외부 API면 timeout, 오류 계약, circuit breaker, 호출 할당량을 연결한다.
5. `config.py`, `.env.example`, 해당 프로필 예제, README, 아키텍처/UML을 함께 갱신한다.
6. 오프라인 테스트와 명령어 표면 검사를 통과시킨다.

Cog는 `async def setup(bot)`으로 등록하고, 명시적 프로필의 필수 Cog라면
`MASAMONG_REQUIRED_COGS` 및 프로필 검증 테스트도 갱신한다.

## 커밋과 리뷰

작은 논리 단위로 커밋하고 제목은 변경 결과를 설명한다.

```text
fix: Keep TiDB writes inside one task-owned transaction
feat: Add private school notice confirmation flow
docs: Align deployment guide with explicit profiles
```

리뷰에서는 다음을 확인한다.

- 다른 서버/DM/사용자의 데이터가 섞이지 않는가
- 실패가 무한 retry나 추가 과금으로 확대되지 않는가
- 재시작 후 이미 전송한 알림이 반복되지 않는가
- Discord 2,000자·embed·interaction 제약에서 읽기 쉬운가
- 기존 운영 데이터와 무삭제 롤백 경로가 유지되는가
- 문서의 모델명·주기·기능 flag가 코드와 일치하는가

배포 절차와 롤백은 [DEPLOYMENT.md](DEPLOYMENT.md), 인스턴스 경계는
[INSTANCE_SEPARATION.ko.md](INSTANCE_SEPARATION.ko.md)를 따른다.

# 마사몽 운영·배포 가이드

이 문서는 누적 데이터가 있는 Masamo를 보존하면서 공통 코드의 새 release를 반영하고,
General을 별도 인스턴스로 준비하는 절차다. 단순 `git pull && python main.py` 절차가 아니다.

핵심 원칙:

- 기존 Masamo Discord 앱/토큰, TiDB `masamong`, DB 계정, 프롬프트, Kakao mapping과
  누적 데이터를 그대로 보존한다.
- General은 별도 앱/토큰, 새 `masamong_general`, 별도 계정·설정·로그·service로 시작한다.
- 같은 Discord 토큰의 구·신 프로세스를 겹쳐 실행하지 않는다.
- 운영 DB 변경은 snapshot·읽기 전용 fingerprint·typed confirmation 뒤에만 한다.
- Masamo는 항상 `MASAMONG_AUTO_MIGRATE=false`로 기동하고, 현재 학교·편입 공지 schema,
  전용 writable path와 23:00/23:35 timer를 소유한다.
- General은 학교·편입 공지를 기본 비활성화하며 Masamo의 DB·digest·snapshot·timer를
  공유하지 않는다.

명령의 `<release-dir>`, `<venv>`, `<service>`, 사용자·그룹은 실제 서버 값으로 바꾼다.
저장소의 school systemd 템플릿은 `/srv/masamong/current`와 `root` 사용자를 기준으로
같은 release에 포함된 core를 실행한다. 실제 서버에서 다른 경로를 선택했다면 env와 unit의 모든
절대 경로를 그 배포 레이아웃 하나로 통일한다.

## 1. 로컬 release 검증

배포할 정확한 commit에서 실행한다.

```bash
<venv>/bin/python -m pytest -q
<venv>/bin/python -m compileall -q .
<venv>/bin/python -m pip check
git diff --check
git status --short
git rev-parse HEAD
```

테스트가 실패하거나 의도하지 않은 파일이 섞여 있으면 commit/push/deploy를 진행하지 않는다.
vendored `school_notice/` core의 전체 테스트도 함께 통과해야 한다.

## 2. 운영 인벤토리와 복원 수단

서비스를 내리기 전에 다음을 접근 제한된 운영 기록에 남긴다. secret 값은 출력하거나
일반 로그에 복사하지 않는다.

- 실행 중인 PID, 사용자, working directory, Python 버전, commit SHA
- systemd unit 원문과 재시작 정책
- `CPUQuota`, `CPUAffinity`, `MemoryMax`, thread/concurrency 설정
- 실제 env/config/prompt/embedding 파일의 권한, key 목록과 checksum
- DB backend/host/database 이름, grant, schema, 핵심 table row count와 최신 timestamp
- 현재 오류율, RSS/thread 수, LLM 호출 집계

예시:

```bash
systemctl show <service> \
  -p ActiveState -p SubState -p MainPID -p User -p Group \
  -p WorkingDirectory -p ExecStart -p Restart \
  -p CPUQuotaPerSecUSec -p MemoryMax
systemctl cat <service>
git -C <release-dir> rev-parse HEAD
git -C <release-dir> status --short
```

### DB와 설정 백업

- TiDB는 공급자 snapshot/PITR 또는 일관된 논리 백업을 만들고, 별도 staging DB에 실제
  restore되는지 확인한다.
- 로컬 `database/remasamong.db` 파일은 원격 TiDB 원본의 백업이 아니다.
- SQLite 운영이라면 writer를 멈추거나 SQLite backup API를 사용한다. 실행 중 파일을
  단순 복사하면 WAL 데이터가 빠질 수 있다.
- 현재 release SHA, env, unit, config, prompts, embedding 설정과 remote 전용 수정 파일을
  접근 제한된 timestamp 디렉터리에 복사하고 checksum을 기록한다.
- secret backup은 mode `0600`, 디렉터리는 service 계정만 접근 가능하게 한다.

복원이 시험되지 않은 “백업 생성 성공” 메시지만으로 다음 단계로 넘어가지 않는다.

## 3. 프로필 파일 준비

Masamo는 `profiles/masamo.env.example`로 기존 env를 교체하지 않는다. 현재 실제 env의 모든
값을 먼저 `/etc/masamong/masamo.env`에 보존한 뒤 경계 키를 추가한다. systemd
`Environment=`나 shell export에만 있던 값도 이 파일로 옮긴다.

반드시 보존·명시할 항목:

- 현재 token과 `MASAMONG_EXPECTED_DISCORD_BOT_USER_ID`
- 현재 TiDB host/port/user/password와 DB `masamong`
- `MASAMONG_EXPECTED_DB_NAME=masamong`, strict TLS/CA/hostname 검증
- 현재 prompt, embedding, Kakao mapping과 실제 저장소 경로
- 현재 API key, 모델, 기능 flag, guild 설정 모드
- 현재 CPU/RAG/AI 제한
- 별도 일반/오류 로그
- `MASAMONG_AUTO_MIGRATE=false`
- `SCHOOL_NOTICE_ENABLED=true`와 Masamo 전용 digest/core DB/catalog/source 경로
- `TRANSFER_NOTICE_ENABLED=true`와 Masamo 전용 snapshot/output/source 경로

General은 `profiles/general.env.example`과 전용 JSON 예제를 새 경로로 복사하고 placeholder를
바꾼다. Masamo prompt, Kakao mapping, DB row, 로컬 embedding data를 복사하지 않는다.

실제 두 env와 관련 JSON/CA가 준비된 서버에서 오프라인 경계 검사를 실행한다.

```bash
<venv>/bin/python scripts/validate_profile_separation.py \
  /etc/masamong/masamo.env \
  /etc/masamong/general.env
```

`OK`가 아니면 어느 서비스도 전환하지 않는다. 경고도 의도와 대조한다.

## 4. 읽기 전용 운영 preflight

`scripts/inspect_runtime_readonly.py`는 운영 데이터 원문, 사용자 ID, token, 비밀번호,
API key를 출력하지 않는다. 고정된 SELECT로 table/column, 집계 row 수, 최신 timestamp,
운세 구독 수만 JSON으로 기록한다. SQLite는 `mode=ro`, TiDB는 read-only transaction을
사용한다.

Masamo:

```bash
MASAMONG_ENV_FILE=/etc/masamong/masamo.env \
  <venv>/bin/python scripts/inspect_runtime_readonly.py \
  --expected-profile masamo \
  --expected-db masamong
```

General:

```bash
MASAMONG_ENV_FILE=/etc/masamong/general.env \
  <venv>/bin/python scripts/inspect_runtime_readonly.py \
  --expected-profile general \
  --expected-db masamong_general
```

초기 Masamo 검사에서 privacy 또는 `channel_summary_state` table 누락을 보고하는 것은
각 additive migration 전이라면 예상 가능한 상태다. 새 release의 검사기는
`channel_summary_state`를 필수 대상으로 보므로, schema 적용 전 기준값은 현재 운영
release의 검사기로 먼저 보존한다. 그 밖의 DB/profile mismatch, 기존 핵심 table/column
누락, 연결 실패를 무시하지 않는다. 출력은 배포 전·migration 후·재시작 후 세 번 저장해
비교한다.

## 5. 개인정보 동의 table one-shot

누적 Masamo DB에는 범용 `database/init_db.py`나 런타임 자동 migration을 실행하지 않는다.
`scripts/apply_privacy_consent_schema.py`는 정확히 다음 두 table만
`CREATE TABLE IF NOT EXISTS`로 추가한다.

- `privacy_consents`: 목적별 현재 상태
- `privacy_consent_events`: append-only 동의·철회 이력

기존 row를 update/delete/backfill/seed하지 않는다.

### Dry-run

다음 명령은 DB에 연결하지 않고 대상과 허용 SQL, typed confirmation 문구만 출력한다.

```bash
MASAMONG_ENV_FILE=/etc/masamong/masamo.env \
  <venv>/bin/python scripts/apply_privacy_consent_schema.py \
  --expected-profile masamo \
  --expected-db masamong
```

### 적용

snapshot과 dry-run을 검토한 뒤 출력 문구를 그대로 넣는다.

```bash
MASAMONG_ENV_FILE=/etc/masamong/masamo.env \
  <venv>/bin/python scripts/apply_privacy_consent_schema.py \
  --expected-profile masamo \
  --expected-db masamong \
  --apply \
  --confirm 'APPLY PRIVACY CONSENT SCHEMA TO profile=masamo backend=tidb database=masamong'
```

스크립트는 strict explicit profile, `AUTO_MIGRATE=false`, TLS/CA/hostname, 예상 DB,
연결 뒤 `DATABASE()`를 재확인하고 두 table의 필수 column을 read-back한다. 완료 직후
read-only preflight를 다시 실행해 기존 table 집계가 보존됐는지 확인한다.

기존 운세·학교 프로필과 구독은 삭제되지 않는다. 다만 현재 정책에 대한 동의가 없는
사용자의 개인정보 이용과 자동 발송은 fail-closed로 중단되며, 활성 구독자에게만
정책 버전당 한 번의 유한 동의 요청을 보낸다. 구독 취소·중지 또는 명시 철회 사용자는
자동 요청에서 제외한다.

기억·임베딩은 이 배포에서 전체 재색인하거나 기존 BLOB/vector를 덮어쓰지 않는다. 품질
감사는 `scripts/audit_memory_quality_readonly.py`, 향후 provenance/vector 개선은
[docs/MEMORY_INDEX_MIGRATION.ko.md](docs/MEMORY_INDEX_MIGRATION.ko.md)의 shadow table,
checkpointed backfill, dual-write, feature-flag 전환 절차를 따른다.

### 채널 증분 요약 상태 table one-shot

새 release는 `!요약`의 채널별 기준점과 제한된 요약문만 저장하는
`channel_summary_state`를 요구한다. 코드 전환 전에 새 release 경로의
`scripts/apply_summary_state_schema.py`를 실행한다. 이 스크립트는 정확히 한 개의
`CREATE TABLE IF NOT EXISTS`만 허용하며 기존 table이나 row에 `ALTER`, `UPDATE`,
`DELETE`, backfill 또는 seed를 하지 않는다.

Dry-run:

```bash
MASAMONG_ENV_FILE=/etc/masamong/masamo.env \
  <venv>/bin/python <new-release>/scripts/apply_summary_state_schema.py \
  --expected-profile masamo \
  --expected-db masamong
```

적용:

```bash
MASAMONG_ENV_FILE=/etc/masamong/masamo.env \
  <venv>/bin/python <new-release>/scripts/apply_summary_state_schema.py \
  --expected-profile masamo \
  --expected-db masamong \
  --apply \
  --confirm 'APPLY SUMMARY STATE SCHEMA TO profile=masamo backend=tidb database=masamong'
```

적용 뒤 새 release의 read-only 검사기로 다섯 필수 column과 기존 핵심 table 집계를
확인한다. 최초 행은 사용자가 다음 `!요약`을 성공시킬 때만 생성된다.

### 전체 데이터 이관 도구는 이 배포에 사용하지 않는다

`scripts/migrate_latest_data_to_tidb.py`는 기존 Masamo의 profile 전환이나 개인정보 table
추가 도구가 아니다. 원본 전체를 별도 TiDB에 적재하는 특수 운영 도구이므로 이번 배포에는
실행하지 않는다. 기본 실행과 `--dry-run`은 원격 DB에 연결하지 않으며, 쓰기에는
`--apply`, 현재 profile과 DB의 정확한 재입력이 필요하다. 특히 `--truncate`는 실제 생성
후 복원까지 검증한 backup 식별자를 두 번 입력하고, 출력된
`DROP ALL TABLES ON ... USING VERIFIED BACKUP ...` 문구 전체를 정확히 재입력해야만
허용된다. 연결 뒤 `DATABASE()` 불일치 시에는 어떤 DDL도 실행하지 않는다.

## 6. 코드 반영

운영 working tree가 깨끗한지 먼저 확인한다. 원격 전용 수정이 있으면 diff와 파일을 백업하고
release 코드에 의도적으로 반영했는지 확인한 뒤에만 stash 또는 별도 branch로 보관한다.
사용자 변경을 `reset --hard`로 없애지 않는다.

```bash
git -C <release-dir> fetch origin
git -C <release-dir> merge --ff-only origin/main
git -C <release-dir> rev-parse HEAD
```

가능하면 현재 디렉터리를 직접 덮어쓰기보다 검증된 SHA의 immutable release 디렉터리를 만들고
`current` symlink를 원자적으로 전환한다. 어떤 방식이든 systemd가 실행할 SHA와 로컬에서
테스트한 SHA가 정확히 같아야 한다.

`git archive`처럼 운영 release에 `.git`이 없는 배포는 루트에
`.release-metadata.json`을 함께 설치한다. 그렇지 않으면 `!업데이트`가 현재 SHA와 최근
변경 내역을 읽을 수 없다. 메타데이터는 commit 이후 정확한 저장소에서 생성하고, archive와
같은 release 디렉터리에 mode `0644`로 둔다.

```bash
<venv>/bin/python scripts/build_release_metadata.py \
  --repo . \
  --output /tmp/masamong-release-metadata.json
```

파일은 `commit_sha`, 최근 `commits`와 schema version만 담고 secret을 포함하지 않는다.
별도 경로에 설치하는 경우에만 서비스 env에
`MASAMONG_RELEASE_METADATA_FILE=/absolute/path/release-metadata.json`을 지정한다.

서버에서는 새 가상환경 또는 기존 가상환경의 호환성을 확인한다.

```bash
<venv>/bin/python -m pip install -r requirements.txt
<venv>/bin/python -m pip check
<venv>/bin/python -m compileall -q <release-dir>
```

RAG가 활성인 프로필에만 `requirements-cpu.txt`를 설치한다. 저사양 운영 서버에서 오프라인
embedding 생성·대규모 재색인·모델 다운로드를 배포와 함께 실행하지 않는다.

## 7. Masamo controlled restart

동일 token 프로세스의 overlap은 Discord 중복 응답과 중복 scheduler를 만든다. 짧은
controlled restart를 사용한다.

1. 새 코드와 env의 offline 검사를 끝낸다.
2. 현재 서비스를 정상 stop한다.
3. 기존 PID가 종료되고 같은 token 프로세스가 하나도 없는지 확인한다.
4. unit이 `MASAMONG_ENV_FILE=/etc/masamong/masamo.env`와 정확한 release를 가리키는지
   확인한다.
5. `daemon-reload` 후 Masamo service 하나만 start한다.

```bash
sudo systemctl stop <masamo-service>
systemctl is-active <masamo-service>
sudo systemctl daemon-reload
sudo systemctl start <masamo-service>
systemctl status <masamo-service> --no-pager
```

시작 로그에서 다음을 확인한다. secret 값은 출력하지 않는다.

- profile/instance가 `masamo`
- 실제 Discord bot user ID가 예상 ID와 일치
- DB backend/이름이 기존 `masamong`
- `AUTO_MIGRATE=false`로 DDL 없이 schema 검증 완료
- 학교 공지 Cog 활성, 다섯 table과 전용 경로 검증 완료
- 편입 공지 Cog 활성, 구독·전달·동의안내 table과 전용 경로 검증 완료
- 모든 필수 Cog 로드
- 지진 60초 monitor가 LLM 없이 시작되고, 최초 기존 통보 기준점이
  `system_counters.earthquake_alert_last_occurred_epoch_v1`에 생성됨
- thread/executor/AI/LLM/RAG/message-cache 상한
- 프로세스 하나, 예상 RSS/thread/CPU

## 8. 기능별 검증

운영 채널에 불필요한 알림을 만들지 않도록 관리자 테스트 guild 또는 DM에서 확인한다.

- 일반 멘션 대화와 DM 대화, 중복 응답 없음
- 명백한 도구 요청이 의도 LLM을 불필요하게 호출하지 않는지
- 날씨, 금융/환율, 웹 검색, 본문 출처 목록, 이미지 quota
- `!랭킹`, `!요약`, `!투표`, `!업데이트`, 설정/페르소나
- `.git` 없는 release에서 `!업데이트`가 `.release-metadata.json`으로 정상 응답하는지
- 서버 `!메뉴`의 상세 화면이 호출자에게만 보이고, 다른 사용자가 launcher를 사용할 수
  없으며 학교·편입·개인정보 버튼이 `DM`으로 표시되어 비활성인지
- `!요약` 뒤 재기동해도 같은 `guild_id/channel_id`의 저장 기준점부터 이어지며, 새
  메시지가 없을 때 `channel_summary_state`를 다시 쓰지 않는지
- `!개인정보` 상태, 운세/학교공지/편입공지의 동의·거부·철회 흐름
- 운세 미동의 차단, 선택 항목 `NULL`, 개인 LLM 운세 합산 일 3회, 구독 설정
- 기존 Discord/Kakao RAG 조회와 기존 prompt 채널
- A/B 테스트 guild의 일반·창의형 응답이 각자 `guild_id` 페르소나만 사용하고,
  다른 서버 말투·대화·RAG 조각을 포함하지 않는지
- 재기동 뒤 이전 지진·여진이 다시 전송되지 않고, 지진 경로의 LLM 시도가 0인지
- 같은 시간창·진앙 반경의 후속 지진은 최초 메시지 ID를
  `system_counters`에서 복원해 새 글이 아니라 Discord edit로 갱신하고, 먼 독립
  지진은 별도 메시지를 만드는지
- DM 차단이나 공급자 timeout 뒤 scheduler가 반복 폭주하지 않는지
- 서버에서 학교·편입 명령/메뉴가 저장·조회 없이 DM 사용법만 안내하는지
- 편입 구독 취소·철회 상태에서 새 공지와 과거 retry가 발송되지 않는지

재시작 뒤 read-only fingerprint를 다시 실행해 기존 대화·운세·사용량·기억 table의 row 수와
최신 timestamp를 전후와 대조한다.

최소 몇 분간 로그, CPU/RSS/thread와 LLM 시도 집계를 관찰한다. 유휴 상태인데
`llm_attempt`이 지속 증가하거나 1분 scheduler가 같은 사용자를 계속 생성한다면 즉시
rollback한다. 정상 모닝 브리핑은 생성/발송 각각 기본 최대 3회이며 발송 재시도는 저장된
문장을 재사용한다.

## 9. General 첫 배포

General은 Masamo 전환과 별개로 준비한다.

1. 새 Discord 앱/토큰과 bot user ID를 만든다.
2. 새 빈 `masamong_general`과 그 DB만 접근하는 전용 계정을 만든다.
3. RAG와 모든 반복 scheduler, 학교·편입 공지를 끈다.
4. 빈 DB bootstrap 한 번만 `MASAMONG_AUTO_MIGRATE=true`로 실행한다.
5. schema와 seed를 확인한 뒤 즉시 `MASAMONG_AUTO_MIGRATE=false`로 바꾼다.
6. 다시 기동해 read-only schema 검증만으로 통과하는지 확인한다.
7. 테스트 guild에서 General DB에만 write가 생기고 Masamo fingerprint가 변하지 않는지
   확인한다.
8. 두 프로세스 합산 CPU/RSS 여유가 있을 때 기능을 하나씩 활성화한다.

초기 저사양 권장값:

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
AI_MEMORY_ENABLED=false
EMBEDDING_ENABLED=false
RERANK_ENABLED=false
RAG_ARCHIVING_ENABLED=false
BM25_AUTO_REBUILD_ENABLED=false
FORTUNE_MORNING_BRIEFING_ENABLED=false
SCHOOL_NOTICE_ENABLED=false
TRANSFER_NOTICE_ENABLED=false
```

Masamo와 General의 service, env, DB account, prompt/embedding, writable state와 logs는 모두
서로 달라야 한다.

## 10. Masamo 학교 공지 schema와 timer

학교 공지 core는 release의 `school_notice/`에 포함된다. 운영 DB에는 아래 다섯 table만
고정된 additive one-shot으로 추가한다.

- `school_notice_profiles`
- `school_notice_feedback`
- `school_notice_deliveries`
- `school_notice_batch_runs`
- `school_notice_delivery_runs`

먼저 DB에 연결하지 않는 dry-run을 확인한다.

```bash
MASAMONG_ENV_FILE=/etc/masamong/masamo.env \
  <venv>/bin/python scripts/apply_school_notice_schema.py \
  --expected-profile masamo \
  --expected-db masamong
```

snapshot과 기존 table fingerprint를 확보한 뒤 출력된 확인 문구 전체를 그대로 넣는다.

```bash
MASAMONG_ENV_FILE=/etc/masamong/masamo.env \
  <venv>/bin/python scripts/apply_school_notice_schema.py \
  --expected-profile masamo \
  --expected-db masamong \
  --apply \
  --confirm 'APPLY SCHOOL NOTICE SCHEMA TO profile=masamo backend=tidb database=masamong'
```

이 migration은 `CREATE TABLE IF NOT EXISTS`와 필요한 index만 실행한다. 기존 table/row에
`ALTER`, `UPDATE`, `DELETE`, backfill을 하지 않으며 연결 뒤 DB identity와 필수 column을
다시 확인한다. 적용 전후 read-only fingerprint의 기존 table count를 비교한다.

Masamo env는 아래 경계를 가져야 한다.

```dotenv
SCHOOL_NOTICE_ENABLED=true
SCHOOL_NOTICE_DIGEST_DIR=/var/lib/masamong/masamo/notice/out
SCHOOL_NOTICE_CORE_DB=/var/lib/masamong/masamo/notice/core.db
SCHOOL_NOTICE_CATALOG_PATH=/srv/masamong/current/profiles/catalogs/school_notice_catalog.v1.json
SCHOOL_NOTICE_SOURCE_CONFIG=/srv/masamong/current/school_notice/sources.json
SCHOOL_NOTICE_DELIVERY_HOUR=9
SCHOOL_NOTICE_DELIVERY_MINUTE=0
SCHOOL_NOTICE_INITIAL_CRAWL_ENABLED=true
SCHOOL_NOTICE_INITIAL_CRAWL_TIMEOUT_SECONDS=660
SCHOOL_NOTICE_INITIAL_CRAWL_MAX_ATTEMPTS=2
SCHOOL_NOTICE_INITIAL_CRAWL_RETRY_SECONDS=20
```

bot을 시작하기 전에 batch dry-run으로 대상 학교 집계만 확인한다.

```bash
MASAMONG_ENV_FILE=/etc/masamong/masamo.env \
  <venv>/bin/python scripts/run_school_notice_batch.py \
  --core-python <venv>/bin/python \
  --core-cwd /srv/masamong/current \
  --source-config /srv/masamong/current/school_notice/sources.json \
  --dry-run
```

dry-run은 SQLite일 때 `mode=ro`를 쓰고 profile/temp/digest 파일이나 DB update를 만들지
않는다. 실제 제한된 수동 batch에서는 기본 `--no-llm --low-resource`가 적용된다. LLM 공지
분석은 운영자가 명시적으로 `--use-llm`을 준 경우에만 켜진다.

검증할 항목:

- 현재 동의가 있고 활성인 등록 프로필만 대상
- 첫 등록 직후 child batch는 `--only-user-id`로 정확히 그 사용자 한 명만 DB에서 읽고,
  해당 학교 source만 `--no-llm --low-resource`로 최대 2회 안에 처리
- 카탈로그와 core 설정이 공통으로 가진 해당 학교 source만 `--source`로 전달
- KST 날짜가 core에 명시
- 사용자·날짜가 일치하는 검증된 digest와 최소 run report만 mode `0600`으로 원자적 공개
- 관련 공지 0건이면 다음 날 자동 DM 없음
- 재시작·장애 복구 때 최근 3일 안의 가장 최신 유효 성공·부분 성공 batch만 전달 대상으로
  선택하고, 더 최신 성공 batch가 있으면 오래된 결과로 되돌아가지 않음
- 한 DM 상한을 넘는 공지는 페이지로 나누고 성공한 revision을 즉시 기록한 뒤, 남은
  페이지를 다음 1분 tick에서 이어 보내며 성공 페이지는 실패 attempt를 소비하지 않음
- batch 오류, timeout, profile 상한, lock 충돌이 성공처럼 기록되지 않음
- 23:00 KST timer와 사용자별 기본 09:00/설정 시각 전달

systemd 템플릿을 실제 경로와 사용자로 수정해 설치한 뒤:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now masamong-school-notice-batch.timer
systemctl list-timers masamong-school-notice-batch.timer --all
```

timer는 `OnCalendar=*-*-* 23:00:00 Asia/Seoul`, `Persistent=true`다. Masamo timer만
활성화하고 General에는 설치하지 않는다. wrapper는 현재 동의·활성·등록 프로필의 source만
선택하므로 카탈로그의 모든 학교를 일괄 수집하지 않는다.

## 11. Masamo 편입 공지 schema와 timer

운영 DB에는 기존 row를 건드리지 않는 세 table만 추가한다.

- `privacy_consent_prompts`
- `transfer_notice_subscriptions`
- `transfer_notice_deliveries`

먼저 DB에 연결하지 않는 dry-run을 확인한다.

```bash
MASAMONG_ENV_FILE=/etc/masamong/masamo.env \
  <venv>/bin/python scripts/apply_transfer_notice_schema.py \
  --expected-profile masamo \
  --expected-db masamong
```

snapshot과 기존 table fingerprint를 확보한 뒤 출력된 확인 문구 전체를 넣는다.

```bash
MASAMONG_ENV_FILE=/etc/masamong/masamo.env \
  <venv>/bin/python scripts/apply_transfer_notice_schema.py \
  --expected-profile masamo \
  --expected-db masamong \
  --apply \
  --confirm 'APPLY TRANSFER NOTICE SCHEMA TO profile=masamo backend=tidb database=masamong'
```

이 migration은 세 table의 `CREATE TABLE IF NOT EXISTS`만 실행한다. 기존 table/row에
`ALTER`, `UPDATE`, `DELETE`, backfill 또는 seed를 하지 않는다.

Masamo env에는 다음 경계를 둔다.

```dotenv
TRANSFER_NOTICE_ENABLED=true
TRANSFER_NOTICE_SOURCE_CONFIG=/srv/masamong/current/transfer_notice/sources.json
TRANSFER_NOTICE_DATABASE=/var/lib/masamong/masamo/transfer_notice/core.db
TRANSFER_NOTICE_OUTPUT_DIR=/var/lib/masamong/masamo/transfer_notice/out
TRANSFER_NOTICE_DELIVERY_MAX_ATTEMPTS=3
TRANSFER_NOTICE_DELIVERY_RETRY_MINUTES=30
TRANSFER_NOTICE_MAX_ITEMS_PER_DM=10
```

운영 경로에 쓰기 전 새 임시 DB로 실제 공식 페이지를 한 번 수집한다. 모든 source의 첫 성공은
기준선이므로 결과의 `changes`가 0이어야 한다. source별 `healthy/degraded/failed`, item 수,
robots 금지 상태를 확인하고 금지 source는 우회하지 않는다.

systemd 템플릿을 실제 서비스 사용자와 Python 경로에 맞춘 뒤 timer를 설치한다.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now masamong-transfer-notice-batch.timer
systemctl list-timers masamong-transfer-notice-batch.timer --all
```

timer는 `OnCalendar=*-*-* 23:35:00 Asia/Seoul`, `Persistent=true`이며 General에는 설치하지
않는다. 배치는 LLM 없이 순차 실행하고 전체 900초, 재시도 1회, CPU/RSS 상한을 적용한다.
구독 전달은 활성·현재 동의 사용자만 대상으로 하며 성공 revision은 DB 키로 중복 방지한다.

## 12. Rollback

### DB 쓰기 전 또는 additive privacy/school/transfer/summary-state table만 추가한 경우

1. 새 service/timer를 중지한다.
2. 이전 release SHA, env, config, prompt/embedding과 unit을 복원한다.
3. `daemon-reload` 후 이전 Masamo service 하나만 시작한다.
4. identity/DB target와 read-only fingerprint를 다시 확인한다.

privacy table 두 개, school table 다섯 개, 편입 관련 세 table과
`channel_summary_state`는 additive이므로 이전 코드가 사용하지 않으면 그대로 둘 수 있다.
서둘러 drop하지 않는다.

### 새 코드가 DB에 쓰기 시작한 경우

- 먼저 모든 writer와 school/transfer timer를 중지한다.
- 배포 후 생성된 row와 backward compatibility를 확인한다.
- 코드/env rollback만으로 안전한지 판단한다.
- DB snapshot restore가 필요하면 이후 정상 write를 잃을 범위를 승인받고 검증된 복원 절차를
  사용한다.
- 운영 DB에 `DROP`, `TRUNCATE`, 광범위 `DELETE`, `git reset --hard`를 즉흥적으로 실행하지
  않는다.

롤백 후에도 기존 Masamo DB 이름·계정·토큰을 바꾸지 않는다. 실패한 school digest는
보존해 원인을 확인하되 전달 timer는 끈다.

## 13. 배포 완료 기준

- local/server 테스트, compile, dependency check 통과
- 배포 SHA가 push된 검증 SHA와 일치
- 두 profile 경계 검사 통과
- snapshot/restore 증거와 배포 전·후 fingerprint 확보
- 기존 Masamo 핵심 table의 count/latest timestamp와 Kakao mapping 보존
- Masamo 단일 PID, 올바른 bot ID와 기존 `masamong`
- General은 별도 bot ID·`masamong_general`, Masamo 데이터 조회 없음
- 목적별 동의 없이 운세/학교 개인정보를 읽거나 자동 발송하지 않음
- 철회는 처리 중단, 삭제는 해당 프로필만 삭제, 일반 대화는 유지
- LLM/운세 scheduler의 유한 retry와 유휴 호출 증가 없음
- 웹 검색 한 턴의 최종 답변 LLM이 1회이고 출처가 Discord 본문에 유지되며, 같은 동시
  검색은 singleflight로 합쳐짐
- `.git` 없는 immutable release에서도 `!업데이트`가 release metadata로 응답함
- 서버 `!메뉴` 상세는 호출자 전용이고 DM 전용 버튼은 비활성, 개인 정보 조회 없음
- `channel_summary_state` 추가 뒤 기존 table count가 감소하지 않고 재기동 요약이 이어짐
- Masamo 학교 flag/Cog 활성, 23:00 KST timer 하나, 전용 writable path와 다섯 table 확인
- 등록 프로필 source만 수집하고 관련 공지가 없을 때 무알림, 사용자별 기본 09:00 전달
- General 학교 flag false이고 Masamo school DB/file/timer 접근 없음
- Masamo 편입 flag/Cog 활성, 23:35 KST timer 하나, 전용 snapshot/output과 세 table 확인
- 편입 첫 기준선 `changes=0`, 선택 대학의 새 revision만 DM, 취소/철회/과거 retry 무발송
- General 편입 flag false이고 Masamo transfer DB/file/timer 접근 없음
- 학교·편입은 DM 전용이며 서버 명령·메뉴가 개인 설정·결과를 읽거나 표시하지 않음
- 두 프로세스 합산 CPU/RSS가 기존 Masamo 안정성을 해치지 않음

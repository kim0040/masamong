# 마사몽 General / Masamo 인스턴스 분리 운영 가이드

## 결론

가능하다. 가장 안전한 구조는 코드를 두 벌로 갈라서 유지하는 것이 아니라, 동일한
검증된 release SHA를 두 개의 독립 프로세스로 실행하고 각 프로세스의 정체성·저장소·설정·
로그·스케줄러 소유권을 프로필로 완전히 분리하는 것이다.

- `masamo`: 현재 원격 서버에서 운영 중인 Discord 앱과 기존 TiDB `masamong`,
  기존 프롬프트·Kakao mapping·누적 데이터를 그대로 소유한다.
- `general`: 새 Discord 앱과 새 빈 TiDB `masamong_general`, 별도 설정 파일로 시작한다.
  Masamo 데이터를 복사하거나 조회하지 않는다.

기존 `masamong` DB의 이름을 바꾸거나 General로 재사용하지 않는다. 현재 운영 DB를 두
프로세스가 공유하는 것도 금지한다. 일부 테이블은 인스턴스 식별자가 없어서 guild ID만
나누는 방식으로는 운세, DM 제한, API 카운터, 사용자 선호, 메모리가 완전히 격리되지 않는다.

## 고정해야 할 경계

| 경계 | Masamo | General |
|---|---|---|
| 프로필 | `masamo` | `general` |
| Discord 앱/토큰 | 현재 앱과 토큰 유지 | 새 앱과 새 토큰 |
| 예상 bot user ID | 현재 앱 ID | 새 앱 ID |
| TiDB database | 기존 `masamong` | 새 `masamong_general` |
| TiDB 계정 | `masamong`만 접근 | `masamong_general`만 접근 |
| env | `/etc/masamong/masamo.env` | `/etc/masamong/general.env` |
| config | `/etc/masamong/masamo/config.json` | `/etc/masamong/general/config.json` |
| prompt | 현재 파일을 그대로 복사 | 별도 파일, 초기 허용 채널 없음 |
| embedding | 현재 파일과 Kakao mapping 보존 | 별도 파일, Kakao mapping 없음 |
| 기억 소스 | 정확히 `discord,kakao` | 정확히 `discord` |
| 로그 | `masamo*.log` | `general*.log` |
| 최고/등록 관리자 | Masamo 전용 env ID와 `instance_name=masamo` 행 | 별도 General env ID와 `instance_name=general` 행 |
| 서비스 unit | `masamong-masamo.service` | `masamong-general.service` |
| 정기 작업 | 기존 작업·학교 05:00·편입 05:35 timer 소유 | 첫 부트스트랩에는 전부 끄고 검증 뒤 필요한 작업만 소유 |
| 학교 공지 | 현재 schema/core DB/digest/timer 소유 | `SCHOOL_NOTICE_ENABLED=false`, Masamo 상태 공유 금지 |
| 편입 공지 | 현재 구독 schema/snapshot/output/timer 소유 | `TRANSFER_NOTICE_ENABLED=false`, Masamo 상태 공유 금지 |

토큰과 DB 이름만 다르게 두는 것으로는 충분하지 않다. DB 계정, 설정 및 prompt/embedding
경로, 실제 embedding DB 경로, 로그, bot user ID까지 모두 달라야 한다.

## 전환 전에 원격 상태를 읽기 전용으로 기록

먼저 운영 서버에서 다음을 변경 없이 기록한다.

- 실행 PID, 실행 사용자, cwd, 배포 commit SHA와 Python 버전
- 실제 systemd/screen 실행 명령, 재시작 정책
- `CPUQuota`, `CPUAffinity`, `MemoryMax`와 현재 thread 관련 환경 설정
- DB backend/host/database와 주요 테이블의 row count 및 최신 timestamp
- 현재 `prompts.json`, `emb_config.json`, 서비스 env 파일의 안전한 사본과 해시
- TiDB snapshot/PITR 또는 실제 복원이 확인된 논리 백업

인벤토리 출력에는 토큰, 비밀번호, API key 값을 남기지 않는다. 키 이름의 존재 여부와
파일 해시만 기록한다. 로컬 `database/remasamong.db`는 원격 TiDB 운영 원본으로 간주하지
않는다.

## 운영 파일 준비

secret 파일은 Git에 커밋하지 않는다. 디렉터리와 로그 파일은 서비스 계정만 접근하게
미리 생성한다.

```bash
sudo install -d -m 0750 -o <service-user> -g <service-group> \
  /etc/masamong/masamo /etc/masamong/general \
  /var/lib/masamong/masamo /var/lib/masamong/general \
  /var/log/masamong
```

### Masamo 파일

`profiles/masamo.env.example`은 경계 키 목록을 설명하는 축약 예제이지, 현재 운영 env를
대체하는 파일이 아니다.

1. 현재 운영 env를 `/etc/masamong/masamo.env`로 정확히 복사한다.
2. 기존 token, DB, 모델, API key, CPU 값을 바꾸지 않고 프로필 경계 키만 추가한다.
3. 현재 `prompts.json`을 `/etc/masamong/masamo/prompts.json`으로 정확히 복사한다.
4. 현재 `emb_config.json`과 실제 Kakao server/room mapping을
   `/etc/masamong/masamo/emb_config.json`으로 정확히 복사한다.
5. `profiles/config.masamo.example.json`을 인스턴스 전용 `config.json`의 출발점으로 쓴다.

첫 프로필 전환은 반드시 아래 값으로 시작한다.

```dotenv
MASAMONG_PROFILE=masamo
MASAMONG_INSTANCE_NAME=masamo
MASAMONG_REQUIRE_EXPLICIT_PROFILE=true
MASAMONG_AUTO_MIGRATE=false
MASAMONG_GUILD_SETTINGS_MODE=static
MASAMONG_DB_NAME=masamong
MASAMONG_EXPECTED_DB_NAME=masamong
MASAMONG_MEMORY_SOURCES=discord,kakao
MASAMONG_SUPERADMIN_USER_IDS=replace-with-current-masamo-superadmin-user-id
```

`static`은 기존 prompt의 채널/페르소나 동작을 유지한다. 오래된 `guild_settings` 값을
읽기 전용으로 대조하기 전에는 Masamo를 `database` 모드로 전환하지 않는다.

현재 저사양 서비스의 `MASAMONG_CPU_THREADS`, `MASAMONG_EXECUTOR_WORKERS`,
`AI_MAX_CONCURRENT_PROCESSING`, `AI_QUEUE_WAIT_TIMEOUT_SECONDS`,
`LLM_MAX_CONCURRENT_CALLS`, `EMBEDDING_MAX_CONCURRENCY`,
`RAG_MAX_BACKGROUND_TASKS`, `RAG_MAX_TRACKED_WINDOWS`,
`MASAMONG_DISCORD_MAX_MESSAGES`, `TOKENIZERS_PARALLELISM`도 실제 Masamo env
파일에 명시한다. 숫자는 예제값을 그대로 추정해 쓰지 말고 전환 전 인벤토리에서 확인한
현재 값을 복사한다. 특히 기존 systemd의 CPU 관련 값이 env 밖에만 있었다면 명시적
프로필 전환 전에 반드시 파일 안으로 옮긴다.

### 선택한 env 파일이 유일한 설정 출처다

명시적 프로필에서 `load_config_value`는 `os.environ`을 조회하지 않는다. 즉 접두사와
무관하게 **선택한 env 파일에 없는 키는 전부 무시**되고 `config.json` 또는 코드
기본값으로 떨어진다. 추가로 `MASAMONG_*` 상속값은 os.environ에서 아예 제거된다.

따라서 지금 돌아가는 프로세스에 값을 공급하는 세 경로를 전환 전에 모두 확인한다.

| 지금 값을 공급하는 위치 | 전환 후 |
|---|---|
| 현재 `.env` | 그대로 복사하면 보존 |
| systemd `Environment=` / shell export | **무시됨** — 파일로 옮겨야 보존 |
| repository 루트 `config.json` | 프로필 전용 `config.json`으로 경로가 바뀜 |

```bash
systemctl cat masamong-masamo.service | grep -E "Environment|EnvironmentFile|ExecStart"
ls -la /srv/masamong/current/config.json
```

경계 키(토큰·DB·TLS)와 저사양 제한값은 기동 시점에 fail-closed로 막힌다. 기능
자격증명(`COMETAPI_KEY`/`LLM_*_API_KEY`, `KMA_API_KEY`, 활성 상태의
`LINKUP_API_KEY`)도 켜 둔 기능에 한해 기동 시점에 검증하므로, 값이 빠지면 사용자가
봇을 부를 때가 아니라 배포 순간에 실패한다. key가 없는 인스턴스는
`MASAMONG_DISABLED_COGS`로 해당 Cog를 빼는 것이 정직한 표현이다.

### General 파일

- `profiles/general.env.example`
- `profiles/config.general.example.json`
- `profiles/emb.general.example.json`
- `profiles/prompts.general.example.json`

위 파일을 별도 경로로 복사한 후 placeholder를 실제 값으로 바꾼다. General에는 Masamo
prompt의 채널 ID, Kakao mapping, 기존 embedding 파일 또는 사용자 데이터를 복사하지 않는다.
새 DB 계정은 `masamong_general`에만 최소 권한을 가져야 한다.
General의 `MASAMONG_SUPERADMIN_USER_IDS`는 별도로 정하기 전까지 비워 두며 Masamo 값을
복사하지 않는다. `bot_admin_accounts`도 General DB에서 독립적으로 시작한다.

General의 첫 빈 DB 생성 때만 `MASAMONG_AUTO_MIGRATE=true`를 사용한다. 이 계정이 기존
`masamong`에 접근하지 못한다는 것을 먼저 확인한다. 스키마 생성과 smoke test가 끝난 뒤에는
`MASAMONG_AUTO_MIGRATE=false`로 바꾸고 정상 운영한다. 이후 schema 변경은 검증한 one-shot
절차로 적용하며, 운영 봇 재시작에 DDL 권한을 묶지 않는다.

General은 학교 공지를 끈 상태로 시작하며 Masamo의 학교 table, core DB, digest, timer를
절대 공유하지 않는다. 나중에 General에서도 필요하다면 General DB에 additive schema를
별도로 적용하고 `/var/lib/masamong/general/...` 경로와 General 전용 timer를 새로 검증해야
한다. 현재 운영 school timer는 Masamo 하나만 소유한다.

편입 공지도 같은 원칙을 적용한다. General은 `TRANSFER_NOTICE_ENABLED=false`로 시작하고
Masamo의 `transfer_notice_*` table, `/var/lib/masamong/masamo/transfer_notice` 또는
05:35 timer에 접근하지 않는다. 나중에 켤 때는 General DB에 별도 additive schema를
적용하고 경로에 `general`이 독립 구성요소로 들어가는지 경계 검사로 확인한다.

## `MASAMONG_ENV_FILE` 선택 방식

env 파일 안의 `MASAMONG_ENV_FILE`은 파일 정체성을 확인하는 fingerprint다. 그 줄 자체가
Python이 어느 파일을 열지 선택할 수는 없다. 서비스 관리자가 프로세스 외부에서 같은 절대
경로를 주어야 한다.

```ini
# /etc/systemd/system/masamong-masamo.service
[Service]
User=<service-user>
WorkingDirectory=/srv/masamong/current
Environment=MASAMONG_ENV_FILE=/etc/masamong/masamo.env
ExecStart=/srv/masamong/venv/bin/python /srv/masamong/current/main.py
Restart=on-failure
```

```ini
# /etc/systemd/system/masamong-general.service
[Service]
User=<service-user>
WorkingDirectory=/srv/masamong/current
Environment=MASAMONG_ENV_FILE=/etc/masamong/general.env
ExecStart=/srv/masamong/venv/bin/python /srv/masamong/current/main.py
Restart=on-failure
```

두 unit은 같은 검증된 release SHA를 사용할 수 있지만 env와 writable path는 공유하지 않는다.
기존 Masamo unit의 CPU 관련 systemd 속성은 그대로 보존하고, General에는 서버의 남는 용량
안에서 더 보수적인 제한을 준다. env 안에서 `${KEY}` 별칭을 사용할 때는 같은 파일 안에
정의된 키만 참조하고, 미해결 참조를 남기지 않는다.

## 코드의 fail-closed 보호장치

- 명시한 env/config/prompt/embedding 파일이 없거나 JSON 최상위 값이 객체가 아니면 기동 실패
- 프로필과 인스턴스 이름이 다르거나 `masamo|general` 이외이면 기동 실패
- Discord 로그인 직후 실제 bot user ID가 프로필의 예상 ID와 다르면 DB 연결 전에 기동 실패
- 명시적 TiDB 프로필은 DB 이름이 각각 `masamong`, `masamong_general`이 아니면 기동 실패
- strict TiDB는 TLS CA와 hostname 검증이 없으면 기동 실패
- General은 기억 소스가 정확히 `discord`가 아니거나 Kakao mapping이 있으면 기동 실패
- Masamo는 기억 소스가 정확히 `discord,kakao`가 아니거나 Kakao mapping이 없으면 기동 실패
- 필수 Cog가 비활성화됐거나 로드에 실패하면 기동 실패
- `SCHOOL_NOTICE_ENABLED=false`인 인스턴스는 school Cog 자체를 로드하지 않으며 학교
  테이블·digest·명령에 접근하지 않음
- `TRANSFER_NOTICE_ENABLED=false`인 인스턴스는 transfer Cog를 로드하지 않으며 편입
  구독 테이블·snapshot·명령에 접근하지 않음
- `MASAMONG_AUTO_MIGRATE=false`에서는 startup 및 runtime helper가 DDL을 실행하지 않고
  필수 테이블과 `guild_settings`, `user_profiles` 컬럼을 읽기 전용으로 검증
- 프로필별 로그 파일을 열 수 없으면 명시적 운영 프로필은 기동 실패
- 최고 관리자 env는 현재 프로필에서만 읽고, 등록 관리자는
  `bot_admin_accounts(instance_name, user_id)`로 다시 격리

`MASAMONG_EXPECTED_DISCORD_BOT_USER_ID`는 Discord Developer Portal의 해당 애플리케이션
bot user ID를 십진 정수로 적는다. 두 프로필 값은 반드시 달라야 한다.

## 오프라인 경계 검사

실제 파일과 CA 인증서가 모두 준비된 운영 호스트에서 실행한다.

```bash
python3 scripts/validate_profile_separation.py \
  /etc/masamong/masamo.env \
  /etc/masamong/general.env
```

검사기는 네트워크나 DB에 접속하지 않고 다음을 확인한다.

- 토큰과 예상 bot ID가 서로 다른지
- DB host/name 조합과 DB 계정이 서로 다른지
- TiDB 이름, TLS, strict mode가 프로필 계약과 맞는지
- config/prompt/embedding/log 및 실제 embedding DB 경로가 절대 경로이고 서로 다른지
- General에 Kakao mapping이 없고 Masamo mapping이 placeholder가 아닌지
- AI 허용 Discord channel ID가 겹치지 않는지
- 첫 배포 migration, scheduler, 필수 Cog 설정이 안전한지
- 학교·편입 writable path가 프로필 이름을 포함하고 두 프로필 사이에서 공유되지 않는지

출력이 `OK`가 아니면 기동하지 않는다. 이 검사는 live DB의 grant, 데이터 내용 또는
`guild_settings` 상태를 확인하지 않으므로 아래 항목은 별도 읽기 전용 점검이 필요하다.

- 각 DB 계정의 실제 grant
- General 핵심 테이블이 초기에는 비어 있는지
- Masamo 주요 테이블 row count와 최신 timestamp
- 두 DB 간 동일한 기존 사용자/대화/운세/Kakao row가 유입되지 않았는지
- Masamo의 DB 기반 guild 설정과 현재 static prompt가 충돌하지 않는지

## 운영 DB 읽기 전용 fingerprint

`scripts/inspect_runtime_readonly.py`는 선택한 명시 프로필과 예상 DB를 먼저 대조한 뒤
운영 데이터 원문이나 사용자 식별자를 출력하지 않고 table/column 존재 여부, 집계 row 수,
최신 timestamp와 운세 구독 집계만 JSON으로 낸다. SQLite는 `mode=ro`, TiDB는 read-only
transaction과 허용된 고정 SELECT만 사용한다.

```bash
MASAMONG_ENV_FILE=/etc/masamong/masamo.env \
  /srv/masamong/venv/bin/python scripts/inspect_runtime_readonly.py \
  --expected-profile masamo \
  --expected-db masamong
```

개인정보·편입 table을 처음 추가하기 전 실행은 해당 table 누락을 정확히 보고할 수 있다.
배포 전·migration 후·재시작 후 출력을 각각 접근 제한된 디렉터리에 보관해 기존 table의
count와 최신 timestamp가 의도치 않게 바뀌지 않았는지 비교한다. 이 fingerprint는 DB
snapshot이나 복원 시험을 대신하지 않는다.

## Masamo 개인정보 동의 schema

Masamo는 `MASAMONG_AUTO_MIGRATE=false`를 유지한다. 기존 DB에
`privacy_consents`, `privacy_consent_events`가 없다면 범용 초기화 스크립트 대신 고정된
additive one-shot을 사용한다.

먼저 dry-run 한다. 이 단계는 DB에 연결하지 않는다.

```bash
MASAMONG_ENV_FILE=/etc/masamong/masamo.env \
  /srv/masamong/venv/bin/python scripts/apply_privacy_consent_schema.py \
  --expected-profile masamo \
  --expected-db masamong
```

출력된 전체 확인 문구를 그대로 재입력해서만 적용할 수 있다.

```bash
MASAMONG_ENV_FILE=/etc/masamong/masamo.env \
  /srv/masamong/venv/bin/python scripts/apply_privacy_consent_schema.py \
  --expected-profile masamo \
  --expected-db masamong \
  --apply \
  --confirm 'APPLY PRIVACY CONSENT SCHEMA TO profile=masamo backend=tidb database=masamong'
```

스크립트는 strict explicit profile, TLS, `MASAMONG_EXPECTED_DB_NAME`,
`MASAMONG_AUTO_MIGRATE=false`를 확인하고 연결 뒤 `DATABASE()`를 다시 대조한다. 실행 가능한
SQL은 두 table의 `CREATE TABLE IF NOT EXISTS` 두 문장뿐이다. 기존 행의
`UPDATE`·`DELETE`, backfill, seed는 하지 않으며 마지막에 필수 column을 read-back한다.
적용 직후 read-only fingerprint를 다시 저장한다.

## Masamo 편입 공지 schema와 기준선

편입 기능과 기존 활성 구독자의 정책 안내에는 세 개의 additive table이 필요하다.

- `privacy_consent_prompts`: 정책별 유한 안내 발송 상태
- `transfer_notice_subscriptions`: Discord ID, 선택 대학과 활성 상태
- `transfer_notice_deliveries`: 공지 revision별 중복 방지와 유한 retry payload

`scripts/apply_transfer_notice_schema.py`를 먼저 dry-run하고, snapshot과 대상 DB를 확인한
뒤 출력된 문구 전체를 그대로 넣어 적용한다.

```bash
MASAMONG_ENV_FILE=/etc/masamong/masamo.env \
  /srv/masamong/venv/bin/python scripts/apply_transfer_notice_schema.py \
  --expected-profile masamo \
  --expected-db masamong

MASAMONG_ENV_FILE=/etc/masamong/masamo.env \
  /srv/masamong/venv/bin/python scripts/apply_transfer_notice_schema.py \
  --expected-profile masamo \
  --expected-db masamong \
  --apply \
  --confirm 'APPLY TRANSFER NOTICE SCHEMA TO profile=masamo backend=tidb database=masamong'
```

허용 SQL은 세 table의 `CREATE TABLE IF NOT EXISTS`뿐이다. 기존 row의 변경·삭제·backfill은
없다. 첫 공개 수집은 새 전용 SQLite에서 실행하고 `changes=0`인지 확인한 뒤 운영
`latest.json`으로 사용한다. 이 기준선 절차 때문에 배포 전 과거 공지가 DM으로 다시
전송되지 않는다.

## Masamo 무중단 데이터 보존 전환

동일 Discord 토큰으로 두 프로세스를 동시에 실행하면 안 된다. 따라서 데이터 이전은 없지만,
새 프로필 설정으로 넘어갈 때 한 번의 controlled restart는 필요하다.

1. snapshot/PITR과 실제 복원 시험, 코드 SHA·env/unit/config의 접근 제한 사본을 완료한다.
2. read-only fingerprint를 저장한다.
3. 필요한 privacy·학교·편입 additive one-shot만 적용하고 fingerprint를 다시 비교한다.
4. 실제 두 env에 경계 검사기와 전체 테스트를 실행한다.
5. 현재 Masamo 프로세스를 정상 종료한다.
6. 같은 토큰을 쓰는 프로세스가 0개인지 확인한다.
7. 동일 release SHA의 Masamo service 하나만 시작한다.
8. bot identity, DB target, profile, 로드 Cog와 CPU/RSS 로그를 확인하되 secret을 출력하지 않는다.
9. 기존 명령, 동의 흐름, 운세, DM 제한, Discord/Kakao RAG를 읽기 중심으로 smoke test한다.
10. 재시작 전후 주요 row count와 최신 timestamp를 비교하고 LLM 시도량이 유휴 상태에서
    증가하지 않는지 관찰한다.

필수 schema가 부족하면 운영 프로세스에서 `MASAMONG_AUTO_MIGRATE=true`를 임시로 켜지 않는다.
복원한 staging DB에서 변경을 검증한 뒤, snapshot을 확보하고 필요한 DDL만 별도 승인된
migration으로 적용한다. 현재 자동 migration에는 schema 생성 외에 과거 활동 backfill과
위치 데이터 재시딩도 포함될 수 있다.

실패하면 새 unit을 중지하고, DB를 변경하지 않은 상태에서 이전 unit과 이전 env로 즉시
되돌린다. 새 코드가 DB에 쓰기 시작한 뒤의 rollback은 먼저 snapshot/변경 범위를 확인한다.

## General 첫 배포

General은 아래 순서로 Masamo와 독립적으로 준비한다.

1. 새 Discord 앱/토큰과 예상 bot user ID를 발급한다.
2. 새 빈 `masamong_general`과 전용 최소권한 DB 계정을 만든다.
3. 정기 알림과 RAG를 모두 끈 상태로 첫 schema를 생성한다.
4. 전용 테스트 guild/channel에서 명령과 DB write target을 확인한다.
5. General DB에만 새 테스트 데이터가 생기고 Masamo DB row count는 변하지 않는지 확인한다.
6. `MASAMONG_AUTO_MIGRATE=false`로 바꾸고 읽기 전용 schema 검증으로 재기동한다.
7. 예상 CPU/RSS 범위 안일 때만 제한된 기능을 하나씩 활성화한다.
8. 학교·편입 공지는 계속 끄고 Masamo의 관련 table/파일/timer에 접근이 없는지 확인한다.
9. General용 학교 또는 편입 기능을 별도로 승인하는 시점에만 General 전용 additive
   schema, writable path와 timer를 새로 준비한다.

첫 배포 권장값:

```dotenv
MASAMONG_CPU_THREADS=1
MASAMONG_EXECUTOR_WORKERS=1
AI_MAX_CONCURRENT_PROCESSING=1
AI_QUEUE_WAIT_TIMEOUT_SECONDS=5
LLM_MAX_CONCURRENT_CALLS=1
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
ENABLE_RAIN_NOTIFICATION=false
ENABLE_GREETING_NOTIFICATION=false
ENABLE_EARTHQUAKE_ALERT=false
FORTUNE_MORNING_BRIEFING_ENABLED=false
SCHOOL_NOTICE_ENABLED=false
TRANSFER_NOTICE_ENABLED=false
```

로컬 SentenceTransformer를 두 프로세스가 각각 로드하면 모델 메모리가 거의 두 벌 필요하다.
General RAG는 두 프로세스 합산 RSS, thread 수, load average를 측정한 뒤 활성화한다.

## 최종 합격 기준

- 두 env의 Discord token과 예상 bot ID가 다르다.
- DB identity와 DB 계정, 모든 writable/config 경로가 다르다.
- Masamo 재시작 전후 핵심 테이블 row count와 최신 timestamp가 보존된다.
- 기존 운세 구독, DM 제한, 대화, Discord/Kakao RAG가 그대로 작동한다.
- General에서 Kakao 저장소 객체/쿼리가 생성되지 않고 Masamo 기억이 검색되지 않는다.
- 같은 사용자/guild/message ID를 넣어도 양쪽 DB에서 교차 조회되지 않는다.
- 같은 프로세스 안의 여러 Discord 서버도 페르소나 캐시를 `guild_id`로 구분하고,
  일반 응답·창의형 명령·일상 알림이 목적지 서버의 말투만 사용한다.
- 공통 지진 경보에는 어떤 서버 페르소나나 LLM도 적용되지 않고, 최초 기준점 생성이나
  재기동으로 기존 지진·여진이 재전송되지 않는다. 같은 지진군은 서버별 원본 메시지
  ID만 해당 채널에 매핑해 수정하며 다른 서버의 message ID를 사용하지 않는다.
- General 첫 배포 중 Masamo DB에는 쓰기가 0건이다.
- 정기 운세·기상·지진·maintenance 작업의 소유자가 명확하고 중복 발송이 없다.
- Masamo의 학교 공지 flag와 05:00 timer가 현재 batch를 소유하고 General은 비활성이다.
- Masamo의 편입 공지 flag와 05:35 timer가 전용 snapshot을 소유하고 General은 비활성이다.
- 운영 timer의 정확한 unit 이름은 `masamong-school-notice-batch.timer`와
  `masamong-transfer-notice-batch.timer`이며 `systemctl list-timers --all`에서 다음
  실행 시각을 확인한다. one-shot service가 평소 `inactive (dead)`인 것은 정상이다.
- 미동의·철회 사용자의 운세/학교 프로필 조회와 자동 발송이 중단되고 일반 대화는 유지된다.
- 학교·편입 설정과 결과는 DM에서만 접근 가능하고 서버에서는 개인정보를 조회·표시하지 않는다.
- 학교 batch가 전체 학교가 아니라 동의·활성·등록 프로필의 source만 선택한다.
- 두 프로세스 합산 CPU/RSS가 기존 Masamo 안정성을 해치지 않는다.
- 종료와 재시작 뒤 background task와 DB 연결이 정상 정리된다.

## 남아 있는 운영 과제

프로필 경계는 코드와 설정으로 강제하지만 다음은 별도 운영 강화가 필요하다.

- 정기 알림의 DB lease/claim 및 idempotency key
- API/이미지/DM quota의 원자적 예약
- 학교 공지 수집 core의 사용자별 파생 데이터 삭제를 운영 환경에서 끝까지 검증하는 작업
- 버전형 `schema_migrations`와 staging restore 기반 migration 테스트
- 실제 원격 service unit, grant, CPU 제한과 백업 복원 가능성의 읽기 전용 감사

이 과제가 남아 있으므로 scheduler를 여러 replica에서 동시에 돌리거나 두 프로필이 DB를
공유하는 구성은 계속 금지한다.

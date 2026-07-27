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
| 서비스 unit | `masamong-masamo.service` | `masamong-general.service` |
| 정기 작업 | 기존 소유권 보존 | 첫 배포에서는 전부 끔 |

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
```

`static`은 기존 prompt의 채널/페르소나 동작을 유지한다. 오래된 `guild_settings` 값을
읽기 전용으로 대조하기 전에는 Masamo를 `database` 모드로 전환하지 않는다.

현재 저사양 서비스의 `MASAMONG_CPU_THREADS`, `AI_MAX_CONCURRENT_PROCESSING`,
`EMBEDDING_MAX_CONCURRENCY`, `RAG_MAX_BACKGROUND_TASKS`,
`RAG_MAX_TRACKED_WINDOWS`, `TOKENIZERS_PARALLELISM`도 실제 Masamo env 파일에
명시한다. 명시적 프로필은 다른 인스턴스의 환경값 유입을 막기 위해 env 파일에 없는
`MASAMONG_*` 상속값을 제거하므로, systemd `Environment=`에만 있던 값을 파일로 옮기지
않으면 기존 CPU/동시성 제한이 보존되지 않는다. 숫자는 예제값을 그대로 추정해 쓰지 말고
전환 전 인벤토리에서 확인한 현재 값을 복사한다.

### General 파일

- `profiles/general.env.example`
- `profiles/config.general.example.json`
- `profiles/emb.general.example.json`
- `profiles/prompts.general.example.json`

위 파일을 별도 경로로 복사한 후 placeholder를 실제 값으로 바꾼다. General에는 Masamo
prompt의 채널 ID, Kakao mapping, 기존 embedding 파일 또는 사용자 데이터를 복사하지 않는다.
새 DB 계정은 `masamong_general`에만 최소 권한을 가져야 한다.

General의 첫 빈 DB 생성 때만 `MASAMONG_AUTO_MIGRATE=true`를 사용한다. 이 계정이 기존
`masamong`에 접근하지 못한다는 것을 먼저 확인한다. 스키마 생성과 smoke test가 끝난 뒤에는
향후 명시적인 migration 절차를 적용할 때만 자동 migration을 다시 허용한다.

## `MASAMONG_ENV_FILE` 선택 방식

env 파일 안의 `MASAMONG_ENV_FILE`은 파일 정체성을 확인하는 fingerprint다. 그 줄 자체가
Python이 어느 파일을 열지 선택할 수는 없다. 서비스 관리자가 프로세스 외부에서 같은 절대
경로를 주어야 한다.

```ini
# /etc/systemd/system/masamong-masamo.service
[Service]
User=<service-user>
WorkingDirectory=/opt/masamong/current
Environment=MASAMONG_ENV_FILE=/etc/masamong/masamo.env
ExecStart=/opt/masamong/venv/bin/python /opt/masamong/current/main.py
Restart=on-failure
```

```ini
# /etc/systemd/system/masamong-general.service
[Service]
User=<service-user>
WorkingDirectory=/opt/masamong/current
Environment=MASAMONG_ENV_FILE=/etc/masamong/general.env
ExecStart=/opt/masamong/venv/bin/python /opt/masamong/current/main.py
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
- `MASAMONG_AUTO_MIGRATE=false`에서는 startup 및 runtime helper가 DDL을 실행하지 않고
  필수 테이블과 `guild_settings`, `user_profiles` 컬럼을 읽기 전용으로 검증
- 프로필별 로그 파일을 열 수 없으면 명시적 운영 프로필은 기동 실패

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

출력이 `OK`가 아니면 기동하지 않는다. 이 검사는 live DB의 grant, 데이터 내용 또는
`guild_settings` 상태를 확인하지 않으므로 아래 항목은 별도 읽기 전용 점검이 필요하다.

- 각 DB 계정의 실제 grant
- General 핵심 테이블이 초기에는 비어 있는지
- Masamo 주요 테이블 row count와 최신 timestamp
- 두 DB 간 동일한 기존 사용자/대화/운세/Kakao row가 유입되지 않았는지
- Masamo의 DB 기반 guild 설정과 현재 static prompt가 충돌하지 않는지

## Masamo 무중단 데이터 보존 전환

동일 Discord 토큰으로 두 프로세스를 동시에 실행하면 안 된다. 따라서 데이터 이전은 없지만,
새 프로필 설정으로 넘어갈 때 한 번의 controlled restart는 필요하다.

1. snapshot과 인벤토리를 완료한다.
2. 현재 DB에 필수 테이블과 컬럼이 있는지 읽기 전용으로 확인한다.
3. 실제 두 env에 경계 검사기를 실행한다.
4. 현재 Masamo 프로세스를 정상 종료한다.
5. 같은 토큰을 쓰는 프로세스가 0개인지 확인한다.
6. 동일 release SHA의 `masamong-masamo.service` 하나만 시작한다.
7. bot identity/DB target/profile 로그를 확인하되 secret 값은 출력하지 않는다.
8. 기존 명령, 운세 구독, DM 제한, Discord/Kakao RAG를 읽기 중심으로 smoke test한다.
9. 재시작 전후 주요 row count와 최신 timestamp를 비교한다.

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
6. 예상 CPU/RSS 범위 안일 때만 제한된 기능을 하나씩 활성화한다.

첫 배포 권장값:

```dotenv
MASAMONG_CPU_THREADS=1
AI_MAX_CONCURRENT_PROCESSING=1
EMBEDDING_MAX_CONCURRENCY=1
RAG_MAX_BACKGROUND_TASKS=2
RAG_MAX_TRACKED_WINDOWS=64
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
- General 첫 배포 중 Masamo DB에는 쓰기가 0건이다.
- 정기 운세·기상·지진·maintenance 작업의 소유자가 명확하고 중복 발송이 없다.
- 두 프로세스 합산 CPU/RSS가 기존 Masamo 안정성을 해치지 않는다.
- 종료와 재시작 뒤 background task와 DB 연결이 정상 정리된다.

## 남아 있는 운영 과제

프로필 경계는 코드와 설정으로 강제하지만 다음은 별도 운영 강화가 필요하다.

- 정기 알림의 DB lease/claim 및 idempotency key
- API/이미지/DM quota의 원자적 예약
- 개인정보 삭제 시 history/archive/window/embedding/log까지 지우는 검증된 삭제 작업
- 버전형 `schema_migrations`와 staging restore 기반 migration 테스트
- 실제 원격 service unit, grant, CPU 제한과 백업 복원 가능성의 읽기 전용 감사

이 과제가 남아 있으므로 scheduler를 여러 replica에서 동시에 돌리거나 두 프로필이 DB를
공유하는 구성은 계속 금지한다.

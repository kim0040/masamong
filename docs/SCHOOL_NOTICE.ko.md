# 학교 공지 기능 설명서

이 문서에서는 학교 공지 기능의 사용자 흐름, 수집 코어와의 계약, 운영 설정, 실패 처리와
한계를 확인할 수 있습니다. 사용 방법만 빠르게 보려면 [사용자 가이드](README.ko.md)를,
전체 문서 구조를 보려면 [문서 허브](README.md)를 참고할 수 있습니다. 현재 기능과 누적
상태, 05:00 timer는 Masamo가 소유합니다. General은 기본 비활성이며, 나중에 사용하더라도
별도 DB·파일·timer를 사용해야 합니다.

## 현재 지원 범위

- Masamo: 전용 학교 schema/core DB/digest와 timer로 현재 기능 운영
- General: `SCHOOL_NOTICE_ENABLED=false`로 시작하고 Masamo 상태를 공유하지 않음
- Discord bot: 자연어 등록·확인, 동의, profile/feedback, digest 계약 검증, Discord 전달
- vendored `school_notice` core: 공개 학교 게시판 수집, 사실 분석, 개인화 점수, digest 생성
- 수집 시각: 신규 프로필 확인 직후 해당 사용자만 1회, 이후 매일 `05:00` KST systemd one-shot
- 전달 시각: 사용자별, 신규 기본 `09:00` KST
- 전달 대상 날짜: 당일 05:00 batch가 만든 digest를 당일 사용자 시각에 전달하고,
  장애 복구 때는 최근 3일 안의 가장 최신 유효 결과만 제한적으로 확인
- 관련 공지가 없으면 자동 DM을 보내지 않음

수집 core는 저장소의 `school_notice/`에 포함되어 봇과 같은 release SHA로 배포된다.
다만 CPU·메모리와 실패 범위를 격리하기 위해 상주 봇 event loop에서 import해 돌리지
않고, 신규 1회 확인과 정기 수집 모두 별도 저자원 프로세스로 실행한다.

학교 측 연동 방식은 API가 아니라 공개 HTML 순수 크롤링이다. 학교별 목록·상세 URL과 CSS
selector를 `school_notice/sources.json`에 명시하고, robots·허용 host·redirect·응답 크기·
요청 예산을 적용한다. 쿠키·Referer·환경 proxy는 사용하지 않는다.

## 사용자 시나리오

### 1. 목적별 동의

DM에서 `!메뉴` 또는 `!공지`의 설정 버튼을 누르거나 기능을 자연어로 요청해 시작한다.

```text
!개인정보 동의 학교공지
```

고지문에는 필수 Discord 사용자 ID·학교·과정·학부 학년과, 사용자가 직접 제공했을 때만
저장하는 캠퍼스·학과·입학/학적·관심·알림 설정·피드백의 수집·이용, 보관·철회·삭제
범위가 나온다. 명령을 실행한 본인이
`동의합니다` 버튼을 누르기 전에는 새 정보를 수집하거나 기존 프로필을 이용하지 않는다.
기능 진입 도중 나온 동의 버튼을 누르면 원래 설정 흐름이 한 번만 자동으로 이어진다.

운영에서 프로필 추출 LLM을 켠 경우 사용자가 등록 명령에 입력한 제한된 자연어가 외부 LLM
제공자 한 곳에 Discord ID 없이 전달될 수 있다. 등록 직후 초기 확인은 LLM 없이
수행한다. 05:00 정기 수집의 공지 분석 LLM에는 공개 공지 내용만 전달하고 개인 프로필은
전달하지 않는다. 학교 사이트 요청에도 Discord ID, 사용자 입력, 학과·학년·관심사 등
개인 프로필을 넣지 않는다.

### 2. 자연어 등록

```text
!공지 등록 [학교 이름] [학과] [학년]이고 오전 9시에 알려줘
[학교 이름] [학과] [학년] 공지를 오전 9시에 알려줘
```

먼저 유한한 로컬 파서로 해석하므로 입력만으로 값이 명확하면 프로필 LLM 호출은 0회다.
해결되지 않은 입력만 해석 시도당 routing primary 한 곳을 1회 호출하며 공급자 fallback이나
자동 재시도는 하지 않는다. 한 등록 세션의 실제 공급자 호출은 초안과 보정을 합쳐 기본
최대 3회다. 학교와 캠퍼스는 지원 카탈로그 값만 허용한다. 학과는 알려진 별칭을
카탈로그 값으로 정규화하고, 카탈로그에 없더라도 사용자가 원문에 직접 쓴 제한된 공식
학과명 형식만 보존한다. LLM이 학교·캠퍼스·학과를 임의로 만들 수는 없다.

추출 대상에는 다음과 같은 값이 포함될 수 있다.

- 학교, 캠퍼스, 학과
- 학위 과정과 학년
- 입학 유형과 현재 학적
- 선호 주제
- 사용자별 전달 시각

필수 값이 빠지면 자연스럽게 다시 묻는다.

### 3. 확인 후 저장

봇은 정규화한 내용을 사람이 읽을 수 있는 형태로 보여주고 “제가 이렇게 이해했어요.
맞을까요?”라고 묻는다.

- `맞아`, `맞아요`, `네`, `예`, `확인`, `저장`: 그 값으로 저장
- `학년은 4학년이고 8시 30분에 보내줘`: 현재 후보를 자연어로 수정한 뒤 다시 확인
- `취소`, `그만`, `중단`: 저장하지 않고 종료

확인 전에는 DB에 저장하지 않는다. 한 사용자당 등록 세션은 하나만 허용하며, 각 입력 대기,
수정 횟수, LLM 추출과 전체 흐름은 유한한 timeout/횟수 상한을 가진다. 기존 프로필을 다시
등록·수정하면 새 확인이 끝난 뒤에만 profile version을 올린다.

신규 사용자의 전달 기본값은 `09:00` KST다. 사용자는 등록 대화 또는 별도 시간 명령으로
바꿀 수 있다.

### 4. 최초 수집, 정기 수집과 전달

새 프로필을 처음 확인해 저장하면 `--only-user-id`로 그 사용자 한 명만 DB에서 읽고,
그 학교 source만 대상으로 별도 1스레드·`--no-llm --low-resource` 프로세스를 즉시 한 번
실행한다. 최대 실행 시간과 시도 수는 설정으로 제한하며, 정기 batch lock 충돌만 한 번
기다렸다 재시도한다. 실패·timeout 뒤 무한 재실행하지 않고 다음 05시 수집으로 넘긴다.
같은 notice/revision 전달 기록을 사용하므로 최초 결과가 뒤의 정기 수집에서 재전송되지
않는다.

05:00 KST batch는 다음 조건을 모두 만족하는 profile만 읽는다.

- 현재 학교공지 정책에 동의함
- 철회되지 않음
- 학교 프로필이 존재함
- `enabled=1`

프로필의 `school_id`를 버전 관리된 카탈로그와 vendored core `sources.json`에 대조하고, 교집합의
source ID만 core의 반복 `--source` 인자로 전달한다. 등록되지 않은 학교와 사용자 없는
학교는 수집하지 않는다.

source에 `department:`, `campus:`, `degree:` 범위가 있으면 프로필에서 이미 아는 값과
명백히 다른 전용 게시판은 HTTP 요청 전에 제외한다. 필요한 소속 값이 아직 없으면
게시판 건강도는 확인할 수 있으나 해당 전용 공지는 자동 전달 점수 상한을 39로 두어
숨긴다. 다른 소속임이 확인된 공지는 `INELIGIBLE`로 숨긴다. 범위 태그가 없는 학교 공통
게시판은 최소 프로필 사용자에게도 계속 열린다.

처음 읽은 profile snapshot만 믿지 않는다. 실제 처리 직전, feedback 전후, daily 직전과
publish 직전에 현재 동의, `enabled`, profile version과 정규화 JSON이 그대로인지 다시
확인한다. 성공 run에는 사용한 `profile_version`과 전달 시각을 제외한 정규 프로필의
`profile_hash`를 함께 기록한다. 전달 Cog도 현재 값과 정확히 일치할 때만 전달하므로,
초 단위 timestamp가 같은 순간에 설정이 바뀌어도 이전 digest를 보내지 않는다. 전달 시각만
바꾼 경우에는 개인화 내용이 그대로이므로 이미 생성된 digest를 무효화하지 않는다. 그 사이
사용자가 철회·중지·수정하면 결과를 공개하거나 run을 성공 기록하지 않는다.

검증된 사용자별 digest는 당일 사용자가 선택한 시각에 전달한다. bot 재시작이나
짧은 장애 뒤에는 최근 3일 범위에서 가장 최신의 유효한 성공·부분 성공 batch만 선택한다.
더 최신 성공 batch가 있으면 그보다 오래된 결과를 대신 보내지 않는다. 개인화 결과의
visible item이 0건이면 전달 run만 완료 상태로 기록하고 DM은 보내지 않는다.

### 5. 피드백과 제어

공지별 버튼:

- `유용해요`: 비슷한 주제의 다음 맞춤 우선순위를 조금 높임
- `이 공지 처리했어요`: 같은 공지를 다음 맞춤 목록에서 숨김
- `비슷한 주제 덜 보기`: 유사 주제의 우선순위를 완만하게 낮춤
- `원문 확인`: 공식 상세 페이지로 이동하며 DB나 LLM을 사용하지 않음

모호했던 `지원함` 버튼은 제거했다. 같은 Discord interaction은 한 번만 기록한다.
피드백 버튼은 Discord 접수 응답을 먼저 보낸 뒤 현재 동의와 DB 기록을 확인하며 LLM을
호출하지 않는다. `비슷한 주제 덜 보기`는 영구 차단이 아니고, 강한 주제 숨김은 사용자가
직접 `음소거`로 설정한다.
등록금·수강·학적·졸업·병무 관련 근거 있는 필수 행정 공지는 보호 규칙에 따라 계속 보일 수
있다.

## 사용자 명령

모든 개인화 명령은 DM에서 사용한다.

| 명령 | 동작 |
|---|---|
| `!공지` | 설정·최근 공지·시간·중지/재개 버튼이 있는 대시보드 |
| `!공지 1` | 현재 사용할 수 있는 digest의 1페이지를 다시 표시 |
| `!공지 등록 <자연어>` | 신규 등록 또는 확인 기반 갱신 |
| `!공지 수정 <자연어>` | 기존 프로필 후보를 자연어로 보정하고 다시 확인 |
| `!공지 정보` | 저장된 최소 프로필, 전달 시각과 활성 상태 확인 |
| `!공지 상태` | 최근 등록 학교 공개 게시판의 수집 결과와 다음 수집 시각 확인 |
| `!공지 시간 HH:MM` | 사용자별 KST 전달 시각 변경 |
| `!공지 중지` | 수집·전달 대상에서 일시 제외, 데이터 보존 |
| `!공지 재개` | 현재 동의 확인 뒤 다시 활성화 |
| `!공지 음소거 [주제]` | 목록 확인 또는 주제 숨김 |
| `!공지 음소거해제 <주제>` | 주제 숨김 해제 |
| `!공지 삭제` | 학교 프로필과 연결된 개인화 데이터 삭제 및 동의 철회 |

기존 프로필을 바꾸려면 `!공지 수정 <자연어>`를 실행하고 새 확인 과정을 거친다.

`!개인정보 철회 학교공지`는 향후 profile 조회, batch, 개인화, feedback, 자동 전달을 즉시
중단하지만 기존 데이터와 `enabled` 상태를 보존한다. `!공지 삭제`는 profile, feedback,
전달·batch·delivery run과 사용자별 digest/profile 파생 파일을 정리한다. 일반 Discord
대화와 서버 기록, 목적별 동의 감사 이벤트는 삭제하지 않는다.
수집 batch가 같은 digest root의 lock을 잡고 있으면 삭제는 부분 삭제를 시도하지 않고
수집 종료 후 다시 요청하도록 안내한다.

## 지원 학교 카탈로그

현재 마사몽 카탈로그는 14개 학교와 17개 core source ID를 정의한다.

| school ID | 학교 | source ID |
|---|---|---|
| `jbnu` | 전북대학교 | `jbnu_campus`, `jbnu_software` |
| `snu` | 서울대학교 | `snu_general` |
| `pnu` | 부산대학교 | `pnu_general` |
| `korea` | 고려대학교 | `korea_academic`, `korea_cs_undergrad` |
| `jj` | 전주대학교 | `jj_academic` |
| `skku` | 성균관대학교 | `skku_general` |
| `gachon` | 가천대학교 | `gachon_general` |
| `ssu` | 숭실대학교 | `ssu_general` |
| `jnu` | 전남대학교 | `jnu_software` |
| `scnu` | 국립순천대학교 | `scnu_academic` |
| `mju` | 명지대학교 | `mju_general` |
| `konkuk` | 건국대학교 | `konkuk_academic` |
| `kookmin` | 국민대학교 | `kookmin_academic` |
| `hanyang` | 한양대학교 | `hanyang_seoul`, `hanyang_erica` |

카탈로그는 `profiles/catalogs/school_notice_catalog.v1.json`이다. 학교가 목록에 있어도
같은 release의 `school_notice/sources.json`에 같은 source ID가 없으면 실행하지 않는다. 새 학교는 카탈로그와
core source 설정, selector/host/robots 계약, fixture/live-check를 함께 추가해야 한다.
로그인·SSO·CAPTCHA 우회는 지원하지 않는다.

2026-07-29 최종 live-check에서는 17개 source 모두 목록과 상세 진입에 성공했고,
source당 상세 2건씩 총 34건을 확인해 모두 `healthy`였다. 본문이 이미지뿐인 개별
공지가 다시 나타나면 성공으로 숨기거나 이미지 내용을 추측하지 않고 `degraded`와
원문 확인 필요를 표시한다.

## 프로세스와 저장소 경계

```text
Discord DM
  └─ 동의 → 자연어 후보 → 사용자 확인 → Masamo DB profile

신규 프로필 확인
  └─ run_school_notice_batch.py --only-user-id <id> (one-shot, thread 1, no LLM)
       └─ 해당 사용자와 등록 학교 source만 즉시 한 번 확인

05:00 KST systemd timer
  └─ run_school_notice_batch.py (one-shot, thread 1)
       ├─ 동의·활성 profile 조회
       ├─ profile 학교 source만 선택
       ├─ 미반영 feedback을 수집 core에 먼저 반영
       ├─ vendored core daily를 사용자별 순차 실행
       └─ 계약 검증 후 digest/run report 원자적 공개

상주 Masamo bot
  └─ 1분 delivery catch-up
       ├─ 사용자별 시각과 보통 전날 digest 확인
       ├─ 장애 시 최근 3일 중 최신 유효 batch만 제한적으로 확인
       ├─ 동의 재검증
       ├─ revision 단위 중복 방지
       └─ 관련 item이 있을 때만 DM
```

상주 봇의 event loop는 크롤링하거나 공지 분석 LLM을 호출하지 않는다. CPU/RSS가
일시적으로 증가하는 수집·HTML parsing·분석은 초기 child process 또는 `Type=oneshot`
systemd 별도 프로세스에서 수행한다.

저장소:

| 데이터 | 소유자/위치 |
|---|---|
| Discord 사용자 ID와 정규화 profile | Masamo main DB |
| 목적별 동의 current/event | Masamo main DB |
| feedback, 전달·batch·delivery run | Masamo main DB |
| 공지 snapshot/revision/분석 cache | Masamo 전용 core SQLite |
| 사용자별 digest/run report | Masamo 전용 digest directory |
| 학교/별칭/source mapping | 버전 관리된 catalog + vendored `sources.json` |

두 프로필이 이 경로와 DB를 공유하지 않는다. General에는 school table이 없어도
`SCHOOL_NOTICE_ENABLED=false`이면 정상 기동해야 한다.

## Batch wrapper

기본 명령:

```bash
MASAMONG_ENV_FILE=/etc/masamong/masamo.env \
  <masamong-venv>/bin/python scripts/run_school_notice_batch.py \
  --core-python <masamong-venv>/bin/python \
  --core-cwd /srv/masamong/current \
  --source-config /srv/masamong/current/school_notice/sources.json
```

옵션:

```text
--date YYYY-MM-DD
--dry-run
--no-llm / --use-llm
--low-resource
--max-details-per-source N
--max-requests N
--max-profiles N
--profile-timeout-seconds N
--feedback-timeout-seconds N
--batch-deadline-seconds N
--only-user-id DISCORD_USER_ID
```

`--source-config`를 생략하면 `SCHOOL_NOTICE_SOURCE_CONFIG`, 그것도 없으면
`<core-cwd>/school_notice/sources.json`을 사용한다. 날짜를 생략해도 서버 timezone에
의존하지 않고 `Asia/Seoul` 오늘 날짜를 계산해 core `daily --date`에 항상 명시한다.

CLI 기본은 안전한 수동 실행을 위해 `--no-llm --low-resource`다. 등록 직후 초기 확인도
이 모드를 사용한다. 운영 05:00 systemd service는 `--use-llm --low-resource`를 명시해
공개 상세 본문을 분석한다.

### Feedback 선처리

미반영 feedback은 사용자 `daily`보다 먼저 수집 core의 `feedback` CLI에 전달한다.
프로세스 종료 코드 0과 구조화 JSON의 `recorded > 0`을 모두 확인한 ID만 `consumed_at`으로
표시한다. 한 건이라도 실패하면 옛 선호 상태로 digest를 만들지 않고 그 사용자의 daily를
실패로 남겨 다음 유한 batch에 보류한다.

### Stage와 publish

- 임시 profile과 directory는 각각 mode `0600`, `0700`
- core 출력은 사용자·날짜·schema·크기·타입·URL·항목 수 계약을 검증
- 계약·source 경계·subprocess·publish 실패는 개인정보나 원문 없이 유한한
  `failure_stage` 코드로 운영 로그에 남김
- 공개 run report는 원문/error text를 제외한 최소 집계만 보존
- run report를 먼저, digest를 마지막에 `os.replace`
- 최종 JSON은 mode `0600`
- 임시 profile은 성공/실패와 관계없이 제거
- digest root별 non-blocking file lock으로 batch 하나만 허용
- lock 획득 뒤 같은 service UID가 만든 실제 `.profiles/run-*` directory만 stale cleanup;
  symlink와 다른 이름은 건드리지 않음

dry-run은 SQLite일 때 `mode=ro`로 열고 lock, directory, profile/digest 파일, DB update를
만들지 않는다.

### 자원 한도와 종료 코드

기본 한도:

| 한도 | 기본 | 코드 허용 범위 |
|---|---:|---:|
| profile 수 | 50 | 1~500 |
| profile daily timeout | 600초 | 1~1800초 |
| feedback timeout | 60초 | 1~300초 |
| 전체 deadline | 1800초 | 1~7200초 |

profile 상한을 넘으면 `never-run → least-recently-run → user_key` 순으로 고르는 공정
rotation을 사용한다. 처리하지 못한 사용자가 다음 batch에서 우선된다. 하지만 용량 부족을
숨기지 않고 종료 코드 2와 경고를 남긴다.

| 코드 | 의미 |
|---:|---|
| 0 | 대상 없음, 모두 `succeeded`, 또는 정확히 기록된 `partial` |
| 1 | `SCHOOL_NOTICE_ENABLED=false` |
| 2 | profile/feedback/daily 실패, timeout, deadline, profile cap |
| 3 | single-flight lock 충돌 |

수집 core가 nonzero로 끝나면 생성된 파일이 있어도 성공으로 간주하지 않는다. 각 사용자
`school_notice_batch_runs.status`와 run report의 `status`/`collection_health`도 함께 본다.
`partial`은 숨겨진 완전 성공이 아니다. `school_notice_batch_runs`에는
`profile_version`과 `profile_hash`도 필요하며, 이 컬럼이 생기기 전의 기존 run은 새 batch가
성공할 때까지 전달 불가 상태로 취급한다. 이는 옛 개인화 결과를 잘못 보내지 않기 위한
의도적인 fail-closed 동작이다.

## Digest 계약

마사몽이 읽는 JSON 최상위:

```text
schema_version: int
user_key: str
date: YYYY-MM-DD
summary: {action, opportunity, reference}
collection_health: object | null
items: array
```

각 item의 핵심:

```text
notice_id: int
revision_count: int
change: new | updated | unchanged
notice.candidate: source_id, external_id, title, url, source metadata
analysis: summary, audiences, topics, actions, required, urgency, dates, evidence
score: score, band, eligibility, reasons, deadline, profile_version
duplicate_sources: array
```

수신 검증은 fail-closed다.

- 파일 최대 8 MiB
- item 최대 300개
- JSON depth와 문자열 길이 상한
- 필수 타입/enum/URL 검증
- digest의 `user_key`와 요청한 사용자가 다르면 거부
- digest 날짜와 요청 날짜가 다르면 거부
- 예상 schema version 불일치 거부
- 중복 notice/revision 거부
- Discord 일반 메시지 2,000자, embed 설명 4,096자·전체 6,000자·field 25개 제한
- 자동 digest 한 페이지의 공지 항목은 기본 최대 10개

본문 전체 대신 `analysis.summary`를 표시하고 `score.reasons`를 “왜 추천됐는지” 근거로
보여준다. `eligibility=UNKNOWN`과 추론 날짜는 원문 확인이 필요함을 표시한다. 본문이
이미지 중심이거나 공개 HTML 텍스트가 짧다는 parser 경고가 있으면 제목·게시판 분류만
읽었을 수 있음을 별도 표시한다. 공지 원문 링크가 최종 판단 기준이다.

## 개인화와 알림 의미

수집 core는 공지의 사실 추출과 사용자별 판단을 분리한다. LLM을 켜도 공지 사실·근거 후보
구조화에만 사용하고, 사용자 profile에 대한 최종 점수는 재현 가능한 로컬 규칙이 담당해야
한다.

대표 band:

```text
score >= 80  → action
score >= 60  → opportunity
score >= 40  → reference
그 외        → hidden
INELIGIBLE   → hidden
다른 학위 과정 대상 가능성 → UNKNOWN, score <= 39, hidden
```

명시 자격 조건이 있으나 profile 값이 없어 판단할 수 없으면 `UNKNOWN`과 69점 상한을
사용한다. 마감이 지난 비필수 공지는 숨긴다. 등록금·수강·학적·졸업·병무의 근거 있는 필수
공지는 사용자의 일반 주제 음소거보다 우선할 수 있다.

이 점수는 합격 확률이나 모델 confidence가 아니다. 설명 가능한 우선순위다. 신청·제출 전에
항상 원문을 확인해야 한다.

## 전달 멱등성과 재시도

- 전달 식별은 사용자 + notice + revision 기준이다.
- 같은 revision은 재발송하지 않는다.
- 내용이 바뀌어 revision이 증가한 공지는 업데이트로 다시 전달할 수 있다.
- 하루 delivery run은 사용자/대상 날짜별 상태, attempt, 다음 시도, 안전한 error code를
  기록한다.
- 1분 catch-up이 사용자별 시각을 확인하므로 bot 재시작이나 짧은 지연을 흡수한다.
- 05:00에 생성한 오늘 digest를 같은 날 설정 시각(기본 09:00)에 우선 처리한다.
  이전 날짜 backlog는 오늘의 더 최신 성공 digest가 없을 때만 최근 3일 상한 안에서 본다.
- 한 tick의 사용자 수, 사용자별 처리 시간, 최대 attempt와 retry 간격을 제한한다.
- DM 차단, contract 오류, timeout, send 실패를 성공으로 기록하지 않는다.
- 일부 item 전송 뒤 실패한 경우 이미 기록된 revision은 다시 보내지 않고 남은 item만
  재시도한다.
- 자동 전달은 `SCHOOL_NOTICE_MAX_ITEMS_PER_DM`개씩 페이지로 나눈다. 한 페이지가
  성공하면 각 revision을 즉시 기록하고 failure attempt를 0으로 되돌린 뒤
  `more_pending` 상태로 남은 페이지를 다음 1분 tick에서 이어 보낸다. 연속 실패만 최대
  attempt를 소비한다.
- visible item이 없으면 완료로 기록하되 DM하지 않는다.

사용자가 수동 `!공지 [페이지]`를 실행하는 것과 자동 전달 기록은 구분한다. 예를 들어
`!공지 2`로 두 번째 페이지를 확인할 수 있다. 수동 확인은 “오늘 아무 관련 공지 없음”을
알려줄 수 있지만 자동 scheduler는 불필요한 빈 알림을 보내지 않는다.

## 운영 설정

주요 env:

```dotenv
SCHOOL_NOTICE_ENABLED=true
SCHOOL_NOTICE_DIGEST_DIR=/var/lib/masamong/masamo/notice/out
SCHOOL_NOTICE_CORE_DB=/var/lib/masamong/masamo/notice/core.db
SCHOOL_NOTICE_CATALOG_PATH=/srv/masamong/current/profiles/catalogs/school_notice_catalog.v1.json
SCHOOL_NOTICE_SOURCE_CONFIG=/srv/masamong/current/school_notice/sources.json

SCHOOL_NOTICE_DELIVERY_HOUR=9
SCHOOL_NOTICE_DELIVERY_MINUTE=0

SCHOOL_NOTICE_PROFILE_LLM_ENABLED=true
SCHOOL_NOTICE_PROFILE_MAX_REVISIONS=3
SCHOOL_NOTICE_PROFILE_INPUT_TIMEOUT_SECONDS=120
SCHOOL_NOTICE_PROFILE_LLM_TIMEOUT_SECONDS=20

SCHOOL_NOTICE_INITIAL_CRAWL_ENABLED=true
SCHOOL_NOTICE_INITIAL_CRAWL_TIMEOUT_SECONDS=660
SCHOOL_NOTICE_INITIAL_CRAWL_MAX_ATTEMPTS=2
SCHOOL_NOTICE_INITIAL_CRAWL_RETRY_SECONDS=20

SCHOOL_NOTICE_DELIVERY_BATCH_SIZE=10
SCHOOL_NOTICE_DELIVERY_MAX_ATTEMPTS=3
SCHOOL_NOTICE_DELIVERY_RETRY_MINUTES=10
SCHOOL_NOTICE_DELIVERY_USER_TIMEOUT_SECONDS=30

SCHOOL_NOTICE_BATCH_MAX_PROFILES=50
SCHOOL_NOTICE_BATCH_PROFILE_TIMEOUT_SECONDS=600
SCHOOL_NOTICE_BATCH_FEEDBACK_TIMEOUT_SECONDS=60
SCHOOL_NOTICE_BATCH_TOTAL_TIMEOUT_SECONDS=1800

SCHOOL_NOTICE_MAX_ITEMS_PER_DM=10
SCHOOL_NOTICE_SCHEMA_VERSION=1
SCHOOL_NOTICE_STALE_WARNING_ENABLED=false
```

`SCHOOL_NOTICE_MAX_ITEMS_PER_DM`은 자동·수동 한 페이지의 최대 항목 수다.
경로는 실제 배포 layout의 절대 경로여야 한다. `SCHOOL_NOTICE_ENABLED=true`이면 digest와
core DB 경로는 비어 있지 않은 절대 경로여야 하고, catalog와 source config는 실제 파일로
존재해야 한다. 조건을 만족하지 않으면 기동을 중단한다. 위 예시는 `/srv` layout이며
`profiles/masamo.env.example` 및 systemd 템플릿도 같은 layout을 사용한다. 실제 서버에서
다른 경로를 쓰면 env와 unit의 모든 절대 경로를 함께 바꾼다.

05:00 수집 시각의 source of truth는 아래 systemd timer다. 봇 env의
`SCHOOL_NOTICE_DELIVERY_HOUR/MINUTE`는 신규 profile의 기본 전달 시각이며, 저장된
사용자별 `delivery_time`이 우선한다.

## systemd timer

템플릿:

- `deploy/systemd/masamong-school-notice-batch.service`
- `deploy/systemd/masamong-school-notice-batch.timer`

timer:

```ini
OnCalendar=*-*-* 05:00:00 Asia/Seoul
Persistent=true
RandomizedDelaySec=0
AccuracySec=1min
```

service는 `Type=oneshot`, 내부 최대 deadline 7,200초에 cleanup 여유를 둔
`TimeoutStartSec=7800`, CPU/BLAS thread 1, `CPUQuota=25%`, `MemoryMax=384M`,
낮은 CPU/IO weight, `UMask=0077`, `NoNewPrivileges=true`를 사용한다. env에는
secret을 unit literal로 넣지 않고
`MASAMONG_ENV_FILE=/etc/masamong/masamo.env`만 선택한다.

템플릿은 별도 core repository를 내려받지 않는다. 같은 release의 `school_notice/`와
Masamo Python 환경을 사용한다. 실제 service 사용자, group, path, 권한을 맞춘 뒤 수동
dry-run과 제한된 batch가 성공해야 Masamo timer를 켠다. General에는 이 timer를 설치하지
않는다.

## 실패와 알림 정책

| 상황 | 처리 |
|---|---|
| profile 동의 없음/철회 | batch와 전달에서 제외 |
| 지원하지 않는 학교/source 불일치 | profile 실행 실패, 다른 학교 전체 수집으로 fallback하지 않음 |
| feedback 반영 실패 | 그 사용자 daily 보류 |
| 일부 source 실패 | `partial`/degraded를 정확히 기록 |
| 모든 source/core 실패 | failed, 새 digest 성공 공개 안 함 |
| digest 계약 불일치 | 전달 거부, `failure=digest_contract` 운영 진단 |
| 관련 visible item 0 | 완료 기록, 자동 DM 없음 |
| DM 차단/HTTP 실패 | 유한 재시도 상태 |
| batch 중복 실행 | lock 충돌 종료 코드 3 |
| deadline/profile cap | 종료 코드 2, 다음 실행 공정 rotation |

`SCHOOL_NOTICE_STALE_WARNING_ENABLED=false`가 기본이므로 수집 문제만으로 사용자에게 빈 경고
DM을 만들지 않는다. 운영자가 true로 켜면 stale 경고의 의미와 사용자 경험을 별도로
검토해야 한다.

## 알려진 한계

- selector가 변경되거나 OCR/로그인/브라우저 렌더링이 필요한 게시판은 source별 보강이
  필요하다.
- 제목 기반 중복은 의미 기반 중복이 아닐 수 있다.
- 공개·비로그인 게시판만 대상이며 robots, 허용 host, redirect와 요청 크기 제한을 지켜야
  한다.
- 학교 공지는 추천 후보와 근거를 제공할 뿐 신청·제출을 대신하거나 자격을 보증하지 않는다.
- bot 여러 replica에서 같은 scheduler를 동시에 소유하는 distributed lease는 제공하지
  않는다. profile당 상주 bot 하나, batch timer 하나만 운영한다.

## 검증

마사몽:

```bash
.venv/bin/python -m pytest -q \
  tests/school_notice_profile_spec.py \
  tests/school_notice_contract_spec.py \
  tests/school_notice_render_spec.py \
  tests/school_notice_batch_spec.py \
  tests/school_notice_cog_spec.py \
  tests/school_notice_isolation_spec.py
```

실제 공개 게시판 selector와 본문 품질은 네트워크 live-check로 별도 확인한다.

```bash
.venv/bin/python -m school_notice live-check \
  --details-per-source 2 \
  --max-requests 96 \
  --output-dir /tmp/masamong-school-livecheck
```

`healthy/degraded/failed`, 목록 후보 수, 상세 성공 수와 body-quality를 source별로 확인한다.
본문이 이미지뿐인 공지는 `degraded`일 수 있지만 목록·상세 selector 실패와 구분해야 한다.

추가 확인:

- vendored core 전체 테스트
- 등록 → 오인식 수정 → 확인 → 저장 → 재수정 → 취소 시나리오
- 미동의·철회·정책 변경 fail-closed
- 14개 학교 별칭과 지원하지 않는 학교 거부
- 등록 profile source만 core 명령에 포함
- 신규 사용자 1명만 읽는 즉시 수집과 KST 05:00 timer, 당일 09:00/사용자 시간
- 최근 3일 복구 범위에서 최신 유효 batch만 선택
- 빈 digest 자동 무알림
- revision 중복 방지, 수동 페이지 조회, 자동 페이지 연속 전달과 부분 전송 후 재시도
- feedback 실제 반영 뒤에만 consumed
- dry-run 완전 무변경
- timeout/deadline/lock/profile rotation
- General flag false에서 school table 없이 정상 기동
- General과 Masamo의 DB/core/digest/log/timer 경로가 겹치지 않음

운영 배치와 rollback은 [배포 가이드](DEPLOYMENT.md)를 따릅니다.

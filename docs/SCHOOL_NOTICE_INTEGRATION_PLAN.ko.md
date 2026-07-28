# 학교 공지 추적 기능 명세와 마사몽 통합 계획

이 문서 하나로 기능의 의도·설계·규칙·데이터 계약을 모두 파악할 수 있게 작성했다.
원본 코어(`school_notice`, 17개 모듈 4,887 LOC, 테스트 1,781 LOC)는 이 저장소에
없으므로, 여기 적힌 내용이 구현 기준이다. 수치와 규칙은 전부 코어 소스에서 확인해
옮겼다.

---

## 1. 이 기능이 푸는 문제

학교 공지는 게시판·학과·캠퍼스마다 흩어져 있고, 같은 글이 여러 곳에 다시 올라오며,
중요한 조건과 마감이 긴 본문 안에 묻혀 있다.

목적은 **모든 공지를 대신 읽어주는 것도, 사용자를 대신해 신청하는 것도 아니다.**
하루 한 번 공개 게시판의 최근 글을 제한적으로 확인해 다음 세 질문에 **근거 있는
후보**를 제시하는 것이다.

1. 내가 반드시 확인하거나 해야 할 일인가? → `action`
2. 내 조건에 맞고 도움이 될 기회인가? → `opportunity`
3. 당장 행동하지 않아도 알아둘 공지인가? → `reference`

출력은 최종 판단이 아니라 **우선순위가 붙은 후보 목록과 그 근거**다. 신청 전 원문
확인은 항상 사용자 몫이며, UI는 이를 명시해야 한다.

## 2. 설계 원칙

이 여섯 가지가 구현 전반을 지배한다. 통합 과정에서 깨뜨리면 기능의 성격이 바뀐다.

| 원칙 | 의미 |
|---|---|
| **사실 추출과 사용자 판단 분리** | LLM은 공지에서 사실만 뽑는다. "이 학생에게 추천할지"는 로컬 규칙이 정한다. 그래야 같은 분석을 여러 사용자에게 재사용하고 판정 이유를 재현할 수 있다 |
| **놓칠 위험과 과잉 알림을 함께 관리** | 명시적 자격 불일치는 숨기되, 프로필 값이 없어 판단 불가한 것은 `UNKNOWN`으로 남기고 상한만 건다. 임의로 지워버리지 않는다 |
| **피드백을 완만하게 반영** | `관심 없음` 한 번으로 비슷한 공지를 영구 차단하지 않는다. 90일 반감기로 서서히 중립에 돌아온다 |
| **실패를 성공처럼 보이지 않기** | "오늘 새 공지 없음"과 "오늘 확인 실패"를 반드시 구분해 표시한다 |
| **저사양 상한을 분명히** | 한 번 실행 후 종료. 요청 수·응답 크기·첨부·LLM 호출 전부 상한이 있다 |
| **학교와 전달 채널 분리** | 학교별 차이는 JSON 설정으로, Discord는 얇은 어댑터로 처리한다 |

## 3. 한 번의 실행에서 일어나는 일

```
프로필 검증
  → 같은 school_id의 source 선택
  → source별 robots 확인 · 목록 첫 페이지 수집
  → 최근 N개 상세 수집 · 본문 정규화
  → (선택) 첨부 텍스트 추출
  → 공지 upsert · 내용 변경 시 revision 저장
  → 학교 DB의 active 공지 분석 또는 분석 캐시 조회
  → 사용자 프로필·피드백으로 로컬 점수화
  → 중복 묶기 · 알림 설정 적용
  → Markdown/JSON digest와 실행 보고서 저장
  → 프로세스 종료
```

책임 분리:

| 구성 요소 | 하는 일 | 하지 않는 일 |
|---|---|---|
| `sources.json` | 학교 URL·CSS 선택자·검증 계약 | 네트워크 요청 |
| `http` | 안전한 제한 HTTP·robots·재시도 | HTML 의미 해석 |
| `parsing` | 목록/상세 HTML → 공통 Notice | 사용자 관련성 판단 |
| `attachments` | 선택 첨부의 제한적 텍스트 추출 | OCR·무제한 압축 해제 |
| `storage` | 공지·revision·분석·프로필·피드백·digest 저장 | 외부 알림 전송 |
| `analysis`, `llm` | 사실·날짜·자격 후보 구조화 | 사용자별 최종 결정 |
| `personalization` | 프로필·피드백 기반 점수와 자격 판정 | 공지 원문 변경 |
| `digest` | 중복 묶기·알림 설정·표현 | Discord API 호출 |
| `daily` | 위 단계를 한 번 조율 | 상주 스케줄링 |

모든 source가 같은 selector 기반 파서를 쓴다. 학교별 분기 클래스는 없고 특수한 URL도
JSON의 정규식·template으로 표현한다.

## 4. 공지의 식별·변경·중복

- 영속 식별자는 `(source_id, external_id)`.
- 제목·게시일·본문·첨부 URL·본문 이미지 URL을 정규화해 `content_hash`를 만든다.
  첨부 추출을 켜면 첨부 SHA-256도 최종 hash에 포함한다.
- hash가 바뀌면 revision을 1 올리고 전체 snapshot을 저장한다.
- 게시판 간 중복은 **정규화한 제목의 SHA 키**로 묶는다. 점수가 높은 한 건을 대표로
  보여주고 나머지는 `duplicate_sources`에 원문 링크로 남긴다.

**한계**: 의미 기반(fuzzy/embedding) 비교가 아니다. 제목이 크게 다른 재게시는 놓치고,
우연히 제목이 같은 다른 공지는 합쳐질 수 있다.

## 5. 분석 — 규칙과 LLM의 경계

### 5.1 규칙 우선

분석 입력은 제목 + 본문 (+선택적 첨부 텍스트)을 합쳐 **최대 18,000자**로 자른다.
먼저 규칙으로 뽑는다.

- 명시 날짜와 마감/행사 구분
- 행동: `신청`, `지원`, `제출`, `등록`, `수강신청`, `납부`, `신고`, `참여`
- 대상: `학부생`, `대학원생`, `재학생`, `휴학생`, `복학생`, `신입생`, `졸업예정자`,
  `교직원`, `외국인학생`, `편입생`
- 주제: `장학`, `등록금`, `수강`, `학적`, `졸업`, `취업`, `기숙사`, `국제교류`,
  `공모전`, `병무`
- 자격 조건: 학년 1~6, 학번 연도, 이수 학기, 편입 대상, GPA
- 필수 표현, 근거 문장

연도 없는 `월/일`은 제목이나 게시일에서 기준 연도를 찾은 경우에만 보완하고
`inferred_year: true`로 표시한다. **날짜가 안 보이면 만들지 않는다.**

### 5.2 LLM(DeepSeek)의 역할과 제한

LLM을 켜면 규칙 결과와 공지 텍스트를 보내 구조화 JSON의 품질을 보완한다.

- **프로필·피드백·API 키는 프롬프트에 넣지 않는다.**
- 공지 내용은 신뢰할 수 없는 입력으로 취급하고, 그 안의 지시를 따르지 않도록
  system prompt에서 분리한다.

LLM 결과를 그대로 신뢰하지 않고 다음을 **버린다**:

| 검증 | 처리 |
|---|---|
| 규칙이 못 찾은 날짜를 LLM이 새로 만듦 | 버림 |
| 근거 문장이 원문에 실제로 없음 | 버림 |
| 자격 조건이 검증 가능한 학년·편입·GPA 범위 밖 | 버림 |
| 필수 여부에 필수 표현 근거가 없음 | 받지 않음 |
| JSON 오류 | 의미를 더하지 않는 1회 수리 후 실패 처리 |
| API 장애·예산 소진·계약 위반 | 규칙 결과로 fallback |

분석 캐시 키는 `notice_id + content_hash + analyzer_version`이며 analyzer version에
모델명이 들어간다. 내용·규칙 버전·모델이 같으면 같은 DB에서 재호출하지 않는다.

기본 예산: 실행당 20회, 한국 날짜 기준 하루 30회, 요청당 재시도 2회, 연속 실패 3회 시
회로 차단, JSON 수리 호출 1회.

## 6. 개인화 점수 규칙

**기능의 핵심.** 점수는 20점에서 시작한다. 아래가 코드의 실제 규칙이다.

| 신호 | 효과 |
|---|---:|
| 학부/대학원 대상 일치 | +20 |
| 학부생 프로필에 재학생 대상 | +10 |
| 학위 대상이 반대인 공지 | −40 |
| 명시 학년 일치 / 불일치 | +15 / −35 및 `INELIGIBLE` |
| 학번·이수학기·GPA·입학유형 일치 | `ELIGIBLE` |
| 위 강한 조건 불일치 | −45 및 `INELIGIBLE` |
| 위 조건이 있으나 프로필 값 없음 | `UNKNOWN`, 최종 **69점 상한** |
| 전공 직접 일치 / 다른 전공 전용 게시판 | +22 / −20 |
| source 학위 tag 일치 / 불일치 | +10 / −35 |
| 캠퍼스 일치 / 불일치 | +12 / −15 |
| `strict_campus`에서 캠퍼스 불일치 | −60 및 `INELIGIBLE` |
| 편입 프로필 + 편입 관련 원문 | +15 |
| 학적 대상 일치 | +10 |
| 휴학생인데 재학생 전용 | −25 |
| 관심·선호 항목 일치 | 항목당 +6, 최대 +18 |
| 우선 키워드 일치 | 항목당 +10, 최대 +20 |
| 해야 할 행동 존재 / 필수 표현 | +10 / +20 |
| 긴급도 low / normal / high / critical | +0 / +3 / +10 / +18 |
| 마감 3일 / 7일 / 30일 이내 / 그 이후 | +20 / +15 / +8 / +2 |
| 마감 지남 | −50, 필수가 아니면 숨김 |

적용 순서가 중요하다.

1. 위 신호를 합산하고 **0~100으로 clamp**
2. 주제 피드백 가중치를 **곱한다** (7장)
3. `muted_topics` 또는 제외 키워드 일치 → **20점 상한**
4. 단, **필수 행정 공지 보호**: 주제가 `등록금·수강·학적·졸업·병무` 중 하나이고
   필수 표현이 있으며 `INELIGIBLE`이 아니면 → **최소 70점 보장** (3번을 무시)
5. 확인 불가 자격 조건이 있으면 → **69점 상한**
6. 다시 0~100 clamp 후 반올림

밴드:

```
마감 지났고 필수 아님 → hidden
score >= 80 → action
score >= 60 → opportunity
score >= 40 → reference
그 외        → hidden
INELIGIBLE  → 점수와 무관하게 숨김
```

digest 단계에서 사용자의 `minimum_score`, `include_bands`, 밴드별 최대 개수를 한 번 더
적용한다.

**이 점수는 확률이나 모델 confidence가 아니다.** 설명 가능하고 조정 가능한 우선순위
휴리스틱이다. 그래서 `score.reasons`에 근거가 함께 나오며, UI는 이것을 반드시
노출해야 한다.

## 7. 피드백 설계

두 종류로 나뉜다.

**선호 학습** — 공지의 topic에 완만한 가중치를 남긴다.

| 피드백 | 가중치 델타 |
|---|---:|
| `applied` | +0.10 |
| `useful` | +0.06 |
| `saved` | +0.04 |
| `already_knew` | −0.03 |
| `not_interested` | −0.06 |

누적 효과는 **90일 반감기**(`0.5 ^ (경과일/90)`)로 감쇠하고, 최종 가중치는
**0.70~1.30**으로 제한된다.

**해당 공지 상태** — 그 공지에 직접 적용된다.

- `completed`, `dismiss_once`: 즉시 숨김
- `not_eligible`: 명시적 자격 없음 처리
- `mute_topic`: 사용자가 고른 주제를 강하게 낮춤 (단 6장 4번 보호 규칙이 우선)

**의도적으로 하지 않는 것**: `not_interested`를 여러 번 눌러도 자동으로 `mute_topic`으로
승격하지 않는다. 사용자 의도를 과도하게 추측하지 않기 위한 선택이다.

topic 분류가 틀리면 피드백이 인접 공지에도 영향을 준다. 그래서 UI는 **"왜 덜 보이는지"
근거를 표시하고 명시적 음소거 해제 기능을 제공해야 한다.** "영구 차단"처럼 표현하면
설계 의도와 어긋난다.

## 8. 실패 처리

source 상태:

- `healthy`: 오류 없이 선택한 상세를 처리
- `degraded`: 일부 상세는 성공했지만 오류 있음
- `failed`: 오류가 있고 상세 성공 0건

전체 실행은 모든 source 실패 시 `failed`, 오류가 하나라도 있으면 `partial`, 그 외
`succeeded`.

| 상황 | 동작 | 사용자가 알아야 할 것 |
|---|---|---|
| 목록 요청 실패 | 그 source 신규 수집 생략 | 이전 active 공지가 다시 표시될 수 있음 |
| 일부 상세 실패 | 성공한 글만 갱신 | 실패한 새 글은 당일 digest에서 빠질 수 있음 |
| HTML 구조 변경 | 최소 계약 경고·실패 | source 설정 수정 필요 |
| LLM 키 없음/장애 | 규칙 분석으로 계속 | 요약·자격 정밀도 하락 |
| 요청/LLM 예산 소진 | 유한 실패 | 무한 재시도 안 함 |
| 이미지 전용 본문 | 텍스트 부족 경고 | 세부 조건을 놓칠 수 있음 |
| 자격 정보 없음 | `UNKNOWN`, 69점 상한 | 원문 확인 필요 |
| 마감 추출 실패 | 날짜를 만들지 않음 | 원문 일정 확인 필요 |

실패한 source가 있으면 `may_include_stale_notices: true`가 되고, **digest에 오래된
공지가 섞였을 수 있다는 경고를 반드시 함께 표시해야 한다.** 이것이 원칙 4를 구현하는
유일한 신호다.

## 9. 보안·자원 경계

- **공개·비로그인 페이지만** 수집. 로그인/SSO/CAPTCHA 우회 없음.
- URL scheme은 HTTP/HTTPS만. source의 `allowed_hosts` 밖으로 나가는 redirect 차단.
- DNS·IP가 private/loopback/link-local/reserved/multicast면 차단 (SSRF 방어).
- HTML 3 MB, binary 기본 최대 20 MB.
- HTTP connector 전체 4, host당 1, host별 최소 0.2초 간격.
- 429·5xx·네트워크/timeout만 최대 2회 재시도.
- robots 401/403은 전체 금지로 처리. 200이면 내용을 따름.
- ZIP 계열 문서는 엔트리 100개, 개별 20 MB, 총 해제 100 MB 상한.
- `--low-resource`: source당 상세 **4건**, 첨부 **0**, 전체 HTTP **30회** 강제.

실측(`--low-resource --no-llm`, 전북대 2개 source): **HTTP 12회, 3.95초, 최대 RSS 약
68 MiB.**

`--ignore-robots`가 구현되어 있으나 **운영에서 사용 금지.** 자동 신청·대리 제출 기능은
범위 밖이며 추가하지 않는다.

## 10. 지원 학교 (16개 source)

`jbnu_campus`(전북대 교내), `jbnu_software`(전북대 소프트웨어공학과), `snu_general`(서울대),
`pnu_general`(부산대), `korea_cs_undergrad`(고려대 컴퓨터학과), `jj_academic`(전주대),
`skku_general`(성균관대), `gachon_general`(가천대), `ssu_general`(숭실대),
`jnu_software`(전남대 소프트웨어공학과), `scnu_academic`(순천대), `mju_general`(명지대),
`konkuk_academic`(건국대), `kookmin_academic`(국민대), `hanyang_seoul`, `hanyang_erica`.

전남대 중앙 홈페이지는 `robots.txt`가 전체 수집을 금지하므로 **우회하지 않고** 학과
게시판을 쓴다. 한양대는 한 목록에서 캠퍼스 표식을 읽어 두 source로 분리한다.

새 학교 추가는 `sources.json`에 항목을 복제하고 selector·정규식을 지정한 뒤
`live-check --source <id>`로 계약을 확인한다. 인증·SPA·브라우저 렌더링이 필요한
게시판은 범위 밖이다.

## 11. 알려진 한계 (구현됨으로 오해하면 안 되는 것)

- `DailyNoticeJob` 1회 = **프로필 1명 + 학교 1개**. 사용자가 늘면 같은 학교를 사람 수만큼
  재크롤링한다.
- 오래된 `active` 공지의 자동 만료 정책이 없다. 마감이 없거나 추출 실패한 옛 글이 남을 수 있다.
- 목록 **첫 페이지만** 읽는다. 과거 backfill 없음.
- 제목 기반 중복은 semantic 판정이 아니다 (4장).
- OCR·이미지 비전 없음. 스캔 PDF는 `ocr_required`로 보존만 한다.
- `language_scores`, 인정학점, 이수과목은 저장은 되지만 **자격 판정에 쓰이지 않는다.**
- digest 파일명에 user_key가 없다. 같은 날짜·디렉터리면 **덮어쓴다.**
- 전달 성공·재전송·interaction 멱등성은 코어 범위 밖이다 (마사몽이 구현해야 함).

---

## 12. 데이터 계약

코어가 산출하는 구조. 통합 코드는 이것만 보고 작성한다.

### 12.1 digest JSON

```
schema_version: int          # 현재 1. 다르면 전달 중단
user_key: str
date: str                    # "YYYY-MM-DD" (Asia/Seoul)
summary: {action: int, opportunity: int, reference: int}
collection_health: object | null
items: [ item, ... ]
```

`collection_health`:

```
status: "healthy" | "degraded" | "failed"
healthy / degraded / failed: int
may_include_stale_notices: bool
sources: { <source_id>: {status, list_candidates, details_succeeded,
                         details_failed, errors: [str]} }
```

`item`:

```
notice_id: int
dedup_key: str
revision_count: int
change: "new" | "updated" | "unchanged"
duplicate_sources: [ {source_id, url} ]

notice:
  candidate: {source_id, external_id, title, url, published_text|null,
              author|null, category|null, pinned: bool,
              source_university|null, source_board|null, source_tags: [str]}
  title, body_text, body_characters, published_text, author
  attachments: [ {kind, url, name|null} ]
  inline_images, attachment_extractions
  base_content_hash, content_hash
  warnings: [str]

analysis:
  schema_version, summary, audiences[], topics[], actions[]
  required: bool
  urgency: "low" | "normal" | "high" | "critical"
  dates: [ {date: "YYYY-MM-DD", kind, evidence, inferred_year?: bool} ]
  eligibility_rules[], evidence[], confidence: float
  analysis_source: "rules" | <모델명>
  warnings: [str]

score:
  score: float (0~100)
  band: "action" | "opportunity" | "reference" | "hidden"
  eligibility: "ELIGIBLE" | "LIKELY_ELIGIBLE" | "INELIGIBLE" | "UNKNOWN"
  reasons: [str]
  topics: [str]
  deadline: "YYYY-MM-DD" | null
  next_event: "YYYY-MM-DD" | null
  profile_version: int
  mandatory_protected: bool
```

렌더링 규칙:

- `body_text`는 수백~수천 자다. Embed에는 **`analysis.summary`**를 쓴다.
- `score.reasons`가 "왜 추천됨"의 근거다. **반드시 노출한다.**
- `eligibility == "UNKNOWN"`이면 "원문 확인 필요"를 명시한다.
- `dates[].inferred_year == true`는 연도를 추론한 값이므로 마감 표시 시 주의 문구를 붙인다.
- 밴드·최소점수 필터는 코어가 이미 적용했으므로 **재적용하지 않는다.**

### 12.2 사용자 프로필

필수: `user_key`, `school_id`, `degree_level`.

```
degree_level ∈ {undergraduate, master, doctorate, integrated, non_degree}
grade: int 1~6            # undergraduate이면 필수
```

리스트 필드 (각 최대 100개, 항목당 100자): `career_interests`, `preferred_topics`,
`muted_topics`, `include_keywords`, `exclude_keywords`, `double_majors`, `minors`,
`completed_courses`, `unknown_fields`

숫자 범위: `student_number_year` 1900~2100, `completed_semesters` 0~30,
`gpa_last_semester` 0~4.5, `transfer_approved_credits` 0~300

`language_scores`: 최대 20개 객체. `timezone`: 기본 `Asia/Seoul`.

`notification_preferences`: `minimum_score`(기본 40), `include_bands`,
`max_action` / `max_opportunity` / `max_reference`, `strict_campus`.

**마사몽 매핑**: `user_key = f"discord-{user_id}"`로 고정하고 테이블에 원본 `user_id`를 함께 둔다.

### 12.3 피드백 타입

```
선호 학습:  useful, saved, applied, not_interested, already_knew
공지 상태:  completed, dismiss_once, not_eligible
강한 설정:  mute_topic
```

식별은 `notice_id` 또는 `(source_id, external_id)`.

### 12.4 실행 상태

```
status: "succeeded" | "partial" | "failed"
exit code: failed → 2, 그 외 → 0
```

**`partial`은 exit 0이다.** 종료 코드만으로 성공 판정하면 안 되고 `daily-run-*.json`의
status와 `collection_health`를 확인해야 한다.

### 12.5 CLI

```
python -m school_notice daily \
  --no-llm --low-resource \
  --profile <profile.json> --db <sqlite> --output-dir <dir> \
  [--date YYYY-MM-DD] [--max-details-per-source N] [--max-requests N]
```

산출: `daily-digest-<date>.md`, `daily-digest-<date>.json`, `daily-run-<date>.json`
기타 명령: `list-sources`, `init-db`, `live-check`, `feedback`, `llm-check`

### 12.6 테스트 fixture

`tests/fixtures/school_notice_digest.json`으로 커밋할 실제 구조 (축약).

```json
{
  "schema_version": 1,
  "user_key": "discord-100000000000000001",
  "date": "2026-07-27",
  "summary": { "action": 10, "opportunity": 3, "reference": 0 },
  "collection_health": {
    "status": "degraded",
    "healthy": 1, "degraded": 1, "failed": 0,
    "may_include_stale_notices": false,
    "sources": {
      "jbnu_software": { "status": "healthy", "list_candidates": 10,
        "details_succeeded": 4, "details_failed": 0, "errors": [] },
      "jbnu_campus": { "status": "degraded", "list_candidates": 10,
        "details_succeeded": 3, "details_failed": 1,
        "errors": ["detail_timeout"] }
    }
  },
  "items": [
    {
      "notice_id": 16,
      "dedup_key": "368b4d339aca1d77a82c13dd",
      "revision_count": 1,
      "change": "unchanged",
      "notice": {
        "candidate": {
          "source_id": "jbnu_software",
          "external_id": "394145",
          "title": "2026학년도 2학기 휴학 및 복학 신청 안내",
          "url": "https://software.jbnu.ac.kr/bbs/software/527/394145/artclView.do",
          "published_text": "2026.06.18",
          "author": "소프트웨어공학과",
          "category": null,
          "pinned": true,
          "source_university": "전북대학교",
          "source_board": "소프트웨어공학과 공지",
          "source_tags": ["소프트웨어공학과"]
        },
        "title": "[학부] 2026학년도 2학기 휴학 및 복학 신청 안내",
        "body_text": "2026학년도 2학기 휴학 및 복학 신청기간 등을 다음과 같이 알려드립니다. ...",
        "body_characters": 529,
        "published_text": "2026.06.18",
        "author": "소프트웨어공학과",
        "attachments": [
          { "kind": "attachment",
            "url": "https://software.jbnu.ac.kr/bbs/software/527/353100/download.do",
            "name": "자주하는 문의사항(휴학 및 복학)v5.hwp" }
        ],
        "inline_images": [],
        "attachment_extractions": [],
        "base_content_hash": "8f2b9a0a01b4a96e1eb79949ff3939e66cf1f68a76ea8a4ff72188b34ff17029",
        "content_hash": "33d45ae6c86fd6600ac1bc83954b24a7a5bedd973b9d0ca0f47767c178b011a8",
        "warnings": ["list_detail_title_difference"]
      },
      "analysis": {
        "schema_version": 1,
        "summary": "학기 휴학 및 복학 신청기간 등을 다음과 같이 알려드립니다.",
        "audiences": ["학부생"],
        "topics": ["등록금", "수강", "학적"],
        "actions": ["신청", "등록", "수강신청", "납부"],
        "required": true,
        "urgency": "high",
        "dates": [
          { "date": "2026-09-01", "kind": "deadline",
            "evidence": "휴학 · 복학 허가일 : 2026. 9. 1.( 화 ) [ 개강일 ] 부터" },
          { "date": "2026-09-29", "kind": "deadline",
            "evidence": "7.21.( 화 ) ~ 9.29.( 화 ) [ 수업일수 1/4 선 ]",
            "inferred_year": true }
        ],
        "eligibility_rules": [],
        "evidence": ["휴학 · 복학 신청 가능 기간"],
        "confidence": 0.58,
        "analysis_source": "rules",
        "warnings": ["image_only_section"]
      },
      "score": {
        "score": 100.0,
        "band": "action",
        "eligibility": "LIKELY_ELIGIBLE",
        "reasons": ["학부 대상 일치", "필수 표현 포함", "마감 30일 이내"],
        "topics": ["등록금", "수강", "학적"],
        "deadline": "2026-09-01",
        "next_event": "2026-09-29",
        "profile_version": 1,
        "mandatory_protected": true
      },
      "duplicate_sources": []
    }
  ]
}
```

추가 fixture: 빈 결과(items 0건), `may_include_stale_notices: true`,
`eligibility: "UNKNOWN"`, `schema_version: 99`(거부 확인용).

---

## 13. 마사몽 통합

### 13.1 대상 프로필은 `general`

신규 테이블이 필요한데 `masamo`는 `MASAMONG_AUTO_MIGRATE=true`가 config에서 금지되어
있다. general은 빈 DB라 실패해도 누적 운영 데이터에 영향이 없고, 롤백은 유닛 하나만
내리면 된다. `masamo` 승격은 별도 승인된 migration으로만 한다.

### 13.2 크롤링을 봇 프로세스 안에서 돌리지 않는다

코어는 실행당 RSS가 약 68 MiB 뛰고 bs4 파싱은 CPU 바운드다. `tasks.loop`로 봇
이벤트 루프에 넣으면 `MASAMONG_CPU_THREADS=1` 환경에서 봇 응답성이 무너지고,
크롤링 실패가 봇 안정성에 전이된다.

```
[systemd timer 08:00]  코어 batch (별도 프로세스)
        └─> digest JSON  +  sidecar SQLite

[마사몽 봇 (상주)]  tasks.loop 08:10
        └─> digest JSON 읽기 → Embed 변환 → DM 발송 → 전달 상태 기록
        └─> 버튼 interaction → 피드백 기록 → 다음 batch가 반영
```

접점은 **digest JSON 파일과 피드백 테이블뿐**이다.

### 13.3 저장소 2단 분리

| 데이터 | 위치 | 이유 |
|---|---|---|
| 공지 snapshot·revision·분석 캐시·API 예산 | sidecar SQLite (코어 소유) | 재생성 가능한 크롤링 부산물 |
| 사용자 프로필·피드백·전달 상태 | 마사몽 DB (신규 테이블) | Discord 사용자와 결합 |

코어 `storage`(710 LOC)를 async/TiDB로 포팅하지 않는다. 위험 대비 이득이 없다.

### 13.4 신규 테이블

마사몽 `compat_db`가 `?`를 TiDB용 `%s`로 변환하므로 쿼리는 **`?` 스타일로만** 쓴다.

SQLite (`database/schema.sql`):

```sql
CREATE TABLE IF NOT EXISTS school_notice_profiles (
    user_id INTEGER PRIMARY KEY,
    user_key TEXT NOT NULL UNIQUE,
    school_id TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    profile_version INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc')),
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS school_notice_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key TEXT NOT NULL,
    source_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    feedback_type TEXT NOT NULL,
    topic TEXT,
    interaction_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    consumed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_school_notice_feedback_user
    ON school_notice_feedback (user_key, created_at);

CREATE TABLE IF NOT EXISTS school_notice_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key TEXT NOT NULL,
    digest_date TEXT NOT NULL,
    notice_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    failure_reason TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 1,
    delivered_at TEXT NOT NULL,
    UNIQUE (user_key, digest_date, notice_id)
);

CREATE TABLE IF NOT EXISTS school_notice_batch_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key TEXT NOT NULL,
    run_date TEXT NOT NULL,
    status TEXT NOT NULL,
    collection_status TEXT,
    may_include_stale INTEGER NOT NULL DEFAULT 0,
    item_count INTEGER NOT NULL DEFAULT 0,
    http_requests INTEGER,
    llm_calls INTEGER,
    finished_at TEXT NOT NULL,
    UNIQUE (user_key, run_date)
);
```

TiDB (`database/schema_tidb.sql`) — 같은 구조에 `BIGINT PRIMARY KEY AUTO_RANDOM`,
`VARCHAR(n)`, `KEY` / `UNIQUE KEY` 문법을 적용한다. 시각 컬럼은 기존 관례대로
`VARCHAR(64)`.

멱등성 키: 전달 `(user_key, digest_date, notice_id)`, 피드백 `interaction_id`,
batch `(user_key, run_date)`.

### 13.5 config 키

전부 기본값을 두어 legacy/masamo에 영향이 없어야 한다.

```
SCHOOL_NOTICE_ENABLED                 기본 false   # 마스터 스위치
SCHOOL_NOTICE_DIGEST_DIR              기본 ""      # 절대 경로
SCHOOL_NOTICE_CORE_DB                 기본 ""      # sidecar SQLite 절대 경로
SCHOOL_NOTICE_DELIVERY_HOUR           기본 8
SCHOOL_NOTICE_DELIVERY_MINUTE         기본 10
SCHOOL_NOTICE_MAX_ITEMS_PER_DM        기본 10
SCHOOL_NOTICE_SCHEMA_VERSION          기본 1
SCHOOL_NOTICE_STALE_WARNING_ENABLED   기본 true
```

`SCHOOL_NOTICE_ENABLED=true`인데 `DIGEST_DIR`가 비었거나 상대 경로면 기동 실패로
처리한다 (기존 명시적 프로필 fail-closed 패턴과 일치). 명시적 프로필은 env 파일 밖의
값을 읽지 않으므로 `general.env.example`에 예시를 넣는다.

### 13.6 영향 지점

| 파일 | 변경 |
|---|---|
| `config.py` | 13.5 키 추가 |
| `database/schema.sql`, `schema_tidb.sql` | 13.4 DDL |
| `main.py` `_verify_runtime_schema`, `_migrate_db` | 신규 테이블 **조건부** 추가 |
| `main.py` `cog_list` | `school_notice_cog` 등록 |
| `cogs/school_notice_cog.py` | 신규 |
| `utils/school_notice_contract.py` | digest 파싱·검증 |
| `utils/school_notice_render.py` | digest → Embed |
| `scripts/run_school_notice_batch.py` | 프로필 export → 코어 호출 → 피드백 import |
| `profiles/general.env.example` | 신규 키 예시 |

### 13.7 반드시 지킬 제약

1. **신규 테이블을 무조건 required로 만들면 masamo 기동이 실패한다.** `AUTO_MIGRATE=false`라
   테이블이 없으면 startup에서 막힌다. `kakao_chunks`와 같은 방식으로
   `if config.SCHOOL_NOTICE_ENABLED:` 조건부로 넣고, **조건부 검증을 먼저 작성한 뒤
   테이블을 추가한다.**
2. `SCHOOL_NOTICE_ENABLED` 기본값은 `false`. masamo에서 기본으로 켜지지 않는다.
3. 새 Cog를 `MASAMONG_REQUIRED_COGS`에 넣지 않는다. 로드 실패가 봇 기동을 막으면 안 된다.
4. 봇 프로세스 안에서 크롤링하지 않는다 (13.2).
5. 다중 사용자 분리(11장) 전까지 사용자 수를 제한한다 (권장 5명 이하). 안 그러면 같은
   학교를 사람 수만큼 재크롤링해 차단당할 수 있다.

### 13.8 착수 순서

1. `utils/school_notice_contract.py` + fixture — 계약이 없으면 나머지가 추측이 된다.
2. `utils/school_notice_render.py` — Discord 없이 순수 함수로. 테스트가 쉬워진다.
3. Cog와 명령 — 수동 조회부터.
4. DDL + 조건부 검증 (13.7-①의 순서 준수) → 프로필 CRUD, 피드백 버튼.
5. batch 스크립트 — 코어가 아직 없으면 fixture를 복사하고 12.4 상태 계약만 재현하는
   fake로 인터페이스를 완성한다. 나중에 실행 경로만 교체된다.

### 13.9 완료 기준

- [ ] `schema_version` 불일치 digest를 거부한다
- [ ] fixture 4종(정상·빈·health 실패·UNKNOWN)이 모두 렌더링된다
- [ ] `may_include_stale_notices: true`면 경고가 표시된다
- [ ] `eligibility: "UNKNOWN"`이면 원문 확인 안내가 표시된다
- [ ] `score.reasons`가 "왜 추천됨"으로 노출된다
- [ ] 음소거 해제 경로가 있고 "영구 차단"으로 표현하지 않는다
- [ ] 프로필 CRUD가 12.2 enum·범위를 그대로 검증한다
- [ ] 같은 `interaction_id` 피드백이 1건만 저장된다
- [ ] 신규 테이블이 masamo 기동을 막지 않는다
- [ ] `SCHOOL_NOTICE_ENABLED=false`에서 기존 동작이 완전히 동일하다
- [ ] `partial` 상태가 성공으로 오판되지 않는다
- [ ] 같은 날 같은 공지가 두 번 전달되지 않는다

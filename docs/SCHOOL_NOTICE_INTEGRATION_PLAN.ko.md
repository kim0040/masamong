# 학교 공지 추적 기능 마사몽 통합 계획

## 0. 이 문서의 전제

외부 프로젝트 `학교 공지 추적`(이하 **코어**)을 마사몽에 통합한다. 작업은 클라우드
세션에서 진행하며 **코어 폴더는 업로드하지 않는다.**

그래서 이 문서는 "코어를 보고 알아내라"가 아니라 **코어의 계약을 동결해 옮겨 적은
문서**다. 4장의 계약과 5장의 fixture만으로 마사몽 쪽 통합 코드를 전부 작성하고
테스트할 수 있게 구성했다. 코어 소스가 필요한 작업은 11장에 따로 분리했다.

작업 순서 원칙:

- 클라우드 세션은 **마사몽 저장소 안에서만** 작업한다.
- 코어 패키지는 나중에 로컬에서 vendoring한다. 그 전까지는 4장 계약을 구현한
  **fake adapter**로 개발·테스트한다.
- 계약이 바뀌면 이 문서를 먼저 고치고 코드를 고친다.

---

## 1. 코어 현황 요약

폴더를 열지 않고도 판단할 수 있도록 실측 인벤토리를 남긴다.

| 항목 | 값 |
|---|---|
| 패키지명 | `school_notice` (`school-notice-research` 0.4.0) |
| 코드 규모 | 17개 모듈, 4,887 LOC |
| 테스트 | 12개 파일, 1,781 LOC |
| Python | >= 3.11 |
| 런타임 의존성 | `aiohttp`, `beautifulsoup4`, `soupsieve`, `olefile`, `pypdf` |
| 지원 학교 | 16개 source (전북대·서울대·부산대·고려대·전주대·성균관대·가천대·숭실대·전남대·순천대·명지대·건국대·국민대·한양대 서울/ERICA) |
| 저장소 | SQLite 단일 파일 (동기 `sqlite3`, WAL, busy_timeout 5초) |
| LLM | DeepSeek `deepseek-v4-flash` (선택), 규칙 fallback |
| 실행 형태 | 1회성 CLI batch (상주 루프 없음) |
| 실측 자원 | `--low-resource --no-llm` 기준 HTTP 12회 / 3.95초 / RSS 약 68 MiB |

### 1.1 마사몽과의 의존성 겹침

마사몽 venv에 **이미 설치된 것**: `aiohttp` 3.13.5, `beautifulsoup4` 4.14.3,
`soupsieve` 2.8.3.

**추가 필요**: `olefile`, `pypdf` — 단, 첨부 분석은 기본값 OFF이므로 Phase 1에서는
설치하지 않아도 된다. 첨부 기능을 켤 때만 추가한다.

### 1.2 코어가 이미 갖춘 것 / 없는 것

갖춘 것: 다대학 파싱, 증분 저장·revision, 규칙+LLM 구조화 분석, 프로필 기반
점수화, 피드백, digest 생성, robots·SSRF·크기·요청·LLM 예산 상한.

**없는 것 (통합 전 반드시 인지)**:

- Discord 객체 일절 없음 (의도된 설계)
- 다중 사용자 배치 분리 없음 — `DailyNoticeJob` 1회 = **프로필 1명 + 학교 1개**
- 오래된 `active` 공지 만료 정책 없음
- 사용자별 output 파일 분리 없음 (같은 날짜·디렉터리면 **덮어씀**)
- 전달 성공/재시도/interaction 멱등성 없음
- OCR 없음, 목록 첫 페이지만 읽음, 제목 중복은 정규화 일치(semantic 아님)

---

## 2. 통합 아키텍처 결정

### 2.1 결정 1 — 대상 프로필은 `general`부터

마사몽은 직전 작업에서 `masamo`(운영)와 `general`(신규)로 완전히 분리됐다.
이 기능은 **`general` 프로필에 먼저 올린다.**

| 근거 | 내용 |
|---|---|
| DDL 필요 | 신규 테이블이 필요한데 `masamo`는 `MASAMONG_AUTO_MIGRATE=true`가 **코드에서 금지**됨 (config.py). general은 bootstrap 시 허용 |
| 누적 데이터 위험 | general DB는 비어 있어 실패해도 운영 데이터 영향 0 |
| 자원 | 운영 서버는 저사양. 검증 전 masamo 프로세스에 부하를 더하지 않음 |
| 롤백 | general 유닛만 내리면 끝 |

`masamo` 승격은 Phase 4에서 **별도 승인된 migration**으로만 진행한다.

### 2.2 결정 2 — 크롤링을 봇 프로세스 안에서 돌리지 않는다

**이것이 가장 중요한 결정이다.**

코어는 batch로 설계됐고 실행 중 RSS가 약 68 MiB 뛴다. 이를 `tasks.loop`로 봇
이벤트 루프 안에서 돌리면:

- 저사양 서버에서 봇 상주 RSS에 크롤링 피크가 더해진다
- HTML 파싱(bs4)은 CPU 바운드 → `MASAMONG_CPU_THREADS=1` 환경에서 이벤트 루프를 막는다
- 크롤링 실패가 봇 프로세스 안정성에 영향을 준다

**채택 구조**: 코어는 **systemd timer / cron으로 별도 프로세스** 실행. 봇은
산출된 digest JSON을 읽어 전달만 한다.

```
[systemd timer 08:00]
   └─> python -m school_notice daily --low-resource ... --output-dir /var/lib/masamong/general/notice
          └─> daily-digest-<user>-<date>.json  +  sidecar SQLite

[마사몽 봇 프로세스 (상주)]
   └─> tasks.loop(08:10) → digest JSON 읽기 → Discord DM/Embed 전달 → 전달 상태 기록
   └─> 버튼 interaction → feedback 이벤트를 마사몽 DB에 기록
                                  └─> 다음 batch 실행 시 코어가 읽어감
```

봇과 batch의 유일한 접점은 **파일시스템(digest JSON)과 feedback 테이블**이다.

### 2.3 결정 3 — 저장소는 2단 분리

| 데이터 | 위치 | 이유 |
|---|---|---|
| 공지 snapshot·revision·분석 캐시·API 예산 | **sidecar SQLite** (코어 소유) | 크롤링 부산물. 운영 TiDB에 넣을 이유 없음. 재생성 가능 |
| 사용자 프로필·피드백·전달 상태 | **마사몽 DB** (신규 테이블) | Discord 사용자와 결합. 봇이 읽고 씀 |

코어 `storage.py`를 async/TiDB로 포팅하지 **않는다**. 710 LOC를 포팅하는 위험 대비
이득이 없다.

접점은 batch 실행 전후로 **프로필 export / 피드백 import**하는 얇은 동기화다.

### 2.4 결정 4 — LLM은 Phase 1에서 끈다

코어는 DeepSeek을 쓰고 자체 예산·캐시·회로차단기를 갖췄다. 마사몽은 NanoGPT/CometAPI
레인 구조다. 섞으면 예산 추적이 이원화된다.

Phase 1은 `--no-llm`(규칙 분석)으로 간다. 실측에서 규칙만으로도 동작이 검증됐고,
비용 0에 장애 경로가 단순하다. LLM은 Phase 3에서 별도 판단.

---

## 3. 단계별 계획

각 단계에 **클라우드 가능 여부**를 표시한다.

### Phase 0 — 계약 고정과 fixture (클라우드 ✅)

코어 없이 진행. 이 문서 4·5장을 코드로 옮긴다.

산출물:
- `utils/school_notice_contract.py` — digest JSON 파싱·검증 (dataclass 또는 TypedDict)
- `tests/fixtures/school_notice_digest.json` — 5장 fixture 커밋
- `tests/school_notice_contract_spec.py` — 스키마 검증·잘못된 입력 거부 테스트

완료 조건: fixture를 읽어 타입 안전한 객체로 변환하고, 필수 필드 누락·enum 위반·
스키마 버전 불일치를 거부한다.

### Phase 1 — 읽기 전용 전달 (클라우드 ✅)

digest JSON → Discord Embed. 크롤링·DB 쓰기 없음.

산출물:
- `cogs/school_notice_cog.py` — 신규 Cog
- `utils/school_notice_render.py` — digest → Embed 변환
- `!공지` 계열 명령 (수동 조회)
- config 키 (9장)

완료 조건: fixture만으로 Embed가 생성되고, band별 분리·중복 source 표기·
`collection_health` 경고가 렌더링된다. Discord 연결 없이 단위 테스트 가능해야 한다.

### Phase 2 — 프로필·피드백 저장 (클라우드 ✅, DDL 포함)

산출물:
- 8장 DDL을 `database/schema.sql` + `database/schema_tidb.sql`에 추가
- `main.py`의 `_verify_runtime_schema` / `_migrate_db` 테이블 목록에 추가
  (**주의**: 기존 `kakao_chunks`처럼 프로필 조건부로 할지 판단. 이 기능은
  general 전용이므로 `SCHOOL_NOTICE_ENABLED` 조건부 권장)
- 프로필 등록·수정 명령 (4.2 스키마 enum 검증 그대로)
- 피드백 버튼 View + interaction 멱등성 (`interaction.id` 중복 차단)

완료 조건: 프로필 CRUD와 피드백 기록이 sqlite/TiDB 양쪽에서 동작. 같은 버튼을
두 번 눌러도 이벤트가 1건만 남는다.

### Phase 3 — batch 연동 (클라우드 ⚠️ 부분 / 로컬 필요)

코어 패키지가 있어야 실제 실행이 된다. 클라우드에서는 **fake batch**로 인터페이스만
완성한다.

산출물:
- `scripts/run_school_notice_batch.py` — 프로필 export → 코어 CLI 호출 → 결과 검증
- 피드백 import 경로
- systemd timer / cron 예시 (docs)
- 전달 상태 기록 및 재시도

완료 조건: fake batch로 전체 흐름이 돌고, 코어 vendoring 후 실 실행으로 교체만 하면 됨.

### Phase 4 — 다중 사용자 분리와 masamo 승격 (클라우드 ⚠️ 설계만)

코어의 최대 구조적 한계를 해소하는 단계. **Phase 3까지 검증 전에는 착수 금지.**

- 학교별 collect/analyze 1회 → 사용자별 score N회 분리
- 오래된 active 공지 만료 정책
- 사용자별 output 파일 분리
- masamo 승격용 승인된 migration

---

## 4. 동결된 계약 (코어 폴더 없이 작업하기 위한 핵심)

여기 적힌 것이 **코어의 실제 계약**이다. 클라우드 세션은 이것만 보고 구현한다.

### 4.1 digest JSON 최상위

```
schema_version: int          # 현재 1. 다르면 거부할 것
user_key: str
date: str                    # "YYYY-MM-DD" (Asia/Seoul)
summary: {action: int, opportunity: int, reference: int}
collection_health: object | null      # 4.5 참조
items: [ item, ... ]
```

### 4.2 item 구조

```
notice_id: int
dedup_key: str
revision_count: int
change: "new" | "updated" | "unchanged"
duplicate_sources: [ {source_id: str, url: str}, ... ]

notice:
  candidate: {source_id, external_id, title, url,
              published_text|null, author|null, category|null, pinned: bool,
              source_university|null, source_board|null, source_tags: [str]}
  title: str
  body_text: str            # 길다. Embed에 그대로 넣지 말 것
  body_characters: int
  published_text: str|null
  author: str|null
  attachments: [ {kind, url, name|null} ]
  inline_images: [ ... ]
  attachment_extractions: [ ... ]
  content_hash: str
  base_content_hash: str
  warnings: [str]

analysis:
  schema_version: int
  summary: str
  audiences: [str]
  topics: [str]
  actions: [str]
  required: bool
  urgency: "low" | "normal" | "high" | "critical"
  dates: [ {date: "YYYY-MM-DD", kind: str, evidence: str, inferred_year?: bool} ]
  eligibility_rules: [ ... ]
  evidence: [str]
  confidence: float
  analysis_source: "rules" | (LLM 사용 시 모델 표기)
  warnings: [str]

score:
  score: float                 # 0~100
  band: "action" | "opportunity" | "reference" | "hidden"
  eligibility: "ELIGIBLE" | "LIKELY_ELIGIBLE" | "INELIGIBLE" | "UNKNOWN"
  reasons: [str]               # 사용자에게 "왜 추천됨" 표시용
  topics: [str]
  deadline: "YYYY-MM-DD" | null
  next_event: "YYYY-MM-DD" | null
  profile_version: int
  mandatory_protected: bool
```

렌더링 주의:
- `body_text`는 수백~수천 자다. Embed에는 `analysis.summary`를 쓴다.
- `score.reasons`가 "왜 추천됨" 화면의 근거다. 반드시 노출한다.
- `eligibility == "UNKNOWN"`이면 "원문 확인 필요"를 명시한다 (코어가 69점 상한을 건다).
- `analysis.dates[].inferred_year == true`는 연도를 추론한 값이다. 마감 표시 시 주의 문구 필요.

### 4.3 사용자 프로필 스키마

필수: `user_key`, `school_id`, `degree_level`.

```
degree_level ∈ {undergraduate, master, doctorate, integrated, non_degree}
grade: int 1~6   # undergraduate이면 필수
```

리스트 필드(각 최대 100개, 항목당 최대 100자):
`career_interests`, `preferred_topics`, `muted_topics`, `include_keywords`,
`exclude_keywords`, `double_majors`, `minors`, `completed_courses`, `unknown_fields`

숫자 범위:
```
student_number_year   1900 ~ 2100
completed_semesters   0 ~ 30
gpa_last_semester     0 ~ 4.5
transfer_approved_credits  0 ~ 300
```

`language_scores`: 최대 20개 항목 객체. `timezone`: 기본 `Asia/Seoul`.

`notification_preferences`:
```
minimum_score: float (기본 40)
include_bands: ["action","opportunity","reference"] 부분집합
max_action / max_opportunity / max_reference: int
strict_campus: bool
```

**마사몽 매핑**: `user_key`는 Discord 사용자와 1:1이어야 한다.
`user_key = f"discord-{user_id}"` 규칙을 쓰고 8장 테이블에 원본 `user_id`를 함께 둔다.

### 4.4 피드백 타입

```
선호 학습(완만):  useful, saved, applied, not_interested, already_knew
공지 상태:        completed, dismiss_once, not_eligible
강한 설정:        mute_topic
```

의미상 주의: `not_interested` 1회는 영구 차단이 아니다(90일 반감기). UI에서
"영구 차단"처럼 표현하면 안 된다. 음소거 해제 경로를 반드시 제공한다.

식별은 `notice_id` 대신 `(source_id, external_id)`도 가능하다.

### 4.5 collection_health

```
status: "healthy" | "degraded" | "failed"
healthy / degraded / failed: int
may_include_stale_notices: bool
sources: { <source_id>: {status, list_candidates, details_succeeded,
                         details_failed, errors: [str]} }
```

`may_include_stale_notices == true`면 **digest에 오래된 공지가 섞였을 수 있다**는
경고를 사용자에게 반드시 표시한다. 이것이 "오늘 새 공지 없음"과 "오늘 확인 실패"를
구분하는 유일한 신호다.

### 4.6 배치 실행 상태 / 종료 코드

```
status: "succeeded" | "partial" | "failed"
exit code: failed → 2, 그 외 → 0
```

`partial`은 exit 0이므로 **종료 코드만으로 성공 판정하면 안 된다.**
반드시 `daily-run-YYYY-MM-DD.json`의 status와 `collection_health`를 확인한다.

### 4.7 코어 CLI (Phase 3용)

```
python -m school_notice daily \
  --no-llm --low-resource \
  --profile <profile.json> \
  --db <sqlite path> \
  --output-dir <dir> \
  [--date YYYY-MM-DD] [--max-details-per-source N] [--max-requests N]
```

산출: `daily-digest-<date>.md`, `daily-digest-<date>.json`, `daily-run-<date>.json`

기타 명령: `list-sources`, `init-db`, `live-check`, `feedback`, `llm-check`

`--low-resource` 강제 상한: source당 상세 4건, 첨부 0, 전체 HTTP 30회.

**파일명에 user_key가 없다.** 다중 사용자면 `--output-dir`를 사용자별로 분리해야
덮어쓰지 않는다.

---

## 5. 테스트 fixture

`tests/fixtures/school_notice_digest.json`으로 커밋할 실제 구조 샘플(축약).

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

추가로 만들어야 할 fixture:
- `..._empty.json` — items 0건 (오늘 새 공지 없음)
- `..._failed_health.json` — `may_include_stale_notices: true`
- `..._unknown_eligibility.json` — `eligibility: "UNKNOWN"`
- `..._bad_schema.json` — `schema_version: 99` (거부 확인용)

---

## 6. 마사몽 코드베이스 영향 지점

| 파일 | 변경 | Phase |
|---|---|---|
| `config.py` | 9장 config 키 추가. **명시적 프로필 자격증명 검증 블록에 영향 없게** 주의 | 1 |
| `database/schema.sql` | 8장 DDL (SQLite) | 2 |
| `database/schema_tidb.sql` | 8장 DDL (TiDB) | 2 |
| `main.py` `_verify_runtime_schema` | 신규 테이블 검증 추가 (조건부) | 2 |
| `main.py` `_migrate_db` | `core_tables`에 추가 (조건부) | 2 |
| `main.py` `cog_list` | `school_notice_cog` 등록 | 1 |
| `cogs/school_notice_cog.py` | 신규 | 1 |
| `utils/school_notice_contract.py` | 신규 (digest 파싱) | 0 |
| `utils/school_notice_render.py` | 신규 (Embed 변환) | 1 |
| `scripts/run_school_notice_batch.py` | 신규 | 3 |
| `requirements.txt` | 첨부 기능 켤 때만 `olefile`, `pypdf` | 3 |
| `profiles/general.env.example` | 신규 키 예시 | 1 |
| `scripts/validate_profile_separation.py` | 신규 키 경계 검사 | 2 |

### 6.1 반드시 지킬 기존 제약

직전 프로필 격리 작업에서 세운 규칙을 깨지 않아야 한다.

1. **`masamo`에서 이 기능이 기본으로 켜지면 안 된다.** `SCHOOL_NOTICE_ENABLED`
   기본값을 `false`로 두고 general env에서만 켠다.
2. **신규 테이블을 무조건 required로 만들면 안 된다.** masamo는
   `AUTO_MIGRATE=false`라 테이블이 없으면 **기동이 실패한다.**
   `kakao_chunks`와 같은 방식으로 `if config.SCHOOL_NOTICE_ENABLED:` 조건부로 넣는다.
3. **명시적 프로필은 env 파일 밖의 값을 읽지 않는다.** 신규 config 키는 반드시
   env 파일에 적어야 하며, `load_config_value`로 읽는다.
4. **저사양 예산을 넘기지 않는다.** 봇 프로세스 안에서 크롤링하지 않는다(2.2).
5. 새 Cog는 `MASAMONG_REQUIRED_COGS`에 넣지 않는다. 로드 실패가 봇 기동을 막으면 안 된다.

---

## 7. 프로세스·데이터 흐름 상세

```
① [batch, systemd timer 08:00]
   마사몽 DB에서 활성 프로필 export → <user_key>.json
   for each 프로필:
       python -m school_notice daily --no-llm --low-resource
              --profile <user>.json --db /var/lib/masamong/general/notice/core.db
              --output-dir /var/lib/masamong/general/notice/out/<user_key>/
   결과 status/collection_health 검증 → batch 결과 요약 기록

② [봇, tasks.loop 08:10]
   out/<user_key>/daily-digest-<today>.json 읽기
   → schema_version 검증 (4.1)
   → band/minimum_score 필터는 코어가 이미 적용했으므로 재적용하지 않음
   → Embed 변환 (utils/school_notice_render.py)
   → DM 발송 (기존 dm_usage_logs 제한 경로 준수)
   → school_notice_deliveries에 idempotency key로 기록

③ [봇, interaction]
   버튼 클릭 → school_notice_feedback에 기록 (interaction 중복 차단)

④ [다음 batch]
   ①에서 프로필 export 시 피드백을 함께 반영
```

### 7.1 idempotency

- 전달 키: `(user_key, digest_date, notice_id)` — 같은 날 같은 공지 재전송 금지
- 피드백 키: `interaction_id` UNIQUE — 버튼 연타 방지
- batch 키: `(user_key, run_date)` — 같은 날 중복 실행 방지

### 7.2 동시 실행 방지

코어 SQLite는 WAL + busy_timeout 5초지만 중복 배치를 띄울 이유가 없다.
systemd timer에 `Persistent=true`, 서비스에 flock 또는 `RemainAfterExit` 조합으로
single-flight를 보장한다.

---

## 8. 신규 테이블 DDL 초안

마사몽 기존 컨벤션(TiDB `AUTO_RANDOM` / SQLite `AUTOINCREMENT`, 시각은 `VARCHAR(64)`/`TEXT`)을 따른다.

### 8.1 SQLite (`database/schema.sql`)

```sql
CREATE TABLE IF NOT EXISTS school_notice_profiles (
    user_id INTEGER PRIMARY KEY,          -- Discord user id
    user_key TEXT NOT NULL UNIQUE,        -- "discord-<user_id>"
    school_id TEXT NOT NULL,
    profile_json TEXT NOT NULL,           -- 4.3 스키마 전체
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
    feedback_type TEXT NOT NULL,          -- 4.4 enum
    topic TEXT,                           -- mute_topic 전용
    interaction_id TEXT NOT NULL UNIQUE,  -- 중복 interaction 차단
    created_at TEXT NOT NULL,
    consumed_at TEXT                      -- batch가 반영한 시각
);
CREATE INDEX IF NOT EXISTS idx_school_notice_feedback_user
    ON school_notice_feedback (user_key, created_at);

CREATE TABLE IF NOT EXISTS school_notice_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key TEXT NOT NULL,
    digest_date TEXT NOT NULL,
    notice_id INTEGER NOT NULL,
    status TEXT NOT NULL,                 -- sent | failed | skipped
    failure_reason TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 1,
    delivered_at TEXT NOT NULL,
    UNIQUE (user_key, digest_date, notice_id)
);

CREATE TABLE IF NOT EXISTS school_notice_batch_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_key TEXT NOT NULL,
    run_date TEXT NOT NULL,
    status TEXT NOT NULL,                 -- succeeded | partial | failed
    collection_status TEXT,               -- healthy | degraded | failed
    may_include_stale INTEGER NOT NULL DEFAULT 0,
    item_count INTEGER NOT NULL DEFAULT 0,
    http_requests INTEGER,
    llm_calls INTEGER,
    finished_at TEXT NOT NULL,
    UNIQUE (user_key, run_date)
);
```

### 8.2 TiDB (`database/schema_tidb.sql`)

```sql
CREATE TABLE IF NOT EXISTS school_notice_profiles (
    user_id BIGINT PRIMARY KEY,
    user_key VARCHAR(128) NOT NULL UNIQUE,
    school_id VARCHAR(64) NOT NULL,
    profile_json TEXT NOT NULL,
    profile_version INT NOT NULL DEFAULT 1,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at VARCHAR(64),
    updated_at VARCHAR(64)
);

CREATE TABLE IF NOT EXISTS school_notice_feedback (
    id BIGINT PRIMARY KEY AUTO_RANDOM,
    user_key VARCHAR(128) NOT NULL,
    source_id VARCHAR(64) NOT NULL,
    external_id VARCHAR(128) NOT NULL,
    feedback_type VARCHAR(32) NOT NULL,
    topic VARCHAR(128),
    interaction_id VARCHAR(64) NOT NULL UNIQUE,
    created_at VARCHAR(64) NOT NULL,
    consumed_at VARCHAR(64),
    KEY idx_school_notice_feedback_user (user_key, created_at)
);

CREATE TABLE IF NOT EXISTS school_notice_deliveries (
    id BIGINT PRIMARY KEY AUTO_RANDOM,
    user_key VARCHAR(128) NOT NULL,
    digest_date VARCHAR(32) NOT NULL,
    notice_id BIGINT NOT NULL,
    status VARCHAR(16) NOT NULL,
    failure_reason TEXT,
    attempt_count INT NOT NULL DEFAULT 1,
    delivered_at VARCHAR(64) NOT NULL,
    UNIQUE KEY uk_school_notice_delivery (user_key, digest_date, notice_id)
);

CREATE TABLE IF NOT EXISTS school_notice_batch_runs (
    id BIGINT PRIMARY KEY AUTO_RANDOM,
    user_key VARCHAR(128) NOT NULL,
    run_date VARCHAR(32) NOT NULL,
    status VARCHAR(16) NOT NULL,
    collection_status VARCHAR(16),
    may_include_stale BOOLEAN NOT NULL DEFAULT FALSE,
    item_count INT NOT NULL DEFAULT 0,
    http_requests INT,
    llm_calls INT,
    finished_at VARCHAR(64) NOT NULL,
    UNIQUE KEY uk_school_notice_run (user_key, run_date)
);
```

**SQL 작성 규칙**: 마사몽 `compat_db`는 `?`를 TiDB용 `%s`로 자동 변환한다
(`compat_db.py:270`). 쿼리는 **`?` 스타일로만** 작성한다.

---

## 9. config 키

`config.py`에 `load_config_value`로 추가한다. 전부 기본값을 두어 legacy/masamo에
영향이 없어야 한다.

```
SCHOOL_NOTICE_ENABLED                 기본 false   # 마스터 스위치
SCHOOL_NOTICE_DIGEST_DIR              기본 ""      # 절대 경로. 명시적 프로필은 필수
SCHOOL_NOTICE_CORE_DB                 기본 ""      # sidecar SQLite 절대 경로
SCHOOL_NOTICE_DELIVERY_HOUR           기본 8
SCHOOL_NOTICE_DELIVERY_MINUTE         기본 10
SCHOOL_NOTICE_MAX_ITEMS_PER_DM        기본 10      # Discord 메시지 상한 보호
SCHOOL_NOTICE_SCHEMA_VERSION          기본 1       # 4.1과 불일치면 전달 중단
SCHOOL_NOTICE_STALE_WARNING_ENABLED   기본 true
```

주의:
- `SCHOOL_NOTICE_ENABLED=true`인데 `DIGEST_DIR`가 비었거나 상대 경로면 **기동 실패**로
  처리한다 (기존 명시적 프로필 fail-closed 패턴과 일치).
- 명시적 프로필에서는 이 키들이 env 파일에 없으면 무시되고 기본값이 된다.
  `general.env.example`에 반드시 예시를 넣는다.

---

## 10. 리스크와 완화

| 리스크 | 영향 | 완화 |
|---|---|---|
| 신규 테이블 required → masamo 기동 실패 | **운영 중단** | 6.1-②. `SCHOOL_NOTICE_ENABLED` 조건부. masamo는 false |
| 봇 프로세스 내 크롤링 | 저사양 서버 CPU/RSS 초과 | 2.2. 별도 프로세스 강제 |
| `partial` 상태를 성공으로 오판 | 조용한 데이터 누락 | 4.6. exit code 아닌 status/health 확인 |
| 오래된 공지 재전송 | 사용자 신뢰 하락 | 7.1 idempotency + Phase 4 만료 정책 |
| 다중 사용자 시 output 덮어씀 | digest 유실 | 사용자별 `--output-dir` 분리 (4.7) |
| 다중 사용자 시 재크롤링 | 학교 사이트 부하·차단 | Phase 4까지 사용자 수 제한(권장 5명 이하) |
| 버튼 연타 중복 피드백 | 점수 왜곡 | `interaction_id` UNIQUE |
| 학교 HTML 구조 변경 | 조용한 파싱 실패 | `live-check` 주기 실행 + `collection_health` 알림 |
| 코어 스키마 버전 변경 | 렌더링 오류 | `schema_version` 불일치 시 전달 중단·경고 |
| DeepSeek 키 유출 | 비용·보안 | Phase 1은 `--no-llm`. 켤 때 env로만 주입 |

### 10.1 법적·윤리적 경계 (반드시 유지)

- 공개·비로그인 페이지만 수집한다. 로그인/SSO/CAPTCHA 우회 금지.
- `--ignore-robots`는 **운영에서 사용 금지**.
- 자동 신청·대리 제출 기능을 붙이지 않는다.
- 사용자에게 "원문 확인 필요"를 항상 함께 표시한다 (분석은 보조 수단).

---

## 11. 클라우드에서 할 수 없는 것

폴더가 없으므로 아래는 **로컬에서만** 가능하다. 클라우드 세션은 손대지 않는다.

| 항목 | 이유 | 대체 |
|---|---|---|
| `school_notice` 패키지 vendoring | 소스 4,887 LOC가 없음 | Phase 3에서 로컬 수행 |
| 코어 단위 테스트 실행 | 테스트 1,781 LOC가 없음 | 마사몽 쪽 테스트만 작성 |
| `live-check` 실사이트 검증 | 코어 CLI 필요 | 로컬에서 사전 수행 |
| 실제 digest 생성 | 코어 필요 | 5장 fixture 사용 |
| DeepSeek 계약 검증 | 코어 + 키 필요 | Phase 3 이후 |

### 11.1 클라우드 세션용 fake adapter

Phase 3 인터페이스를 코어 없이 완성하기 위해 아래 형태의 fake를 만든다.

```python
# tests/fakes/fake_school_notice_batch.py
# 실제 코어 CLI 대신 fixture를 output-dir에 복사하고
# 4.6의 status/exit code 계약만 재현한다.
```

이렇게 하면 코어 vendoring 후 **호출부 교체 없이** 실행 경로만 바뀐다.

---

## 12. 완료 기준

### Phase 0~2 (클라우드에서 검증 가능)

- [ ] `schema_version` 불일치 digest를 거부한다
- [ ] fixture 4종(정상·빈·health 실패·UNKNOWN)이 모두 렌더링된다
- [ ] `may_include_stale_notices: true`면 경고가 표시된다
- [ ] `eligibility: "UNKNOWN"`이면 원문 확인 안내가 표시된다
- [ ] `score.reasons`가 "왜 추천됨"으로 노출된다
- [ ] 프로필 CRUD가 4.3 enum·범위를 그대로 검증한다
- [ ] 같은 `interaction_id` 피드백이 1건만 저장된다
- [ ] 신규 테이블이 **masamo 프로필 기동을 막지 않는다** (조건부 검증)
- [ ] `SCHOOL_NOTICE_ENABLED=false`에서 기존 동작이 완전히 동일하다
- [ ] legacy 경로에 영향이 없다

### Phase 3~4 (로컬 검증 필요)

- [ ] batch가 별도 프로세스로 실행되고 봇 RSS에 영향이 없다
- [ ] `partial` 상태가 성공으로 오판되지 않는다
- [ ] 같은 날 같은 공지가 두 번 전달되지 않는다
- [ ] 사용자별 output이 덮어써지지 않는다
- [ ] 피드백이 다음 실행 점수에 반영된다
- [ ] general에서 충분히 검증된 뒤에만 masamo 승격을 논의한다

---

## 13. 권장 착수 순서

1. Phase 0을 먼저 끝낸다. 계약과 fixture가 없으면 나머지가 전부 추측이 된다.
2. Phase 1은 Discord 연결 없이 순수 함수로 만든다. 테스트가 쉬워진다.
3. Phase 2의 DDL은 **조건부 검증**을 먼저 작성하고 테이블을 추가한다.
   순서를 바꾸면 masamo 기동이 깨질 수 있다.
4. Phase 3은 fake로 인터페이스를 완성한 뒤 로컬에서 코어를 붙인다.
5. Phase 4는 실사용 데이터가 쌓인 뒤에 판단한다.

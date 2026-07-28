# 마사몽 제품·사용 설명서

마사몽은 Discord에서 AI 대화, 선택적 기억/RAG, 날씨·금융·웹 검색, 이미지 생성,
개인 운세, 커뮤니티 기능과 선택적 학교·편입 공지 개인화를 제공하는 한국어 중심 봇이다.
운영판은 하나의 공통 코드를 사용하되 `masamo`와 `general` 두 프로필의 데이터와
실행 경계를 완전히 분리한다.

- Python 3.10+
- `discord.py` 2.7.1+
- 운영 DB: TiDB, 개발·격리 테스트: SQLite
- LLM: OpenAI 호환 공급자와 Gemini, 레인별 primary/fallback
- 로컬 기억 기능: CPU용 SentenceTransformer/RAG 스택(선택)

운영 절차는 [배포 가이드](../DEPLOYMENT.md), 두 판의 경계는
[인스턴스 분리 가이드](INSTANCE_SEPARATION.ko.md), 학교 공지의 세부 계약은
[학교 공지 통합 설명서](SCHOOL_NOTICE_INTEGRATION_PLAN.ko.md), 편입 공지는
[편입 공지 구독 설명서](TRANSFER_NOTICE.ko.md)를 따른다.

## 제품 구성

### AI 대화와 도구

- 허용된 서버 채널에서는 봇 멘션으로, DM에서는 멘션 없이 대화한다.
- 명백한 인사·도구 요청은 로컬 규칙으로 먼저 분기해 불필요한 의도 분석 LLM 호출을
  줄인다. 애매한 요청만 설정에 따라 routing LLM을 쓴다.
- 응답 LLM과 routing LLM은 서로 다른 레인으로 설정할 수 있다.
- 기상청 실황, 향후 6시간 초단기예보, 단기·중기예보, 현재 발효 특보,
  지진·태풍·영향예보와 금융, 환율, 웹/뉴스 검색, 이미지 생성 도구를 연결한다.
- 한 AI 턴이 계획할 수 있는 도구 호출은 최대 3개다.
- 대화 입력·RAG 블록·프롬프트·출력에는 길이와 토큰 상한이 있다.
- DB 서버 말투 캐시는 `guild_id`, 현재 Masamo의 static 말투는 Discord 전역에서
  고유한 `channel_id`로 구분한다. 일반 대화, 창의형 명령 응답, 일상 알림은 목적지
  서버·채널의 페르소나만 사용하며, 대화·RAG 조회는 같은 `guild_id`와 `channel_id`
  조건을 함께 만족해야 한다. A 서버 말투나 기억을 B 서버 응답에 사용하는 경로는
  허용하지 않는다.
- 지진 같은 공통 재난 알림은 이 페르소나 경로와 LLM을 모두 우회한다. 모든 서버에
  동일한 기상청 기반의 형식적·엄중 문구만 전송한다.

### 기억/RAG

- Discord 대화와, Masamo에 한해 기존 Kakao 대화 저장소를 검색할 수 있다.
- 임베딩, BM25, 구조화 메모리, 재순위화는 각각 기능 플래그로 끌 수 있다.
- General은 Masamo의 Kakao mapping이나 누적 데이터를 복사·조회하지 않는다.
- 로컬 벡터는 메모리 매핑과 제한된 배치 검색을 사용하고, 백그라운드 작업과 추적
  윈도 수를 제한한다.
- NumPy·SentenceTransformer·Transformers·Torch의 무거운 최초 import와 모델 생성은
  worker thread에서 실행해 Discord heartbeat를 막지 않는다. 로드 실패 뒤에는 유한
  cooldown을 적용해 메시지마다 같은 모델 로드를 반복하지 않는다.

### 일반 기능

- `!날씨 [지역] [날짜]`: 기상청 기반 날씨 조회
- `!이미지 <프롬프트>`: 사용자/전역 사용량 제한을 확인한 이미지 생성
- `!랭킹`: 서버 활동 순위와 차트
- `!요약`: 제한된 대화 문맥 요약
- `!투표 "주제" "항목1" "항목2"`: 투표
- `/config`: 서버 AI 활성화·허용 채널·언어 설정
- `/persona`: 서버 페르소나 조회·설정
- `!메뉴`, `!도움`: 모든 사용자 기능을 한 화면에서 찾고 버튼으로 진입

기능별 API 키 또는 feature flag가 없으면 해당 기능은 비활성화되거나 명시적인 오류를
반환한다. 부분적으로 로드된 운영 상태를 숨기지 않도록 명시적 프로필은 필수 Cog 로드
실패 시 기동을 중단한다.

## General과 Masamo

코드를 두 벌로 포크하지 않는다. 같은 검증된 release SHA를 서로 다른 두 프로세스로
실행한다.

| 항목 | Masamo | General |
|---|---|---|
| 역할 | 현재 운영 중인 기존 커뮤니티 봇 | 새 일반판 |
| Discord 앱/토큰 | 현재 값 그대로 | 별도 신규 앱/토큰 |
| TiDB | 기존 `masamong` 그대로 | 신규 `masamong_general` |
| DB 계정 | 기존 DB만 접근 | General DB만 접근 |
| 대화·운세·사용량 | 누적 데이터 보존 | 빈 상태에서 시작 |
| 기억 소스 | `discord,kakao` | `discord`만 |
| env/config/prompt/embedding | Masamo 전용 절대 경로 | General 전용 절대 경로 |
| 로그와 service | 별도 | 별도 |
| 학교 공지 | 현재 운영 기능·DB·digest·23시 timer 소유 | 기본 비활성, 나중에 켤 때도 General 전용 경로만 사용 |
| 편입 공지 | 20개 공식 공지원·snapshot·23:35 timer 소유 | 기본 비활성, Masamo 구독·파일·timer 공유 금지 |

기존 `masamong` DB의 이름을 바꾸거나 새 General에 복사하지 않는다. 두 프로세스가 같은
DB를 공유하면 운세 프로필, DM/LLM 사용량, 사용자 선호, 메시지·기억 키가 섞이므로 완전
분리가 아니다. 자세한 배치 순서는 [인스턴스 분리 가이드](INSTANCE_SEPARATION.ko.md)에
있다.

## 개인정보 동의

### 동의가 필요한 정보

일반 Discord 대화와 Discord 서버가 제공하는 정보는 아래 목적별 동의의 대상이 아니다.
봇이 별도로 질문해 저장하고 이후 재사용하는 세 종류의 프로필만 명시 동의를 받는다.

| 목적 | 수집·이용 정보 | 주된 이용 |
|---|---|---|
| 운세 | Discord 사용자 ID, 필수 생년월일, 사용자가 선택한 출생 시각·성별·출생지 | 운세 생성, 구독 DM, 동의한 운세 문맥 |
| 학교공지 | 필수 Discord 사용자 ID·학교·학위 과정·학부 학년, 사용자가 직접 말한 경우만 캠퍼스·학과·입학/학적·관심·알림 설정과 피드백 | 관련 공지 판정, digest, DM |
| 편입공지 | Discord 사용자 ID, 선택한 대학 ID, 구독 활성·전달 상태 | 선택한 대학의 새 공식 편입 공지 DM |

운세 문장 생성에는 실제 등록한 항목과 Discord 표시 이름이 외부 LLM에 전달될 수 있다.
학교 자연어 등록은 먼저 로컬에서 해석한다. 해결되지 않은 등록·수정 문장만 Discord ID를
제외하고 외부 LLM 한 곳에 전달될 수 있다. 기본 공지 수집·분석은 LLM 없이 실행하며,
운영자가 분석 LLM을 명시적으로 켜도 공개 공지 내용만 전달하고 사용자 프로필은 전달하지
않는다. 제공하지 않은 선택 항목은 추측하거나 임의 기본값으로 바꾸지 않는다.

### 동의 방법

DM에서 실행한다.

```text
!개인정보
!개인정보 동의 운세
!개인정보 동의 학교공지
!개인정보 동의 편입공지
```

정책 본문과 버전을 표시한 뒤 명령을 실행한 본인이 `동의합니다` 버튼을 눌러야 저장된다.
기능 사용 도중 동의 화면이 나온 경우 버튼 동의가 끝나면 사용자가 같은 명령을 다시
입력하지 않아도 원래 운세 등록·조회 또는 학교 공지 설정 흐름이 한 번만 자동으로 이어진다.
버튼을 누르기 전에는 새 개인정보를 수집하지 않고 기존 프로필도 이용하지 않는다.
현재 동의 상태와 동의·철회 이벤트 이력은 분리해 기록한다. 정책 버전 또는 고지문 hash가
바뀌면 재동의 전까지 fail-closed로 처리한다.

### 철회와 삭제의 차이

```text
!개인정보 철회 운세
!개인정보 철회 학교공지
!개인정보 철회 편입공지
```

철회는 즉시 향후 프로필 조회, 개인화, LLM 처리, 피드백 수집과 자동 발송을 중단한다.
기존 프로필·구독·활성 설정은 자동 삭제하지 않으므로 나중에 재동의하면 이어서 사용할 수
있다.

```text
!운세 삭제
!공지 삭제
!편입 삭제
```

삭제는 해당 기능의 프로필과 구독/대기 상태, 피드백, 전달·실행 기록 및 사용자별 파생
파일을 정리하고 동의도 철회한다. 일반 Discord 대화와 서버 기록은 변경하지 않는다.
동의·철회 감사 이벤트는 처리 이력으로 별도 보존한다. 파생 파일 정리 일부가 실패하면
봇은 완전 삭제처럼 응답하지 않고 운영자 확인이 필요하다고 알린다.

## 운세

### 사용자 흐름

1. DM에서 `!개인정보 동의 운세`를 실행하고 본인이 버튼으로 동의한다.
2. `!운세 등록`으로 대화형 등록을 시작한다.
3. 생년월일은 필수이며 실제 달력 날짜, 미래일 여부, 최대 120년 범위를 검증한다.
4. 출생 시각·성별·출생지는 선택 사항이다. `모름` 또는 `응답 안 함`이면 `NULL`로
   저장하며 정오·특정 성별·지역으로 대신 채우지 않는다.
5. 각 단계는 최대 3번만 다시 묻고, 60초 timeout 또는 `취소` 입력 시 아무것도 저장하지
   않고 끝낸다.

명령:

```text
!운세
!운세 상세
!이번달운세
!올해운세
!별자리
!운세 구독 HH:MM
!운세 구독취소
!운세 삭제
```

서버의 `!운세`는 짧은 결과, DM 또는 `상세`·월간·연간은 상세 결과를 제공한다. 개인
프로필을 사용해 외부 LLM으로 생성하는 일간 요약·상세·월간·연간 운세는 모두 합산 하루
3회다. 사용자별 직렬화 구간에서 한도를 확인하고 외부 호출을 시작하기 전에 사용량을
예약하므로 동시 요청으로 한도를 우회하지 않는다. 공급자 실패나 빈 응답도 이미 시작된
호출이면 사용량에 포함한다. 사용자가 별자리를 직접 지정한 조회·순위는 개인 프로필을
읽지 않아 운세 동의 없이 쓸 수 있지만, 저장된 생년월일에서 별자리를 찾는 경로는 현재
운세 동의를 요구한다. 별자리 외부 생성도 일별 물리 호출 상한·singleflight·TTL/LRU
cache·timeout·실패 cache로 제한한다.

### 모닝 브리핑의 유한 상태

모닝 브리핑은 1분 주기로 확인하되 한 tick에서 사용자 한 명의 한 단계만 처리한다.
`pending_payload`에 대상 날짜, 상태, 생성/발송 횟수, 다음 시도 시각과 생성 문장을
영속화한다.

- 생성과 DM 발송을 분리한다.
- 기본 생성 최대 3회, 발송 최대 3회다.
- 실패 시 유한한 지수 backoff를 사용한다.
- 발송 재시도는 저장된 문장을 재사용하므로 LLM을 다시 부르지 않는다.
- 기본 LLM timeout 35초, 발송 10초, tick 전체 50초로 60초 주기보다 짧다.
- 자정 사전 생성은 다음 날짜 운세를 만들고, 과거 대상 날짜 문장을 보내지 않는다.
- 재등록 시 이전 대기 작업과 운세 문맥을 지운다.
- 철회 또는 현재 정책 미동의 사용자는 조회와 발송 직전 두 단계에서 제외한다.

따라서 느린 공급자·DM 차단·프로세스 재시작이 매분 무한 LLM 호출로 이어지지 않는다.

## 학교 공지

학교 공지는 현재 Masamo 운영판이 소유한다. 수집 코어는 저장소의 `school_notice/`에
포함되지만 상주 Discord event loop 안에서 실행하지 않는다. 첫 등록 직후에는 해당 사용자
한 명만 대상으로 별도 저자원 프로세스를 한 번 실행하고, 이후에는 systemd one-shot이
Masamo 전용 core DB와 digest 디렉터리를 사용한다. General은 기본 비활성이며 Masamo의
학교 테이블·파일·timer를 공유하지 않는다.

현재 방식은 학교 API 연동이 아니라 공개 HTML 순수 크롤링이다. 학교별 공개 목록·상세
URL과 CSS selector를 버전 관리하며 robots, 허용 host, redirect, 응답 크기와 요청 수
상한을 지킨다. 쿠키·Referer·환경 proxy를 사용하지 않고 학교 요청에 Discord ID, 학과,
학년, 관심사나 사용자 입력을 절대 넣지 않는다.

### 자연어 등록과 확인

DM에서 `!메뉴`의 `학교 공지` 버튼 또는 `!공지`의 `설정·변경` 버튼을 누르거나 다음처럼
자연스럽게 말한다.

```text
!공지 등록 전북대 소프트웨어공학과 3학년이고 오전 9시에 알려줘
전북대 소프트웨어공학과 3학년 공지를 오전 9시에 알려줘
```

봇은 지원 카탈로그의 값으로만 학교·과정·학년·학과·캠퍼스·전달 시각 등을 정규화하고
“제가 이렇게 이해했어요. 맞을까요?”라는 요약을 보여준다.

- 맞으면 `맞아`, `네`, `확인`, `저장` 등으로 확정한다.
- 틀리면 `학년은 4학년이고 8시 30분에 보내줘`처럼 자연어로 수정한다.
- `취소`를 입력하면 저장하지 않는다.
- 누락된 필수 정보는 다시 묻는다.
- 확인 전에는 DB에 저장하지 않는다.
- 한 사용자에게 등록 세션은 하나만 허용하고 입력·수정 횟수·대기 시간·LLM timeout에
  상한을 둔다.
- 먼저 유한한 로컬 파서로 해석한다. 입력만으로 값이 명확하면 프로필 LLM을 호출하지
  않는다.
- 로컬 해석으로 해결되지 않은 입력만 해석 시도당 routing primary 한 곳을 1회 호출하며,
  공급자 fallback이나 자동 재시도는 하지 않는다. 한 등록 세션의 실제 공급자 호출은
  초안과 보정을 합쳐 기본 최대 3회다.
- 신규 기본 전달 시각은 `09:00` KST다.
- 신규 프로필을 처음 확정하면 해당 학교만 즉시 한 번 확인한다. 정기 batch와 lock이
  겹치면 한 번만 기다렸다 재시도하며, timeout 뒤 무한 재실행하지 않는다.

사용 명령:

```text
!공지
!공지 1
!공지 등록 <자연어>
!공지 수정 <자연어>
!공지 정보
!공지 상태
!공지 시간 HH:MM
!공지 중지
!공지 재개
!공지 음소거 [주제]
!공지 음소거해제 <주제>
!공지 삭제
```

`!공지 정보`로 현재 저장값과 전달 상태를 확인할 수 있다. 기존 값을 바꾸려면
`!공지 수정 <자연어>`로 새 후보를 확인한 뒤 저장한다.
`관심 없음` 피드백은 유사 주제를 영구 차단하지 않고 우선순위를 완만하게 낮춘다.
명시적인 주제 숨김은 `음소거`를 사용하며, 등록금·수강·학적·졸업·병무의 근거 있는 필수
공지는 보호 규칙에 따라 계속 표시될 수 있다.

### 수집과 전달

```text
동의·활성 프로필
  → 첫 등록: 해당 사용자·학교만 즉시 저자원 1회 수집
  → 이후: 해당 프로필 학교의 source ID만 선택
  → 23:00 KST 외부 one-shot core 실행
  → 사용자별 digest 계약 검증·원자적 공개
  → 보통 다음 날 사용자별 시각(기본 09:00 KST)에 DM
```

- 모든 학교를 일괄 크롤링하지 않는다.
- 첫 등록 수집도 `--only-user-id` SQL 조건으로 다른 사용자 프로필을 읽지 않는다.
- 현재 동의가 있고 `enabled=1`인 등록 프로필만 batch 대상이다.
- 카탈로그와 vendored core source 설정 양쪽에 존재하는 해당 학교 source만 실행한다.
- 사용자의 조건과 관련된 visible item이 없으면 자동 DM을 보내지 않는다.
- 수집 실패와 “관련 공지 없음”은 내부 실행 상태에서 구분한다. 기본 설정에서는 실패만으로
  사용자에게 불필요한 stale 경고 DM을 만들지 않는다.
- 재시작·장애 복구 때는 최근 3일 안에서 가장 최신의 유효한 성공·부분 성공 batch만
  고려한다. 더 최신 성공 batch가 있으면 그보다 오래된 결과를 대신 보내지 않는다.
- 같은 공지 revision은 중복 발송하지 않고 내용이 바뀐 revision은 새 변경으로 다룬다.
- 자동 전달 항목이 한 DM의 상한을 넘으면 페이지로 나눈다. 성공한 revision은 즉시
  기록하고 남은 페이지는 다음 1분 scheduler tick에서 이어 보낸다. 성공한 페이지는
  실패 attempt를 소비하지 않는다.
- DM 차단·계약 오류·일시 실패는 사용자별 유한 횟수와 다음 재시도 시각으로 관리한다.
- 본문이 이미지뿐이거나 HTML 텍스트가 너무 짧으면 제목·공개 게시판 분류만 후보 판단에
  쓰고, Discord에 원문 이미지·첨부 확인 필요를 표시한다.

지원 카탈로그는 14개 학교, 16개 source ID다.

| 학교 | source |
|---|---|
| 전북대학교 | `jbnu_campus`, `jbnu_software` |
| 서울대학교 | `snu_general` |
| 부산대학교 | `pnu_general` |
| 고려대학교 | `korea_cs_undergrad` |
| 전주대학교 | `jj_academic` |
| 성균관대학교 | `skku_general` |
| 가천대학교 | `gachon_general` |
| 숭실대학교 | `ssu_general` |
| 전남대학교 | `jnu_software` |
| 국립순천대학교 | `scnu_academic` |
| 명지대학교 | `mju_general` |
| 건국대학교 | `konkuk_academic` |
| 국민대학교 | `kookmin_academic` |
| 한양대학교 | `hanyang_seoul`, `hanyang_erica` |

이 목록은 마사몽 측 프로필 카탈로그이며 실제 수집 정의는 같은 release의
`school_notice/sources.json`이다. 현재 구조는 동의하고 활성화한 등록 프로필만 대상으로
삼으며 모든 학교를 일괄 수집하지 않는다.

## 편입 공지

편입 공지는 Masamo DM에서 `!편입`을 실행해 명시적으로 동의하고 20개 대학 중 원하는
곳만 선택하는 구독 기능이다. 학교 공지와 마찬가지로 서버 명령·메뉴 진입은 DM 사용법만
안내하며, 서버에서 개인 설정이나 결과를 읽거나 출력하지 않는다.

- 23:35 KST 저자원 one-shot이 공식 입학처 목록을 하루 한 번 확인한다.
- 첫 성공 수집은 기준선으로만 저장해 과거 공지를 재전송하지 않는다.
- 새 글 또는 제목 revision이 생겼을 때 선택 대학과 일치하는 활성 구독자에게만 DM한다.
- 구독 취소, 동의 철회 또는 선택 변경 뒤에는 이전 실패 payload를 되살리지 않는다.
- TOEIC 점수·학력·지원 학과·실명·연락처는 수집하지 않는다.
- 공개 snapshot SQLite에는 Discord 사용자 정보가 전혀 들어가지 않는다.
- 20개 전체를 지원 가능 대학이라고 판정하지 않는다. 공인영어 반영 범위는 연도·학과별로
  달라질 수 있으므로 알림의 당해 모집요강 원문을 확인해야 한다.

수집은 LLM 없이 순차 실행하고, 전체 제한 시간·source당 요청 수·응답 크기·재시도 횟수에
상한이 있다. robots 금지 source는 우회하지 않고 실패 상태를 공개한다. 상세한 20개
공지원, 개인정보 경계와 운영 절차는 [편입 공지 구독 설명서](TRANSFER_NOTICE.ko.md)에
있다.

## 기상청 날씨와 재난 알림

`!날씨 전주`, `!날씨 내일 부산`, `!날씨 이번주 제주`처럼 사용한다. 현재 조회는 다음을
가능한 한 한 번의 묶음으로 보여준다.

- 관측 기준 시각, 기온·체감온도, 습도, 강수형태·1시간 강수량, 풍향·풍속
- 향후 약 6시간의 강수확률·강수량·낙뢰·최대 풍속·습도 변화
- 오늘/단기예보의 오전·오후 하늘, 최저·최고기온, 강수·적설, 습도, 최대 풍속
- 요청 지역의 현재 발효 특보와 발효 시각
- 주간 요청의 3~10일 날씨·강수확률·기온 및 기온 범위
- 사용자가 태풍·폭염/한파 영향예보·기상 개황을 명시한 경우 해당 부가자료

같은 발표시각·격자 요청은 TTL cache와 singleflight로 한 물리 호출로 합치고, 스레드별
HTTPS 연결을 재사용한다. 일상적인 현재 날씨는 실황·초단기·단기·현재 특보 중심이라
기존처럼 개황·특보·영향예보·태풍을 매번 중복 호출하지 않는다. 부가자료는 질문에 관련될
때만 조회한다.

지진 감시는 기본 30초 간격이지만 알림 가능 시각은 기상청이 통보문을 공개한 뒤다. 봇은
발생 시각과 기상청 발표 시각을 함께 표시해 이 차이를 숨기지 않는다. 지진 경로에는
서버별 페르소나나 LLM을 적용하지 않으며, 최신 통보를 감지하면 공식 필드와 국민행동요령을
고정된 엄중한 형식으로 즉시 보낸다. 재시작 중복 방지를 위해 마지막 발생시각을 기존
`system_counters`에 Discord 전송 전에 저장한다. 이 키를 처음 도입한 배포에서는 이미
발표된 최신 지진·여진을 기준점으로만 저장해 재전송하지 않으며, 이후 더 최신 발생시각의
통보만 전송한다.

기본 72시간·진앙 150km 안의 후속 지진은 한 지진군으로 자동 분류한다. 첫 통보는 새
Discord 메시지를 만들고, 같은 지진군의 후속 통보는 그 메시지를 `edit`하여 총 발생 건수,
현재 지진군 내 최대 규모와 최신 후속 지진 목록을 이어 표시한다. 메시지 ID는
`system_counters`에 저장하므로 봇이 재기동해도 같은 메시지를 계속 수정한다. 진앙이 멀거나
시간창 밖의 독립 지진은 새 메시지로 시작한다. 이 묶음은 표시 편의를 위한
`후속 지진(여진 가능)` 자동 분류이지 기상청의 공식 여진 판정이라고 단정하지 않는다.
Discord 2,000자 제한 안에서 최신 6건을 우선 표시하고 전체 건수는 유지한다.

필드와 생산주기는 기상청 API허브의
[단기·초단기·실황 계약](https://apihub.kma.go.kr/apiList.do?apiMov=4.+%EB%8F%99%EB%84%A4%EC%98%88%EB%B3%B4%28%EC%B4%88%EB%8B%A8%EA%B8%B0%EC%8B%A4%ED%99%A9%C2%B7%EC%B4%88%EB%8B%A8%EA%B8%B0%EC%98%88%EB%B3%B4%C2%B7%EB%8B%A8%EA%B8%B0%EC%98%88%EB%B3%B4%29+%EC%A1%B0%ED%9A%8C&seqApi=10&seqApiSub=286),
[중기예보 계약](https://apihub.kma.go.kr/apiList.do?apiMov=%EC%A4%91%EA%B8%B0%EC%98%88%EB%B3%B4%EC%9E%90%EB%A3%8C%282001%EB%85%84+2%EC%9B%94+%EC%9D%B4%ED%9B%84%29+%EC%A1%B0%ED%9A%8C&seqApi=10&seqApiSub=287),
[현재 특보 계약](https://apihub.kma.go.kr/apiList.do?apiMov=%ED%8A%B9.%EC%A0%95%EB%B3%B4+%EC%9E%90%EB%A3%8C+%EC%A1%B0%ED%9A%8C&seqApi=10&seqApiSub=288)을 기준으로 한다.
지진 통보의 발생·발표시각, 위·경도, 규모, 진도, 깊이 필드는
[기상청 지진 API 계약](https://apihub.kma.go.kr/apiList.do?seqApi=7)을 따른다.

## Discord 출력 계약

Discord의 부분적 Markdown과 한도를 기준으로 모든 동적 AI/도구 응답은 전송 전에
정규화한다.

- `#` 헤더는 굵은 제목으로, Markdown 표는 단순 불릿으로 변환한다.
- 일반 메시지는 2,000자보다 작은 청크로 나누고 코드블록 fence를 청크별로 닫는다.
- embed 설명·field는 각각의 Discord 한도 안에서 생략 표시와 함께 자른다.
- 동적 응답의 사용자/역할 mention은 비활성화한다.
- 운세·요약·날씨 AI 프롬프트도 표·HTML·복잡한 중첩 목록을 만들지 않도록 통일한다.

## API·자원 보호

모든 LLMClient 물리 호출은 공통 세마포어와 획득 timeout, 호출 timeout을 거친다.
OpenAI 호환 SDK 자체 재시도는 꺼 두고, Gemini 시도 수도 제한한다. timeout이 불명확한
요청 위에 fallback을 중첩해 호출하지 않는다. 사용자/글로벌 일일 사용량과 공급자 RPM/RPD
제한도 별도로 적용한다.

저사양 프로필에는 다음 값을 env 파일에 양의 정수로 명시한다.

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

Masamo 전환에서는 예제 숫자로 덮어쓰지 말고 현재 서비스의 실제 제한을 보존한다.
General은 처음에 로컬 memory와 반복 scheduler를 끈 뒤 두 프로세스의 합산 CPU, RSS,
thread, load average를 측정해 기능을 단계적으로 켠다. 학교·편입 수집은 봇 프로세스가
아닌 `Type=oneshot` 프로세스에서 CPU thread 1개, low nice/IO와 systemd
`TimeoutStartSec`을 포함한 전체 deadline으로 실행한다.

## 설치와 실행

```bash
git clone https://github.com/kim0040/masamong.git
cd masamong
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

로컬 임베딩/RAG가 필요할 때만:

```bash
python -m pip install -r requirements-cpu.txt
```

개발 환경은 루트 예제를 복사해 별도 개발 토큰과 SQLite를 사용한다. 운영은
`profiles/general.env.example`, `profiles/masamo.env.example`과
[인스턴스 분리 가이드](INSTANCE_SEPARATION.ko.md)를 사용하며, 저장소의 `.env`를 두
인스턴스가 공유하지 않는다.

```bash
MASAMONG_ENV_FILE=/absolute/path/to/profile.env \
  PYTHONPATH=. .venv/bin/python main.py
```

명시적 프로필에서는 선택한 env 파일이 유일한 환경 설정 출처다. systemd나 shell에만
남아 있는 키는 파일로 옮기지 않으면 전환 후 사용되지 않는다.

## 테스트와 운영 문서

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q .
.venv/bin/python -m pip check
```

실제 두 env의 오프라인 경계 검사:

```bash
.venv/bin/python scripts/validate_profile_separation.py \
  /etc/masamong/masamo.env \
  /etc/masamong/general.env
```

| 문서 | 내용 |
|---|---|
| [INSTANCE_SEPARATION.ko.md](INSTANCE_SEPARATION.ko.md) | General/Masamo 경계와 전환 |
| [../DEPLOYMENT.md](../DEPLOYMENT.md) | 백업, 읽기 전용 preflight, migration, 재시작, rollback |
| [SCHOOL_NOTICE_INTEGRATION_PLAN.ko.md](SCHOOL_NOTICE_INTEGRATION_PLAN.ko.md) | 수집 core와 학교 공지 계약 |
| [TRANSFER_NOTICE.ko.md](TRANSFER_NOTICE.ko.md) | 20개 대학 편입 공지 구독·개인정보·운영 계약 |
| [MEMORY_INDEX_MIGRATION.ko.md](MEMORY_INDEX_MIGRATION.ko.md) | 운영 기억 품질 감사와 무삭제 shadow vector migration |
| [ARCHITECTURE.ko.md](ARCHITECTURE.ko.md) | 전체 아키텍처 |
| [SETTINGS_GUIDE.md](SETTINGS_GUIDE.md) | 일반 설정 |

## 라이선스

MIT

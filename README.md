# Masamong (마사몽)

> [!WARNING]
> **지원·업데이트 중단 (2026-10-13)**
>
> 이 저장소(마사몽 1)는 더 이상 업데이트하지 않습니다. 버그 수정, 보안 패치, 새 기능은 모두 후속 프로젝트 **[마사몽2](https://github.com/kim0040/masamong2)**에서 이어집니다. 마사몽2는 Go로 새로 만든 버전으로, 관리 콘솔(GUI)·기억 검색·여러 AI 공급자를 지원합니다. 저장소는 현재 비공개이며, 공개되면 위 링크에서 볼 수 있습니다.
>
> - 이 코드는 기록과 참고용으로만 남겨 둡니다. 새로 설치하는 것은 권하지 않습니다.
> - 이슈와 PR은 더 이상 받지 않습니다.
> - 이미 운영 중인 설치는 당장 멈추지 않지만, 의존성이나 외부 API가 바뀌어도 대응하지 않습니다. 마사몽2로 옮기면 이전 봇의 개인 설정을 가져올 수 있습니다.
> - 2026-10-13에 Git 기록을 다시 써서, 예전에 실수로 올라간 실제 Discord ID와 로그 파일을 지웠습니다. 그 전에 clone했다면 새로 clone하세요.

> [!WARNING]
> **No longer supported or updated (2026-10-13)**
>
> This repository (Masamong 1) is no longer updated. Bug fixes, security patches and new features continue in its successor, **[Masamong2](https://github.com/kim0040/masamong2)**, a rewrite in Go with an admin console (GUI), memory search and multiple AI providers. That repository is private for now; the link works once it is public.
>
> - The code stays here as a record and reference only. New installs are not recommended.
> - Issues and pull requests are no longer accepted.
> - Existing installs keep running, but nothing here will follow dependency or external API changes. Masamong2 can import the old bot's personal settings.
> - On 2026-10-13 the Git history was rewritten to remove real Discord IDs and log files that had been committed by mistake. If you cloned before that, clone again.

마사몽은 Discord에서 대화하고, 필요한 과거 맥락을 찾아보고, 날씨·재난·학교 공지처럼
놓치기 쉬운 정보를 챙겨주는 한국어 중심 봇입니다. 사용자는 자연어로 요청할 수 있고,
개인 설정이 필요한 기능은 DM에서 확인과 동의를 거쳐 사용할 수 있습니다.

[사용자 가이드](docs/README.ko.md) · [English guide](docs/README.en.md) · [日本語ガイド](docs/README.ja.md) · [문서 전체 보기](docs/README.md)

```text
!메뉴                 버튼으로 기능 둘러보기
!날씨 전주             기상청 날씨·특보 확인
!공지                  내 학교 공지 설정 및 확인 (DM)
!운세                  오늘의 운세 보기
@마사몽 안녕            서버에서 대화 시작
```

## 무엇을 할 수 있나요?

| 영역 | 할 수 있는 일 |
| --- | --- |
| 대화와 기억 | 멘션 또는 DM으로 대화하고, 현재 질문과 관련된 서버·DM의 이전 맥락을 찾아 답변할 수 있습니다. |
| 사실 확인 | 날씨, 지진, 태풍, 시장, 웹·뉴스·장소 정보를 조회하고 확인된 근거가 있을 때만 최신 사실로 답변할 수 있습니다. |
| 학교 공지 | 지원하는 학교의 공개 게시판을 읽어, 동의한 사용자의 학교·과정·관심사와 관련된 공지만 DM으로 알려줄 수 있습니다. |
| 편입 공지 | 공인영어 전형 정보를 확인하기 좋은 대학의 공식 입학처 공지를 구독하고 새 공지를 받을 수 있습니다. |
| 창작과 커뮤니티 | 기억을 반영한 이미지 생성, 운세, 채널 요약, 투표, 활동 랭킹을 사용할 수 있습니다. |

서버에서는 `!메뉴`로 기능을 범주별로 살펴볼 수 있습니다. 메뉴는 연 사람에게만
보이고, 날씨·요약처럼 공개해도 되는 결과는 선택한 채널에 보냅니다. 학교·편입·개인정보
설정은 DM에서만 열립니다.

## 빠르게 시작하기

Python 3.10 이상, Discord 봇 토큰, OpenAI 호환 또는 Gemini 계열 LLM 엔드포인트가
필요합니다.

```bash
git clone https://github.com/kim0040/masamong.git
cd masamong
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
cp prompts.example.json prompts.json
cp emb_config.example.json emb_config.json
python main.py
```

로컬 임베딩과 기억 검색도 사용하려면 CPU 의존성을 추가로 설치할 수 있습니다.

```bash
python -m pip install -r requirements-cpu.txt
```

설정값과 로컬 실행 확인은 [빠른 시작](docs/QUICKSTART.md)을, 운영 환경의 값은
[설정 가이드](docs/SETTINGS_GUIDE.md)를 참고하세요. 토큰·API 키·DB 비밀번호는
`.env` 또는 접근이 제한된 운영 환경 파일에만 두고 Git에 추가하면 안 됩니다.

## 안전하게 동작하도록 설계했습니다

- 개인 프로필을 저장하는 운세·학교 공지·편입 공지는 기능별 명시 동의를 받은 뒤에만
  사용할 수 있습니다. 동의 철회와 데이터 삭제는 서로 다른 동작으로 안내합니다.
- 학교 사이트에는 Discord ID, 학과, 학년, 관심사 등 개인 프로필을 보내지 않습니다.
  공개 게시판을 읽은 뒤 봇 내부에서 관련도를 판단합니다.
- 최신·지역·금융·뉴스처럼 확인이 필요한 질문은 도구 조회가 성공해야 답변합니다.
  확인하지 못하면 추측을 사실처럼 말하지 않습니다.
- 정확한 가격·제원·기록을 묻는 비교는 공개 자료를 확인하고, 주관적 선택만 묻는
  경우에는 조회하지 않은 수치표를 만들지 않습니다. 전송된 봇 답변도 서버·DM별
  기억에 보존해 이전 선택과 설명의 일관성을 유지할 수 있습니다.
- LLM·이미지·검색 요청에는 기능·서버·사용자·DM 단위의 한도, timeout, 유한 재시도와
  동시성 제한이 적용됩니다. 동시에 들어온 대화는 유한 FIFO 대기열에서 접수 순서대로
  처리하며, 표시용 Discord 진행 상태와 대기는 별도 LLM 호출이나 과금을 만들지 않습니다.
- 지진 알림은 기상청 발표를 약 1분 간격으로 확인하며, 같은 지진군은 Discord 메시지를
  갱신해 중복 알림을 줄입니다. 재시작 후 이전 알림을 다시 보내지 않습니다.

## 두 인스턴스, 한 코드베이스

같은 릴리스를 사용하되 운영 프로필은 완전히 분리할 수 있습니다.

| 프로필 | 용도 | 데이터 경계 |
| --- | --- | --- |
| `masamo` | 기존 커뮤니티 운영 | 기존 TiDB, Discord/Kakao 기억, 공지 배치 상태를 유지합니다. |
| `general` | 새 일반 배포 | 별도 Discord 앱, DB, 프롬프트, 파일 경로, 관리자 목록으로 시작합니다. |

두 인스턴스는 토큰, DB, 프롬프트, 임베딩 저장소, 로그, timer, 관리자 설정을 공유하지
않습니다. 프로필·봇 ID·DB·TLS·필수 기능·리소스 제한이 맞지 않으면 시작을 중단하도록
검증합니다. 자세한 전환 기준은 [인스턴스 분리 운영 가이드](docs/INSTANCE_SEPARATION.ko.md)에
정리되어 있습니다.

## 운영 환경

운영에서는 저장소 루트의 `.env` 대신 서비스 밖의 명시적 환경 파일을 선택합니다.

```bash
MASAMONG_ENV_FILE=/etc/masamong/masamo.env \
PYTHONPATH=. \
.venv/bin/python main.py
```

저사양 서버에서는 다음과 같이 동시성·스레드·BM25 자동 재구축을 명시적으로 제한할 수
있습니다.

```dotenv
MASAMONG_CPU_THREADS=1
MASAMONG_EXECUTOR_WORKERS=1
AI_MAX_CONCURRENT_PROCESSING=1
AI_QUEUE_MAX_SIZE=8
LLM_MAX_CONCURRENT_CALLS=1
LLM_CALL_TIMEOUT_SECONDS=120
EMBEDDING_MAX_CONCURRENCY=1
TIDB_STARTER_FREE_PLAN_MODE=true
TOKENIZERS_PARALLELISM=false
BM25_AUTO_REBUILD_ENABLED=false
```

누적 데이터가 있는 Masamo 환경은 자동 마이그레이션을 켜지 않습니다. 배포·롤백·읽기 전용
점검·additive schema 적용 절차는 [운영·배포 가이드](docs/DEPLOYMENT.md)에서 확인할 수
있습니다.

## 개발과 검증

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q .
.venv/bin/python scripts/verify_bang_commands.py
.venv/bin/python scripts/audit_tracked_secrets.py --secret-env .env
git diff --check
```

운영 DB 상태는 쓰기를 물리적으로 거부하는 stale-read 검사로만 확인할 수 있습니다.

```bash
.venv/bin/python scripts/inspect_runtime_readonly.py \
  --expected-profile masamo --expected-db masamong
.venv/bin/python scripts/audit_tidb_free_plan_readonly.py \
  --expected-profile masamo --expected-db masamong
```

`scripts/smoke_*.py`는 실제 외부 API를 호출할 수 있습니다. 실행 전 `--help`, 호출 한도,
대상 프로필을 확인하세요.

## 문서

| 목적 | 문서 |
| --- | --- |
| 사용 방법 | [사용자 가이드](docs/README.ko.md), [빠른 시작](docs/QUICKSTART.md) |
| 기능 기준 | [학교 공지](docs/SCHOOL_NOTICE.ko.md), [편입 공지](docs/TRANSFER_NOTICE.ko.md) |
| 운영 | [설정 가이드](docs/SETTINGS_GUIDE.md), [배포·롤백](docs/DEPLOYMENT.md), [인스턴스 분리](docs/INSTANCE_SEPARATION.ko.md) |
| 개발 | [아키텍처](docs/ARCHITECTURE.ko.md), [UML 명세](docs/UML_SPEC.ko.md), [기여 가이드](docs/CONTRIBUTING.md) |
| 전체 목록 | [문서 허브](docs/README.md) |

RAG 측정, 마이그레이션 판단, 변경 이력처럼 운영·개발팀의 근거로 남기는 자료는
`docs/internal/`에서 Git으로 추적합니다. 사용자용 문서 목록에는 포함하지 않습니다.

## 저장소 구성

```text
main.py              기동과 Cog 수명주기
config.py            프로필·기능 플래그·리소스 제한
cogs/                Discord 대화·메뉴·알림 기능
utils/               LLM, 기억 검색, 개인정보, 외부 도구
database/            SQLite/TiDB 어댑터와 스키마
school_notice/       학교 공지 수집 코어
transfer_notice/     편입 공지 수집 코어
profiles/            분리된 운영 프로필 예제
scripts/             검증·읽기 전용 감사·one-shot 도구
docs/                사용자·운영·개발 문서
tests/               네트워크 없는 회귀 테스트
```

## 라이선스

MIT

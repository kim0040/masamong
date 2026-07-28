# 기억·임베딩 품질과 무삭제 migration

이 문서는 운영 Masamo의 누적 대화·요약·임베딩을 보존하면서 검색 품질과 구조를 개선하는
기준이다. 현재 데이터는 삭제·truncate·전체 재색인하지 않는다. General은 별도 빈 DB와
별도 embedding 저장소에서 시작하며 아래 운영 수치를 복사하지 않는다.

## 현재 데이터 흐름

Masamo 기억은 서로 다른 세 경로를 함께 검색한다.

1. `discord_memory_entries`: 대화에서 추출한 구조화 기억과 요약
2. `discord_chat_embeddings`: 과거 Discord 대화 청크의 384차원 BLOB 임베딩
3. `kakao_chunks`: 기존 Kakao 자료의 TiDB `VECTOR(384)` 임베딩

구조화 기억, legacy Discord 임베딩, Kakao vector, BM25 후보는 출처별 품질이 다르다.
따라서 검색 후보가 가진 `acceptance_threshold`를 사용한다. 구조화 기억이 자체 기준을
통과했는데 최종 단계의 더 높은 전역 기준으로 다시 버려지는 이중 threshold는 사용하지
않는다.

2026-07-28 읽기 전용 운영 감사에서 확인한 집계는 다음과 같다.

| 데이터 | 행 수 |
|---|---:|
| `conversation_history` | 20,185 |
| `conversation_history_archive` | 22,697 |
| `discord_chat_embeddings` | 2,913 |
| `discord_memory_entries` | 22,968 |
| `kakao_chunks` | 13,716 |

표본 임베딩은 384차원 float32, 단위 norm, 유효한 byte/vector 형식이었고 표본 내 완전 중복은
발견되지 않았다. 구조화 기억의 summary/raw context와 임베딩 표본에도 빈 값은 없었다.
감사는 원문, Discord 사용자 ID, channel/guild ID를 출력하지 않는다.

```bash
MASAMONG_ENV_FILE=/etc/masamong/masamo.env \
  <venv>/bin/python scripts/audit_memory_quality_readonly.py \
  --expected-profile masamo \
  --expected-db masamong
```

스크립트는 TiDB stale read/read-only transaction 또는 SQLite read-only URI와 고정 SELECT만
사용한다. 결과는 분포·차원·norm·중복 hash 같은 비식별 집계다.

## 현재 구조의 한계

- 임베딩 행에 model 이름·revision, 차원, 정규화 방식, content hash, summary version,
  생성 시각 같은 provenance가 없다.
- Discord legacy 임베딩은 BLOB이라 TiDB vector index를 직접 사용할 수 없다.
- Discord 검색은 최근 제한 창을 Python으로 점수화하므로 데이터가 계속 누적되면 오래된
  관련 기억이 후보 창 밖에 남을 수 있다.
- Kakao는 DB vector search, Discord는 Python scan이라 검색 비용과 recall 특성이 다르다.
- 모델을 바꾸거나 요약 규칙을 바꿀 때 어떤 행을 다시 만들었는지 안전하게 판별하기 어렵다.

이 한계는 기존 데이터가 잘못됐다는 뜻이 아니다. 현재 표본 품질은 정상이며, 운영 중인
v1을 덮어쓰는 것보다 v2 shadow index로 비교하는 편이 안전하다.

## 현재 즉시 적용하는 무삭제 최적화

이번 release는 vector schema나 기존 BLOB을 바꾸지 않는다. 대신 한 사용자 검색 안에서
질의 변형마다 같은 `discord_memory_entries` 행을 반복 SELECT하던 경로를 하나의 제한된
후보 집합으로 공유한다. 운영 권장 상한은 `STRUCTURED_MEMORY_QUERY_LIMIT=384`,
fallback은 `STRUCTURED_MEMORY_FALLBACK_QUERY_LIMIT=1024`다. 이는 DB read와 Python
점수화 비용을 줄이는 설정이며, 누적 기존 행을 삭제하거나 다시 임베딩하지 않는다.

`channel_summary_state`는 `!요약`의 서버·채널별 마지막 메시지 기준점과 제한된 요약문을
보존하는 별도 additive table이다. 임베딩 index가 아니며 v1/v2 기억 migration 대상에도
섞지 않는다.

## 목표 v2 계약

새 index는 기존 table을 변경하지 않고 별도 table로 만든다. 실제 TiDB의 지원 vector
index 문법과 제한은 staging restore에서 현재 서버 버전으로 확인한 뒤 고정 DDL로 만든다.

필수 metadata:

- `instance_name`: `masamo` 또는 `general`
- `source_kind`, `source_pk`: 원본 종류와 안정적인 원본 키
- `content_hash`: 정규화한 임베딩 입력의 SHA-256
- `summary_version`: 요약/청킹 규칙 버전
- `model_provider`, `model_name`, `model_revision`
- `embedding_dim`, `normalized`
- `embedding`: 현재 모델과 맞는 `VECTOR(384)`
- `source_created_at`, `indexed_at`
- 원본 키 + model revision + content hash의 idempotent unique key

원문 전체를 v2에 중복 저장하지 않는다. 검색 결과는 기존 원본 table의 권한·보존 계약으로
다시 읽는다.

## 단계별 migration

### 0. 사전 조건

- 공급자 snapshot/PITR과 staging restore를 확인한다.
- 배포 SHA, model cache checksum, v1 row count와 read-only 감사 결과를 보존한다.
- Masamo는 `MASAMONG_AUTO_MIGRATE=false`, CPU/BLAS thread 1을 유지한다.
- 운영 배포와 대규모 backfill을 같은 점검 창에서 실행하지 않는다.

### 1. Additive shadow schema

고정된 `CREATE TABLE IF NOT EXISTS`만 별도 typed-confirmation 도구로 실행한다. 기존 table에
`ALTER`, `UPDATE`, `DELETE`, `DROP`, `TRUNCATE`를 하지 않는다. 생성 직후 v1 count와 최신
timestamp가 바뀌지 않았는지 확인한다.

### 2. Checkpointed backfill

- 기본 25~50행의 작은 batch와 thread 1로 실행한다.
- 원본 안정 키 순서의 checkpoint를 별도 migration state에 기록한다.
- 각 행은 content hash와 model revision unique key로 idempotent하게 upsert한다.
- provider/model timeout, batch deadline, 일일 최대 행 수를 둔다.
- 실패한 행은 error 종류와 원본 키 hash만 기록하고 무한 재시도하지 않는다.
- 사용자 요청이 많은 시간에는 중단할 수 있고 다음 checkpoint에서 재개한다.

### 3. Dual-write 관찰

새로 생성되는 기억은 v1을 계속 쓰면서 v2에도 쓴다. v2 실패가 사용자 응답이나 v1 저장을
막지 않도록 격리하되 실패율을 계측한다. 철회·삭제 정책이 적용되는 개인 프로필 파생 기억은
양쪽 경로에서 같은 정책을 따른다. 일반 Discord 대화와 서버 제공 정보의 기존 처리 범위는
바꾸지 않는다.

### 4. Shadow read 평가

사용자에게는 v1 결과를 보내면서 v2 검색을 제한된 비율로 병행한다. 다음을 원문 노출 없이
비교한다.

- 후보 overlap과 순위 상관
- 오래된 관련 기억의 recall
- 무관 후보 비율과 source별 acceptance rate
- p50/p95 검색 지연, DB CPU, bot RSS
- 동일 질의 singleflight/cache hit와 오류율

고정 평가 질의와 운영자가 승인한 비식별 sample에서 v2 품질이 같거나 높고 저사양 예산을
지켜야 다음 단계로 간다.

### 5. Feature-flag read 전환

인스턴스별 flag로 Masamo만 v2 read를 켠다. v1 dual-write와 table은 유지한다. 오류율,
latency 또는 recall이 기준을 벗어나면 flag만 v1으로 되돌린다. rollback에 DB restore나
재색인이 필요해서는 안 된다.

### 6. 장기 보존 결정

관찰 기간과 snapshot retention이 끝나기 전에는 v1을 drop하지 않는다. 삭제가 필요해지는
경우 별도 변경 승인, 복원 시험, 사용량/비용 근거와 정확한 대상 table 확인을 새 작업으로
진행한다. 이 문서 자체는 v1 삭제를 승인하지 않는다.

## 합격 기준

- v1의 행 수·원문·기존 key가 migration 전후 동일하다.
- v2 coverage가 source별 기대치와 일치하고 차원/model/content hash가 모두 검증된다.
- 같은 원본을 재실행해도 v2 행 수가 증가하지 않는다.
- 오래된 기억 recall이 개선되며 무관 후보 비율은 악화되지 않는다.
- Masamo CPU/RSS와 사용자 응답 p95가 운영 한도를 넘지 않는다.
- General query가 Masamo v1/v2 어느 쪽도 읽지 못한다.
- flag 하나로 즉시 v1 read로 돌아갈 수 있다.

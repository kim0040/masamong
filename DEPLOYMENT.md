# 마사몽 기능 업데이트 및 배포 가이드

이 문서는 기존 운영 중인 서버(DB 유지 필수)에서 '개인 운세' 및 '배포 안정성' 업데이트를 적용하는 절차를 안내합니다.

## 1. 사전 준비 (백업)
가장 중요한 단계입니다. 만약의 사태를 대비해 현재 데이터베이스를 백업합니다.

```bash
# 프로젝트 루트 디렉토리에서 실행
cp database/remasamong.db database/remasamong.db.backup_$(date +%Y%m%d)
```

## 2. 코드 업데이트
최신 코드를 서버에 반영합니다.

```bash
git pull origin main
```

## 3. 라이브러리 설치 (최적화됨)
CPU 전용 서버 환경을 고려하여 필수 라이브러리만 설치합니다. (`torch` 등 무거운 라이브러리 제외됨)

```bash
# 가상환경 활성화
source .venv/bin/activate

# 필수 의존성 설치
pip install -r requirements.txt
pip install -r requirements.txt
```

> **참고**: `flatlib` 및 `pyswisseph` 라이브러리는 **서양 점성술** 기능에 필요하지만, 빌드 오류(컴파일러 필요 등)가 잦아 기본 의존성에서 제외되었습니다. 
> 해당 라이브러리가 없어도 봇은 정상 작동하며, 점성술 기능만 자동으로 비활성화됩니다.
> 만약 점성술 기능을 사용하려면 별도 설치가 필요합니다 (`pip install flatlib pyswisseph`).

### [선택사항] AI 기억(RAG) 기능 활성화
만약 서버에서 **장기 기억(RAG)** 기능을 사용하려면 `numpy`와 `sentence-transformers`가 필요합니다.
CPU 서버에서는 용량을 아끼기 위해 PyTorch CPU 버전을 먼저 설치하는 것을 권장합니다.

```bash
# 1. PyTorch CPU 버전 설치 (Linux/Mac CPU)
pip install torch --index-url https://download.pytorch.org/whl/cpu

# 2. RAG 관련 라이브러리 설치
pip install numpy sentence-transformers
```
*기억 기능을 사용하지 않는다면 이 단계는 건너뛰어도 됩니다. 봇은 RAG 없이도 정상 작동합니다.*

## 4. 데이터베이스 마이그레이션
기존 데이터를 안전하게 유지하며 새 테이블만 추가합니다.

```bash
python3 database/init_db.py
```

## 5. 봇 재시작
```bash
# 프로세스 재시작
python3 main.py
```

## 6. 업데이트 검증
1.  **관리자 기능**: `!debug status` (시스템 상태 확인)
2.  **운세 기능**: `!운세` (DM 테스트)
3.  **기존 기능**: 정상 작동 확인

## ⚠️ 주요 변경 사항
- **DM 제한**: 3시간 5회 (과금/스팸 방지)
- **운세 추가**: `!운세 등록`, `!운세` (보러가기)
- **최적화**: 불필요한 GPU 라이브러리 의존성 제거

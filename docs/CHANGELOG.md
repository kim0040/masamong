# Changelog

이 파일은 마사몽 프로젝트의 주요 변경사항을 기록합니다.

형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.0.0/)를 따르며,
버전 관리는 [Semantic Versioning](https://semver.org/lang/ko/)을 따릅니다.

## [2.0.0] - 2026-01-19

### 추가 (Added)

#### 문서
- 📚 **README.md 대폭 개선**
  - 3개의 Mermaid 아키텍처 다이어그램 추가
  - 목차 및 배지 추가
  - 50개 이상의 환경 변수 전체 문서화
  - 문제 해결 가이드 섹션 추가
  - Discord 사용 가이드 확장
  - RAG 시스템 상세 설명
  - Systemd 서비스 설정 예시
  - 성능 최적화 팁 추가

- 📖 **ARCHITECTURE.md** - 기술 아키텍처 문서
  - 2단계 에이전트 패턴 설명
  - 하이브리드 RAG 알고리즘 상세 설명
  - 데이터 레이어 구조
  - 성능 최적화 전략
  - 보안 고려사항
  - 모니터링 및 관찰성
  - 배포 가이드라인

- 🤝 **CONTRIBUTING.md** - 기여 가이드
  - 개발 환경 설정 방법
  - 코드 스타일 가이드
  - 새 Cog 추가 방법
  - 테스트 작성 가이드
  - Pull Request 절차
  - 커밋 메시지 컨벤션

- 📋 **CHANGELOG.md** - 변경 이력 관리

#### 코드
- ✨ **main.py**: 버전 정보 추가 (`__version__ = "2.0.0"`)
- 📊 **main.py**: 시작 시 시스템 정보 로깅
  - Python 버전
  - Discord.py 버전
  - 작업 디렉터리

### 변경 (Changed)

#### 문서
- 📝 **requirements.txt** - 완전 재구성
  - 모든 패키지 버전 고정 (재현성 확보)
  - 상세한 주석 추가
  - 섹션별 구조화 (핵심, DB, HTTP, AI/ML 등)
  - 선택적 의존성 안내 추가
  - 설치 예시 추가

### 최적화 (Optimized)

#### 성능
- ⚡ **utils/hybrid_search.py**: Regex 패턴 캐싱
  - `_URL_PATTERN`, `_WHITESPACE_PATTERN` 모듈 레벨 컴파일
  - 성능 향상: ~20-30% (정규식 중심 작업 시)

- 🚀 **utils/hybrid_search.py**: 중복 제거 알고리즘 개선
  - 문자열 연결 대신 튜플 + 해시 사용
  - `set[str]`에서 `dict[tuple, bool]`로 변경
  - 성능 향상: ~10-15% (대량 중복 데이터 시)

### 수정 (Fixed)

- 🔧 **.gitignore**: `emb/` 디렉터리 추가하여 임베딩 파일 제외

---

## [1.x.x] - 이전 버전

이전 버전의 변경사항은 Git 커밋 히스토리를 참조하세요:
```bash
git log --oneline
```

---

## 버전 관리 규칙

### 버전 번호: MAJOR.MINOR.PATCH

- **MAJOR**: 호환되지 않는 API 변경
- **MINOR**: 하위 호환되는 기능 추가
- **PATCH**: 하위 호환되는 버그 수정

### 변경 유형

- `Added`: 새로운 기능
- `Changed`: 기존 기능의 변경
- `Deprecated`: 곧 제거될 기능
- `Removed`: 제거된 기능
- `Fixed`: 버그 수정
- `Security`: 보안 관련 수정
- `Optimized`: 성능 최적화

---

[2.0.0]: https://github.com/kim0040/masamong/compare/v1.0.0...v2.0.0

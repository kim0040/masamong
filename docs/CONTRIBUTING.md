# 기여 가이드

마사몽 프로젝트에 기여해 주셔서 감사합니다! 이 문서는 프로젝트의 코드 스타일, 개발 워크플로우, 기여 절차를 안내합니다.

## 목차

- [개발 환경 설정](#개발-환경-설정)
- [코드 스타일 가이드](#코드-스타일-가이드)
- [새 Cog 추가하기](#새-cog-추가하기)
- [테스트 작성](#테스트-작성)
- [Pull Request 절차](#pull-request-절차)

## 개발 환경 설정

### 1. Fork 및 Clone

```bash
# 1. GitHub에서 Fork
# 2. Clone
git clone https://github.com/YOUR_USERNAME/masamong.git
cd masamong

# 3. Upstream 추가
git remote add upstream https://github.com/kim0040/masamong.git
```

### 2. 가상환경 설정

```bash
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 3. 개발 의존성 설치

```bash
pip install pytest pytest-cov pytest-asyncio black flake8 mypy
```

### 4. Pre-commit Hook 설정 (선택)

```bash
# .git/hooks/pre-commit 생성
cat > .git/hooks/pre-commit << 'EOF'
#!/bin/bash
black --check .
flake8 .
pytest tests/
EOF

chmod +x .git/hooks/pre-commit
```

## 코드 스타일 가이드

### Python 스타일

마사몽은 **PEP 8** 스타일 가이드를 따릅니다.

#### 포매팅

```bash
# Black 포매터 사용 (권장)
black .

# 또는 수동으로 확인
black --check .
```

#### 네이밍 컨벤션

```python
# ✅ 좋은 예
class UserActivityTracker:
    def get_top_users(self, limit: int) -> list[dict]:
        user_count = 0
        TOP_LIMIT = 10
        
# ❌ 나쁜 예
class user_activity_tracker:
    def GetTopUsers(self, Limit: int) -> list:
        UserCount = 0
        topLimit = 10
```

**규칙**:
- 클래스: `PascalCase`
- 함수/메서드: `snake_case`
- 변수: `snake_case`
- 상수: `UPPER_SNAKE_CASE`
- Private 멤버: `_leading_underscore`

#### 타입 힌트

모든 함수에 타입 힌트를 추가하세요:

```python
# ✅ 좋은 예
async def get_weather(self, location: str) -> dict[str, Any]:
    """날씨 정보를 조회합니다."""
    ...

# ❌ 나쁜 예
async def get_weather(self, location):
    ...
```

#### Docstring

Google 스타일 docstring을 사용하세요:

```python
def complex_function(param1: str, param2: int) -> bool:
    """함수의 간단한 설명.
    
    더 자세한 설명이 필요하면 여기에 작성합니다.
    여러 줄로 작성할 수 있습니다.
    
    Args:
        param1: 첫 번째 파라미터 설명
        param2: 두 번째 파라미터 설명
        
    Returns:
        반환값 설명
        
    Raises:
        ValueError: 언제 발생하는지 설명
        
    Examples:
        >>> complex_function("test", 42)
        True
    """
    ...
```

#### Import 순서

```python
# 1. 표준 라이브러리
import os
import sys
from pathlib import Path

# 2. 서드파티 라이브러리
import discord
from discord.ext import commands

# 3. 로컬 모듈
import config
from logger_config import logger
from utils.db import get_connection
```

### 비동기 코드 스타일

```python
# ✅ 좋은 예 - async/await 명확히 사용
async def fetch_data(self, user_id: int) -> dict:
    async with aiosqlite.connect(self.db_path) as db:
        async with db.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else {}

# ❌ 나쁜 예 - 블로킹 호출
def fetch_data(self, user_id: int) -> dict:
    conn = sqlite3.connect(self.db_path)  # 블로킹!
    cursor = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    return dict(row) if row else {}
```

### 에러 처리

```python
# ✅ 좋은 예 - 구체적인 예외 처리
try:
    result = await api_call()
except aiohttp.ClientError as e:
    logger.error(f"API 호출 실패: {e}", exc_info=True)
    return None
except asyncio.TimeoutError:
    logger.warning("API 호출 타임아웃")
    return None

# ❌ 나쁜 예 - 광범위한 예외 처리
try:
    result = await api_call()
except:  # 절대 사용 금지!
    return None
```

## 새 Cog 추가하기

### 1. Cog 파일 생성

`cogs/my_feature_cog.py`:

```python
# -*- coding: utf-8 -*-
"""내 새로운 기능을 제공하는 Cog입니다."""

import discord
from discord.ext import commands

from logger_config import logger


class MyFeatureCog(commands.Cog):
    """새로운 기능 Cog.
    
    이 Cog는 다음 기능을 제공합니다:
    - 기능 1
    - 기능 2
    """
    
    def __init__(self, bot: commands.Bot):
        """MyFeatureCog 초기화.
        
        Args:
            bot: Discord 봇 인스턴스
        """
        self.bot = bot
        logger.info("MyFeatureCog이 초기화되었습니다.")
    
    @commands.command(name="mycommand")
    async def my_command(self, ctx: commands.Context, arg: str):
        """명령어 설명.
        
        Args:
            ctx: 명령어 컨텍스트
            arg: 사용자 입력 인수
        """
        await ctx.send(f"받은 인수: {arg}")
    
    @commands.Cog.listener()
    async def on_ready(self):
        """봇이 준비되면 호출됩니다."""
        logger.info("MyFeatureCog이 준비되었습니다.")


async def setup(bot: commands.Bot):
    """Cog를 봇에 추가합니다.
    
    Args:
        bot: Discord 봇 인스턴스
    """
    await bot.add_cog(MyFeatureCog(bot))
```

### 2. main.py에 등록

```python
# main.py의 COGS 리스트에 추가
COGS = [
    "cogs.events",
    "cogs.ai_handler",
    "cogs.tools_cog",
    # ... 기존 cogs ...
    "cogs.my_feature_cog",  # 추가
]
```

### 3. 테스트 작성

`tests/test_my_feature_cog.py`:

```python
import pytest
from cogs.my_feature_cog import MyFeatureCog


@pytest.mark.asyncio
async def test_my_command(bot):
    """mycommand 테스트"""
    cog = MyFeatureCog(bot)
    # 테스트 코드 작성
    assert cog is not None
```

## 테스트 작성

### 테스트 구조

```
tests/
├── conftest.py          # pytest 설정 및 fixture
├── test_ai_handler_mentions.py
├── test_hybrid_search.py
└── test_my_new_feature.py
```

### Fixture 사용

```python
# conftest.py에 정의된 fixture 활용
@pytest.mark.asyncio
async def test_database_operation(test_db):
    """데이터베이스 테스트"""
    async with aiosqlite.connect(test_db) as db:
        await db.execute("INSERT INTO users (id, name) VALUES (?, ?)", (1, "Test"))
        await db.commit()
```

### Mock 사용

```python
from unittest.mock import AsyncMock, patch

@pytest.mark.asyncio
async def test_api_call():
    """외부 API 호출 테스트"""
    with patch('aiohttp.ClientSession.get') as mock_get:
        mock_get.return_value.__aenter__.return_value.json = AsyncMock(
            return_value={"status": "success"}
        )
        
        result = await my_api_function()
        assert result["status"] == "success"
```

### 테스트 실행

```bash
# 전체 테스트
pytest

# 특정 파일
pytest tests/test_my_feature.py

# 커버리지 확인
pytest --cov=. --cov-report=html
```

## Pull Request 절차

### 1. Branch 생성

```bash
# upstream에서 최신 코드 가져오기
git fetch upstream
git checkout main
git merge upstream/main

# 새 브랜치 생성
git checkout -b feature/my-awesome-feature
```

### 2. 코드 작성

- 작은 단위로 커밋
- 명확한 커밋 메시지 작성

```bash
git add .
git commit -m "feat: Add weather alert feature

- 날씨 알림 기능 추가
- 설정 가능한 임계값
- 테스트 추가"
```

### 커밋 메시지 컨벤션

```
<타입>: <제목>

<본문>

<푸터>
```

**타입**:
- `feat`: 새 기능
- `fix`: 버그 수정
- `docs`: 문서 변경
- `style`: 코드 포맷팅 (로직 변경 없음)
- `refactor`: 리팩토링
- `test`: 테스트 추가/수정
- `chore`: 빌드, 설정 변경

**예시**:
```
feat: Add hybrid search reranking

- Cross-Encoder 리랭킹 추가
- config에 RERANK_ENABLED 옵션 추가
- 테스트 케이스 작성

Closes #123
```

### 3. 테스트 및 검증

```bash
# 코드 포맷 확인
black --check .

# Lint 확인
flake8 .

# 타입 체크
mypy .

# 테스트 실행
pytest
```

### 4. Push 및 PR 생성

```bash
git push origin feature/my-awesome-feature
```

GitHub에서:
1. "Pull Request" 클릭
2. 템플릿에 따라 내용 작성
3. Reviewer 지정 (선택)
4. Label 추가 (선택)

### PR 템플릿

```markdown
## 변경 사항
- 변경 1
- 변경 2

## 변경 이유
왜 이 변경이 필요한지 설명

## 테스트
- [ ] 단위 테스트 추가/수정
- [ ] 통합 테스트 실행
- [ ] 수동 테스트 완료

## 체크리스트
- [ ] 코드가 스타일 가이드를 따름
- [ ] 자기 리뷰 완료
- [ ] 주석 추가 (복잡한 부분)
- [ ] 문서 업데이트 (필요시)
- [ ] 테스트 통과
- [ ] Breaking change 없음 (또는 명시함)

## 스크린샷 (선택)
변경 사항을 보여주는 스크린샷
```

### 5. 리뷰 대응

- 리뷰어의 피드백에 성실히 응답
- 요청된 변경사항 반영
- 추가 커밋은 같은 브랜치에

```bash
git add .
git commit -m "review: Apply feedback from @reviewer"
git push origin feature/my-awesome-feature
```

## 코드 리뷰 가이드라인

### 리뷰어

- [ ] 코드가 명확하고 이해하기 쉬운가?
- [ ] 에러 처리가 적절한가?
- [ ] 테스트가 충분한가?
- [ ] 성능 이슈가 없는가?
- [ ] 보안 취약점이 없는가?
- [ ] 문서가 업데이트되었는가?

### 작성자

- 방어적이지 말고 피드백을 환영
- 이해되지 않는 피드백은 질문
- 모든 코드를 설명할 수 있어야 함

## 버전 관리

[Semantic Versioning](https://semver.org/)을 따릅니다:

- `MAJOR`: 호환되지 않는 API 변경
- `MINOR`: 하위 호환되는 기능 추가
- `PATCH`: 하위 호환되는 버그 수정

## 라이선스

기여한 코드는 프로젝트의 MIT 라이선스를 따릅니다.

## 질문이 있나요?

- GitHub Issues에 질문 등록
- Discord 서버에서 논의 (있는 경우)
- 프로젝트 메인테이너에게 연락

---

다시 한번 기여해 주셔서 감사합니다! 🎉

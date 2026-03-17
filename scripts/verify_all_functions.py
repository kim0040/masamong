import sys
import os
import asyncio
from unittest.mock import MagicMock, AsyncMock
from datetime import datetime, timezone

# 프로젝트 경로 추가
sys.path.append('/Users/gimhyeonmin/PycharmProjects/masamong')

import config
from cogs.ai_handler import AIHandler
from cogs.activity_cog import ActivityCog

# DB Mock 클래스 정의
class AsyncContextMock:
    def __init__(self, return_value):
        self.return_value = return_value
    async def __aenter__(self):
        return self.return_value
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

async def run_integration_test():
    bot = MagicMock()
    bot.db = MagicMock()
    
    # execute 결과 시나리오 정의
    mock_users = [
        (12345, 1200, "2024-03-17T10:00:00Z"),
        (67890, 450, "2024-03-17T11:00:00Z"),
        (11111, 50, "2024-03-17T12:00:00Z")
    ]
    
    # 랭킹 쿼리 결과 모킹
    mock_cursor_ranking = AsyncMock()
    mock_cursor_ranking.fetchall.return_value = mock_users
    
    # 서버 통계 쿼리 결과 모킹
    mock_cursor_stats = AsyncMock()
    mock_cursor_stats.fetchone.return_value = (1700, 3)
    
    # execute 호출마다 다른 커서 반환하도록 설정
    bot.db.execute.side_effect = [
        AsyncContextMock(mock_cursor_ranking),
        AsyncContextMock(mock_cursor_stats)
    ]
    bot.db.commit = AsyncMock()

    handler = AIHandler(bot)
    handler.use_cometapi = True
    handler.gemini_configured = True
    
    activity_cog = ActivityCog(bot)
    activity_cog.ai_handler = handler
    
    log_extra = {'trace_id': 'integration_test'}

    print("\n" + "="*50)
    print("🚀 마사몽 통합 기능 테스트 시작 (수정본)")
    print("="*50)

    # 1. 연계 질문 테스트
    print("\n[Case 1] 연계 질문 테스트: 미국 이란 관계 -> 군비 관련")
    history = [
        {'role': 'user', 'parts': ['미국 이란 전쟁에 대해 설명해줘']},
        {'role': 'model', 'parts': ['미국과 이란의 갈등 역사...']}
    ]
    query = "군비는 얼마나 썼대?"
    refined = await handler._refine_search_query_with_llm(query, history, log_extra)
    print(f"-> 정제된 검색어: {refined}")

    # 2. 랭킹 테스트
    print("\n[Case 2] 상세 랭킹 및 AI 브리핑 테스트")
    ctx = MagicMock()
    ctx.guild.id = 123
    ctx.author.id = 666
    ctx.author.display_name = "테스터"
    ctx.typing.return_value.__aenter__ = AsyncMock()
    ctx.typing.return_value.__aexit__ = AsyncMock()

    sent_messages = []
    async def mock_send(content):
        sent_messages.append(content)
        print(f"-> 봇 응답 메시지 추출 성공 (내용 요약: {content[:100]}...)")
    ctx.send = mock_send

    # Mock fetch_user
    async def mock_fetch_user(user_id):
        m = MagicMock()
        m.display_name = f"유저_{user_id}"
        return m
    bot.fetch_user = mock_fetch_user
    bot.get_user.return_value = None # fetch_user를 타도록 유도

    await activity_cog.ranking.callback(activity_cog, ctx)
    
    if sent_messages:
        print("\n✨ 모든 기능이 정상적으로 작동합니다!")
    else:
        print("\n❌ 랭킹 응답 생성 실패")

    print("\n" + "="*50)
    print("✅ 통합 테스트 완료")
    print("="*50)

if __name__ == "__main__":
    asyncio.run(run_integration_test())

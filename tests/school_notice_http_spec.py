"""학교 공개 게시판 HTTP 요청의 개인정보 최소화 명세."""

import aiohttp
import pytest

from school_notice.http import AsyncFetcher


@pytest.mark.asyncio
async def test_public_school_fetcher_has_no_persistent_cookie_or_user_identifier():
    async with AsyncFetcher(max_requests=1) as fetcher:
        session = fetcher._session
        assert session is not None
        assert isinstance(session.cookie_jar, aiohttp.DummyCookieJar)
        assert session.headers["DNT"] == "1"
        assert "Discord" not in session.headers["User-Agent"]
        assert "Referer" not in session.headers
        assert session.trust_env is False

"""학교 공지 분석 캐시와 LLM 장애 호출 상한 명세."""

from datetime import date
from types import SimpleNamespace

import pytest

from school_notice.analysis import AnalyzerService


class _Repository:
    def __init__(self):
        self.cache = {}

    def get_analysis(self, notice_id, content_hash, analyzer_version):
        return self.cache.get((notice_id, content_hash, analyzer_version))

    def save_analysis(
        self,
        *,
        notice_id,
        content_hash,
        analyzer_version,
        provider,
        model,
        analysis,
    ):
        self.cache[(notice_id, content_hash, analyzer_version)] = analysis


class _FailingLLM:
    configured = True
    model = "deepseek-v4-flash"

    def __init__(self):
        self.calls = 0

    async def complete_json(self, **_kwargs):
        self.calls += 1
        raise TimeoutError("provider timeout")


def _notice():
    return SimpleNamespace(
        title="2027학년도 수강 신청 안내",
        body_text="수강 신청은 2026년 8월 10일까지 완료해야 합니다.",
        published_text="2026-07-29",
        content_hash="content-v1",
        candidate=SimpleNamespace(category="학사"),
        attachment_extractions=[],
        warnings=(),
        signals=SimpleNamespace(
            audiences=("학부생",),
            topics=("수강",),
            actions=("신청",),
        ),
    )


@pytest.mark.asyncio
async def test_failed_analysis_llm_is_called_once_per_notice_and_day():
    repository = _Repository()
    llm = _FailingLLM()
    service = AnalyzerService(repository, llm)

    first = await service.analyze(
        notice_id=1,
        notice=_notice(),
        run_date=date(2026, 7, 29),
    )
    second = await service.analyze(
        notice_id=1,
        notice=_notice(),
        run_date=date(2026, 7, 29),
    )
    await service.analyze(
        notice_id=1,
        notice=_notice(),
        run_date=date(2026, 7, 30),
    )

    assert first == second
    assert first["analysis_source"] == "rules"
    assert any(
        warning.startswith("llm_fallback:")
        for warning in first["warnings"]
    )
    assert llm.calls == 2

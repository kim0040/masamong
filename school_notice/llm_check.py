from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .analysis import AnalyzerService
from .llm import DeepSeekClient, DeepSeekSettings
from .models import Notice, NoticeCandidate
from .personalization import score_notice, validate_profile
from .signals import extract_signals
from .storage import NoticeRepository


def _sample_notice(run_key: str) -> Notice:
    title = "소프트웨어공학과 3학년 편입생 장학 신청"
    body = (
        "소프트웨어공학과 학부 3학년 편입생 대상이며 신청 가능합니다. "
        "직전학기 평점 3.0 이상인 학생은 2026. 8. 5.까지 지원서를 "
        "반드시 제출해야 합니다."
    )
    digest = hashlib.sha256(f"{title}\n{body}".encode()).hexdigest()
    return Notice(
        candidate=NoticeCandidate(
            source_id="deepseek_contract_check",
            external_id=run_key,
            title=title,
            url="https://example.invalid/deepseek-contract-check",
            published_text="2026. 7. 27.",
            source_university="테스트대학교",
            source_board="소프트웨어공학과 공지",
            source_tags=("department:소프트웨어공학과",),
        ),
        title=title,
        body_text=body,
        published_text="2026. 7. 27.",
        author="테스트",
        attachments=(),
        inline_images=(),
        signals=extract_signals(title, body),
        base_content_hash=digest,
        content_hash=digest,
    )


def _profile(
    *,
    user_key: str,
    department: str,
    grade: int,
    admission_type: str,
) -> dict[str, object]:
    return validate_profile(
        {
            "user_key": user_key,
            "school_id": "deepseek_check",
            "department": department,
            "degree_level": "undergraduate",
            "grade": grade,
            "admission_type": admission_type,
            "enrollment_status": "enrolled",
            "career_interests": ["장학"],
            "muted_topics": [],
            "timezone": "Asia/Seoul",
        }
    )


async def run_deepseek_check(
    *,
    database: str | Path,
    output_dir: str | Path,
) -> tuple[dict[str, object], Path]:
    settings = DeepSeekSettings.from_environment()
    if not settings.api_key:
        raise RuntimeError(
            "SCHOOL_NOTICE_LLM_API_KEY, OPENROUTER_API_KEY, COMETAPI_KEY 또는 "
            "DEEPSEEK_API_KEY가 환경변수에 없습니다. 새 키를 환경변수로 "
            "주입한 뒤 다시 실행하세요."
        )
    settings = replace(
        settings,
        max_calls_per_run=min(settings.max_calls_per_run, 2),
        max_retries=0,
    )
    now = datetime.now(UTC)
    run_key = now.strftime("%Y%m%dT%H%M%S%fZ")
    notice = _sample_notice(run_key)
    eligible_profile = _profile(
        user_key=f"eligible-{run_key}",
        department="소프트웨어공학과",
        grade=3,
        admission_type="transfer",
    )
    ineligible_profile = _profile(
        user_key=f"ineligible-{run_key}",
        department="경영학과",
        grade=1,
        admission_type="regular",
    )
    usage_date = datetime.now(ZoneInfo("Asia/Seoul")).date()

    with NoticeRepository(database) as repository:
        persisted = repository.upsert_notice("deepseek_check", notice)
        client = DeepSeekClient(repository, settings)
        analyzer = AnalyzerService(repository, client)
        try:
            first = await analyzer.analyze(
                notice_id=persisted.notice_id,
                notice=notice,
                run_date=usage_date,
            )
            calls_after_first = client.run_calls
            second = await analyzer.analyze(
                notice_id=persisted.notice_id,
                notice=notice,
                run_date=usage_date,
            )
        finally:
            await client.close()
        eligible = score_notice(
            profile=eligible_profile,
            profile_version=1,
            notice_id=persisted.notice_id,
            notice_payload=notice.as_dict(),
            analysis=first,
            feedback_events=[],
            today=usage_date,
        )
        ineligible = score_notice(
            profile=ineligible_profile,
            profile_version=1,
            notice_id=persisted.notice_id,
            notice_payload=notice.as_dict(),
            analysis=first,
            feedback_events=[],
            today=usage_date,
        )
        usage = repository.api_usage(
            usage_date=usage_date,
            api_type=DeepSeekClient.API_TYPE,
        )

    checks = {
        "deepseek_analysis": first.get("analysis_source") == "deepseek",
        "cache_reused": second == first and client.run_calls == calls_after_first,
        "bounded_calls": 1 <= client.run_calls <= 2,
        "matching_user_visible": eligible["band"] != "hidden",
        "mismatching_user_hidden": ineligible["band"] == "hidden",
    }
    report: dict[str, object] = {
        "status": "passed" if all(checks.values()) else "failed",
        "checked_at": now.isoformat(),
        "model": settings.model,
        "checks": checks,
        "api_usage": usage,
        "analysis": {
            "summary": first.get("summary"),
            "topics": first.get("topics"),
            "actions": first.get("actions"),
            "eligibility_rules": first.get("eligibility_rules"),
            "warnings": first.get("warnings"),
        },
        "personalization": {
            "matching_user": eligible,
            "mismatching_user": ineligible,
        },
    }
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    report_path = destination / f"deepseek-check-{run_key}.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report, report_path

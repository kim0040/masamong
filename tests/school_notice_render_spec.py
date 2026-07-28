"""digest → Discord Embed 표현 명세.

점수는 확률이 아니라 설명 가능한 우선순위이므로 근거가 함께 보여야 하고,
확정할 수 없는 자격과 수집 실패는 반드시 사용자에게 드러나야 한다.
"""

import json
from datetime import date
from pathlib import Path

import discord
import pytest

from utils.school_notice_contract import load_digest, parse_digest
from utils.school_notice_render import (
    build_item_embed,
    chunk_embeds,
    render_digest,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TODAY = date(2026, 7, 27)


def _digest(name: str = "school_notice_digest.json"):
    return load_digest(FIXTURES / name)


def _all_text(embed) -> str:
    parts = [embed.title or "", embed.description or ""]
    parts.extend(f"{field.name} {field.value}" for field in embed.fields)
    if embed.footer:
        parts.append(embed.footer.text or "")
    return "\n".join(parts)


def test_header_summarizes_bands():
    embeds = render_digest(_digest(), max_items=10, today=TODAY)
    header = embeds[0]

    assert "2026-07-27" in header.title
    assert "지금 확인" in header.description


def test_empty_digest_says_no_notices_rather_than_failing():
    embeds = render_digest(_digest("school_notice_digest_empty.json"), today=TODAY)

    assert len(embeds) == 1
    assert "새 공지가 없습니다" in embeds[0].description


def test_stale_collection_produces_explicit_warning():
    # "오늘 새 공지 없음"과 "오늘 확인 실패"를 구분하는 유일한 신호다.
    embeds = render_digest(_digest("school_notice_digest_stale.json"), today=TODAY)
    header_text = _all_text(embeds[0])

    assert "수집 상태 경고" in header_text
    assert "오래된 공지가 섞여 있을 수 있습니다" in header_text
    assert "jbnu_campus" in header_text


def test_healthy_collection_has_no_warning():
    embeds = render_digest(_digest(), today=TODAY)

    assert "수집 상태 경고" not in _all_text(embeds[0])


def test_reasons_are_always_shown():
    digest = _digest()
    item = digest.visible_items()[0]

    text = _all_text(build_item_embed(item, today=TODAY))

    assert "왜 추천됐나" in text
    for reason in item.reasons[:3]:
        assert reason in text


def test_unknown_eligibility_tells_user_to_check_the_original():
    digest = _digest("school_notice_digest_unknown.json")
    item = digest.visible_items()[0]

    text = _all_text(build_item_embed(item, today=TODAY))

    assert "확인 필요" in text
    assert "원문을 직접 확인" in text


def test_short_or_image_only_body_is_disclosed():
    payload = json.loads((FIXTURES / "school_notice_digest.json").read_text(encoding="utf-8"))
    payload["items"][0]["notice"]["warnings"] = ["body_too_short:0<40"]
    payload["items"][0]["analysis"]["summary"] = payload["items"][0]["notice"]["title"]
    item = parse_digest(payload).items[0]

    text = _all_text(build_item_embed(item, today=TODAY))

    assert "본문 확인" in text
    assert "이미지 중심" in text
    assert "원문 이미지와 첨부파일" in text


def test_inferred_year_deadline_is_marked():
    digest = _digest("school_notice_digest_unknown.json")
    item = digest.visible_items()[0]

    text = _all_text(build_item_embed(item, today=TODAY))

    assert "연도 추론값" in text


def test_deadline_shows_remaining_days():
    payload = json.loads((FIXTURES / "school_notice_digest.json").read_text(encoding="utf-8"))
    payload["items"][0]["score"]["deadline"] = "2026-07-30"
    item = parse_digest(payload).items[0]

    text = _all_text(build_item_embed(item, today=TODAY))

    assert "3일 남음" in text


def test_expired_deadline_is_labelled():
    payload = json.loads((FIXTURES / "school_notice_digest.json").read_text(encoding="utf-8"))
    payload["items"][0]["score"]["deadline"] = "2026-07-20"
    item = parse_digest(payload).items[0]

    text = _all_text(build_item_embed(item, today=TODAY))

    assert "지남" in text


def test_body_text_is_not_used_as_description():
    # 본문 전문은 수천 자라 Embed 제한을 넘고 읽기도 어렵다.
    payload = json.loads((FIXTURES / "school_notice_digest.json").read_text(encoding="utf-8"))
    payload["items"][0]["notice"]["body_text"] = "본문전문" * 500
    payload["items"][0]["analysis"]["summary"] = "짧은 요약"
    item = parse_digest(payload).items[0]

    embed = build_item_embed(item, today=TODAY)

    assert embed.description == "짧은 요약"
    assert "본문전문" not in embed.description


def test_long_fields_are_truncated_within_discord_limits():
    payload = json.loads((FIXTURES / "school_notice_digest.json").read_text(encoding="utf-8"))
    payload["items"][0]["notice"]["title"] = "제" * 900
    payload["items"][0]["analysis"]["summary"] = "요" * 9000
    payload["items"][0]["score"]["reasons"] = ["근거" * 400] * 5
    item = parse_digest(payload).items[0]

    embed = build_item_embed(item, today=TODAY)

    assert len(embed.title) <= 256
    assert len(embed.description) <= 4096
    for field in embed.fields:
        assert len(field.value) <= 1024
    assert len(embed) <= 6000


def test_max_items_limits_output_and_is_disclosed():
    digest = _digest()
    embeds = render_digest(digest, max_items=1, today=TODAY)

    assert len(embeds) == 2  # header + 1
    assert "상위 1건만 표시" in embeds[0].description


def test_required_notice_is_marked_in_title():
    payload = json.loads((FIXTURES / "school_notice_digest.json").read_text(encoding="utf-8"))
    payload["items"][0]["analysis"]["required"] = True
    item = parse_digest(payload).items[0]

    assert build_item_embed(item, today=TODAY).title.startswith("[필수]")


def test_attachments_are_linked_not_downloaded():
    digest = _digest()
    item = next(i for i in digest.visible_items() if i.attachments)

    text = _all_text(build_item_embed(item, today=TODAY))

    assert "첨부" in text
    assert item.attachments[0]["url"] in text


def test_duplicate_sources_are_disclosed():
    payload = json.loads((FIXTURES / "school_notice_digest.json").read_text(encoding="utf-8"))
    payload["items"][0]["duplicate_sources"] = [
        {"source_id": "jbnu_campus", "url": "https://example.ac.kr/a"}
    ]
    item = parse_digest(payload).items[0]

    text = _all_text(build_item_embed(item, today=TODAY))

    assert "다른 게시판에도 게시됨" in text
    assert "jbnu_campus" in text


def test_chunking_respects_discord_embed_limit():
    embeds = [build_item_embed(_digest().visible_items()[0])] * 23

    groups = chunk_embeds(embeds)

    assert all(len(group) <= 10 for group in groups)
    assert all(sum(len(embed) for embed in group) <= 6000 for group in groups)
    assert sum(len(group) for group in groups) == 23


def test_render_rejects_nonpositive_max_items():
    with pytest.raises(ValueError):
        render_digest(_digest(), max_items=0)


def test_item_embed_is_fitted_to_total_discord_text_limit():
    payload = json.loads((FIXTURES / "school_notice_digest.json").read_text(encoding="utf-8"))
    item_payload = payload["items"][0]
    item_payload["analysis"]["summary"] = "요약" * 9000
    item_payload["analysis"]["topics"] = ["주제" * 900] * 5
    item_payload["score"]["reasons"] = ["근거" * 900] * 5
    item_payload["notice"]["attachments"] = [
        {"name": "첨부" * 300, "url": "https://example.ac.kr/file"}
    ]
    item_payload["duplicate_sources"] = [
        {"source_id": "other", "url": "https://example.ac.kr/other"}
    ]

    embed = build_item_embed(parse_digest(payload).items[0], today=TODAY)

    assert len(embed) <= 6000
    assert "왜 추천됐나" in _all_text(embed)


def test_chunking_splits_on_total_text_even_below_ten_embeds():
    embeds = [discord.Embed(description="x" * 3500) for _ in range(3)]

    groups = chunk_embeds(embeds)

    assert [len(group) for group in groups] == [1, 1, 1]
    assert all(sum(len(embed) for embed in group) <= 6000 for group in groups)


def test_chunking_rejects_a_single_oversized_embed():
    oversized = discord.Embed(description="x" * 4096)
    oversized.add_field(name="a", value="y" * 1024)
    oversized.add_field(name="b", value="z" * 1024)

    with pytest.raises(ValueError, match="6000"):
        chunk_embeds([oversized])


@pytest.mark.parametrize("per_message", [0, -1, True, 1.5])
def test_chunking_rejects_invalid_group_size(per_message):
    with pytest.raises(ValueError):
        chunk_embeds([discord.Embed(title="ok")], per_message=per_message)

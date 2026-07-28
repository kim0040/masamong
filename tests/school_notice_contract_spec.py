"""digest 계약 검증 명세.

digest는 봇이 아니라 별도 batch가 만든 파일이므로, 계약이 깨진 입력을 조용히
부분 해석하지 않고 거부하는지가 핵심이다.
"""

import copy
import json
from datetime import date
from pathlib import Path

import pytest

from utils.school_notice_contract import (
    DigestContractError,
    MAX_DIGEST_FILE_BYTES,
    MAX_DIGEST_ITEMS,
    load_digest,
    parse_digest,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _payload(name: str = "school_notice_digest.json") -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_real_core_digest_parses():
    digest = load_digest(FIXTURES / "school_notice_digest.json")

    assert digest.schema_version == 1
    assert digest.digest_date == date(2026, 7, 27)
    assert digest.items
    first = digest.visible_items()[0]
    assert first.source_id
    assert first.external_id
    assert first.url.startswith("https://")
    assert 0 <= first.score <= 100


def test_unknown_schema_version_is_rejected():
    # 알 수 없는 스키마를 부분 해석하면 잘못된 마감·자격을 보여줄 수 있다.
    with pytest.raises(DigestContractError, match="schema_version"):
        load_digest(FIXTURES / "school_notice_digest_bad_schema.json")


def test_empty_digest_is_valid_and_reports_empty():
    digest = load_digest(FIXTURES / "school_notice_digest_empty.json")

    assert digest.items == ()
    assert digest.is_empty
    assert digest.collection_health is not None
    assert digest.collection_health.has_problem is False


def test_stale_health_is_surfaced():
    digest = load_digest(FIXTURES / "school_notice_digest_stale.json")
    health = digest.collection_health

    assert health is not None
    assert health.may_include_stale_notices is True
    assert health.has_problem is True
    assert [item.source_id for item in health.failed_sources()] == ["jbnu_campus"]


def test_unknown_eligibility_is_flagged_for_manual_check():
    digest = load_digest(FIXTURES / "school_notice_digest_unknown.json")
    item = digest.visible_items()[0]

    assert item.eligibility == "UNKNOWN"
    assert item.needs_manual_check is True
    assert item.has_inferred_deadline is True


def test_items_are_ordered_by_band_then_score():
    digest = load_digest(FIXTURES / "school_notice_digest.json")
    visible = digest.visible_items()

    bands = [item.band for item in visible]
    assert bands == sorted(bands, key=lambda band: ["action", "opportunity", "reference"].index(band))
    action_scores = [item.score for item in visible if item.band == "action"]
    assert action_scores == sorted(action_scores, reverse=True)


def test_hidden_items_are_not_visible():
    payload = _payload()
    payload["items"][0]["score"]["band"] = "hidden"

    digest = parse_digest(payload)

    assert len(digest.items) == len(payload["items"])
    assert all(item.band != "hidden" for item in digest.visible_items())


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda p: p.pop("user_key"), "user_key"),
        (lambda p: p.update(user_key="  "), "user_key"),
        (lambda p: p.pop("date"), "date"),
        (lambda p: p.update(date="2026-13-01"), "날짜"),
        (lambda p: p.update(items={}), "items"),
        (lambda p: p.update(collection_health=[]), "collection_health"),
    ],
)
def test_malformed_top_level_is_rejected(mutate, expected):
    payload = _payload()
    mutate(payload)

    with pytest.raises(DigestContractError, match=expected):
        parse_digest(payload)


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda i: i["score"].update(band="urgent"), "band"),
        (lambda i: i["score"].update(eligibility="MAYBE"), "eligibility"),
        (lambda i: i["score"].update(score=140), "0~100"),
        (lambda i: i["analysis"].update(urgency="extreme"), "urgency"),
        (lambda i: i.update(change="deleted"), "change"),
        (lambda i: i["score"].update(deadline="2026/09/01"), "날짜"),
        (lambda i: i["notice"]["candidate"].pop("source_id"), "source_id"),
        (lambda i: i.pop("notice_id"), "notice_id"),
    ],
)
def test_malformed_item_is_rejected(mutate, expected):
    payload = _payload()
    mutate(payload["items"][0])

    with pytest.raises(DigestContractError, match=expected):
        parse_digest(payload)


def test_missing_file_is_a_contract_error():
    with pytest.raises(DigestContractError, match="읽을 수 없습니다"):
        load_digest(FIXTURES / "does-not-exist.json")


def test_invalid_json_is_a_contract_error(tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")

    with pytest.raises(DigestContractError, match="유효한 JSON"):
        load_digest(broken)


def test_dates_without_valid_value_are_dropped_not_invented():
    payload = _payload()
    payload["items"][0]["analysis"]["dates"] = [{"kind": "deadline"}]

    digest = parse_digest(payload)

    # 날짜를 만들어내지 않는다.
    assert digest.items[0].dates == ()


def test_feedback_key_uses_stable_identifiers():
    digest = load_digest(FIXTURES / "school_notice_digest.json")
    item = digest.visible_items()[0]

    assert item.feedback_key() == (item.source_id, item.external_id)


def test_expected_schema_version_is_configurable():
    payload = _payload("school_notice_digest_bad_schema.json")

    digest = parse_digest(payload, expected_schema_version=99)

    assert digest.schema_version == 99


def test_file_larger_than_contract_limit_is_rejected_before_json_parse(tmp_path):
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (MAX_DIGEST_FILE_BYTES + 1))

    with pytest.raises(DigestContractError, match="너무 큽니다"):
        load_digest(oversized)


def test_too_many_items_are_rejected_before_item_parsing():
    payload = _payload()
    payload["items"] = [payload["items"][0]] * (MAX_DIGEST_ITEMS + 1)

    with pytest.raises(DigestContractError, match="항목이 너무 많습니다"):
        parse_digest(payload)


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda p: p["items"][0]["analysis"].update(required="false"), "boolean"),
        (
            lambda p: p["items"][0]["score"].update(mandatory_protected=1),
            "boolean",
        ),
        (
            lambda p: p["collection_health"].update(
                may_include_stale_notices="false"
            ),
            "boolean",
        ),
        (
            lambda p: p["items"][0]["analysis"]["dates"][0].update(
                inferred_year="false"
            ),
            "boolean",
        ),
    ],
)
def test_boolean_fields_require_actual_json_booleans(mutate, expected):
    payload = _payload()
    mutate(payload)

    with pytest.raises(DigestContractError, match=expected):
        parse_digest(payload)


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda p: p["collection_health"].update(failed="0"), "정수"),
        (lambda p: p["items"][0].update(revision_count="1"), "정수"),
        (lambda p: p["summary"].update(action="2"), "정수"),
        (lambda p: p["items"][0]["score"].update(score=float("nan")), "유한"),
    ],
)
def test_invalid_numbers_always_raise_contract_error(mutate, expected):
    payload = _payload()
    mutate(payload)

    with pytest.raises(DigestContractError, match=expected):
        parse_digest(payload)


def test_duplicate_notice_ids_are_rejected():
    payload = _payload()
    payload["items"][1]["notice_id"] = payload["items"][0]["notice_id"]

    with pytest.raises(DigestContractError, match="중복 notice_id"):
        parse_digest(payload)


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (
            lambda p: p["items"][0]["notice"]["candidate"].update(source_id=""),
            "source_id",
        ),
        (
            lambda p: p["items"][0]["notice"]["candidate"].update(external_id=" "),
            "external_id",
        ),
        (lambda p: p["items"][0].update(dedup_key=""), "dedup_key"),
        (lambda p: p["items"][0]["notice"].update(title=""), "title"),
    ],
)
def test_empty_item_identifiers_are_rejected(mutate, expected):
    payload = _payload()
    mutate(payload)
    if expected == "title":
        payload["items"][0]["notice"]["candidate"]["title"] = ""

    with pytest.raises(DigestContractError, match=expected):
        parse_digest(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["items"][0]["notice"]["candidate"].update(
            url="javascript:alert(1)"
        ),
        lambda p: p["items"][0]["notice"]["attachments"][0].update(
            url="file:///tmp/a"
        ),
        lambda p: p["items"][0].update(
            duplicate_sources=[{"source_id": "other", "url": "/relative"}]
        ),
    ],
)
def test_urls_must_be_absolute_http_or_https(mutate):
    payload = _payload()
    mutate(payload)

    with pytest.raises(DigestContractError, match="http/https"):
        parse_digest(payload)


def test_consumed_strings_have_explicit_length_limits():
    payload = _payload()
    payload["items"][0]["analysis"]["summary"] = "x" * 20_001

    with pytest.raises(DigestContractError, match="문자열이 너무 깁니다"):
        parse_digest(payload)


def test_digest_can_be_bound_to_expected_owner_and_date():
    payload = _payload()

    digest = parse_digest(
        payload,
        expected_user_key=payload["user_key"],
        expected_digest_date=date.fromisoformat(payload["date"]),
    )

    assert digest.user_key == payload["user_key"]


@pytest.mark.parametrize(
    "expected_user_key, expected_date, expected",
    [
        ("discord-other", date(2026, 7, 27), "user_key"),
        ("discord-100000000000000001", date(2026, 7, 28), "date"),
    ],
)
def test_digest_owner_or_date_mismatch_is_rejected(
    expected_user_key, expected_date, expected
):
    with pytest.raises(DigestContractError, match=expected):
        load_digest(
            FIXTURES / "school_notice_digest.json",
            expected_user_key=expected_user_key,
            expected_digest_date=expected_date,
        )

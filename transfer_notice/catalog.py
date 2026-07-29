from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from urllib.parse import urlparse

import soupsieve


DEFAULT_CATALOG = Path(__file__).with_name("sources.json")


@dataclass(frozen=True)
class TransferSource:
    source_id: str
    university: str
    list_url: str
    official_url: str
    allowed_hosts: tuple[str, ...]
    link_selector: str
    title_selector: str | None
    title_attribute: str | None
    url_value_selector: str | None
    url_value_attribute: str
    url_value_regex: str | None
    detail_url_template: str | None
    href_regex: str | None
    title_mode: str
    toeic_note: str


@dataclass(frozen=True)
class ManualTransferSource:
    source_id: str
    university: str
    official_url: str
    reason: str


def _as_source(raw: dict) -> TransferSource:
    return TransferSource(
        source_id=str(raw["id"]),
        university=str(raw["university"]),
        list_url=str(raw["list_url"]),
        official_url=str(raw.get("official_url") or raw["list_url"]),
        allowed_hosts=tuple(str(item).casefold() for item in raw["allowed_hosts"]),
        link_selector=str(raw.get("link_selector") or "a[href]"),
        title_selector=(
            str(raw["title_selector"]) if raw.get("title_selector") else None
        ),
        title_attribute=(
            str(raw["title_attribute"]) if raw.get("title_attribute") else None
        ),
        url_value_selector=(
            str(raw["url_value_selector"])
            if raw.get("url_value_selector")
            else None
        ),
        url_value_attribute=str(raw.get("url_value_attribute") or "href"),
        url_value_regex=(
            str(raw["url_value_regex"]) if raw.get("url_value_regex") else None
        ),
        detail_url_template=(
            str(raw["detail_url_template"])
            if raw.get("detail_url_template")
            else None
        ),
        href_regex=str(raw["href_regex"]) if raw.get("href_regex") else None,
        title_mode=str(raw.get("title_mode") or "transfer_board"),
        toeic_note=str(raw.get("toeic_note") or "모집단위별 최신 요강 확인"),
    )


def _validate(source: TransferSource) -> None:
    if not re.fullmatch(r"[a-z0-9_]{2,40}", source.source_id):
        raise ValueError(f"잘못된 편입 공지 source id: {source.source_id!r}")
    if source.title_mode not in {"transfer_board", "mixed_board"}:
        raise ValueError(f"{source.source_id}: 잘못된 title_mode")
    if not source.allowed_hosts:
        raise ValueError(f"{source.source_id}: allowed_hosts가 비어 있습니다.")
    for name, url in (
        ("list_url", source.list_url),
        ("official_url", source.official_url),
    ):
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.hostname.casefold() not in source.allowed_hosts
        ):
            raise ValueError(
                f"{source.source_id}: {name} 호스트가 allowed_hosts에 없습니다."
            )
    try:
        soupsieve.compile(source.link_selector)
        if source.title_selector:
            soupsieve.compile(source.title_selector)
        if source.url_value_selector:
            soupsieve.compile(source.url_value_selector)
        if source.href_regex:
            re.compile(source.href_regex)
        if source.url_value_regex:
            compiled = re.compile(source.url_value_regex)
            if compiled.groups != 1:
                raise ValueError("url_value_regex는 캡처 그룹이 정확히 하나여야 합니다.")
    except (re.error, soupsieve.SelectorSyntaxError) as exc:
        raise ValueError(
            f"{source.source_id}: selector/regex 설정 오류: {exc}"
        ) from exc
    if source.detail_url_template:
        if source.detail_url_template.count("{value}") != 1:
            raise ValueError(
                f"{source.source_id}: detail_url_template에는 {{value}}가 "
                "정확히 한 번 있어야 합니다."
            )
        try:
            candidate_url = source.detail_url_template.format(value="1")
        except (KeyError, ValueError) as exc:
            raise ValueError(
                f"{source.source_id}: detail_url_template 형식 오류"
            ) from exc
        parsed_candidate = urlparse(candidate_url)
        if (
            parsed_candidate.scheme
            and (
                not parsed_candidate.hostname
                or parsed_candidate.hostname.casefold() not in source.allowed_hosts
            )
        ):
            raise ValueError(
                f"{source.source_id}: detail_url_template 호스트가 허용되지 않습니다."
            )


def load_transfer_sources(
    path: str | Path | None = None,
) -> dict[str, TransferSource]:
    catalog_path = Path(path) if path else DEFAULT_CATALOG
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list):
        raise ValueError("편입 공지 catalog의 sources는 배열이어야 합니다.")
    sources = [_as_source(raw) for raw in raw_sources]
    if len(sources) != 20:
        raise ValueError(
            f"자동 편입 공지 catalog는 정확히 20개교여야 합니다: {len(sources)}"
        )
    result: dict[str, TransferSource] = {}
    for source in sources:
        _validate(source)
        if source.source_id in result:
            raise ValueError(f"중복 편입 공지 source id: {source.source_id}")
        result[source.source_id] = source
    return result


def load_manual_transfer_sources(
    path: str | Path | None = None,
) -> dict[str, ManualTransferSource]:
    """robots 정책 등으로 자동 수집하지 않는 공식 바로가기 목록을 읽는다."""
    catalog_path = Path(path) if path else DEFAULT_CATALOG
    payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    raw_sources = payload.get("manual_sources") or []
    if not isinstance(raw_sources, list):
        raise ValueError("편입 공지 catalog의 manual_sources는 배열이어야 합니다.")
    automatic_ids = set(load_transfer_sources(catalog_path))
    result: dict[str, ManualTransferSource] = {}
    for raw in raw_sources:
        if not isinstance(raw, dict):
            raise ValueError("manual source 항목은 객체여야 합니다.")
        source = ManualTransferSource(
            source_id=str(raw.get("id") or "").strip(),
            university=str(raw.get("university") or "").strip(),
            official_url=str(raw.get("official_url") or "").strip(),
            reason=str(raw.get("reason") or "").strip(),
        )
        parsed = urlparse(source.official_url)
        if (
            not re.fullmatch(r"[a-z0-9_]{2,40}", source.source_id)
            or not source.university
            or parsed.scheme != "https"
            or not parsed.hostname
            or not source.reason
        ):
            raise ValueError(f"잘못된 manual 편입 공지 source: {source.source_id!r}")
        if source.source_id in automatic_ids or source.source_id in result:
            raise ValueError(f"중복 편입 공지 source id: {source.source_id}")
        result[source.source_id] = source
    return result

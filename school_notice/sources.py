from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import soupsieve

from .models import DetailSpec, ListSpec, SourceConfig, ValidationSpec


DEFAULT_SOURCE_FILE = Path(__file__).with_name("sources.json")


def _tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def _source_from_mapping(raw: dict[str, Any]) -> SourceConfig:
    list_raw = raw["list"]
    detail_raw = raw["detail"]
    validation_raw = raw.get("validation", {})
    return SourceConfig(
        source_id=str(raw["id"]),
        school_id=str(raw["school_id"]),
        university=str(raw["university"]),
        board_name=str(raw["board_name"]),
        adapter=str(raw["adapter"]),
        list_url=str(raw["list_url"]),
        allowed_hosts=_tuple(raw["allowed_hosts"]),
        list_spec=ListSpec(
            row_selector=str(list_raw["row_selector"]),
            link_selector=str(list_raw["link_selector"]),
            id_attribute=str(list_raw["id_attribute"]),
            id_regex=str(list_raw["id_regex"]),
            title_selector=list_raw.get("title_selector"),
            date_selector=list_raw.get("date_selector"),
            author_selector=list_raw.get("author_selector"),
            category_selector=list_raw.get("category_selector"),
            include_selector=list_raw.get("include_selector"),
            include_regex=list_raw.get("include_regex"),
            detail_url_template=list_raw.get("detail_url_template"),
            pinned_classes=_tuple(list_raw.get("pinned_classes")),
        ),
        detail_spec=DetailSpec(
            title_selectors=_tuple(detail_raw["title_selectors"]),
            body_selectors=_tuple(detail_raw["body_selectors"]),
            date_selectors=_tuple(detail_raw.get("date_selectors")),
            author_selectors=_tuple(detail_raw.get("author_selectors")),
            attachment_selectors=_tuple(detail_raw.get("attachment_selectors")),
            attachment_attribute=str(
                detail_raw.get("attachment_attribute", "href")
            ),
            attachment_regex=detail_raw.get("attachment_regex"),
            attachment_url_template=detail_raw.get("attachment_url_template"),
            inline_image_selectors=_tuple(detail_raw.get("inline_image_selectors")),
        ),
        validation=ValidationSpec(
            min_list_items=int(validation_raw.get("min_list_items", 3)),
            min_body_characters=int(
                validation_raw.get("min_body_characters", 50)
            ),
        ),
        profile_tags=_tuple(raw.get("profile_tags")),
    )


def _validate_source(source: SourceConfig) -> None:
    if not source.source_id or not re.fullmatch(r"[a-z0-9_]+", source.source_id):
        raise ValueError(f"잘못된 source id: {source.source_id!r}")
    if not source.school_id or not re.fullmatch(r"[a-z0-9_]+", source.school_id):
        raise ValueError(f"{source.source_id}: 잘못된 school_id")
    allowed = {host.casefold() for host in source.allowed_hosts if host}
    if not allowed:
        raise ValueError(f"{source.source_id}: allowed_hosts가 비어 있습니다.")
    list_host = (urlparse(source.list_url).hostname or "").casefold()
    if (
        urlparse(source.list_url).scheme not in {"http", "https"}
        or list_host not in allowed
    ):
        raise ValueError(
            f"{source.source_id}: list_url 호스트가 allowed_hosts에 없습니다."
        )
    try:
        re.compile(source.list_spec.id_regex)
        if source.list_spec.include_regex:
            re.compile(source.list_spec.include_regex)
        if source.detail_spec.attachment_regex:
            re.compile(source.detail_spec.attachment_regex)
        selectors = (
            source.list_spec.row_selector,
            source.list_spec.link_selector,
            *source.detail_spec.title_selectors,
            *source.detail_spec.body_selectors,
            *source.detail_spec.date_selectors,
            *source.detail_spec.author_selectors,
            *source.detail_spec.attachment_selectors,
            *source.detail_spec.inline_image_selectors,
        )
        optional_selectors = (
            source.list_spec.title_selector,
            source.list_spec.date_selector,
            source.list_spec.author_selector,
            source.list_spec.category_selector,
            source.list_spec.include_selector,
        )
        for selector in (*selectors, *optional_selectors):
            if selector:
                soupsieve.compile(selector)
    except (re.error, soupsieve.SelectorSyntaxError) as exc:
        raise ValueError(f"{source.source_id}: selector/regex 설정 오류: {exc}") from exc
    if not source.detail_spec.title_selectors or not source.detail_spec.body_selectors:
        raise ValueError(f"{source.source_id}: 상세 title/body selector가 필요합니다.")
    if source.validation.min_list_items < 1:
        raise ValueError(f"{source.source_id}: min_list_items는 1 이상이어야 합니다.")
    if source.validation.min_body_characters < 1:
        raise ValueError(
            f"{source.source_id}: min_body_characters는 1 이상이어야 합니다."
        )
    for tag in source.profile_tags:
        if ":" not in tag or not all(part.strip() for part in tag.split(":", 1)):
            raise ValueError(
                f"{source.source_id}: profile_tags는 '종류:값' 형식이어야 합니다."
            )


def load_sources(path: str | Path | None = None) -> dict[str, SourceConfig]:
    source_path = Path(path) if path else DEFAULT_SOURCE_FILE
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    sources = [_source_from_mapping(raw) for raw in payload["sources"]]
    duplicated = {
        item.source_id
        for item in sources
        if sum(other.source_id == item.source_id for other in sources) > 1
    }
    if duplicated:
        raise ValueError(f"중복 source id: {sorted(duplicated)}")
    for source in sources:
        _validate_source(source)
    return {source.source_id: source for source in sources}

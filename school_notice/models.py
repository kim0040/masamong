from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class ListSpec:
    row_selector: str
    link_selector: str
    id_attribute: str
    id_regex: str
    title_selector: str | None = None
    date_selector: str | None = None
    author_selector: str | None = None
    category_selector: str | None = None
    include_selector: str | None = None
    include_regex: str | None = None
    detail_url_template: str | None = None
    pinned_classes: tuple[str, ...] = ()


@dataclass(frozen=True)
class DetailSpec:
    title_selectors: tuple[str, ...]
    body_selectors: tuple[str, ...]
    date_selectors: tuple[str, ...] = ()
    author_selectors: tuple[str, ...] = ()
    attachment_selectors: tuple[str, ...] = ()
    attachment_attribute: str = "href"
    attachment_regex: str | None = None
    attachment_url_template: str | None = None
    inline_image_selectors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidationSpec:
    min_list_items: int = 3
    min_body_characters: int = 50


@dataclass(frozen=True)
class SourceConfig:
    source_id: str
    school_id: str
    university: str
    board_name: str
    adapter: str
    list_url: str
    allowed_hosts: tuple[str, ...]
    list_spec: ListSpec
    detail_spec: DetailSpec
    validation: ValidationSpec
    profile_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class NoticeCandidate:
    source_id: str
    external_id: str
    title: str
    url: str
    published_text: str | None = None
    author: str | None = None
    category: str | None = None
    pinned: bool = False
    source_university: str | None = None
    source_board: str | None = None
    source_tags: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MediaLink:
    kind: str
    url: str
    name: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AttachmentExtraction:
    url: str
    name: str | None
    media_type: str
    byte_count: int
    sha256: str
    status: str
    text: str = ""
    warning: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class NoticeSignals:
    dates: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    audiences: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Notice:
    candidate: NoticeCandidate
    title: str
    body_text: str
    published_text: str | None
    author: str | None
    attachments: tuple[MediaLink, ...]
    inline_images: tuple[MediaLink, ...]
    signals: NoticeSignals
    base_content_hash: str
    content_hash: str
    attachment_extractions: tuple[AttachmentExtraction, ...] = ()
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.as_dict(),
            "title": self.title,
            "body_text": self.body_text,
            "body_characters": len(self.body_text),
            "published_text": self.published_text,
            "author": self.author,
            "attachments": [item.as_dict() for item in self.attachments],
            "inline_images": [item.as_dict() for item in self.inline_images],
            "attachment_extractions": [
                item.as_dict() for item in self.attachment_extractions
            ],
            "signals": self.signals.as_dict(),
            "base_content_hash": self.base_content_hash,
            "content_hash": self.content_hash,
            "warnings": list(self.warnings),
        }


def notice_from_dict(payload: dict[str, Any]) -> Notice:
    candidate = NoticeCandidate(**payload["candidate"])
    attachments = tuple(MediaLink(**item) for item in payload.get("attachments", []))
    inline_images = tuple(
        MediaLink(**item) for item in payload.get("inline_images", [])
    )
    attachment_extractions = tuple(
        AttachmentExtraction(**item)
        for item in payload.get("attachment_extractions", [])
    )
    signals_raw = payload.get("signals", {})
    signals = NoticeSignals(
        dates=tuple(signals_raw.get("dates", [])),
        actions=tuple(signals_raw.get("actions", [])),
        audiences=tuple(signals_raw.get("audiences", [])),
        topics=tuple(signals_raw.get("topics", [])),
    )
    return Notice(
        candidate=candidate,
        title=payload["title"],
        body_text=payload.get("body_text", ""),
        published_text=payload.get("published_text"),
        author=payload.get("author"),
        attachments=attachments,
        inline_images=inline_images,
        signals=signals,
        base_content_hash=payload.get(
            "base_content_hash",
            payload.get("content_hash", ""),
        ),
        content_hash=payload["content_hash"],
        attachment_extractions=attachment_extractions,
        warnings=tuple(payload.get("warnings", [])),
    )


@dataclass
class SourceRun:
    source_id: str
    university: str
    board_name: str
    list_url: str
    status: str = "failed"
    discovered_count: int = 0
    unique_count: int = 0
    details_attempted: int = 0
    details_succeeded: int = 0
    list_elapsed_ms: int | None = None
    notices: list[Notice] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    health_checks: dict[str, bool] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "university": self.university,
            "board_name": self.board_name,
            "list_url": self.list_url,
            "status": self.status,
            "discovered_count": self.discovered_count,
            "unique_count": self.unique_count,
            "details_attempted": self.details_attempted,
            "details_succeeded": self.details_succeeded,
            "list_elapsed_ms": self.list_elapsed_ms,
            "notices": [notice.as_dict() for notice in self.notices],
            "warnings": self.warnings,
            "errors": self.errors,
            "health_checks": self.health_checks,
        }

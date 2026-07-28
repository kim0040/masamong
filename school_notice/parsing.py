from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from urllib.parse import quote, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from .models import MediaLink, Notice, NoticeCandidate, SourceConfig
from .signals import extract_signals


_SPACE_RE = re.compile(r"[ \t\u00a0]+")
_BLANK_RE = re.compile(r"\n{3,}")


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    lines = []
    previous = None
    for raw_line in value.replace("\r", "\n").splitlines():
        line = _SPACE_RE.sub(" ", raw_line).strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if line == previous:
            continue
        lines.append(line)
        previous = line
    return _BLANK_RE.sub("\n\n", "\n".join(lines)).strip()


def normalize_inline(value: str | None) -> str:
    return _SPACE_RE.sub(" ", (value or "").replace("\r", " ").replace("\n", " ")).strip()


def _node_text(node: Tag | None) -> str:
    if node is None:
        return ""
    if node.name == "meta":
        return normalize_text(str(node.get("content") or ""))
    return normalize_text(node.get_text("\n", strip=True))


def _select_first(root: Tag | BeautifulSoup, selectors: Iterable[str]) -> Tag | None:
    for selector in selectors:
        node = root.select_one(selector)
        if node is not None:
            return node
    return None


def _safe_web_url(base_url: str, value: str | None) -> str | None:
    if not value:
        return None
    joined = urljoin(base_url, value.strip())
    parsed = urlparse(joined)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return joined


def parse_list(html: str, source: SourceConfig) -> tuple[list[NoticeCandidate], list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    spec = source.list_spec
    candidates: list[NoticeCandidate] = []
    warnings: list[str] = []
    seen: set[str] = set()
    rows = soup.select(spec.row_selector)

    for index, row in enumerate(rows):
        if spec.include_regex:
            include_node = (
                row.select_one(spec.include_selector)
                if spec.include_selector
                else row
            )
            if include_node is None or not re.search(
                spec.include_regex,
                _node_text(include_node),
            ):
                continue
        link = row.select_one(spec.link_selector)
        if link is None:
            continue
        identity_raw = str(link.get(spec.id_attribute) or "")
        match = re.search(spec.id_regex, identity_raw)
        if not match:
            warnings.append(f"row_{index}:stable_id_not_found")
            continue
        external_id = match.group(1)
        if external_id in seen:
            continue

        if spec.detail_url_template:
            url = spec.detail_url_template.format(id=external_id)
        else:
            url = _safe_web_url(source.list_url, str(link.get("href") or ""))
        if not url:
            warnings.append(f"row_{index}:invalid_detail_url")
            continue

        title_node = link.select_one(spec.title_selector) if spec.title_selector else link
        title = normalize_inline(_node_text(title_node))
        if not title:
            warnings.append(f"row_{index}:empty_title")
            continue

        date_node = row.select_one(spec.date_selector) if spec.date_selector else None
        author_node = (
            row.select_one(spec.author_selector) if spec.author_selector else None
        )
        category_node = (
            row.select_one(spec.category_selector) if spec.category_selector else None
        )
        row_classes = set(str(item) for item in row.get("class", []))

        candidates.append(
            NoticeCandidate(
                source_id=source.source_id,
                external_id=external_id,
                title=title,
                url=url,
                published_text=_node_text(date_node) or None,
                author=_node_text(author_node) or None,
                category=_node_text(category_node) or None,
                pinned=bool(row_classes.intersection(spec.pinned_classes)),
                source_university=source.university,
                source_board=source.board_name,
                source_tags=source.profile_tags,
            )
        )
        seen.add(external_id)

    if rows and not candidates:
        warnings.append("rows_found_but_no_candidates")
    if not rows:
        warnings.append("row_selector_returned_zero")
    return candidates, warnings


def _collect_media(
    soup: BeautifulSoup,
    selectors: Iterable[str],
    *,
    base_url: str,
    attribute: str,
    kind: str,
    value_regex: str | None = None,
    url_template: str | None = None,
) -> tuple[MediaLink, ...]:
    results: list[MediaLink] = []
    seen: set[str] = set()
    for selector in selectors:
        for node in soup.select(selector):
            raw_value = str(node.get(attribute) or "")
            if value_regex:
                match = re.search(value_regex, raw_value)
                if not match:
                    continue
                extracted = quote(match.group(1), safe="")
                raw_value = (
                    url_template.format(id=extracted)
                    if url_template
                    else extracted
                )
            url = _safe_web_url(base_url, raw_value)
            if not url or url in seen:
                continue
            name = (
                _node_text(node)
                or normalize_text(str(node.get("title") or ""))
                or urlparse(url).path.rsplit("/", 1)[-1]
                or None
            )
            results.append(MediaLink(kind=kind, url=url, name=name))
            seen.add(url)
    return tuple(results)


def parse_detail(
    html: str,
    source: SourceConfig,
    candidate: NoticeCandidate,
) -> Notice:
    soup = BeautifulSoup(html, "html.parser")
    for unwanted in soup.select("script, style, noscript, template"):
        unwanted.decompose()

    spec = source.detail_spec
    title = (
        normalize_inline(_node_text(_select_first(soup, spec.title_selectors)))
        or candidate.title
    )
    body_node = _select_first(soup, spec.body_selectors)
    body_text = _node_text(body_node)
    published_text = (
        _node_text(_select_first(soup, spec.date_selectors))
        or candidate.published_text
    )
    author = (
        _node_text(_select_first(soup, spec.author_selectors)) or candidate.author
    )

    attachments = _collect_media(
        soup,
        spec.attachment_selectors,
        base_url=candidate.url,
        attribute=spec.attachment_attribute,
        kind="attachment",
        value_regex=spec.attachment_regex,
        url_template=spec.attachment_url_template,
    )
    inline_images = _collect_media(
        soup,
        spec.inline_image_selectors,
        base_url=candidate.url,
        attribute="src",
        kind="inline_image",
    )

    warnings: list[str] = []
    if not body_text:
        fallback = soup.select_one("meta[property='og:description'], meta[name='description']")
        body_text = _node_text(fallback)
        if body_text:
            warnings.append("body_used_meta_description")
    if len(body_text) < source.validation.min_body_characters:
        warnings.append(
            f"body_too_short:{len(body_text)}<{source.validation.min_body_characters}"
        )
    if normalize_inline(title).casefold() != normalize_inline(candidate.title).casefold():
        warnings.append("list_detail_title_difference")

    hash_input = "\n".join(
        [
            normalize_text(title),
            normalize_text(published_text),
            body_text,
            *sorted(item.url for item in attachments),
            *sorted(item.url for item in inline_images),
        ]
    )
    content_hash = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()
    signals = extract_signals(title, body_text)
    return Notice(
        candidate=candidate,
        title=title,
        body_text=body_text,
        published_text=published_text,
        author=author,
        attachments=attachments,
        inline_images=inline_images,
        attachment_extractions=(),
        signals=signals,
        base_content_hash=content_hash,
        content_hash=content_hash,
        warnings=tuple(warnings),
    )

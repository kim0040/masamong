from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import re
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup, Tag

from .catalog import TransferSource


_SPACE_RE = re.compile(r"\s+")
_DATE_RE = re.compile(
    r"(?<!\d)(20\d{2})[.\-/년]\s*(0?[1-9]|1[0-2])[.\-/월]\s*"
    r"(0?[1-9]|[12]\d|3[01])(?:일)?(?!\d)"
)
_ANNOUNCEMENT_RE = re.compile(
    r"(?:20\d{2}학년도|편입(?:학|생)?|모집|원서|합격|고사|등록|충원|"
    r"서류|지원|경쟁률|전형|일정|결과|성적|면접|실기|환불|추가|기본계획)"
)
_MIXED_TRANSFER_RE = re.compile(r"(?:대학\s*)?편입(?:학|생)?")
_MIXED_EXCLUDE_RE = re.compile(
    r"(?:대학원|외국인|재외국민|계약학과|대학원대학)"
)
_DROP_QUERY_KEYS = {
    "page",
    "pageno",
    "pageindex",
    "currpage",
    "startpage",
    "searchno",
    "searchtext",
    "keyword",
    "skey",
}


@dataclass(frozen=True)
class TransferNoticeItem:
    source_id: str
    university: str
    external_id: str
    title: str
    url: str
    published_date: str | None
    fingerprint: str

    def as_dict(self) -> dict:
        return asdict(self)


def normalize_inline(value: str | None) -> str:
    return _SPACE_RE.sub(" ", str(value or "")).strip()


def _canonical_url(base_url: str, raw_href: str) -> str | None:
    url = urljoin(base_url, raw_href.strip())
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() not in _DROP_QUERY_KEYS
    ]
    return urlunparse(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path or "/",
            "",
            urlencode(query, doseq=True),
            "",
        )
    )


def _title(link: Tag, source: TransferSource) -> str:
    node = link
    if source.title_selector:
        selected = link.select_one(source.title_selector)
        if selected is not None:
            node = selected
    if source.title_attribute:
        return normalize_inline(str(node.get(source.title_attribute) or ""))
    return normalize_inline(node.get_text(" ", strip=True))


def _raw_item_url(item: Tag, source: TransferSource) -> str:
    value_node = item
    if source.url_value_selector:
        selected = item.select_one(source.url_value_selector)
        if selected is None:
            return ""
        value_node = selected
    raw_value = str(value_node.get(source.url_value_attribute) or "").strip()
    if not raw_value:
        return ""
    if source.url_value_regex:
        match = re.search(source.url_value_regex, raw_value)
        if not match:
            return ""
        raw_value = match.group(1)
    if source.detail_url_template:
        return source.detail_url_template.format(
            value=quote(raw_value, safe=""),
        )
    return raw_value


def _published_date(link: Tag) -> str | None:
    current: Tag | None = link
    for _ in range(4):
        if current is None:
            break
        text = normalize_inline(current.get_text(" ", strip=True))
        match = _DATE_RE.search(text)
        if match:
            try:
                return date(
                    int(match.group(1)),
                    int(match.group(2)),
                    int(match.group(3)),
                ).isoformat()
            except ValueError:
                pass
        parent = current.parent
        current = parent if isinstance(parent, Tag) else None
    return None


def _looks_like_notice(title: str, source: TransferSource) -> bool:
    if len(title) < 8 or len(title) > 300:
        return False
    if not _ANNOUNCEMENT_RE.search(title):
        return False
    if source.title_mode == "mixed_board":
        return bool(
            _MIXED_TRANSFER_RE.search(title)
            and not _MIXED_EXCLUDE_RE.search(title)
        )
    return not bool(_MIXED_EXCLUDE_RE.search(title))


def parse_transfer_list(
    html: str,
    source: TransferSource,
) -> tuple[list[TransferNoticeItem], list[str]]:
    """공식 목록의 공개 링크만 추출한다.

    상세 본문이나 첨부파일은 읽지 않는다. 알림 목적은 새 공지의 존재와 공식
    링크를 전달하는 것이므로, 대학별 1회 목록 요청으로 제한한다.
    """
    soup = BeautifulSoup(html, "html.parser")
    items: list[TransferNoticeItem] = []
    warnings: list[str] = []
    seen: set[str] = set()
    allowed_hosts = set(source.allowed_hosts)
    links = soup.select(source.link_selector)

    for link in links:
        if not isinstance(link, Tag):
            continue
        href = _raw_item_url(link, source)
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        if source.href_regex and not re.search(source.href_regex, href):
            continue
        title = _title(link, source)
        if not _looks_like_notice(title, source):
            continue
        url = _canonical_url(source.list_url, href)
        if not url:
            continue
        if (urlparse(url).hostname or "").casefold() not in allowed_hosts:
            continue
        external_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
        if external_id in seen:
            continue
        fingerprint = hashlib.sha256(
            f"{title}\n{url}".encode("utf-8")
        ).hexdigest()
        items.append(
            TransferNoticeItem(
                source_id=source.source_id,
                university=source.university,
                external_id=external_id,
                title=title,
                url=url,
                published_date=_published_date(link),
                fingerprint=fingerprint,
            )
        )
        seen.add(external_id)

    if not links:
        warnings.append("link_selector_returned_zero")
    if links and not items:
        warnings.append("no_transfer_notice_links")
    return items, warnings

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
_TITLE_TRAILING_METADATA_RE = re.compile(
    r"(?:"
    r"\s*(?:[|ㅣ]\s*)?(?:(?:작성|등록)일)\s*[:：]?\s*"
    r"20\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2}"
    r"|"
    r"\s*(?:[|ㅣ]\s*)?조회수?\s*[:：]?\s*[\d,]+"
    r")+\s*$",
    re.IGNORECASE,
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
    detail_summary: str = ""
    detail_text: str = ""
    detail_fingerprint: str = ""
    key_dates: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return asdict(self)


def normalize_inline(value: str | None) -> str:
    return _SPACE_RE.sub(" ", str(value or "")).strip()


def normalize_notice_title(value: str | None) -> str:
    """목록 제목 뒤의 작성일·조회수처럼 변하는 표시값을 제거한다."""
    normalized = normalize_inline(value)
    return _TITLE_TRAILING_METADATA_RE.sub("", normalized).strip()


def listing_fingerprint(title: str, url: str) -> str:
    """사용자에게 의미 있는 목록 제목과 정규 URL만 변경 판정에 사용한다."""
    return hashlib.sha256(
        f"{normalize_notice_title(title)}\n{url}".encode("utf-8")
    ).hexdigest()


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
    """공식 목록에서 편입 관련 공개 링크 후보를 추출한다.

    상세 본문은 collector가 이 결과 중 신규·변경·최신 후보만 별도로 순차
    요청한다. 여기서는 대학별 목록 판정과 공식 host 경계만 담당한다.
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
        title = normalize_notice_title(_title(link, source))
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
        fingerprint = listing_fingerprint(title, url)
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


_DETAIL_CONTAINER_SELECTORS = (
    "main",
    "article",
    ".board-view",
    ".board_view",
    ".view-content",
    ".view_content",
    ".view-cont",
    ".view_cont",
    ".bbs-view",
    ".bbs_view",
    ".board-content",
    ".board_content",
    "#content",
    "#contents",
)
_DETAIL_DROP_SELECTORS = (
    "script",
    "style",
    "noscript",
    "nav",
    "header",
    "footer",
    "form",
    ".pagination",
    ".breadcrumb",
    ".share",
    ".sns",
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。])\s+|\n+")


def _detail_summary(text: str, *, limit: int = 420) -> str:
    """공개 상세 본문에서 원문 근거가 남는 짧은 발췌 요약을 만든다."""
    parts: list[str] = []
    used = 0
    for raw in _SENTENCE_SPLIT_RE.split(text):
        sentence = normalize_inline(raw)
        if len(sentence) < 12:
            continue
        if sentence in parts:
            continue
        remaining = limit - used
        if remaining <= 1:
            break
        clipped = sentence if len(sentence) <= remaining else sentence[: remaining - 1] + "…"
        parts.append(clipped)
        used += len(clipped) + 1
        if len(parts) >= 3 or used >= limit:
            break
    return " ".join(parts).strip()


def parse_transfer_detail(
    html: str,
    item: TransferNoticeItem,
) -> TransferNoticeItem:
    """상세 페이지의 공개 본문을 추출해 알림용 발췌 요약을 붙인다.

    대학별 마크업이 달라도 목록 제목과 가장 긴 본문 컨테이너를 근거로
    동작한다. 추출 실패 시 제목·공식 링크 알림은 유지한다.
    """
    soup = BeautifulSoup(html, "html.parser")
    for selector in _DETAIL_DROP_SELECTORS:
        for node in soup.select(selector):
            node.decompose()

    candidates: list[tuple[int, str]] = []
    for selector in _DETAIL_CONTAINER_SELECTORS:
        for node in soup.select(selector):
            text = normalize_inline(node.get_text(" ", strip=True))
            if text:
                candidates.append((len(text), text))
    if candidates:
        body = max(candidates, key=lambda pair: pair[0])[1]
    else:
        body = normalize_inline(soup.get_text(" ", strip=True))

    # 메뉴·사이트맵을 잘못 집는 경우 무제한 저장/전송하지 않는다.
    body = body[:12_000]
    if len(body) < 40:
        return item
    key_dates = tuple(
        dict.fromkeys(
            f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
            for match in _DATE_RE.finditer(body)
        )
    )[:8]
    summary = _detail_summary(body)
    # 조회수·공통 내비게이션처럼 알림 가치가 없는 동적 문구가 상세 전체
    # fingerprint를 매일 바꾸지 않게, 사용자에게 실제 제시할 요약과 핵심 날짜만
    # 변경 판정에 사용한다.
    detail_fingerprint = hashlib.sha256(
        (summary + "\n" + "\n".join(key_dates)).encode("utf-8")
    ).hexdigest()
    return TransferNoticeItem(
        source_id=item.source_id,
        university=item.university,
        external_id=item.external_id,
        title=item.title,
        url=item.url,
        published_date=item.published_date,
        fingerprint=item.fingerprint,
        detail_summary=summary,
        detail_text=body,
        detail_fingerprint=detail_fingerprint,
        key_dates=key_dates,
    )

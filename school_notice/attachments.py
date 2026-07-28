from __future__ import annotations

import hashlib
import io
import re
import zipfile
import zlib
from dataclasses import replace
from pathlib import PurePosixPath
from xml.etree import ElementTree

from .http import AsyncFetcher, FetchError
from .models import AttachmentExtraction, Notice, SourceConfig


_PRINTABLE_RE = re.compile(r"[가-힣A-Za-z0-9][가-힣A-Za-z0-9\s.,:;!?()\[\]{}'\"/%+\-·~]{2,}")
_MAX_ARCHIVE_ENTRIES = 100
_MAX_ARCHIVE_UNCOMPRESSED_BYTES = 100_000_000
_MAX_ARCHIVE_MEMBER_BYTES = 20_000_000
_MAX_HWP_SECTION_BYTES = 20_000_000
_MAX_PDF_PAGES = 100


def _extension(name: str | None, url: str) -> str:
    candidate = name or PurePosixPath(url.split("?", 1)[0]).name
    return PurePosixPath(candidate.casefold()).suffix


def detect_media_type(
    name: str | None,
    url: str,
    content_type: str,
    data: bytes,
) -> str:
    mime = content_type.split(";", 1)[0].strip().casefold()
    extension = _extension(name, url)
    if data.startswith(b"%PDF") or mime == "application/pdf" or extension == ".pdf":
        return "pdf"
    if data.startswith(b"\xd0\xcf\x11\xe0") or extension == ".hwp":
        return "hwp"
    if zipfile.is_zipfile(io.BytesIO(data)):
        if extension == ".hwpx":
            return "hwpx"
        if extension == ".docx":
            return "docx"
        if extension == ".pptx":
            return "pptx"
        if extension == ".xlsx":
            return "xlsx"
        return "zip"
    if mime.startswith("text/") or extension in {".txt", ".csv"}:
        return "text"
    if mime.startswith("image/") or extension in {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".tif",
        ".tiff",
    }:
        return "image"
    return "binary"


def _xml_text(data: bytes) -> str:
    root = ElementTree.fromstring(data)
    values: list[str] = []
    for element in root.iter():
        if element.text and element.text.strip():
            values.append(element.text.strip())
    return "\n".join(values)


def _extract_zip_xml(data: bytes, prefixes: tuple[str, ...]) -> str:
    values: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = [
            item
            for item in archive.infolist()
            if item.filename.casefold().endswith(".xml")
            and any(item.filename.startswith(prefix) for prefix in prefixes)
        ]
        if len(members) > _MAX_ARCHIVE_ENTRIES:
            raise ValueError(
                f"archive_entry_limit:{len(members)}>{_MAX_ARCHIVE_ENTRIES}"
            )
        total_size = sum(item.file_size for item in members)
        if total_size > _MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise ValueError(
                "archive_uncompressed_limit:"
                f"{total_size}>{_MAX_ARCHIVE_UNCOMPRESSED_BYTES}"
            )
        oversized = next(
            (
                item
                for item in members
                if item.file_size > _MAX_ARCHIVE_MEMBER_BYTES
            ),
            None,
        )
        if oversized:
            raise ValueError(
                "archive_member_limit:"
                f"{oversized.file_size}>{_MAX_ARCHIVE_MEMBER_BYTES}"
            )
        for member in sorted(members, key=lambda item: item.filename):
            try:
                text = _xml_text(archive.read(member))
            except (KeyError, ElementTree.ParseError, UnicodeDecodeError):
                continue
            if text:
                values.append(text)
    return "\n".join(values)


def _extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data), strict=False)
    return "\n\n".join(
        (page.extract_text() or "").strip()
        for page in reader.pages[:_MAX_PDF_PAGES]
    )


def _extract_hwp(data: bytes) -> str:
    import olefile

    if not olefile.isOleFile(io.BytesIO(data)):
        return ""
    values: list[str] = []
    with olefile.OleFileIO(io.BytesIO(data)) as document:
        compressed = False
        if document.exists("FileHeader"):
            header = document.openstream("FileHeader").read()
            if len(header) > 36:
                flags = int.from_bytes(header[32:36], "little")
                compressed = bool(flags & 1)
        sections = sorted(
            "/".join(parts)
            for parts in document.listdir()
            if len(parts) >= 2
            and parts[0] == "BodyText"
            and parts[1].startswith("Section")
        )
        for stream_name in sections:
            raw = document.openstream(stream_name).read()
            if compressed:
                try:
                    decompressor = zlib.decompressobj(-15)
                    raw = decompressor.decompress(
                        raw,
                        _MAX_HWP_SECTION_BYTES + 1,
                    )
                except zlib.error:
                    continue
                if len(raw) > _MAX_HWP_SECTION_BYTES:
                    raise ValueError(
                        "hwp_section_limit:"
                        f"{len(raw)}>{_MAX_HWP_SECTION_BYTES}"
                    )
            decoded = raw.decode("utf-16le", errors="ignore")
            for match in _PRINTABLE_RE.finditer(decoded):
                candidate = " ".join(match.group(0).split())
                if candidate:
                    values.append(candidate)
    return "\n".join(values)


def extract_text_from_bytes(
    *,
    media_type: str,
    data: bytes,
    max_characters: int = 30_000,
) -> tuple[str, str, str | None]:
    try:
        if media_type == "pdf":
            text = _extract_pdf(data)
        elif media_type == "docx":
            text = _extract_zip_xml(data, ("word/",))
        elif media_type == "pptx":
            text = _extract_zip_xml(data, ("ppt/slides/", "ppt/notesSlides/"))
        elif media_type == "xlsx":
            text = _extract_zip_xml(data, ("xl/sharedStrings.xml", "xl/worksheets/"))
        elif media_type == "hwpx":
            text = _extract_zip_xml(data, ("Contents/",))
        elif media_type == "hwp":
            text = _extract_hwp(data)
        elif media_type == "text":
            text = data.decode("utf-8", errors="replace")
        elif media_type == "image":
            return "", "ocr_required", "OCR 엔진이 설정되지 않아 이미지 링크만 보존"
        else:
            return "", "unsupported", f"지원하지 않는 형식: {media_type}"
    except Exception as exc:
        return "", "failed", f"{type(exc).__name__}: {exc}"

    cleaned = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if len(cleaned) > max_characters:
        cleaned = cleaned[:max_characters]
        return cleaned, "partial", f"추출 텍스트를 {max_characters}자로 제한"
    if not cleaned:
        status = "ocr_required" if media_type == "pdf" else "empty"
        warning = (
            "텍스트 레이어가 없어 OCR 필요"
            if status == "ocr_required"
            else "추출 가능한 텍스트 없음"
        )
        return "", status, warning
    return cleaned, "success", None


async def enrich_notice_attachments(
    notice: Notice,
    source: SourceConfig,
    fetcher: AsyncFetcher,
    *,
    max_attachments: int = 3,
) -> Notice:
    extractions: list[AttachmentExtraction] = []
    for media in notice.attachments[:max_attachments]:
        try:
            result = await fetcher.fetch_bytes(
                media.url,
                allowed_hosts=source.allowed_hosts,
            )
            digest = hashlib.sha256(result.data).hexdigest()
            media_type = detect_media_type(
                media.name,
                result.final_url,
                result.content_type,
                result.data,
            )
            text, status, warning = extract_text_from_bytes(
                media_type=media_type,
                data=result.data,
            )
            extractions.append(
                AttachmentExtraction(
                    url=media.url,
                    name=media.name,
                    media_type=media_type,
                    byte_count=len(result.data),
                    sha256=digest,
                    status=status,
                    text=text,
                    warning=warning,
                )
            )
        except FetchError as exc:
            extractions.append(
                AttachmentExtraction(
                    url=media.url,
                    name=media.name,
                    media_type="unknown",
                    byte_count=0,
                    sha256="",
                    status="fetch_failed",
                    warning=str(exc),
                )
            )

    if len(notice.attachments) > max_attachments:
        extractions.append(
            AttachmentExtraction(
                url="",
                name=None,
                media_type="limit",
                byte_count=0,
                sha256="",
                status="skipped",
                warning=(
                    f"첨부 {len(notice.attachments)}개 중 "
                    f"{max_attachments}개만 처리"
                ),
            )
        )

    attachment_fingerprint = "\n".join(
        extraction.sha256 for extraction in extractions if extraction.sha256
    )
    content_hash = hashlib.sha256(
        f"{notice.content_hash}\n{attachment_fingerprint}".encode("utf-8")
    ).hexdigest()
    return replace(
        notice,
        attachment_extractions=tuple(extractions),
        content_hash=content_hash,
    )

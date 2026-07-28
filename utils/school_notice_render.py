# -*- coding: utf-8 -*-
"""검증된 digest를 Discord Embed로 변환합니다.

이 모듈은 Discord API를 호출하지 않고 Embed 객체만 만듭니다. 덕분에 봇 연결
없이 표현 규칙을 전부 테스트할 수 있습니다.

표현 규칙은 기능 설계에서 나옵니다(docs/SCHOOL_NOTICE_INTEGRATION_PLAN.ko.md).

- 점수는 확률이 아니라 설명 가능한 우선순위이므로 `reasons`를 반드시 노출한다.
- 자격을 확정할 수 없으면(`UNKNOWN`) 원문 확인이 필요함을 밝힌다.
- 연도를 추론한 마감은 그 사실을 함께 알린다.
- 수집이 실패한 날은 "새 공지 없음"과 구분되게 경고한다.
- 본문 전문 대신 분석 요약을 쓴다.
"""

from __future__ import annotations

from datetime import date

import discord

from utils.school_notice_contract import BAND_ORDER, Digest, DigestItem

# Discord 제한. 초과하면 API가 거절하므로 만들 때 자른다.
_TITLE_LIMIT = 256
_DESCRIPTION_LIMIT = 4096
_FIELD_VALUE_LIMIT = 1024
_EMBEDS_PER_MESSAGE = 10
_EMBED_TEXT_PER_MESSAGE = 6000

BAND_LABELS = {
    "action": "지금 확인",
    "opportunity": "기회",
    "reference": "참고",
}

BAND_COLORS = {
    "action": discord.Color.from_rgb(220, 76, 70),
    "opportunity": discord.Color.from_rgb(64, 132, 214),
    "reference": discord.Color.from_rgb(138, 146, 156),
}

URGENCY_LABELS = {
    "critical": "매우 급함",
    "high": "급함",
    "normal": "보통",
    "low": "낮음",
}


def _truncate(text: str, limit: int) -> str:
    """Discord 제한에 맞춰 자르되 잘렸음을 드러냅니다."""
    rendered = " ".join(str(text or "").split())
    if len(rendered) <= limit:
        return rendered
    return rendered[: max(0, limit - 1)].rstrip() + "…"


def _shrink_text(text: str, excess: int, *, minimum: int = 1) -> str:
    """문자 예산을 `excess`만큼 줄이되 빈 Discord 필드는 만들지 않습니다."""
    target = max(minimum, len(text) - max(0, excess))
    return _truncate(text, target)


def _fit_embed_total(embed: discord.Embed) -> discord.Embed:
    """Discord의 메시지당 전체 Embed 텍스트 6,000자 제한에 맞춥니다.

    요약을 먼저 줄이고, 그래도 부족하면 뒤쪽의 보조 필드부터 줄입니다. 일정,
    추천 근거, 자격 확인 경고 같은 앞쪽 핵심 필드는 가능한 한 보존합니다.
    """
    if len(embed) <= _EMBED_TEXT_PER_MESSAGE:
        return embed

    if embed.description:
        embed.description = _shrink_text(
            embed.description,
            len(embed) - _EMBED_TEXT_PER_MESSAGE,
        )

    for index in range(len(embed.fields) - 1, -1, -1):
        if len(embed) <= _EMBED_TEXT_PER_MESSAGE:
            break
        field = embed.fields[index]
        embed.set_field_at(
            index,
            name=field.name,
            value=_shrink_text(
                field.value,
                len(embed) - _EMBED_TEXT_PER_MESSAGE,
            ),
            inline=field.inline,
        )

    if len(embed) > _EMBED_TEXT_PER_MESSAGE and embed.footer.text:
        embed.set_footer(
            text=_shrink_text(
                embed.footer.text,
                len(embed) - _EMBED_TEXT_PER_MESSAGE,
            )
        )
    if len(embed) > _EMBED_TEXT_PER_MESSAGE and embed.title:
        embed.title = _shrink_text(
            embed.title,
            len(embed) - _EMBED_TEXT_PER_MESSAGE,
        )
    if len(embed) > _EMBED_TEXT_PER_MESSAGE:
        raise ValueError(
            "Embed 텍스트를 Discord 6000자 제한 안으로 줄일 수 없습니다."
        )
    return embed


def _deadline_line(item: DigestItem, today: date | None) -> str | None:
    if item.deadline is None:
        return None
    parts = [f"마감 {item.deadline.isoformat()}"]
    if today is not None:
        remaining = (item.deadline - today).days
        if remaining < 0:
            parts.append(f"{abs(remaining)}일 지남")
        elif remaining == 0:
            parts.append("오늘")
        else:
            parts.append(f"{remaining}일 남음")
    if item.has_inferred_deadline:
        # 원문에 연도가 없어 코어가 추론한 값이다. 그대로 믿게 하면 안 된다.
        parts.append("연도 추론값이라 원문 확인 필요")
    return " · ".join(parts)


def build_item_embed(
    item: DigestItem,
    *,
    today: date | None = None,
) -> discord.Embed:
    """공지 한 건을 Embed로 만듭니다."""
    title_prefix = "[필수] " if item.required else ""
    embed = discord.Embed(
        title=_truncate(f"{title_prefix}{item.title}", _TITLE_LIMIT),
        url=item.url or None,
        description=_truncate(item.summary or "(요약 없음)", _DESCRIPTION_LIMIT),
        color=BAND_COLORS.get(item.band, discord.Color.light_grey()),
    )

    origin = " / ".join(part for part in (item.university, item.board) if part)
    footer_bits = [BAND_LABELS.get(item.band, item.band), f"{item.score:.0f}점"]
    if origin:
        footer_bits.append(origin)
    if item.change in {"new", "updated"}:
        footer_bits.append("새 글" if item.change == "new" else "내용 변경됨")
    embed.set_footer(text=_truncate(" · ".join(footer_bits), _FIELD_VALUE_LIMIT))

    deadline = _deadline_line(item, today)
    if deadline:
        embed.add_field(name="일정", value=_truncate(deadline, _FIELD_VALUE_LIMIT), inline=False)
    elif item.next_event is not None:
        embed.add_field(
            name="일정",
            value=_truncate(f"예정 {item.next_event.isoformat()}", _FIELD_VALUE_LIMIT),
            inline=False,
        )

    # 점수는 휴리스틱이므로 근거를 항상 보여준다.
    if item.reasons:
        reasons = "\n".join(f"· {reason}" for reason in item.reasons[:5])
        embed.add_field(
            name="왜 추천됐나",
            value=_truncate(reasons, _FIELD_VALUE_LIMIT),
            inline=False,
        )

    if item.needs_manual_check:
        embed.add_field(
            name="확인 필요",
            value="자격 조건을 확정할 수 없었습니다. 신청 전에 원문을 직접 확인하세요.",
            inline=False,
        )

    if any(
        warning.startswith("body_too_short:") or warning == "body_used_meta_description"
        for warning in item.warnings
    ):
        embed.add_field(
            name="본문 확인",
            value=(
                "학교 게시글의 본문이 이미지 중심이거나 공개 HTML 텍스트가 짧습니다. "
                "제목·게시판 분류를 바탕으로 표시했으므로 원문 이미지와 첨부파일을 "
                "직접 확인하세요."
            ),
            inline=False,
        )

    if item.topics:
        embed.add_field(
            name="주제",
            value=_truncate(", ".join(item.topics), _FIELD_VALUE_LIMIT),
            inline=True,
        )
    if item.urgency in URGENCY_LABELS and item.urgency != "normal":
        embed.add_field(name="긴급도", value=URGENCY_LABELS[item.urgency], inline=True)

    if item.attachments:
        links = []
        for attachment in item.attachments[:3]:
            name = str(attachment.get("name") or "첨부")
            url = str(attachment.get("url") or "")
            links.append(f"[{_truncate(name, 60)}]({url})" if url else _truncate(name, 60))
        embed.add_field(
            name="첨부",
            value=_truncate("\n".join(links), _FIELD_VALUE_LIMIT),
            inline=False,
        )

    if item.duplicate_sources:
        links = ", ".join(
            f"[{entry['source_id']}]({entry['url']})"
            for entry in item.duplicate_sources[:3]
            if entry.get("url")
        )
        if links:
            embed.add_field(
                name="다른 게시판에도 게시됨",
                value=_truncate(links, _FIELD_VALUE_LIMIT),
                inline=False,
            )

    return _fit_embed_total(embed)


def build_header_embed(digest: Digest, *, shown: int, total: int) -> discord.Embed:
    """요약과 수집 상태 경고를 담은 첫 Embed."""
    counts = digest.items_by_band()
    if total == 0:
        description = "오늘 조건에 맞는 새 공지가 없습니다."
    else:
        parts = [
            f"{BAND_LABELS[band]} {len(counts[band])}건"
            for band in BAND_ORDER
            if counts[band]
        ]
        description = " · ".join(parts) if parts else "표시할 공지가 없습니다."
        if shown < total:
            description += f"\n(상위 {shown}건만 표시, 전체 {total}건)"

    embed = discord.Embed(
        title=f"학교 공지 {digest.digest_date.isoformat()}",
        description=description,
        color=discord.Color.from_rgb(88, 101, 242),
    )

    health = digest.collection_health
    if health is not None and health.has_problem:
        lines: list[str] = []
        if health.may_include_stale_notices:
            # 이것이 "새 공지 없음"과 "확인 실패"를 구분하는 유일한 신호다.
            lines.append(
                "일부 게시판을 오늘 확인하지 못했습니다. "
                "아래 목록에 이전에 저장된 오래된 공지가 섞여 있을 수 있습니다."
            )
        failed = health.failed_sources()
        if failed:
            lines.append("수집 실패: " + ", ".join(item.source_id for item in failed))
        degraded = health.degraded_sources()
        if degraded:
            lines.append(
                "일부만 수집됨: " + ", ".join(item.source_id for item in degraded)
                + " (새 글이 빠졌을 수 있음)"
            )
        if lines:
            embed.add_field(
                name="수집 상태 경고",
                value=_truncate("\n".join(lines), _FIELD_VALUE_LIMIT),
                inline=False,
            )

    return _fit_embed_total(embed)


def render_digest(
    digest: Digest,
    *,
    max_items: int = 10,
    today: date | None = None,
) -> list[discord.Embed]:
    """digest 하나를 Embed 목록으로 변환합니다.

    Args:
        digest: 검증을 통과한 digest.
        max_items: 표시할 공지 최대 개수. Discord 메시지 상한을 보호합니다.
        today: 마감까지 남은 일수 계산 기준일. None이면 남은 일수를 쓰지 않습니다.

    Returns:
        첫 항목이 요약 Embed인 목록.
    """
    if max_items < 1:
        raise ValueError("max_items는 1 이상이어야 합니다.")
    visible = digest.visible_items()
    shown = visible[:max_items]
    embeds = [build_header_embed(digest, shown=len(shown), total=len(visible))]
    embeds.extend(build_item_embed(item, today=today) for item in shown)
    return embeds


def chunk_embeds(
    embeds: list[discord.Embed],
    *,
    per_message: int = _EMBEDS_PER_MESSAGE,
) -> list[list[discord.Embed]]:
    """메시지당 Embed 10개와 전체 텍스트 6,000자 상한에 맞춰 나눕니다."""
    if type(per_message) is not int or per_message < 1:
        raise ValueError("per_message는 1 이상의 정수여야 합니다.")
    size = min(per_message, _EMBEDS_PER_MESSAGE)
    groups: list[list[discord.Embed]] = []
    current: list[discord.Embed] = []
    current_text = 0

    for index, embed in enumerate(embeds):
        if not isinstance(embed, discord.Embed):
            raise TypeError(f"embeds[{index}]는 discord.Embed여야 합니다.")
        embed_text = len(embed)
        if embed_text > _EMBED_TEXT_PER_MESSAGE:
            raise ValueError(
                f"embeds[{index}]가 Discord 6000자 제한을 초과합니다: "
                f"{embed_text}>{_EMBED_TEXT_PER_MESSAGE}"
            )
        if current and (
            len(current) >= size
            or current_text + embed_text > _EMBED_TEXT_PER_MESSAGE
        ):
            groups.append(current)
            current = []
            current_text = 0
        current.append(embed)
        current_text += embed_text

    if current:
        groups.append(current)
    return groups

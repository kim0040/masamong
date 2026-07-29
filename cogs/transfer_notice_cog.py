# -*- coding: utf-8 -*-
"""공인영어(TOEIC 포함) 편입 공지 개인 DM 구독."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

import config
from logger_config import logger
from transfer_notice.catalog import load_transfer_sources
from utils.discord_interactions import ReliableModal, ReliableView
from utils.privacy_consent import (
    CONSENT_GRANTED,
    TRANSFER_NOTICE_SCOPE,
    consent_command_name,
    get_policy,
    has_current_consent,
    withdraw_consent,
)


TRANSFER_POLICY = get_policy(TRANSFER_NOTICE_SCOPE)
_MAX_OUTPUT_BYTES = 2_000_000
_DISCORD_CONTENT_LIMIT = 1_900
_DELIVERY_BACKLOG_DAYS = 3
_MAX_EVENT_FILES = 32
KST = ZoneInfo("Asia/Seoul")
_DELIVERY_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _utc_now() -> str:
    # privacy_consent의 granted_at은 microseconds 정밀도다. SQLite/TiDB에서
    # ISO 문자열을 직접 비교하므로 정밀도를 낮추면 같은 초에 동의→구독한
    # 정상 순서가 역전된 것처럼 보일 수 있다.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _safe_title(value: object, limit: int = 180) -> str:
    text = discord.utils.escape_markdown(str(value or "").strip())
    text = text.replace("\n", " ")
    return text[:limit] or "제목 없음"


def _safe_summary(value: object, limit: int = 420) -> str:
    text = discord.utils.escape_markdown(str(value or "").strip())
    text = " ".join(text.split())
    return text[:limit]


def _normalize_delivery_time(value: object) -> str:
    rendered = str(value or "").strip()
    if not _DELIVERY_TIME_RE.fullmatch(rendered):
        raise ValueError("알림 시각은 `HH:MM` 형식이어야 합니다. 예: `09:00`")
    return rendered


def _notice_block(item: dict, *, change_label: str | None = None) -> str:
    university = discord.utils.escape_markdown(
        str(item.get("university") or "대학").strip()
    )
    published = _safe_title(item.get("published_date") or "날짜 미표기", limit=30)
    meta = " · ".join(
        part for part in (university, change_label, published) if part
    )
    summary = _safe_summary(item.get("detail_summary"))
    return (
        f"📌 **{meta}**\n"
        f"**{_safe_title(item.get('title'))}**"
        + (f"\n**핵심:** {summary}" if summary else "")
        + f"\n[공식 원문 보기]({item.get('url')})"
    )


class TransferDeliveryTimeModal(ReliableModal, title="편입 공지 알림 시각"):
    delivery_time = discord.ui.TextInput(
        label="한국 시간 (HH:MM)",
        placeholder="09:00",
        min_length=5,
        max_length=5,
    )

    def __init__(self, cog: "TransferNoticeCog", user_id: int) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = int(user_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            normalized = await self.cog.update_delivery_time(
                self.user_id,
                str(self.delivery_time),
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception as exc:
            logger.error(
                "편입 공지 알림 시각 저장 실패: error=%s",
                type(exc).__name__,
                exc_info=True,
            )
            await interaction.followup.send(
                "알림 시각을 저장하지 못했습니다. 잠시 후 다시 시도해주세요.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"✅ 편입 공지 알림 시각을 매일 **{normalized} (한국 시간)**으로 "
            "저장했습니다.",
            ephemeral=True,
        )


def _fit_notice_blocks(
    heading: list[str],
    blocks: list[str],
    footer: str,
    *,
    limit: int = _DISCORD_CONTENT_LIMIT,
) -> tuple[str, int]:
    """링크/Markdown 블록을 자르지 않고 Discord 한 메시지 안에 맞춘다."""
    included: list[str] = []
    for block in blocks:
        candidate = "\n\n".join([*heading, *included, block, footer])
        if len(candidate) > limit:
            break
        included.append(block)
    omitted = len(blocks) - len(included)
    suffix = footer
    if omitted:
        suffix = f"그 외 {omitted}건은 다음 알림에서 이어서 전송합니다.\n\n{footer}"
        while included and len(
            "\n\n".join([*heading, *included, suffix])
        ) > limit:
            included.pop()
            omitted += 1
            suffix = (
                f"그 외 {omitted}건은 다음 알림에서 이어서 전송합니다.\n\n"
                f"{footer}"
            )
    text = "\n\n".join([*heading, *included, suffix])
    return text[:limit], len(included)


class TransferSchoolSelect(discord.ui.Select):
    def __init__(
        self,
        cog: "TransferNoticeCog",
        user_id: int,
        selected: set[str],
    ) -> None:
        self.cog = cog
        self.user_id = int(user_id)
        options = [
            discord.SelectOption(
                label=source.university[:100],
                value=source.source_id,
                description=source.toeic_note[:100],
                default=source.source_id in selected,
            )
            for source in cog.sources.values()
        ]
        super().__init__(
            placeholder="알림 받을 대학을 1~20개 선택",
            min_values=1,
            max_values=len(options),
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if int(interaction.user.id) != self.user_id:
            await interaction.response.send_message(
                "이 대학 선택 메뉴는 요청한 사용자만 바꿀 수 있습니다.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await self.cog._has_consent(self.user_id):
            await interaction.followup.send(
                "개인정보 동의가 철회되었거나 현재 정책과 다릅니다. "
                f"`!개인정보 동의 {consent_command_name(TRANSFER_NOTICE_SCOPE)}`를 "
                "다시 실행해주세요.",
                ephemeral=True,
            )
            return
        await self.cog._save_subscription(self.user_id, set(self.values))
        names = [self.cog.sources[item].university for item in self.values]
        selected = set(self.values)
        saved_row = await self.cog._subscription_row(self.user_id)
        await interaction.edit_original_response(
            embed=self.cog._dashboard_embed(
                selected,
                True,
                delivery_time=(saved_row[5] if saved_row else None),
            ),
            view=TransferDashboardView(
                self.cog,
                user_id=self.user_id,
                selected=selected,
                active=True,
            ),
        )
        await interaction.followup.send(
            "✅ 편입 공지 구독을 저장했습니다.\n"
            f"선택 대학: {', '.join(names)}\n"
            "매일 공식 입학처를 한 번 확인하며, 새 공지나 제목 수정이 있을 때만 "
            "이 DM으로 알려드립니다.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class TransferDashboardView(ReliableView):
    def __init__(
        self,
        cog: "TransferNoticeCog",
        *,
        user_id: int,
        selected: set[str],
        active: bool,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = int(user_id)
        self.add_item(TransferSchoolSelect(cog, user_id, selected))
        self.pause_resume.label = "구독 취소" if active else "구독 재개"
        self.pause_resume.style = (
            discord.ButtonStyle.secondary
            if active
            else discord.ButtonStyle.success
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) == self.user_id:
            return True
        await interaction.response.send_message(
            "이 편입 공지 메뉴는 요청한 사용자만 조작할 수 있습니다.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(
        label="20개 모두 구독",
        style=discord.ButtonStyle.primary,
        emoji="📚",
        row=1,
    )
    async def subscribe_all(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await self.cog._has_consent(self.user_id):
            await interaction.followup.send(
                "현재 개인정보 동의가 없어 저장하지 않았습니다.",
                ephemeral=True,
            )
            return
        await self.cog._save_subscription(
            self.user_id,
            set(self.cog.sources),
        )
        selected = set(self.cog.sources)
        saved_row = await self.cog._subscription_row(self.user_id)
        await interaction.edit_original_response(
            embed=self.cog._dashboard_embed(
                selected,
                True,
                delivery_time=(saved_row[5] if saved_row else None),
            ),
            view=TransferDashboardView(
                self.cog,
                user_id=self.user_id,
                selected=selected,
                active=True,
            ),
        )
        await interaction.followup.send(
            "✅ 20개 대학을 모두 구독했습니다. 새 편입 공지가 있을 때만 DM합니다.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(
        label="최근 공지",
        style=discord.ButtonStyle.secondary,
        emoji="📰",
        row=1,
    )
    async def recent(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        text = await self.cog._recent_text(self.user_id)
        await interaction.followup.send(
            text,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(
        label="구독 취소",
        style=discord.ButtonStyle.secondary,
        emoji="🔕",
        row=1,
    )
    async def pause_resume(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        row = await self.cog._subscription_row(self.user_id)
        if row is None:
            await interaction.followup.send(
                "먼저 위에서 대학을 선택하거나 **20개 모두 구독**을 눌러주세요.",
                ephemeral=True,
            )
            return
        active = bool(row[2])
        await self.cog._set_enabled(self.user_id, not active)
        selected = self.cog._decode_schools(row[1])
        await interaction.edit_original_response(
            embed=self.cog._dashboard_embed(
                selected,
                not active,
                delivery_time=row[5],
            ),
            view=TransferDashboardView(
                self.cog,
                user_id=self.user_id,
                selected=selected,
                active=not active,
            ),
        )
        await interaction.followup.send(
            (
                "🔕 구독을 취소했습니다. 저장한 대학 선택은 유지되며 알림은 보내지 않습니다."
                if active
                else "🔔 구독을 재개했습니다. 기존 대학 선택으로 새 공지를 확인합니다."
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(
        label="알림 시각",
        style=discord.ButtonStyle.secondary,
        emoji="⏰",
        row=1,
    )
    async def delivery_time(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        # Modal은 defer 뒤에 열 수 없으므로 DB 조회 없이 즉시 표시한다. 제출 시
        # update_delivery_time이 구독 존재 여부를 다시 검증한다.
        await interaction.response.send_modal(
            TransferDeliveryTimeModal(self.cog, self.user_id)
        )


class TransferNoticeCog(commands.Cog):
    """공식 입학처 편입 공지를 구독자에게만 DM으로 알린다."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.sources = load_transfer_sources(config.TRANSFER_NOTICE_SOURCE_CONFIG)
        self.output_path = Path(config.TRANSFER_NOTICE_OUTPUT_DIR) / "latest.json"
        self._delivery_lock = asyncio.Lock()
        # 단위 테스트나 schema 검사에 쓰는 최소 bot 객체에서는 background
        # task를 시작하지 않는다. 실제 discord.py Bot만 readiness API를 가진다.
        if hasattr(bot, "wait_until_ready"):
            self.delivery_task.start()
        logger.info("편입 공지 Cog 초기화: sources=%d", len(self.sources))

    def cog_unload(self) -> None:
        if self.delivery_task.is_running():
            self.delivery_task.cancel()

    async def _has_consent(self, user_id: int) -> bool:
        try:
            return await has_current_consent(
                self.bot.db,
                int(user_id),
                TRANSFER_NOTICE_SCOPE,
            )
        except Exception:
            logger.error(
                "편입 공지 동의 상태 확인 실패: user_id=%s",
                user_id,
                exc_info=True,
            )
            return False

    async def _send_consent_prompt(self, destination, user_id: int) -> None:
        privacy = self.bot.get_cog("PrivacyCog")
        prefix = (
            "📚 20개 대학의 편입 공지를 개인 DM으로 받으려면 "
            "대학 선택과 Discord 사용자 ID를 저장해야 합니다."
        )
        if privacy is not None:
            await privacy.send_consent_prompt(
                destination,
                user_id=int(user_id),
                scope=TRANSFER_NOTICE_SCOPE,
                prefix=prefix,
                on_granted=lambda _interaction: self.send_dashboard(destination),
            )
            return
        await destination.send(
            f"{prefix}\n`!개인정보 동의 "
            f"{consent_command_name(TRANSFER_NOTICE_SCOPE)}`를 실행해주세요."
        )

    async def _subscription_row(self, user_id: int):
        async with self.bot.db.execute(
            """
            SELECT user_id, schools_json, enabled, created_at, updated_at,
                   delivery_time
            FROM transfer_notice_subscriptions
            WHERE user_id = ?
            """,
            (int(user_id),),
        ) as cursor:
            return await cursor.fetchone()

    async def _save_subscription(
        self,
        user_id: int,
        schools: set[str],
    ) -> None:
        selected = sorted(set(schools) & set(self.sources))
        if not selected:
            raise ValueError("한 개 이상의 지원 대학을 선택해야 합니다.")
        if not await self._has_consent(user_id):
            raise RuntimeError("편입 공지 개인정보 동의가 필요합니다.")
        now = _utc_now()
        payload = json.dumps(selected, ensure_ascii=False, separators=(",", ":"))
        backend = str(getattr(self.bot.db, "backend", config.DB_BACKEND))
        if backend == "tidb":
            query = """
                INSERT INTO transfer_notice_subscriptions (
                    user_id, schools_json, enabled, delivery_time,
                    created_at, updated_at
                ) VALUES (?, ?, TRUE, ?, ?, ?)
                ON DUPLICATE KEY UPDATE
                    schools_json = VALUES(schools_json),
                    enabled = TRUE,
                    updated_at = VALUES(updated_at)
            """
        else:
            query = """
                INSERT INTO transfer_notice_subscriptions (
                    user_id, schools_json, enabled, delivery_time,
                    created_at, updated_at
                ) VALUES (?, ?, 1, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    schools_json = excluded.schools_json,
                    enabled = 1,
                    updated_at = excluded.updated_at
            """
        await self.bot.db.execute(
            query,
            (
                int(user_id),
                payload,
                config.TRANSFER_NOTICE_DEFAULT_DELIVERY_TIME,
                now,
                now,
            ),
        )
        await self.bot.db.commit()

    async def update_delivery_time(self, user_id: int, value: object) -> str:
        normalized = _normalize_delivery_time(value)
        row = await self._subscription_row(user_id)
        if row is None:
            raise ValueError("먼저 편입 공지 구독을 저장해주세요.")
        await self.bot.db.execute(
            """
            UPDATE transfer_notice_subscriptions
            SET delivery_time = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (normalized, _utc_now(), int(user_id)),
        )
        await self.bot.db.commit()
        return normalized

    async def _set_enabled(self, user_id: int, enabled: bool) -> None:
        await self.bot.db.execute(
            """
            UPDATE transfer_notice_subscriptions
            SET enabled = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (bool(enabled), _utc_now(), int(user_id)),
        )
        await self.bot.db.commit()

    @staticmethod
    def _decode_schools(raw: object) -> set[str]:
        try:
            decoded = json.loads(str(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            return set()
        if not isinstance(decoded, list):
            return set()
        return {str(item) for item in decoded}

    def _load_payload_path(self, path: Path) -> dict | None:
        try:
            stat = path.stat()
            if stat.st_size <= 0 or stat.st_size > _MAX_OUTPUT_BYTES:
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or payload.get("schema_version") != 1
                or not isinstance(payload.get("changes"), list)
                or not isinstance(payload.get("latest"), list)
            ):
                return None
            payload["changes"] = [
                item
                for item in payload["changes"]
                if self._valid_public_item(item, require_change=True)
            ]
            payload["latest"] = [
                item
                for item in payload["latest"]
                if self._valid_public_item(item, require_change=False)
            ]
            return payload
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def _load_payload(self) -> dict | None:
        return self._load_payload_path(self.output_path)

    def _load_delivery_payloads(self) -> list[dict]:
        """최근 이벤트와 latest를 함께 읽어 재시작 중 놓친 변경을 복구합니다."""
        candidates: list[dict] = []
        event_dir = self.output_path.parent / "events"
        try:
            paths = sorted(
                event_dir.glob("*.json"),
                key=lambda path: path.name,
                reverse=True,
            )[:_MAX_EVENT_FILES]
        except OSError:
            paths = []
        latest = self._load_payload()
        if latest is not None:
            candidates.append(latest)
        for path in paths:
            payload = self._load_payload_path(path)
            if payload is not None:
                candidates.append(payload)

        cutoff = datetime.now(timezone.utc) - timedelta(
            days=_DELIVERY_BACKLOG_DAYS
        )
        unique: dict[str, dict] = {}
        for payload in candidates:
            generated_at = str(payload.get("generated_at") or "")
            run_id = str(payload.get("run_id") or "")
            if not generated_at or not run_id:
                continue
            try:
                generated = datetime.fromisoformat(generated_at)
                if generated.tzinfo is None:
                    generated = generated.replace(tzinfo=timezone.utc)
                if generated.astimezone(timezone.utc) < cutoff:
                    continue
            except ValueError:
                continue
            unique[run_id] = payload
        return sorted(
            unique.values(),
            key=lambda payload: str(payload.get("generated_at") or ""),
        )

    def _valid_public_item(
        self,
        item: object,
        *,
        require_change: bool,
    ) -> bool:
        if not isinstance(item, dict):
            return False
        source_id = str(item.get("source_id") or "")
        source = self.sources.get(source_id)
        if source is None:
            return False
        parsed = urlparse(str(item.get("url") or ""))
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").casefold()
            not in set(source.allowed_hosts)
        ):
            return False
        if (
            not str(item.get("external_id") or "")
            or not str(item.get("title") or "")
            or len(str(item.get("title") or "")) > 300
            or len(str(item.get("url") or "")) > 1_000
        ):
            return False
        try:
            if int(item.get("revision") or 0) < 1:
                return False
        except (TypeError, ValueError):
            return False
        if require_change and item.get("change_type") not in {"new", "updated"}:
            return False
        return True

    def _dashboard_embed(
        self,
        selected: set[str],
        active: bool,
        *,
        delivery_time: object | None = None,
    ) -> discord.Embed:
        selected_names = [
            self.sources[item].university
            for item in sorted(selected)
            if item in self.sources
        ]
        description = (
            "선택한 대학의 공식 입학처를 매일 순서대로 확인해요.\n"
            "- 새 글이나 중요한 수정이 있을 때만 DM\n"
            "- 목록과 필요한 상세 본문을 함께 확인\n"
            "- 점수·학력·지원 학과·실명·연락처는 수집하지 않음\n"
            "지원 조건은 알림에 연결된 해당 연도 모집요강에서 최종 확인해주세요."
        )
        if selected_names:
            rendered_delivery_time = (
                str(delivery_time or "").strip()
                or config.TRANSFER_NOTICE_DEFAULT_DELIVERY_TIME
            )
            description += (
                "\n\n**현재 선택**\n"
                + ", ".join(selected_names)
                + f"\n상태: **{'구독 중' if active else '구독 취소됨'}**"
                + f" · 알림: **{rendered_delivery_time} KST**"
            )
        else:
            description += "\n\n아래에서 관심 대학을 선택하면 구독이 시작됩니다."
        payload = self._load_payload()
        if payload is not None:
            healthy = int(payload.get("healthy_count") or 0)
            source_count = int(payload.get("source_count") or len(self.sources))
            description += (
                f"\n\n**최근 수집 상태:** {healthy}/{source_count}개교 정상"
            )
            if healthy < source_count:
                description += (
                    "\n일부 공식 사이트의 자동 접근 제한·점검으로 누락 가능성이 "
                    "있습니다. `!편입 최근`에서 마지막 확인 상태를 확인하세요."
                )
        embed = discord.Embed(
            title="📚 편입 공지",
            description=description,
            color=0x2457A7,
        )
        return embed

    async def send_dashboard(self, destination) -> None:
        if getattr(destination, "guild", None) is not None:
            await destination.send(
                "🔒 학교·편입 공지는 개인 정보 보호를 위해 DM에서만 "
                "사용할 수 있습니다. 마사몽에게 DM으로 `!편입`을 보내주세요."
            )
            return
        user_id = int(destination.author.id)
        if not await self._has_consent(user_id):
            await self._send_consent_prompt(destination, user_id)
            return
        row = await self._subscription_row(user_id)
        selected = self._decode_schools(row[1]) if row else set()
        active = bool(row and row[2])
        await destination.send(
            embed=self._dashboard_embed(
                selected,
                active,
                delivery_time=(row[5] if row else None),
            ),
            view=TransferDashboardView(
                self,
                user_id=user_id,
                selected=selected,
                active=active,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _recent_text(self, user_id: int) -> str:
        if not await self._has_consent(user_id):
            return "현재 개인정보 동의가 없어 구독 정보를 이용할 수 없습니다."
        row = await self._subscription_row(user_id)
        if row is None or not bool(row[2]):
            return "편입 공지를 구독 중인 사용자만 최근 브리핑을 볼 수 있습니다."
        selected = self._decode_schools(row[1])
        payload = self._load_payload()
        if payload is None:
            return "아직 정상 완료된 편입 공지 수집 결과가 없습니다."
        items = [
            item
            for item in payload["latest"]
            if str(item.get("source_id")) in selected
        ][:10]
        if not items:
            return "선택한 대학에서 저장된 편입 공지를 아직 찾지 못했습니다."
        heading = [
            "📚 **최근 편입 공지**",
            f"마지막 확인: `{payload.get('generated_at', '알 수 없음')}`",
            (
                "수집 상태: "
                f"**{int(payload.get('healthy_count') or 0)}/"
                f"{int(payload.get('source_count') or len(self.sources))}개교 정상**"
            ),
        ]
        blocks: list[str] = []
        for item in items:
            blocks.append(_notice_block(item))
        text, _ = _fit_notice_blocks(
            heading,
            blocks,
            "지원 조건은 공식 원문의 해당 연도 모집요강에서 최종 확인해주세요.",
        )
        return text

    async def _subscriber_rows(
        self,
        generated_at: str,
        *,
        current_time: str | None = None,
    ) -> list:
        due_time = current_time or datetime.now(KST).strftime("%H:%M")
        async with self.bot.db.execute(
            """
            SELECT ts.user_id, ts.schools_json, ts.updated_at,
                   ts.delivery_time
            FROM transfer_notice_subscriptions AS ts
            JOIN privacy_consents AS pc
              ON pc.user_id = ts.user_id
             AND pc.scope = ?
             AND pc.policy_version = ?
             AND pc.notice_hash = ?
             AND pc.status = ?
             AND pc.granted_at IS NOT NULL
             AND pc.withdrawn_at IS NULL
            WHERE ts.enabled = 1
              AND ts.updated_at <= ?
              AND ts.delivery_time <= ?
            ORDER BY ts.user_id
            """,
            (
                TRANSFER_POLICY.scope,
                TRANSFER_POLICY.version,
                TRANSFER_POLICY.notice_hash,
                CONSENT_GRANTED,
                generated_at,
                due_time,
            ),
        ) as cursor:
            return list(await cursor.fetchall())

    async def _delivery_row(self, user_id: int, item: dict):
        async with self.bot.db.execute(
            """
            SELECT status, attempt_count, next_attempt_at
            FROM transfer_notice_deliveries
            WHERE user_id = ? AND source_id = ?
              AND external_id = ? AND revision = ?
            """,
            (
                user_id,
                item["source_id"],
                item["external_id"],
                int(item.get("revision") or 1),
            ),
        ) as cursor:
            return await cursor.fetchone()

    async def _reserve_delivery(
        self,
        user_id: int,
        run_id: str,
        item: dict,
    ) -> bool:
        row = await self._delivery_row(user_id, item)
        if row:
            status = str(row[0])
            attempts = int(row[1] or 0)
            if status in {"sent", "processing", "failed"}:
                return False
            if attempts >= config.TRANSFER_NOTICE_DELIVERY_MAX_ATTEMPTS:
                return False
            if row[2]:
                try:
                    if datetime.fromisoformat(str(row[2])) > datetime.now(timezone.utc):
                        return False
                except ValueError:
                    pass
        attempts = int(row[1] or 0) + 1 if row else 1
        now = _utc_now()
        backend = str(getattr(self.bot.db, "backend", config.DB_BACKEND))
        params = (
            user_id,
            run_id,
            item["source_id"],
            item["external_id"],
            int(item.get("revision") or 1),
            json.dumps(item, ensure_ascii=False, separators=(",", ":")),
            attempts,
            now,
        )
        if backend == "tidb":
            query = """
                INSERT INTO transfer_notice_deliveries (
                    user_id, run_id, source_id, external_id, revision,
                    payload_json, status, attempt_count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'processing', ?, ?)
                ON DUPLICATE KEY UPDATE
                    run_id = VALUES(run_id), status = 'processing',
                    payload_json = VALUES(payload_json),
                    attempt_count = VALUES(attempt_count),
                    next_attempt_at = NULL, updated_at = VALUES(updated_at)
            """
        else:
            query = """
                INSERT INTO transfer_notice_deliveries (
                    user_id, run_id, source_id, external_id, revision,
                    payload_json, status, attempt_count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'processing', ?, ?)
                ON CONFLICT(user_id, source_id, external_id, revision)
                DO UPDATE SET
                    run_id = excluded.run_id, status = 'processing',
                    payload_json = excluded.payload_json,
                    attempt_count = excluded.attempt_count,
                    next_attempt_at = NULL, updated_at = excluded.updated_at
            """
        await self.bot.db.execute(query, params)
        await self.bot.db.commit()
        return True

    async def _finish_deliveries(
        self,
        user_id: int,
        items: list[dict],
        *,
        sent: bool,
        error: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        next_attempt = (
            now + timedelta(minutes=config.TRANSFER_NOTICE_DELIVERY_RETRY_MINUTES)
        ).isoformat(timespec="seconds")
        for item in items:
            row = await self._delivery_row(user_id, item)
            attempts = int(row[1] or 1) if row else 1
            terminal = (
                sent
                or error
                in {
                    "discord_forbidden",
                    "consent_withdrawn",
                    "subscription_changed",
                }
                or attempts >= config.TRANSFER_NOTICE_DELIVERY_MAX_ATTEMPTS
            )
            status = "sent" if sent else "failed" if terminal else "retry"
            await self.bot.db.execute(
                """
                UPDATE transfer_notice_deliveries
                SET status = ?, next_attempt_at = ?, delivered_at = ?,
                    last_error = ?, updated_at = ?
                WHERE user_id = ? AND source_id = ?
                  AND external_id = ? AND revision = ?
                """,
                (
                    status,
                    None if terminal else next_attempt,
                    _utc_now() if sent else None,
                    error,
                    _utc_now(),
                    user_id,
                    item["source_id"],
                    item["external_id"],
                    int(item.get("revision") or 1),
                ),
            )
        await self.bot.db.commit()

    async def _defer_deliveries(
        self,
        user_id: int,
        items: list[dict],
    ) -> None:
        """Discord 한도 때문에 다음 페이지로 넘긴 항목은 실패 횟수를 소비하지 않는다."""
        now = _utc_now()
        for item in items:
            await self.bot.db.execute(
                """
                UPDATE transfer_notice_deliveries
                SET status = 'retry',
                    attempt_count = CASE
                        WHEN attempt_count > 0 THEN attempt_count - 1
                        ELSE 0
                    END,
                    next_attempt_at = ?, last_error = 'page_deferred',
                    updated_at = ?
                WHERE user_id = ? AND source_id = ?
                  AND external_id = ? AND revision = ?
                  AND status = 'processing'
                """,
                (
                    now,
                    now,
                    user_id,
                    item["source_id"],
                    item["external_id"],
                    int(item.get("revision") or 1),
                ),
            )
        await self.bot.db.commit()

    async def _subscription_matches(
        self,
        user_id: int,
        items: list[dict],
        *,
        expected_updated_at: str | None,
    ) -> bool:
        row = await self._subscription_row(user_id)
        if row is None or not bool(row[2]):
            return False
        if (
            expected_updated_at is not None
            and str(row[4]) != str(expected_updated_at)
        ):
            return False
        selected = self._decode_schools(row[1])
        return all(str(item.get("source_id")) in selected for item in items)

    async def _send_user_changes(
        self,
        user_id: int,
        run_id: str,
        items: list[dict],
        *,
        expected_subscription_updated_at: str | None = None,
    ) -> str:
        if not await self._subscription_matches(
            user_id,
            items,
            expected_updated_at=expected_subscription_updated_at,
        ):
            return "subscription_changed"
        reserved: list[dict] = []
        for item in items[: config.TRANSFER_NOTICE_MAX_ITEMS_PER_DM * 4]:
            if await self._reserve_delivery(user_id, run_id, item):
                reserved.append(item)
            if len(reserved) >= config.TRANSFER_NOTICE_MAX_ITEMS_PER_DM:
                break
        if not reserved:
            return "none"
        if not await self._has_consent(user_id):
            await self._finish_deliveries(
                user_id,
                reserved,
                sent=False,
                error="consent_withdrawn",
            )
            return "consent_withdrawn"
        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await asyncio.wait_for(self.bot.fetch_user(user_id), timeout=5)
            except Exception as exc:
                await self._finish_deliveries(
                    user_id,
                    reserved,
                    sent=False,
                    error=type(exc).__name__[:64],
                )
                return "fetch_failed"
        heading = [
            "📚 **편입 공지 새 소식**",
            "선택한 대학의 공식 입학처에 새 글 또는 수정 글이 확인되었습니다.",
        ]
        blocks: list[str] = []
        for item in reserved:
            label = "수정" if item.get("change_type") == "updated" else "새 글"
            blocks.append(_notice_block(item, change_label=label))
        message_text, included_count = _fit_notice_blocks(
            heading,
            blocks,
            "지원 자격·반영 기준·모집단위는 해당 연도 최종 모집요강을 "
            "확인하세요. 이 알림은 지원 가능 여부를 판정하지 않습니다.",
        )
        deferred = reserved[included_count:]
        if deferred:
            await self._defer_deliveries(user_id, deferred)
            reserved = reserved[:included_count]
        if not reserved:
            return "none"
        if not await self._has_consent(user_id):
            await self._finish_deliveries(
                user_id,
                reserved,
                sent=False,
                error="consent_withdrawn",
            )
            return "consent_withdrawn"
        if not await self._subscription_matches(
            user_id,
            reserved,
            expected_updated_at=expected_subscription_updated_at,
        ):
            await self._finish_deliveries(
                user_id,
                reserved,
                sent=False,
                error="subscription_changed",
            )
            return "subscription_changed"
        try:
            await asyncio.wait_for(
                user.send(
                    message_text,
                    allowed_mentions=discord.AllowedMentions.none(),
                ),
                timeout=12,
            )
        except discord.Forbidden:
            await self._finish_deliveries(
                user_id,
                reserved,
                sent=False,
                error="discord_forbidden",
            )
            return "forbidden"
        except Exception as exc:
            await self._finish_deliveries(
                user_id,
                reserved,
                sent=False,
                error=type(exc).__name__[:64],
            )
            return "retry"
        await self._finish_deliveries(user_id, reserved, sent=True)
        return "sent"

    async def _delivery_tick(self) -> str:
        retry_result = await self._retry_delivery_tick()
        if retry_result != "idle":
            return retry_result
        payloads = self._load_delivery_payloads()
        for payload in payloads:
            if not payload["changes"]:
                continue
            generated_at = str(payload.get("generated_at") or "")
            if not generated_at:
                continue
            subscribers = await self._subscriber_rows(generated_at)
            for row in subscribers:
                selected = self._decode_schools(row[1])
                items = [
                    item
                    for item in payload["changes"]
                    if item.get("source_id") in selected
                ]
                if not items:
                    continue
                result = await self._send_user_changes(
                    int(row[0]),
                    str(payload["run_id"]),
                    items,
                    expected_subscription_updated_at=str(row[2]),
                )
                if result != "none":
                    return result
        return "idle"

    async def _retry_delivery_tick(self) -> str:
        """수집 output 교체와 무관하게 due 상태 한 사용자의 실패 DM을 복구한다.

        실패 이후 구독을 취소·재개하거나 대학 선택을 바꾼 경우, 또는 동의를
        철회했다가 다시 한 경우에는 그보다 오래된 알림을 되살리지 않는다.
        """
        now = _utc_now()
        async with self.bot.db.execute(
            """
            SELECT d.user_id, d.run_id, d.payload_json, ts.schools_json,
                   d.source_id, d.external_id, d.revision, ts.updated_at
            FROM transfer_notice_deliveries AS d
            JOIN transfer_notice_subscriptions AS ts
              ON ts.user_id = d.user_id
             AND ts.enabled = 1
             AND ts.updated_at <= d.updated_at
            JOIN privacy_consents AS pc
              ON pc.user_id = d.user_id
             AND pc.scope = ?
             AND pc.policy_version = ?
             AND pc.notice_hash = ?
             AND pc.status = ?
             AND pc.granted_at IS NOT NULL
             AND pc.withdrawn_at IS NULL
             AND pc.granted_at <= d.updated_at
            WHERE d.status = 'retry'
              AND COALESCE(d.next_attempt_at, ?) <= ?
              AND d.attempt_count < ?
            ORDER BY d.updated_at, d.user_id
            LIMIT ?
            """,
            (
                TRANSFER_POLICY.scope,
                TRANSFER_POLICY.version,
                TRANSFER_POLICY.notice_hash,
                CONSENT_GRANTED,
                now,
                now,
                config.TRANSFER_NOTICE_DELIVERY_MAX_ATTEMPTS,
                config.TRANSFER_NOTICE_MAX_ITEMS_PER_DM * 4,
            ),
        ) as cursor:
            rows = list(await cursor.fetchall())
        if not rows:
            return "idle"
        first_user_id = int(rows[0][0])
        selected = self._decode_schools(rows[0][3])
        run_id = str(rows[0][1])
        items: list[dict] = []
        invalid_keys: list[tuple[str, str, int]] = []
        for row in rows:
            if int(row[0]) != first_user_id:
                continue
            try:
                item = json.loads(str(row[2]))
            except (TypeError, ValueError, json.JSONDecodeError):
                invalid_keys.append((str(row[4]), str(row[5]), int(row[6])))
                continue
            if (
                isinstance(item, dict)
                and str(item.get("source_id")) in selected
                and item.get("external_id")
                and item.get("url")
                and item.get("title")
            ):
                items.append(item)
            else:
                invalid_keys.append((str(row[4]), str(row[5]), int(row[6])))
            if len(items) >= config.TRANSFER_NOTICE_MAX_ITEMS_PER_DM:
                break
        for source_id, external_id, revision in invalid_keys:
            await self.bot.db.execute(
                """
                UPDATE transfer_notice_deliveries
                SET status = 'failed', next_attempt_at = NULL,
                    last_error = 'invalid_retry_payload', updated_at = ?
                WHERE user_id = ? AND source_id = ?
                  AND external_id = ? AND revision = ?
                  AND status = 'retry'
                """,
                (
                    _utc_now(),
                    first_user_id,
                    source_id,
                    external_id,
                    revision,
                ),
            )
        if invalid_keys:
            await self.bot.db.commit()
        if not items:
            return "invalid"
        return await self._send_user_changes(
            first_user_id,
            run_id,
            items,
            expected_subscription_updated_at=str(rows[0][7]),
        )

    @tasks.loop(minutes=1)
    async def delivery_task(self) -> None:
        if self._delivery_lock.locked():
            return
        async with self._delivery_lock:
            try:
                await asyncio.wait_for(self._delivery_tick(), timeout=45)
            except asyncio.TimeoutError:
                logger.error("편입 공지 전달 tick이 45초를 초과했습니다.")
            except Exception:
                logger.error("편입 공지 전달 tick 실패", exc_info=True)

    @delivery_task.before_loop
    async def before_delivery_task(self) -> None:
        await self.bot.wait_until_ready()

    @commands.group(name="편입", aliases=["편입공지"], invoke_without_command=True)
    @commands.dm_only()
    async def transfer(self, ctx: commands.Context) -> None:
        """편입 공지 구독 메뉴를 엽니다. (DM 전용)"""
        if ctx.invoked_subcommand is None:
            await self.send_dashboard(ctx)

    @transfer.command(name="최근", aliases=["브리핑", "공지"])
    @commands.dm_only()
    async def recent(self, ctx: commands.Context) -> None:
        """구독 대학의 최근 편입 공지를 확인합니다."""
        await ctx.send(
            await self._recent_text(ctx.author.id),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @transfer.command(name="상태")
    @commands.dm_only()
    async def status(self, ctx: commands.Context) -> None:
        """내 편입 공지 구독 상태를 확인합니다."""
        if not await self._has_consent(ctx.author.id):
            await self._send_consent_prompt(ctx, ctx.author.id)
            return
        row = await self._subscription_row(ctx.author.id)
        if row is None:
            await ctx.send("아직 편입 공지를 구독하지 않았습니다. `!편입`에서 선택해주세요.")
            return
        selected = self._decode_schools(row[1])
        names = [
            self.sources[item].university
            for item in sorted(selected)
            if item in self.sources
        ]
        await ctx.send(
            f"📚 편입 공지: **{'구독 중' if row[2] else '구독 취소됨'}**\n"
            f"선택 대학 {len(names)}곳: {', '.join(names)}\n"
            f"알림 시각: **{row[5]} (한국 시간)**",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @transfer.command(name="구독취소", aliases=["해지", "중지"])
    @commands.dm_only()
    async def unsubscribe(self, ctx: commands.Context) -> None:
        """편입 공지 알림을 중단하고 대학 선택은 유지합니다."""
        row = await self._subscription_row(ctx.author.id)
        if row is None or not bool(row[2]):
            await ctx.send("현재 활성화된 편입 공지 구독이 없습니다.")
            return
        await self._set_enabled(ctx.author.id, False)
        await ctx.send("🔕 편입 공지 구독을 취소했습니다. 이후 알림은 보내지 않습니다.")

    @transfer.command(name="재개")
    @commands.dm_only()
    async def resume(self, ctx: commands.Context) -> None:
        """저장한 대학 선택으로 편입 공지 알림을 재개합니다."""
        if not await self._has_consent(ctx.author.id):
            await self._send_consent_prompt(ctx, ctx.author.id)
            return
        row = await self._subscription_row(ctx.author.id)
        if row is None:
            await ctx.send("저장된 대학 선택이 없습니다. `!편입`에서 먼저 선택해주세요.")
            return
        await self._set_enabled(ctx.author.id, True)
        await ctx.send("🔔 저장된 대학 선택으로 편입 공지 구독을 재개했습니다.")

    @transfer.command(name="시간", aliases=["알림시간"])
    @commands.dm_only()
    async def delivery_time_command(
        self,
        ctx: commands.Context,
        value: str,
    ) -> None:
        """편입 공지 알림 시각을 한국 시간 HH:MM으로 변경합니다."""
        try:
            normalized = await self.update_delivery_time(ctx.author.id, value)
        except ValueError as exc:
            await ctx.send(str(exc))
            return
        await ctx.send(
            f"⏰ 편입 공지 알림 시각을 **{normalized} (한국 시간)**으로 변경했습니다."
        )

    @transfer.command(name="삭제")
    @commands.dm_only()
    async def delete(self, ctx: commands.Context) -> None:
        """편입 공지 대학 선택·구독·전달 기록을 삭제합니다."""
        user_id = int(ctx.author.id)
        await self.bot.db.execute(
            "DELETE FROM transfer_notice_deliveries WHERE user_id = ?",
            (user_id,),
        )
        await self.bot.db.execute(
            "DELETE FROM transfer_notice_subscriptions WHERE user_id = ?",
            (user_id,),
        )
        await self.bot.db.commit()
        await withdraw_consent(self.bot.db, user_id, TRANSFER_NOTICE_SCOPE)
        await ctx.send(
            "🗑️ 편입 공지 대학 선택·구독·전달 기록을 삭제하고 동의를 철회했습니다.\n"
            "동의·철회 증빙용 감사 이력은 별도 보관됩니다."
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TransferNoticeCog(bot))

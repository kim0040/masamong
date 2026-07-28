# -*- coding: utf-8 -*-
"""학교 공지 digest를 사용자에게 전달하고 피드백을 수집하는 Cog입니다.

이 Cog는 크롤링도 LLM 분석도 하지 않습니다. 별도 batch 프로세스가 만든 digest
JSON을 읽어 Discord로 표현하고, 버튼 피드백을 DB에 기록할 뿐입니다. 수집을 봇
프로세스 밖에 두는 것이 저사양 서버에서 봇 응답성을 지키는 핵심입니다.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time as dt_time
from pathlib import Path

import discord
import pytz
from discord.ext import commands, tasks

import config
from logger_config import logger
from utils.school_notice_contract import (
    FEEDBACK_TYPES,
    Digest,
    DigestContractError,
    DigestItem,
    digest_path_for,
    load_digest,
)
from utils.school_notice_render import chunk_embeds, render_digest

KST = pytz.timezone("Asia/Seoul")

# 버튼에 노출할 피드백. 코어의 전체 타입 중 일상적으로 쓰는 것만 둔다.
# `not_interested`는 영구 차단이 아니라 90일 반감기로 감쇠하는 완만한 신호다.
_FEEDBACK_BUTTONS = (
    ("useful", "도움 됨", discord.ButtonStyle.success),
    ("applied", "지원함", discord.ButtonStyle.primary),
    ("not_interested", "관심 없음", discord.ButtonStyle.secondary),
    ("completed", "완료", discord.ButtonStyle.secondary),
)


def user_key_for(user_id: int) -> str:
    """Discord 사용자와 코어 프로필을 잇는 안정적 키."""
    return f"discord-{int(user_id)}"


def _now_text() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


class FeedbackView(discord.ui.View):
    """공지 한 건에 대한 피드백 버튼.

    timeout=None이라 봇 재시작 후에는 동작하지 않습니다. 영속 View로 만들려면
    custom_id 기반 재등록이 필요하지만, digest는 매일 새로 전달되므로 당일
    상호작용만 지원해도 충분합니다.
    """

    def __init__(self, cog: "SchoolNoticeCog", item: DigestItem) -> None:
        super().__init__(timeout=None)
        self._cog = cog
        self._source_id, self._external_id = item.feedback_key()
        for feedback_type, label, style in _FEEDBACK_BUTTONS:
            self.add_item(_FeedbackButton(self, feedback_type, label, style))

    async def submit(
        self,
        interaction: discord.Interaction,
        feedback_type: str,
    ) -> None:
        stored = await self._cog.record_feedback(
            user_id=interaction.user.id,
            source_id=self._source_id,
            external_id=self._external_id,
            feedback_type=feedback_type,
            interaction_id=str(interaction.id),
        )
        if stored:
            message = "기록했습니다. 내일 digest부터 반영됩니다."
            if feedback_type == "not_interested":
                # 사용자가 "영구 차단"으로 오해하지 않게 설계 의도를 밝힌다.
                message += (
                    "\n비슷한 주제를 완전히 차단하지는 않고 우선순위만 낮춥니다. "
                    "`!공지 음소거`로 주제를 직접 숨기거나 해제할 수 있습니다."
                )
        else:
            message = "이미 반영된 피드백입니다."
        await interaction.response.send_message(message, ephemeral=True)


class _FeedbackButton(discord.ui.Button):
    def __init__(
        self,
        view: FeedbackView,
        feedback_type: str,
        label: str,
        style: discord.ButtonStyle,
    ) -> None:
        super().__init__(label=label, style=style)
        self._parent = view
        self._feedback_type = feedback_type

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._parent.submit(interaction, self._feedback_type)


class SchoolNoticeCog(commands.Cog):
    """digest 전달·프로필 관리·피드백 수집."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.digest_dir = Path(config.SCHOOL_NOTICE_DIGEST_DIR).expanduser()
        if config.SCHOOL_NOTICE_ENABLED:
            self.delivery_task.change_interval(
                time=dt_time(
                    hour=config.SCHOOL_NOTICE_DELIVERY_TIME["hour"],
                    minute=config.SCHOOL_NOTICE_DELIVERY_TIME["minute"],
                    tzinfo=KST,
                )
            )
            self.delivery_task.start()
            logger.info(
                "학교 공지 전달 스케줄러 시작: %02d:%02d KST",
                config.SCHOOL_NOTICE_DELIVERY_TIME["hour"],
                config.SCHOOL_NOTICE_DELIVERY_TIME["minute"],
            )
        else:
            logger.info("학교 공지 기능이 비활성화되어 스케줄러를 시작하지 않습니다.")

    def cog_unload(self) -> None:
        if self.delivery_task.is_running():
            self.delivery_task.cancel()

    # ------------------------------------------------------------------
    # 저장소 접근
    # ------------------------------------------------------------------

    async def active_profiles(self) -> list[tuple[int, str]]:
        """전달 대상 사용자 목록을 반환합니다."""
        async with self.bot.db.execute(
            "SELECT user_id, user_key FROM school_notice_profiles WHERE enabled = 1"
        ) as cursor:
            rows = await cursor.fetchall()
        return [(int(row[0]), str(row[1])) for row in rows]

    async def record_feedback(
        self,
        *,
        user_id: int,
        source_id: str,
        external_id: str,
        feedback_type: str,
        interaction_id: str,
        topic: str | None = None,
    ) -> bool:
        """피드백을 기록합니다. 이미 처리한 interaction이면 False를 반환합니다."""
        if feedback_type not in FEEDBACK_TYPES:
            raise ValueError(f"지원하지 않는 피드백 종류입니다: {feedback_type}")
        async with self.bot.db.execute(
            "SELECT 1 FROM school_notice_feedback WHERE interaction_id = ?",
            (str(interaction_id),),
        ) as cursor:
            if await cursor.fetchone():
                # 버튼 연타로 같은 신호가 여러 번 쌓이면 점수가 왜곡된다.
                return False
        await self.bot.db.execute(
            """
            INSERT INTO school_notice_feedback
                (user_key, source_id, external_id, feedback_type, topic,
                 interaction_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_key_for(user_id),
                str(source_id),
                str(external_id),
                feedback_type,
                topic,
                str(interaction_id),
                _now_text(),
            ),
        )
        await self.bot.db.commit()
        return True

    async def already_delivered(
        self,
        *,
        user_key: str,
        digest_date: date,
        notice_id: int,
    ) -> bool:
        async with self.bot.db.execute(
            """
            SELECT 1 FROM school_notice_deliveries
            WHERE user_key = ? AND digest_date = ? AND notice_id = ?
            """,
            (user_key, digest_date.isoformat(), int(notice_id)),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def mark_delivered(
        self,
        *,
        user_key: str,
        digest_date: date,
        notice_id: int,
        status: str,
        failure_reason: str | None = None,
    ) -> None:
        await self.bot.db.execute(
            """
            INSERT INTO school_notice_deliveries
                (user_key, digest_date, notice_id, status, failure_reason, delivered_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_key,
                digest_date.isoformat(),
                int(notice_id),
                status,
                failure_reason,
                _now_text(),
            ),
        )
        await self.bot.db.commit()

    # ------------------------------------------------------------------
    # digest 읽기와 전달
    # ------------------------------------------------------------------

    def load_user_digest(self, user_key: str, digest_date: date) -> Digest:
        """사용자별 디렉터리에서 digest를 읽습니다.

        코어는 파일명에 user_key를 넣지 않으므로 사용자별 하위 디렉터리로
        분리해야 서로 덮어쓰지 않습니다.
        """
        path = digest_path_for(self.digest_dir / user_key, digest_date)
        return load_digest(
            path,
            expected_schema_version=config.SCHOOL_NOTICE_SCHEMA_VERSION,
        )

    async def deliver_to_user(
        self,
        user_id: int,
        user_key: str,
        digest_date: date,
    ) -> str:
        """한 사용자에게 digest를 전달하고 결과 상태를 반환합니다."""
        try:
            digest = self.load_user_digest(user_key, digest_date)
        except DigestContractError as exc:
            # 계약이 깨진 digest를 부분 렌더링하면 잘못된 마감·자격을 보여줄 수 있다.
            logger.warning("학교 공지 digest를 사용할 수 없습니다 (%s): %s", user_key, exc)
            return "contract_error"

        visible = digest.visible_items()
        pending = [
            item
            for item in visible
            if not await self.already_delivered(
                user_key=user_key, digest_date=digest_date, notice_id=item.notice_id
            )
        ]
        health = digest.collection_health
        stale = bool(
            config.SCHOOL_NOTICE_STALE_WARNING_ENABLED
            and health is not None
            and health.has_problem
        )
        if not pending and not stale:
            # 이미 다 보냈고 알릴 이상도 없다.
            return "nothing_to_send"

        user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
        embeds = render_digest(
            _digest_with_items(digest, pending),
            max_items=config.SCHOOL_NOTICE_MAX_ITEMS_PER_DM,
            today=digest_date,
        )
        try:
            for group in chunk_embeds(embeds):
                await user.send(embeds=group)
            for item in pending[: config.SCHOOL_NOTICE_MAX_ITEMS_PER_DM]:
                await user.send(
                    content=f"위 공지 피드백: {item.title[:80]}",
                    view=FeedbackView(self, item),
                )
        except discord.Forbidden:
            logger.info("학교 공지 DM이 차단되어 있습니다: user_id=%s", user_id)
            return "dm_blocked"
        except discord.HTTPException as exc:
            logger.warning("학교 공지 DM 전송 실패 user_id=%s: %s", user_id, exc)
            return "send_failed"

        for item in pending[: config.SCHOOL_NOTICE_MAX_ITEMS_PER_DM]:
            await self.mark_delivered(
                user_key=user_key,
                digest_date=digest_date,
                notice_id=item.notice_id,
                status="sent",
            )
        return "sent"

    @tasks.loop(time=dt_time(hour=8, minute=10, tzinfo=KST))
    async def delivery_task(self) -> None:
        """하루 한 번 활성 사용자에게 digest를 전달합니다."""
        if not config.SCHOOL_NOTICE_ENABLED:
            return
        today = datetime.now(KST).date()
        try:
            profiles = await self.active_profiles()
        except Exception as exc:  # noqa: BLE001 - 스케줄러가 죽으면 안 된다
            logger.error("학교 공지 프로필 조회 실패: %s", exc, exc_info=True)
            return
        for user_id, user_key in profiles:
            try:
                status = await self.deliver_to_user(user_id, user_key, today)
                logger.info("학교 공지 전달 결과 user_key=%s status=%s", user_key, status)
            except Exception as exc:  # noqa: BLE001 - 한 사용자 실패가 전체를 막지 않게
                logger.error(
                    "학교 공지 전달 중 오류 user_key=%s: %s", user_key, exc, exc_info=True
                )

    @delivery_task.before_loop
    async def _before_delivery(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # 명령
    # ------------------------------------------------------------------

    @commands.group(name="공지", invoke_without_command=True)
    async def school_notice(self, ctx: commands.Context) -> None:
        """오늘 digest를 다시 보여줍니다."""
        if not config.SCHOOL_NOTICE_ENABLED:
            await ctx.reply("ℹ️ 이 마사몽 인스턴스에서는 학교 공지 기능을 운영하지 않습니다.")
            return
        user_key = user_key_for(ctx.author.id)
        today = datetime.now(KST).date()
        try:
            digest = self.load_user_digest(user_key, today)
        except DigestContractError as exc:
            await ctx.reply(f"⚠️ 오늘 digest를 읽을 수 없습니다: {exc}")
            return
        if digest.is_empty and not (
            digest.collection_health and digest.collection_health.has_problem
        ):
            await ctx.reply("오늘 조건에 맞는 새 공지가 없습니다.")
            return
        embeds = render_digest(
            digest,
            max_items=config.SCHOOL_NOTICE_MAX_ITEMS_PER_DM,
            today=today,
        )
        for group in chunk_embeds(embeds):
            await ctx.reply(embeds=group) if group is embeds[:1] else await ctx.send(
                embeds=group
            )

    @school_notice.command(name="등록")
    async def register(self, ctx: commands.Context, *, profile_json: str) -> None:
        """프로필 JSON을 등록합니다. DM에서만 허용합니다."""
        if not config.SCHOOL_NOTICE_ENABLED:
            await ctx.reply("ℹ️ 이 인스턴스에서는 학교 공지 기능을 운영하지 않습니다.")
            return
        if ctx.guild:
            await ctx.reply("⚠️ 프로필 등록은 DM에서만 가능합니다.")
            return
        try:
            payload = json.loads(profile_json)
        except json.JSONDecodeError as exc:
            await ctx.reply(f"❌ JSON 형식이 아닙니다: {exc}")
            return
        try:
            validated = validate_profile_payload(payload, user_id=ctx.author.id)
        except ValueError as exc:
            await ctx.reply(f"❌ 프로필이 올바르지 않습니다: {exc}")
            return
        await self.upsert_profile(ctx.author.id, validated)
        await ctx.reply(
            f"✅ `{validated['school_id']}` 프로필을 등록했습니다. "
            "내일 아침부터 digest를 보내드립니다."
        )

    async def upsert_profile(self, user_id: int, profile: dict) -> None:
        user_key = user_key_for(user_id)
        payload = json.dumps(profile, ensure_ascii=False)
        if config.DB_BACKEND == "tidb":
            query = """
                INSERT INTO school_notice_profiles
                    (user_id, user_key, school_id, profile_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON DUPLICATE KEY UPDATE
                    school_id = VALUES(school_id),
                    profile_json = VALUES(profile_json),
                    profile_version = profile_version + 1,
                    updated_at = VALUES(updated_at)
            """
        else:
            query = """
                INSERT INTO school_notice_profiles
                    (user_id, user_key, school_id, profile_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    school_id = excluded.school_id,
                    profile_json = excluded.profile_json,
                    profile_version = school_notice_profiles.profile_version + 1,
                    updated_at = excluded.updated_at
            """
        await self.bot.db.execute(
            query,
            (
                int(user_id),
                user_key,
                str(profile["school_id"]),
                payload,
                _now_text(),
            ),
        )
        await self.bot.db.commit()

    @school_notice.command(name="중지")
    async def disable(self, ctx: commands.Context) -> None:
        """digest 전달을 중지합니다."""
        await self.bot.db.execute(
            "UPDATE school_notice_profiles SET enabled = 0 WHERE user_id = ?",
            (int(ctx.author.id),),
        )
        await self.bot.db.commit()
        await ctx.reply("✅ 학교 공지 전달을 중지했습니다. `!공지 재개`로 다시 켤 수 있습니다.")

    @school_notice.command(name="재개")
    async def enable(self, ctx: commands.Context) -> None:
        """digest 전달을 다시 켭니다."""
        await self.bot.db.execute(
            "UPDATE school_notice_profiles SET enabled = 1 WHERE user_id = ?",
            (int(ctx.author.id),),
        )
        await self.bot.db.commit()
        await ctx.reply("✅ 학교 공지 전달을 다시 시작합니다.")

    @school_notice.command(name="음소거")
    async def mute_topic(self, ctx: commands.Context, *, topic: str = "") -> None:
        """주제를 숨기거나, 인자 없이 부르면 현재 음소거 목록을 보여줍니다."""
        user_key = user_key_for(ctx.author.id)
        topic = topic.strip()
        if not topic:
            async with self.bot.db.execute(
                """
                SELECT topic FROM school_notice_feedback
                WHERE user_key = ? AND feedback_type = 'mute_topic' AND topic IS NOT NULL
                """,
                (user_key,),
            ) as cursor:
                rows = await cursor.fetchall()
            muted = sorted({str(row[0]) for row in rows})
            if not muted:
                await ctx.reply("현재 음소거한 주제가 없습니다. `!공지 음소거 <주제>`로 숨길 수 있습니다.")
                return
            await ctx.reply(
                "음소거한 주제: " + ", ".join(muted)
                + "\n`!공지 음소거해제 <주제>`로 되돌릴 수 있습니다."
            )
            return
        await self.record_feedback(
            user_id=ctx.author.id,
            source_id="",
            external_id="",
            feedback_type="mute_topic",
            interaction_id=f"cmd-{ctx.message.id}",
            topic=topic,
        )
        await ctx.reply(
            f"✅ `{topic}` 주제를 숨깁니다. "
            "단 등록금·수강·학적·졸업·병무 관련 필수 공지는 계속 표시됩니다."
        )

    @school_notice.command(name="음소거해제")
    async def unmute_topic(self, ctx: commands.Context, *, topic: str) -> None:
        """음소거한 주제를 되돌립니다."""
        topic = topic.strip()
        if not topic:
            await ctx.reply("❌ 해제할 주제를 입력해주세요.")
            return
        await self.bot.db.execute(
            """
            DELETE FROM school_notice_feedback
            WHERE user_key = ? AND feedback_type = 'mute_topic' AND topic = ?
            """,
            (user_key_for(ctx.author.id), topic),
        )
        await self.bot.db.commit()
        await ctx.reply(f"✅ `{topic}` 음소거를 해제했습니다.")


def _digest_with_items(digest: Digest, items) -> Digest:
    """표시 대상만 남긴 digest 사본을 만듭니다."""
    return Digest(
        schema_version=digest.schema_version,
        user_key=digest.user_key,
        digest_date=digest.digest_date,
        summary=digest.summary,
        items=tuple(items),
        collection_health=digest.collection_health,
    )


def validate_profile_payload(payload: dict, *, user_id: int) -> dict:
    """코어 프로필 계약을 마사몽 쪽에서 그대로 검증합니다.

    코어가 다시 검증하지만, 잘못된 프로필을 DB에 저장했다가 batch에서야
    실패하면 원인을 찾기 어렵습니다.
    """
    if not isinstance(payload, dict):
        raise ValueError("프로필은 JSON 객체여야 합니다.")

    profile = dict(payload)
    profile["user_key"] = user_key_for(user_id)

    for field in ("school_id", "degree_level"):
        value = profile.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field}는 비어 있지 않은 문자열이어야 합니다.")

    degree_levels = {"undergraduate", "master", "doctorate", "integrated", "non_degree"}
    if profile["degree_level"] not in degree_levels:
        raise ValueError("degree_level은 " + ", ".join(sorted(degree_levels)) + " 중 하나여야 합니다.")

    grade = profile.get("grade")
    if profile["degree_level"] == "undergraduate" and grade is None:
        raise ValueError("학부생 프로필에는 grade가 필요합니다.")
    if grade is not None and (
        isinstance(grade, bool) or not isinstance(grade, int) or not 1 <= grade <= 6
    ):
        raise ValueError("grade는 1~6 정수여야 합니다.")

    list_fields = (
        "career_interests",
        "preferred_topics",
        "muted_topics",
        "include_keywords",
        "exclude_keywords",
        "double_majors",
        "minors",
        "completed_courses",
        "unknown_fields",
    )
    for field in list_fields:
        values = profile.get(field, [])
        if not isinstance(values, list) or len(values) > 100:
            raise ValueError(f"{field}는 최대 100개의 문자열 배열이어야 합니다.")
        if any(
            not isinstance(item, str) or not item.strip() or len(item) > 100
            for item in values
        ):
            raise ValueError(f"{field}에는 비어 있지 않은 짧은 문자열만 허용됩니다.")

    numeric_contracts = {
        "student_number_year": (1900, 2100),
        "completed_semesters": (0, 30),
        "gpa_last_semester": (0, 4.5),
        "transfer_approved_credits": (0, 300),
    }
    for field, (minimum, maximum) in numeric_contracts.items():
        value = profile.get(field)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{field}는 숫자여야 합니다.")
        if not minimum <= value <= maximum:
            raise ValueError(f"{field}는 {minimum}~{maximum} 범위여야 합니다.")

    preferences = profile.get("notification_preferences", {})
    if not isinstance(preferences, dict):
        raise ValueError("notification_preferences는 객체여야 합니다.")
    bands = preferences.get("include_bands", ["action", "opportunity", "reference"])
    if not isinstance(bands, list) or not set(bands) <= {
        "action",
        "opportunity",
        "reference",
    }:
        raise ValueError("include_bands는 action/opportunity/reference의 부분집합이어야 합니다.")

    return profile


async def setup(bot: commands.Bot) -> None:
    """Cog를 등록합니다."""
    await bot.add_cog(SchoolNoticeCog(bot))

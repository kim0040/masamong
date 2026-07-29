# -*- coding: utf-8 -*-
"""학교 공지 digest를 사용자에게 전달하고 피드백을 수집하는 Cog입니다.

이 Cog는 공지를 크롤링하거나 LLM으로 분석하지 않습니다. 별도 batch 프로세스가
만든 digest를 전달하고, 동의된 자연어 등록에서 로컬 파서로 확정하지 못한 경우에만
제한된 LLM 구조화 호출을 사용합니다. 수집을 봇 밖에 두어 저사양 응답성을 지킵니다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import fcntl
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks

import config
from logger_config import logger
from utils.discord_interactions import ReliableModal, ReliableView
from utils import db as db_utils
from utils.discord_helpers import DiscordProgress
from utils.school_notice_contract import (
    FEEDBACK_TYPES,
    Digest,
    DigestContractError,
    DigestItem,
    digest_path_for,
    load_digest,
)
from utils.school_notice_profile import (
    EXTRACTION_FIELDS,
    SchoolCatalog,
    SchoolProfileError,
    build_confirmation_summary,
    build_profile_correction_prompt,
    build_profile_extraction_prompt,
    canonicalize_profile,
    load_school_catalog,
    merge_profile_correction,
    missing_profile_fields,
    normalize_delivery_time,
    parse_llm_profile_json,
    parse_llm_profile_patch,
    parse_profile_correction_locally,
    parse_profile_locally,
    profile_snapshot_hash,
)
from utils.school_notice_render import (
    build_header_embed,
    build_item_embed,
    chunk_embeds,
    render_digest,
)
from utils.privacy_consent import (
    CONSENT_GRANTED,
    ConsentRequiredError,
    SCHOOL_NOTICE_SCOPE,
    consent_command_name,
    get_policy,
    has_current_consent,
    withdraw_consent,
)

KST = ZoneInfo("Asia/Seoul")
SCHOOL_NOTICE_CONSENT_POLICY = get_policy(SCHOOL_NOTICE_SCOPE)

_CONFIRM_WORDS = frozenset({"맞아", "맞아요", "네", "예", "확인", "저장"})
_CANCEL_WORDS = frozenset({"취소", "그만", "중단"})
# 하루 이상 재시작이 늦어져도 최근 결과를 놓치지 않되, 한 tick의 전체 사용자
# 상한은 별도로 지킨다. 더 오래된 결과는 이미 시의성이 낮아 자동 DM하지 않는다.
_DELIVERY_BACKLOG_DAYS = 3
_PROFILE_SESSION_COOLDOWN_SECONDS = 60
_NATURAL_NOTICE_SUBJECT_RE = re.compile(r"(?:학교\s*)?공지")
_NATURAL_NOTICE_ACTION_RE = re.compile(
    r"(?:알려|알림|받아|받고|받을|설정|등록|추가|변경|수정|"
    r"바꿔|고쳐|보내|보고\s*싶|제외|빼)"
)
_NATURAL_CHANGE_WORDS = ("수정", "변경", "바꿔", "고쳐", "추가", "빼줘", "제외")

# 버튼에 노출할 피드백. 코어의 전체 타입 중 사용자가 결과를 바로 이해할 수 있는
# 세 가지만 둔다. `applied`와 `completed`는 화면상 의미가 겹치므로 현재 공지를
# 다시 보지 않는 `completed`만 명시적인 "처리했어요" 동작으로 제공한다.
# `not_interested`는 영구 차단이 아니라 90일 반감기로 감쇠하는 완만한 신호다.
_FEEDBACK_BUTTONS = (
    ("useful", "유용해요", discord.ButtonStyle.success),
    ("completed", "이 공지 처리했어요", discord.ButtonStyle.primary),
    ("not_interested", "비슷한 주제 덜 보기", discord.ButtonStyle.secondary),
)

_FEEDBACK_CONFIRMATIONS = {
    "useful": (
        "✅ 이 공지를 유용한 사례로 저장했습니다. 다음 수집부터 비슷한 주제의 "
        "맞춤 우선순위를 조금 높입니다."
    ),
    "completed": (
        "✅ 이 공지를 처리 완료로 저장했습니다. 다음 수집부터 같은 공지는 "
        "맞춤 목록에서 숨깁니다."
    ),
    "not_interested": (
        "✅ 관심이 낮은 사례로 저장했습니다. 다음 수집부터 비슷한 주제의 "
        "우선순위를 조금 낮춥니다.\n"
        "영구 차단은 아니며, `!공지 음소거 <주제>`로 주제를 직접 숨길 수 있습니다."
    ),
}


@dataclass(frozen=True)
class ProfileUpsertResult:
    """프로필 저장 결과와 최초 수집 승계 여부."""

    created: bool
    needs_initial_collection: bool


def user_key_for(user_id: int) -> str:
    """Discord 사용자와 코어 프로필을 잇는 안정적 키."""
    return f"discord-{int(user_id)}"


def _now_text() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def _as_kst(value: datetime | None = None) -> datetime:
    """테스트에서 주입한 시각도 비교 가능한 KST aware datetime으로 만든다."""
    if value is None:
        return datetime.now(KST)
    if value.tzinfo is None:
        return value.replace(tzinfo=KST)
    return value.astimezone(KST)


def _safe_error_code(value: Any) -> str:
    """DB에는 사용자 입력이나 provider 오류문 대신 제한된 상태 코드만 남긴다."""
    rendered = str(value or "internal_error").strip().lower()
    allowed = {
        "batch_not_ready",
        "contract_error",
        "dm_blocked",
        "send_failed",
        "timeout",
        "internal_error",
    }
    return rendered if rendered in allowed else "internal_error"


def _try_acquire_batch_lock(digest_dir: Path) -> int | None:
    """batch와 삭제가 같은 profile/core 파일을 동시에 건드리지 않게 한다."""
    lock_path = digest_dir / ".school-notice-batch.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    return descriptor


def _release_batch_lock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _delete_local_school_notice_data(
    *,
    digest_dir: Path,
    core_db_path: str,
    user_key: str,
) -> list[str]:
    """명시 삭제 시 사용자별 파생 파일과 sidecar 개인화 행을 정리한다."""
    if not user_key.startswith("discord-") or not user_key.removeprefix("discord-").isdigit():
        raise ValueError("안전하지 않은 학교 공지 사용자 키입니다.")

    errors: list[str] = []
    profile_file = digest_dir / ".profiles" / f"{user_key}.json"
    user_digest_dir = digest_dir / user_key
    for path in (profile_file, user_digest_dir):
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
        except OSError as exc:
            errors.append(f"{path.name}: {type(exc).__name__}")

    core_path = Path(str(core_db_path or "")).expanduser()
    if str(core_db_path or "").strip() and core_path.is_file():
        connection = None
        try:
            connection = sqlite3.connect(core_path, timeout=5)
            connection.execute("PRAGMA foreign_keys = ON")
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'school_profiles'"
            ).fetchone()
            if table:
                connection.execute(
                    "DELETE FROM school_profiles WHERE user_key = ?",
                    (user_key,),
                )
                connection.commit()
        except sqlite3.Error as exc:
            if connection is not None:
                connection.rollback()
            errors.append(f"core.db: {type(exc).__name__}")
        finally:
            if connection is not None:
                connection.close()
    return errors


class FeedbackView(ReliableView):
    """공지 한 건에 대한 피드백 버튼.

    하루 뒤에는 버튼을 닫아 View가 프로세스 메모리에 무기한 남지 않게 한다.
    봇 재시작 뒤의 영속 버튼은 지원하지 않으며 digest 당일 상호작용만 받는다.
    """

    def __init__(self, cog: "SchoolNoticeCog", item: DigestItem) -> None:
        super().__init__(timeout=24 * 60 * 60)
        self._cog = cog
        self._source_id, self._external_id = item.feedback_key()
        for feedback_type, label, style in _FEEDBACK_BUTTONS:
            self.add_item(_FeedbackButton(self, feedback_type, label, style))
        if item.url:
            self.add_item(
                discord.ui.Button(
                    label="원문 확인",
                    style=discord.ButtonStyle.link,
                    emoji="🔗",
                    url=item.url,
                )
            )

    async def submit(
        self,
        interaction: discord.Interaction,
        feedback_type: str,
    ) -> None:
        # Discord component는 약 3초 안에 acknowledgement가 필요하다. 원격 TiDB
        # 동의 확인과 피드백 INSERT보다 먼저 응답해, 저장 성공 여부와 무관한
        # "적시에 응답하지 않았어요" 표시를 방지한다.
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not await self._cog._has_school_notice_consent(interaction.user.id):
            await interaction.followup.send(
                "학교 공지 개인정보 동의가 철회되었거나 현재 정책에 대한 재동의가 "
                "필요합니다. DM에서 `!개인정보 동의 학교공지`를 실행해주세요.",
                ephemeral=True,
            )
            return
        try:
            stored = await self._cog.record_feedback(
                user_id=interaction.user.id,
                source_id=self._source_id,
                external_id=self._external_id,
                feedback_type=feedback_type,
                interaction_id=str(interaction.id),
            )
        except ConsentRequiredError:
            await interaction.followup.send(
                "동의 상태가 변경되어 피드백을 저장하지 않았습니다.",
                ephemeral=True,
            )
            return
        except Exception as exc:
            logger.error(
                "학교 공지 피드백 저장 실패: feedback_type=%s error=%s",
                feedback_type,
                type(exc).__name__,
                exc_info=True,
            )
            await interaction.followup.send(
                "피드백을 저장하지 못했습니다. 잠시 후 다시 눌러주세요.",
                ephemeral=True,
            )
            return
        if stored:
            message = _FEEDBACK_CONFIRMATIONS[feedback_type]
        else:
            message = "이미 반영된 피드백입니다."
        await interaction.followup.send(message, ephemeral=True)


class _FeedbackButton(discord.ui.Button):
    def __init__(
        self,
        view: FeedbackView,
        feedback_type: str,
        label: str,
        style: discord.ButtonStyle,
    ) -> None:
        super().__init__(label=label, style=style)
        # discord.py 2.6+가 내부적으로 사용하는 Item._parent를 덮어쓰면
        # Item._run_checks가 View를 ActionRow로 오인해 콜백 진입 전에 실패한다.
        self._feedback_view = view
        self._feedback_type = feedback_type

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._feedback_view.submit(interaction, self._feedback_type)


class SchoolNoticeDashboardView(ReliableView):
    """학교 공지의 주요 동작을 한 화면에서 시작하는 사용자 전용 메뉴."""

    def __init__(
        self,
        cog: "SchoolNoticeCog",
        ctx: commands.Context,
        *,
        has_profile: bool,
        enabled: bool,
        delivery_time: str,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.ctx = ctx
        self.user_id = int(ctx.author.id)
        self.has_profile = bool(has_profile)
        self.enabled = bool(enabled)
        self.current_delivery_time = str(delivery_time)
        self.toggle.label = "알림 중지" if self.enabled else "알림 재개"
        self.latest.disabled = not self.has_profile
        self.collection_status.disabled = not self.has_profile
        self.profile_info.disabled = not self.has_profile
        self.delivery_time.disabled = not self.has_profile
        self.toggle.disabled = not self.has_profile

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) == self.user_id:
            return True
        await interaction.response.send_message(
            "이 메뉴는 명령을 실행한 사용자만 사용할 수 있습니다.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(
        label="설정·변경",
        style=discord.ButtonStyle.primary,
        emoji="🎓",
        row=0,
    )
    async def setup_profile(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_message(
            "학교 공지 설정 대화를 아래에서 시작합니다.",
            ephemeral=True,
        )
        await self.cog.begin_profile_setup(
            self.ctx,
            initial_text="",
            prefer_existing=True,
        )

    @discord.ui.button(
        label="최근 공지",
        style=discord.ButtonStyle.secondary,
        emoji="📬",
        row=0,
    )
    async def latest(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_message(
            "최근 맞춤 공지를 아래에서 확인합니다.",
            ephemeral=True,
        )
        await SchoolNoticeCog.school_notice.callback(self.cog, self.ctx, 1)

    @discord.ui.button(
        label="수집 상태",
        style=discord.ButtonStyle.secondary,
        emoji="🔎",
        row=0,
    )
    async def collection_status(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_message(
            "최근 공개 게시판 확인 상태를 아래에서 보여드릴게요.",
            ephemeral=True,
        )
        await self.cog.send_collection_status(self.ctx)

    @discord.ui.button(
        label="알림 시간",
        style=discord.ButtonStyle.secondary,
        emoji="⏰",
        row=1,
    )
    async def delivery_time(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        # Modal은 defer 뒤에 열 수 없으므로 DB 조회 없이 즉시 표시한다. 실제
        # 동의·프로필 상태는 제출 시 update_delivery_time이 다시 검증한다.
        await interaction.response.send_modal(
            SchoolNoticeTimeModal(
                self.cog,
                self.user_id,
                current_time=self.current_delivery_time,
            )
        )

    @discord.ui.button(
        label="내 설정",
        style=discord.ButtonStyle.secondary,
        emoji="🧾",
        row=1,
    )
    async def profile_info(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_message(
            "저장된 최소 설정을 아래에서 보여드릴게요.",
            ephemeral=True,
        )
        await SchoolNoticeCog.profile_info.callback(self.cog, self.ctx)

    @discord.ui.button(
        label="알림 중지",
        style=discord.ButtonStyle.secondary,
        emoji="🔔",
        row=1,
    )
    async def toggle(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_message(
            "학교 공지 알림 상태를 변경합니다.",
            ephemeral=True,
        )
        command = SchoolNoticeCog.disable if self.enabled else SchoolNoticeCog.enable
        await command.callback(self.cog, self.ctx)


class SchoolNoticeTimeModal(ReliableModal, title="학교 공지 알림 시간"):
    """24시간제 또는 자연어 시간을 한 번에 받는 간단한 입력창."""

    delivery_time = discord.ui.TextInput(
        label="알림 받을 시각 (한국 시간)",
        placeholder="예: 09:00 또는 오전 9시",
        max_length=20,
    )

    def __init__(
        self,
        cog: "SchoolNoticeCog",
        user_id: int,
        *,
        current_time: str,
    ) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = int(user_id)
        self.delivery_time.default = current_time

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            normalized = await self.cog.update_delivery_time(
                self.user_id,
                str(self.delivery_time.value),
            )
        except ConsentRequiredError:
            await interaction.followup.send(
                "개인정보 동의가 철회되었거나 재동의가 필요합니다. "
                "`!메뉴`에서 다시 시작해주세요.",
                ephemeral=True,
            )
            return
        except SchoolProfileError as exc:
            await interaction.followup.send(
                f"알림 시각을 이해하지 못했습니다: {exc}",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"✅ 학교 공지 알림을 매일 `{normalized}`(한국 시간)로 설정했습니다. "
            "관련 공지가 없으면 DM을 보내지 않습니다.",
            ephemeral=True,
        )


class SchoolNoticeCog(commands.Cog):
    """digest 전달·프로필 관리·피드백 수집."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.digest_dir = Path(config.SCHOOL_NOTICE_DIGEST_DIR).expanduser()
        self._profile_sessions: set[int] = set()
        self._profile_session_started_at: dict[int, float] = {}
        self._profile_llm_calls: dict[int, int] = {}
        self._profile_llm_lock = asyncio.Lock()
        self._delivery_tick_lock = asyncio.Lock()
        self._initial_collection_tasks: dict[int, asyncio.Task] = {}
        self._catalog: SchoolCatalog | None = None
        if config.SCHOOL_NOTICE_ENABLED:
            self.delivery_task.start()
            logger.info(
                "학교 공지 전달 스케줄러 시작: 1분 catch-up, 신규 기본 알림 %s KST",
                config.SCHOOL_NOTICE_DEFAULT_DELIVERY_TIME,
            )
        else:
            logger.info("학교 공지 기능이 비활성화되어 스케줄러를 시작하지 않습니다.")

    def cog_unload(self) -> None:
        if self.delivery_task.is_running():
            self.delivery_task.cancel()
        for task in self._initial_collection_tasks.values():
            task.cancel()
        self._initial_collection_tasks.clear()
        locked_users = getattr(self.bot, "locked_users", None)
        if isinstance(locked_users, set):
            locked_users.difference_update(self._profile_sessions)
        self._profile_sessions.clear()

    def _school_catalog(self) -> SchoolCatalog:
        if self._catalog is None:
            configured = str(getattr(config, "SCHOOL_NOTICE_CATALOG_PATH", "") or "").strip()
            # 비활성 Cog를 단위 테스트로 직접 만들 때는 버전 관리된 기본
            # 카탈로그를 사용한다. 활성 운영 프로필의 경로는 config가 기동 전에
            # 존재 여부를 검증한다.
            path = configured if configured and Path(configured).expanduser().is_file() else None
            self._catalog = load_school_catalog(path)
        return self._catalog

    async def _has_school_notice_consent(self, user_id: int) -> bool:
        """동의 저장소 장애도 개인정보 이용 허용으로 해석하지 않는다."""
        try:
            return await has_current_consent(
                self.bot.db,
                int(user_id),
                SCHOOL_NOTICE_SCOPE,
            )
        except Exception:
            logger.error(
                "학교 공지 개인정보 동의 상태 확인 실패: user_id=%s",
                user_id,
                exc_info=True,
            )
            return False

    async def _send_school_notice_consent_prompt(
        self,
        ctx: commands.Context,
        *,
        on_granted: Callable[[discord.Interaction], Awaitable[None]] | None = None,
    ) -> None:
        message = (
            "🔐 학교 공지 프로필을 수집하거나 기존 개인화 결과를 이용하려면 "
            "현재 개인정보 정책에 대한 명시적 동의가 필요합니다."
        )
        get_cog = getattr(self.bot, "get_cog", None)
        privacy_cog = get_cog("PrivacyCog") if callable(get_cog) else None
        if privacy_cog is not None:
            await privacy_cog.send_consent_prompt(
                ctx,
                user_id=ctx.author.id,
                scope=SCHOOL_NOTICE_SCOPE,
                prefix=message,
                on_granted=on_granted,
            )
            return
        await ctx.send(
            f"{message}\n`!개인정보 동의 "
            f"{consent_command_name(SCHOOL_NOTICE_SCOPE)}`를 실행한 뒤 다시 시도해주세요."
        )

    async def _schedule_initial_collection(
        self,
        ctx: commands.Context,
        *,
        user_id: int,
    ) -> bool:
        """신규 프로필의 공개 게시판을 별도 저자원 프로세스로 한 번 확인합니다."""
        if not (
            config.SCHOOL_NOTICE_ENABLED
            and getattr(config, "SCHOOL_NOTICE_INITIAL_CRAWL_ENABLED", True)
        ):
            return False
        existing = self._initial_collection_tasks.get(int(user_id))
        if existing is not None and not existing.done():
            return True
        status_message = await ctx.reply(
            "🔎 처음 등록한 학교의 공개 게시판을 한 번 확인하고 있어요.\n"
            "학교 사이트에는 Discord ID·학과·학년·관심사 등 사용자 정보를 "
            "보내지 않습니다. 결과가 있으면 이 DM에서 이어서 알려드릴게요.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        task = asyncio.create_task(
            self._run_initial_collection(
                user_id=int(user_id),
                status_message=status_message,
            ),
            name=f"school-notice-initial-{int(user_id)}",
        )
        self._initial_collection_tasks[int(user_id)] = task
        return True

    async def _run_initial_collection(
        self,
        *,
        user_id: int,
        status_message: discord.Message,
    ) -> None:
        """신규 사용자 한 명만 읽는 유한 batch를 실행하고 결과를 즉시 안내합니다."""
        progress_text = "🔎 등록 학교의 공개 공지를 처음 확인하는 중이에요..."
        progress = await DiscordProgress(
            status_message,
            initial_text=progress_text,
            min_update_interval_seconds=2.0,
            heartbeat_seconds=15.0,
        ).start()
        process: asyncio.subprocess.Process | None = None
        try:
            project_root = Path(config.PROJECT_ROOT).resolve()
            command = [
                sys.executable,
                str(project_root / "scripts" / "run_school_notice_batch.py"),
                "--core-python",
                sys.executable,
                "--core-cwd",
                str(project_root),
                "--source-config",
                str(Path(config.SCHOOL_NOTICE_SOURCE_CONFIG).expanduser()),
                "--no-llm",
                "--low-resource",
                "--only-user-id",
                str(int(user_id)),
                "--max-profiles",
                "1",
            ]
            environment = os.environ.copy()
            environment.update(
                {
                    "OMP_NUM_THREADS": "1",
                    "OPENBLAS_NUM_THREADS": "1",
                    "MKL_NUM_THREADS": "1",
                    "NUMEXPR_NUM_THREADS": "1",
                    "TOKENIZERS_PARALLELISM": "false",
                }
            )
            attempts = max(
                1,
                min(
                    2,
                    int(
                        getattr(
                            config,
                            "SCHOOL_NOTICE_INITIAL_CRAWL_MAX_ATTEMPTS",
                            2,
                        )
                    ),
                ),
            )
            timeout_seconds = max(
                30,
                min(
                    1_800,
                    int(
                        getattr(
                            config,
                            "SCHOOL_NOTICE_INITIAL_CRAWL_TIMEOUT_SECONDS",
                            660,
                        )
                    ),
                ),
            )
            retry_delay = max(
                5,
                min(
                    60,
                    int(
                        getattr(
                            config,
                            "SCHOOL_NOTICE_INITIAL_CRAWL_RETRY_SECONDS",
                            20,
                        )
                    ),
                ),
            )

            return_code = 2
            for attempt in range(1, attempts + 1):
                if not await self._has_school_notice_consent(user_id):
                    await progress.stop()
                    await status_message.edit(
                        content=(
                            "학교 공지 동의가 철회되어 초기 확인을 중단했습니다. "
                            "학교 사이트에는 사용자 정보를 보내지 않았습니다."
                        ),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                await progress.update(
                    "🔎 등록 학교의 공개 게시판만 확인하는 중이에요..."
                    + (f" ({attempt}/{attempts})" if attempts > 1 else ""),
                    force=True,
                )
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=str(project_root),
                    env=environment,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    await asyncio.wait_for(
                        process.communicate(),
                        timeout=timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                    return_code = 2
                else:
                    return_code = int(process.returncode or 0)
                finally:
                    process = None

                if return_code != 3 or attempt >= attempts:
                    break
                await progress.update(
                    "🕒 정기 수집과 겹쳐 잠시 기다린 뒤 한 번만 다시 확인할게요.",
                    force=True,
                )
                await asyncio.sleep(retry_delay)

            await progress.stop()
            if return_code == 0:
                delivery_status = await self.deliver_to_user(
                    int(user_id),
                    user_key_for(user_id),
                    _as_kst().date(),
                    verify_batch_snapshot=True,
                )
                if delivery_status == "sent":
                    final_message = (
                        "✅ 첫 확인을 마쳤습니다. 현재 조건에 맞는 공지를 이 DM에 "
                        "이어 보냈어요.\n앞으로는 등록 학교만 매일 05:00(한국 시간)에 "
                        "확인하고, 새롭거나 수정된 관련 공지만 설정 시각에 알려드립니다."
                    )
                elif delivery_status == "nothing_to_send":
                    final_message = (
                        "✅ 첫 확인을 마쳤습니다. 현재 조건에 맞는 새 공지는 없어요.\n"
                        "앞으로는 등록 학교만 매일 05:00(한국 시간)에 확인하며, "
                        "관련 공지가 없으면 DM을 보내지 않습니다."
                    )
                elif delivery_status in {"consent_required", "profile_stale"}:
                    final_message = (
                        "설정 또는 동의 상태가 바뀌어 초기 결과를 보내지 않았습니다. "
                        "`!공지 상태`에서 현재 상태를 확인해주세요."
                    )
                else:
                    final_message = (
                        "공개 게시판 첫 확인은 끝났지만 결과 전달을 완료하지 못했습니다. "
                        "같은 공지를 중복 전송하지 않으며, `!공지 상태`에서 확인하거나 "
                        "다음 05시 정기 수집을 기다려주세요."
                    )
            elif return_code == 3:
                final_message = (
                    "정기 수집과 겹쳐 첫 확인을 바로 끝내지 못했습니다. "
                    "무한 재시도하지 않고 중단했으며, 다음 05시 정기 수집에서 "
                    "등록 학교를 안전하게 확인합니다."
                )
            else:
                final_message = (
                    "⚠️ 등록은 정상적으로 저장했지만 첫 공개 게시판 확인을 완료하지 "
                    "못했습니다. 사용자 정보는 학교 사이트에 보내지 않았고, "
                    "다음 05시 정기 수집에서 다시 확인합니다."
                )
            await status_message.edit(
                content=final_message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            raise
        except Exception:
            logger.error(
                "학교 공지 등록 직후 초기 확인 실패: user_id=%s",
                user_id,
                exc_info=True,
            )
            await progress.stop()
            try:
                await status_message.edit(
                    content=(
                        "⚠️ 등록은 저장했지만 첫 공개 게시판 확인 중 오류가 발생했습니다. "
                        "무한 재시도하지 않으며 다음 05시 정기 수집에서 확인합니다."
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception:
                pass
        finally:
            await progress.stop()
            current = self._initial_collection_tasks.get(int(user_id))
            if current is asyncio.current_task():
                self._initial_collection_tasks.pop(int(user_id), None)

    async def begin_profile_setup(
        self,
        ctx: commands.Context,
        *,
        initial_text: str,
        prefer_existing: bool,
    ) -> None:
        """명령·버튼·자연어 진입이 공유하는 단일 프로필 설정 흐름."""
        if not config.SCHOOL_NOTICE_ENABLED:
            await ctx.reply("ℹ️ 이 인스턴스에서는 학교 공지 기능을 운영하지 않습니다.")
            return
        if ctx.guild:
            await ctx.reply("⚠️ 개인화 학교 공지 설정은 DM에서 진행해주세요.")
            return
        if not await self._has_school_notice_consent(ctx.author.id):
            await self._send_school_notice_consent_prompt(
                ctx,
                on_granted=lambda _interaction: self.begin_profile_setup(
                    ctx,
                    initial_text=initial_text,
                    prefer_existing=prefer_existing,
                ),
            )
            return
        current_profile = None
        if prefer_existing:
            row = await self._profile_row(int(ctx.author.id))
            current_profile = row["profile"] if row is not None else None
        await self._run_profile_session(
            ctx,
            initial_text=initial_text,
            current_profile=current_profile,
        )

    async def try_handle_natural_message(self, message: discord.Message) -> bool:
        """DM의 명확한 학교 공지 설정 문장을 명령어 없이 설정 흐름으로 연결."""
        if (
            not config.SCHOOL_NOTICE_ENABLED
            or message.guild is not None
            or getattr(message.author, "bot", False)
            or int(message.author.id) in getattr(self.bot, "locked_users", set())
        ):
            return False
        text = str(message.content or "").strip()
        lowered = text.casefold()
        if not text or not _NATURAL_NOTICE_SUBJECT_RE.search(lowered):
            return False
        try:
            school_matches = self._school_catalog().matching_schools(text)
        except SchoolProfileError:
            return False
        explicit_setup = lowered in {
            "학교 공지",
            "학교공지",
            "학교 공지 설정",
            "학교공지 설정",
            "공지 설정",
            "공지설정",
        }
        if not explicit_setup and not _NATURAL_NOTICE_ACTION_RE.search(lowered):
            return False
        if not school_matches and not explicit_setup:
            return False

        ctx = await self.bot.get_context(message)
        await self.begin_profile_setup(
            ctx,
            initial_text="" if explicit_setup else text,
            prefer_existing=any(word in lowered for word in _NATURAL_CHANGE_WORDS),
        )
        return True

    # ------------------------------------------------------------------
    # 저장소 접근
    # ------------------------------------------------------------------

    async def active_profiles(self) -> list[tuple[int, str]]:
        """전달 대상 사용자 목록을 반환합니다."""
        async with self.bot.db.execute(
            """
            SELECT snp.user_id, snp.user_key
            FROM school_notice_profiles AS snp
            JOIN privacy_consents AS pc
              ON pc.user_id = snp.user_id
             AND pc.scope = ?
             AND pc.policy_version = ?
             AND pc.notice_hash = ?
             AND pc.status = ?
             AND pc.granted_at IS NOT NULL
             AND pc.withdrawn_at IS NULL
            WHERE snp.enabled = 1
            """,
            (
                SCHOOL_NOTICE_CONSENT_POLICY.scope,
                SCHOOL_NOTICE_CONSENT_POLICY.version,
                SCHOOL_NOTICE_CONSENT_POLICY.notice_hash,
                CONSENT_GRANTED,
            ),
        ) as cursor:
            rows = await cursor.fetchall()
        return [(int(row[0]), str(row[1])) for row in rows]

    async def _profile_row(self, user_id: int) -> dict[str, Any] | None:
        async with self.bot.db.execute(
            """
            SELECT user_key, school_id, profile_json, profile_version, enabled,
                   delivery_time
            FROM school_notice_profiles
            WHERE user_id = ?
            """,
            (int(user_id),),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        try:
            profile = json.loads(str(row[2]))
        except (TypeError, json.JSONDecodeError):
            profile = {}
        if not isinstance(profile, dict):
            profile = {}
        delivery_time = str(row[5] or "").strip()
        if delivery_time:
            profile["delivery_time"] = delivery_time
        profile["user_key"] = str(row[0])
        return {
            "user_key": str(row[0]),
            "school_id": str(row[1]),
            "profile": profile,
            "profile_version": int(row[3]),
            "enabled": bool(row[4]),
            "delivery_time": delivery_time
            or config.SCHOOL_NOTICE_DEFAULT_DELIVERY_TIME,
        }

    async def _require_profile(
        self,
        ctx: commands.Context,
        *,
        require_consent: bool = True,
    ) -> dict[str, Any] | None:
        if require_consent and not await self._has_school_notice_consent(ctx.author.id):
            await self._send_school_notice_consent_prompt(ctx)
            return None
        row = await self._profile_row(ctx.author.id)
        if row is None:
            await ctx.reply(
                "등록된 학교 공지 정보가 없습니다. DM에서 "
                "`!공지 등록 전북대 소프트웨어공학과 3학년, 오전 9시 알림`처럼 "
                "자연스럽게 말씀해주세요."
            )
            return None
        return row

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
        if not await self._has_school_notice_consent(user_id):
            raise ConsentRequiredError(SCHOOL_NOTICE_SCOPE)
        insert_prefix = "INSERT IGNORE" if config.DB_BACKEND == "tidb" else "INSERT OR IGNORE"
        cursor = await self.bot.db.execute(
            f"""
            {insert_prefix} INTO school_notice_feedback
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
        # interaction_id unique 제약과 한 문장 insert로 버튼 연타 경쟁도
        # 원자적으로 한 건만 반영한다.
        return int(getattr(cursor, "rowcount", 0) or 0) == 1

    async def already_delivered(
        self,
        *,
        user_key: str,
        digest_date: date,
        notice_id: int,
        revision_count: int = 0,
    ) -> bool:
        """날짜와 무관하게 같은 공지의 같은 revision을 이미 보냈는지 확인한다."""
        async with self.bot.db.execute(
            """
            SELECT 1 FROM school_notice_deliveries
            WHERE user_key = ? AND notice_id = ? AND revision_count = ?
              AND status = 'sent'
            """,
            (user_key, int(notice_id), int(revision_count)),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def _delivered_keys(self, user_key: str) -> set[tuple[int, int]]:
        """원격 DB 왕복을 공지별 N회가 아닌 사용자당 한 번으로 제한한다."""
        async with self.bot.db.execute(
            """
            SELECT notice_id, revision_count
            FROM school_notice_deliveries
            WHERE user_key = ? AND status = 'sent'
            """,
            (user_key,),
        ) as cursor:
            rows = await cursor.fetchall()
        return {(int(row[0]), int(row[1])) for row in rows}

    async def mark_delivered(
        self,
        *,
        user_key: str,
        digest_date: date,
        notice_id: int,
        revision_count: int = 0,
        status: str,
        failure_reason: str | None = None,
    ) -> None:
        if config.DB_BACKEND == "tidb":
            query = """
                INSERT INTO school_notice_deliveries
                    (user_key, digest_date, notice_id, revision_count, status,
                     failure_reason, delivered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON DUPLICATE KEY UPDATE
                    status = VALUES(status),
                    failure_reason = VALUES(failure_reason),
                    delivered_at = VALUES(delivered_at)
            """
        else:
            query = """
                INSERT INTO school_notice_deliveries
                    (user_key, digest_date, notice_id, revision_count, status,
                     failure_reason, delivered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_key, notice_id, revision_count) DO UPDATE SET
                    status = excluded.status,
                    failure_reason = excluded.failure_reason,
                    delivered_at = excluded.delivered_at
            """
        await self.bot.db.execute(
            query,
            (
                user_key,
                digest_date.isoformat(),
                int(notice_id),
                int(revision_count),
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
            expected_user_key=user_key,
            expected_digest_date=digest_date,
        )

    async def deliver_to_user(
        self,
        user_id: int,
        user_key: str,
        digest_date: date,
        *,
        verify_batch_snapshot: bool = False,
    ) -> str:
        """한 사용자에게 digest를 전달하고 결과 상태를 반환합니다."""
        if not await self._has_school_notice_consent(user_id):
            return "consent_required"
        try:
            # 최대 수 MB JSON read/parse가 Discord event loop를 막지 않게 한다.
            digest = await asyncio.to_thread(
                self.load_user_digest,
                user_key,
                digest_date,
            )
        except DigestContractError as exc:
            # 계약이 깨진 digest를 부분 렌더링하면 잘못된 마감·자격을 보여줄 수 있다.
            logger.warning("학교 공지 digest를 사용할 수 없습니다 (%s): %s", user_key, exc)
            return "contract_error"

        visible = digest.visible_items()
        delivered_keys = await self._delivered_keys(user_key)
        delivered_revision: dict[int, int] = {}
        for delivered_notice_id, delivered_revision_count in delivered_keys:
            if delivered_notice_id > 0:
                delivered_revision[delivered_notice_id] = max(
                    delivered_revision_count,
                    delivered_revision.get(delivered_notice_id, -1),
                )
        pending = [
            item
            for item in visible
            if item.revision_count
            > delivered_revision.get(item.notice_id, -1)
        ]
        health = digest.collection_health
        stale_key = (-digest_date.toordinal(), 0)
        stale = bool(
            config.SCHOOL_NOTICE_STALE_WARNING_ENABLED
            and health is not None
            and health.has_problem
            and stale_key not in delivered_keys
        )
        if not pending and not stale:
            # 이미 다 보냈고 알릴 이상도 없다.
            return "nothing_to_send"

        # digest를 읽는 동안 철회된 경우 외부 DM 발송 직전에 다시 중단한다.
        if not await self._has_school_notice_consent(user_id):
            return "consent_required"

        shown = pending[: config.SCHOOL_NOTICE_MAX_ITEMS_PER_DM]
        display_digest = _digest_with_items(digest, shown)
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            # fetch_user가 네트워크에서 대기하는 동안 철회될 수 있으므로 첫
            # 개인화 메시지인 header 직전에 최신 동의를 다시 확인한다.
            if not await self._has_school_notice_consent(user_id):
                return "consent_required"
            if verify_batch_snapshot:
                readiness = await self._batch_ready_for_profile(
                    user_id,
                    user_key,
                    digest_date,
                )
                if readiness != "ready":
                    # fetch_user 네트워크 대기 중 설정이 바뀐 경우에도 옛
                    # 개인화 header를 보내지 않는다.
                    return readiness
            header = build_header_embed(
                display_digest,
                shown=len(shown),
                total=len(pending),
            )
            await user.send(embeds=[header])
            if stale:
                # 실제 공지 ID는 양수만 허용되므로 음수 ordinal을 날짜별
                # 수집상태 경고의 내부 전용 키로 안전하게 사용할 수 있다.
                await self.mark_delivered(
                    user_key=user_key,
                    digest_date=digest_date,
                    notice_id=stale_key[0],
                    revision_count=0,
                    status="sent",
                )
            for item in shown:
                if not await self._has_school_notice_consent(user_id):
                    return "consent_required"
                if verify_batch_snapshot:
                    readiness = await self._batch_ready_for_profile(
                        user_id,
                        user_key,
                        digest_date,
                    )
                    if readiness != "ready":
                        return readiness
                # 공지와 피드백 버튼을 한 메시지로 보내고 성공 직후 영속화한다.
                # 뒤 항목이 실패해도 이미 성공한 공지는 다음 재시도에서 빠진다.
                await user.send(
                    content=(
                        "아래 피드백은 다음 맞춤 공지에 반영됩니다. "
                        f"해당하는 버튼을 하나만 눌러주세요. · {item.title[:80]}"
                    ),
                    embeds=[build_item_embed(item, today=_as_kst().date())],
                    view=FeedbackView(self, item),
                )
                await self.mark_delivered(
                    user_key=user_key,
                    digest_date=digest_date,
                    notice_id=item.notice_id,
                    revision_count=item.revision_count,
                    status="sent",
                )
        except discord.Forbidden:
            logger.info("학교 공지 DM이 차단되어 있습니다: user_id=%s", user_id)
            return "dm_blocked"
        except discord.HTTPException as exc:
            logger.warning("학교 공지 DM 전송 실패 user_id=%s: %s", user_id, exc)
            return "send_failed"

        if len(pending) > len(shown):
            # 다음 tick에서 이미 보낸 revision을 bulk dedupe한 뒤 다음 페이지를
            # 이어 보낸다. 한 번에 Discord 메시지를 폭주시키지 않는다.
            return "more_pending"
        return "sent"

    async def _due_profiles(
        self,
        *,
        digest_date: date,
        now: datetime,
        limit: int,
        catch_up: bool = False,
    ) -> list[tuple[int, str]]:
        """현재 시각까지 도달했고 아직 끝나지 않은 사용자만 DB에서 제한 조회."""
        now_hhmm = now.strftime("%H:%M")
        now_text = now.isoformat(timespec="seconds")
        default_time = config.SCHOOL_NOTICE_DEFAULT_DELIVERY_TIME
        async with self.bot.db.execute(
            """
            SELECT snp.user_id, snp.user_key
            FROM school_notice_profiles AS snp
            JOIN privacy_consents AS pc
              ON pc.user_id = snp.user_id
             AND pc.scope = ?
             AND pc.policy_version = ?
             AND pc.notice_hash = ?
             AND pc.status = ?
             AND pc.granted_at IS NOT NULL
             AND pc.withdrawn_at IS NULL
            LEFT JOIN school_notice_delivery_runs AS dr
              ON dr.user_key = snp.user_key
             AND dr.digest_date = ?
            WHERE snp.enabled = 1
              AND COALESCE(snp.delivery_time, ?) <= ?
              AND (
                    ? = 0
                    OR EXISTS (
                        SELECT 1
                        FROM school_notice_batch_runs AS current_batch
                        WHERE current_batch.user_key = snp.user_key
                          AND current_batch.run_date = ?
                          AND current_batch.status IN ('succeeded', 'partial')
                    )
              )
              AND (
                    ? = 0
                    OR NOT EXISTS (
                        SELECT 1
                        FROM school_notice_batch_runs AS newer_batch
                        WHERE newer_batch.user_key = snp.user_key
                          AND newer_batch.run_date > ?
                          AND newer_batch.status IN ('succeeded', 'partial')
                    )
              )
              AND NOT EXISTS (
                    SELECT 1
                    FROM school_notice_delivery_runs AS newer
                    WHERE newer.user_key = snp.user_key
                      AND newer.digest_date > ?
                      AND newer.status IN ('pending', 'retry', 'processing')
              )
              AND (
                    dr.user_key IS NULL
                    OR (
                        dr.status IN ('pending', 'retry', 'processing')
                        AND dr.attempt_count < ?
                        AND (dr.next_attempt_at IS NULL OR dr.next_attempt_at <= ?)
                    )
              )
            ORDER BY COALESCE(snp.delivery_time, ?), snp.user_id
            LIMIT ?
            """,
            (
                SCHOOL_NOTICE_CONSENT_POLICY.scope,
                SCHOOL_NOTICE_CONSENT_POLICY.version,
                SCHOOL_NOTICE_CONSENT_POLICY.notice_hash,
                CONSENT_GRANTED,
                digest_date.isoformat(),
                default_time,
                now_hhmm,
                1 if catch_up else 0,
                digest_date.isoformat(),
                1 if catch_up else 0,
                digest_date.isoformat(),
                digest_date.isoformat(),
                config.SCHOOL_NOTICE_DELIVERY_MAX_ATTEMPTS,
                now_text,
                default_time,
                max(1, int(limit)),
            ),
        ) as cursor:
            rows = await cursor.fetchall()
        return [(int(row[0]), str(row[1])) for row in rows]

    async def _ensure_delivery_run(self, user_key: str, digest_date: date) -> None:
        if config.DB_BACKEND == "tidb":
            query = """
                INSERT IGNORE INTO school_notice_delivery_runs
                    (user_key, digest_date, status, attempt_count, updated_at)
                VALUES (?, ?, 'pending', 0, ?)
            """
        else:
            query = """
                INSERT OR IGNORE INTO school_notice_delivery_runs
                    (user_key, digest_date, status, attempt_count, updated_at)
                VALUES (?, ?, 'pending', 0, ?)
            """
        await self.bot.db.execute(
            query,
            (user_key, digest_date.isoformat(), _now_text()),
        )
        await self.bot.db.commit()

    async def _claim_delivery_run(
        self,
        user_key: str,
        digest_date: date,
        *,
        now: datetime,
    ) -> int | None:
        """시도 횟수를 provider/Discord 호출 전에 올려 crash도 유한하게 만든다."""
        lease_until = now + timedelta(
            seconds=config.SCHOOL_NOTICE_DELIVERY_USER_TIMEOUT_SECONDS + 10
        )
        cursor = await self.bot.db.execute(
            """
            UPDATE school_notice_delivery_runs
            SET status = 'processing',
                attempt_count = attempt_count + 1,
                next_attempt_at = ?,
                last_error = NULL,
                updated_at = ?
            WHERE user_key = ? AND digest_date = ?
              AND status IN ('pending', 'retry', 'processing')
              AND attempt_count < ?
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            """,
            (
                lease_until.isoformat(timespec="seconds"),
                now.isoformat(timespec="seconds"),
                user_key,
                digest_date.isoformat(),
                config.SCHOOL_NOTICE_DELIVERY_MAX_ATTEMPTS,
                now.isoformat(timespec="seconds"),
            ),
        )
        await self.bot.db.commit()
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            return None
        async with self.bot.db.execute(
            """
            SELECT attempt_count
            FROM school_notice_delivery_runs
            WHERE user_key = ? AND digest_date = ?
            """,
            (user_key, digest_date.isoformat()),
        ) as read_cursor:
            row = await read_cursor.fetchone()
        return int(row[0]) if row else None

    async def _finish_delivery_run(
        self,
        user_key: str,
        digest_date: date,
        *,
        status: str,
        attempt_count: int,
        now: datetime,
    ) -> None:
        reset_attempt_count: int | None = None
        if status in {"sent", "nothing_to_send"}:
            run_status = "completed"
            next_attempt_at = None
            last_error = None
            finished_at = now.isoformat(timespec="seconds")
        elif status == "more_pending":
            # 한 페이지를 실제로 전송·mark했으므로 실패 횟수를 초기화하고 다음
            # 페이지를 1분 뒤 이어 간다. digest 최대 항목 수로 전체 횟수도 유한하다.
            run_status = "pending"
            reset_attempt_count = 0
            next_attempt_at = (now + timedelta(minutes=1)).isoformat(
                timespec="seconds"
            )
            last_error = None
            finished_at = None
        elif status in {"consent_required", "profile_stale"}:
            run_status = "cancelled"
            next_attempt_at = None
            last_error = None
            finished_at = now.isoformat(timespec="seconds")
        else:
            last_error = _safe_error_code(status)
            if attempt_count >= config.SCHOOL_NOTICE_DELIVERY_MAX_ATTEMPTS:
                run_status = "failed"
                next_attempt_at = None
                finished_at = now.isoformat(timespec="seconds")
            else:
                run_status = "retry"
                delay = config.SCHOOL_NOTICE_DELIVERY_RETRY_MINUTES * (
                    2 ** max(0, attempt_count - 1)
                )
                next_attempt_at = (
                    now + timedelta(minutes=min(delay, 24 * 60))
                ).isoformat(timespec="seconds")
                finished_at = None
        await self.bot.db.execute(
            """
            UPDATE school_notice_delivery_runs
            SET status = ?, next_attempt_at = ?, last_error = ?,
                finished_at = ?,
                attempt_count = COALESCE(?, attempt_count),
                updated_at = ?
            WHERE user_key = ? AND digest_date = ?
            """,
            (
                run_status,
                next_attempt_at,
                last_error,
                finished_at,
                reset_attempt_count,
                now.isoformat(timespec="seconds"),
                user_key,
                digest_date.isoformat(),
            ),
        )
        await self.bot.db.commit()

    @staticmethod
    def _parse_stored_datetime(value: Any) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        return _as_kst(parsed)

    async def _batch_ready_for_profile(
        self,
        user_id: int,
        user_key: str,
        digest_date: date,
    ) -> str:
        """성공/부분 성공 batch가 현재 프로필의 정확한 snapshot인지 검증."""
        async with self.bot.db.execute(
            """
            SELECT br.status, br.profile_version, br.profile_hash,
                   br.finished_at, snp.profile_version, snp.profile_json,
                   snp.updated_at
            FROM school_notice_batch_runs AS br
            JOIN school_notice_profiles AS snp
              ON snp.user_key = br.user_key
             AND snp.user_id = ?
            WHERE br.user_key = ? AND br.run_date = ?
            """,
            (int(user_id), user_key, digest_date.isoformat()),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or str(row[0]) not in {"succeeded", "partial"}:
            return "batch_not_ready"
        try:
            batch_version = int(row[1])
            batch_hash = str(row[2])
            current_version = int(row[4])
            current_hash = profile_snapshot_hash(str(row[5]))
        except (TypeError, ValueError, SchoolProfileError):
            return "profile_stale"
        if (
            batch_version <= 0
            or batch_version != current_version
            or batch_hash != current_hash
        ):
            # 기존 default(0/빈 hash) 행과 설정 변경 뒤의 옛 digest는 모두
            # fail-closed한다. 초 단위 updated_at이 같아도 version/hash가 잡는다.
            return "profile_stale"
        finished_at = self._parse_stored_datetime(row[3])
        profile_updated_at = self._parse_stored_datetime(row[6])
        if finished_at is None or profile_updated_at is None:
            # 정확한 snapshot 선후관계를 증명하지 못하면 옛 학교/조건 결과를
            # 현재 프로필에 결합하지 않는다.
            return "profile_stale"
        if (
            profile_updated_at > finished_at
        ):
            # 05시 수집 뒤 학교/조건을 바꾼 사용자에게 옛 프로필 digest를
            # 새 프로필 결과인 것처럼 보내지 않는다.
            return "profile_stale"
        return "ready"

    async def _run_due_profile(
        self,
        user_id: int,
        user_key: str,
        digest_date: date,
        *,
        now: datetime,
    ) -> str:
        await self._ensure_delivery_run(user_key, digest_date)
        attempt_count = await self._claim_delivery_run(
            user_key,
            digest_date,
            now=now,
        )
        if attempt_count is None:
            return "not_claimed"
        try:
            batch_status = await self._batch_ready_for_profile(
                user_id,
                user_key,
                digest_date,
            )
            if batch_status == "ready":
                result = await asyncio.wait_for(
                    self.deliver_to_user(
                        user_id,
                        user_key,
                        digest_date,
                        verify_batch_snapshot=True,
                    ),
                    timeout=config.SCHOOL_NOTICE_DELIVERY_USER_TIMEOUT_SECONDS,
                )
            else:
                result = batch_status
        except asyncio.TimeoutError:
            result = "timeout"
        except Exception:
            # 예외 본문에는 provider/경로/외부 payload가 섞일 수 있어 DB에는
            # 저장하지 않고 운영 로그에 stack만 남긴다.
            logger.error(
                "학교 공지 사용자 전달 중 내부 오류: user_key=%s",
                user_key,
                exc_info=True,
            )
            result = "internal_error"
        await self._finish_delivery_run(
            user_key,
            digest_date,
            status=result,
            attempt_count=attempt_count,
            now=now,
        )
        return result

    async def process_due_deliveries(self, *, now: datetime | None = None) -> int:
        """전날과 제한된 backlog 중 due 작업을 전체 batch 상한 안에서 처리."""
        current = _as_kst(now)
        async with self._delivery_tick_lock:
            attempted = 0
            remaining = config.SCHOOL_NOTICE_DELIVERY_BATCH_SIZE
            seen_users: set[int] = set()
            # 최신 digest부터 처리한다. 동일 notice의 더 높은 revision이 먼저
            # 기록되므로 이후 오래된 digest의 낮은 revision은 자동으로 빠진다.
            # 05:00에 만든 오늘 digest를 같은 날 사용자 설정 시각(기본
            # 09:00)에 먼저 처리한다. 이전 23:00 수집 시절의 "전날 digest"
            # 기준을 유지하면 모든 알림이 하루 늦어지므로 today가 0번이다.
            for days_ago in range(_DELIVERY_BACKLOG_DAYS):
                if remaining <= 0:
                    break
                digest_date = current.date() - timedelta(days=days_ago)
                profiles = await self._due_profiles(
                    digest_date=digest_date,
                    now=current,
                    limit=remaining,
                    catch_up=days_ago > 0,
                )
                for user_id, user_key in profiles:
                    if user_id in seen_users:
                        continue
                    result = await self._run_due_profile(
                        user_id,
                        user_key,
                        digest_date,
                        now=current,
                    )
                    if result != "not_claimed":
                        seen_users.add(user_id)
                        attempted += 1
                        remaining -= 1
                        logger.info(
                            "학교 공지 전달 결과 user_key=%s digest_date=%s status=%s",
                            user_key,
                            digest_date.isoformat(),
                            result,
                        )
            return attempted

    @tasks.loop(minutes=1.0)
    async def delivery_task(self) -> None:
        """사용자별 시각을 1분 단위 catch-up하며 한 tick 작업량은 제한한다."""
        if not config.SCHOOL_NOTICE_ENABLED:
            return
        try:
            await self.process_due_deliveries()
        except Exception:
            # tasks.loop 자체가 멈추지 않게 하되 다음 tick도 같은 유한 batch다.
            logger.error("학교 공지 전달 scheduler tick 실패", exc_info=True)

    @delivery_task.before_loop
    async def _before_delivery(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------
    # 명령
    # ------------------------------------------------------------------

    async def send_collection_status(self, ctx: commands.Context) -> None:
        """최근 수집 상태를 내부 식별자 없이 사용자 관점으로 설명합니다."""
        profile_row = await self._require_profile(ctx)
        if profile_row is None:
            return
        async with self.bot.db.execute(
            """
            SELECT run_date, status, collection_status, may_include_stale,
                   item_count, finished_at
            FROM school_notice_batch_runs
            WHERE user_key = ?
            ORDER BY finished_at DESC, run_date DESC
            LIMIT 1
            """,
            (str(profile_row["user_key"]),),
        ) as cursor:
            run_row = await cursor.fetchone()
        if not await self._has_school_notice_consent(ctx.author.id):
            await self._send_school_notice_consent_prompt(ctx)
            return
        if run_row is None:
            active_initial = int(ctx.author.id) in self._initial_collection_tasks
            await ctx.reply(
                (
                    "🔎 첫 공개 게시판 확인을 진행 중입니다."
                    if active_initial
                    else "아직 완료된 공개 게시판 확인 기록이 없습니다."
                )
                + "\n등록 학교만 확인하며, 다음 정기 수집은 매일 05:00(한국 시간)입니다. "
                "학교 사이트에는 사용자 프로필을 보내지 않습니다."
            )
            return
        try:
            run_date = date.fromisoformat(str(run_row[0]))
        except ValueError:
            await ctx.reply("⚠️ 최근 수집 상태의 날짜를 안전하게 읽지 못했습니다.")
            return
        status = str(run_row[1])
        collection_status = str(run_row[2] or "unknown")
        readiness = (
            await self._batch_ready_for_profile(
                int(ctx.author.id),
                str(profile_row["user_key"]),
                run_date,
            )
            if status in {"succeeded", "partial"}
            else "not_ready"
        )
        status_label = {
            "succeeded": "완료",
            "partial": "일부 게시판 저하",
            "failed": "실패",
        }.get(status, "확인 필요")
        health_label = {
            "healthy": "정상",
            "degraded": "일부 저하",
            "failed": "수집 실패",
        }.get(collection_status, "확인 필요")
        lines = [
            "🔎 **학교 공지 수집 상태**",
            f"- 최근 기준일: {run_date.isoformat()}",
            f"- 작업 상태: {status_label}",
            f"- 공개 게시판 상태: {health_label}",
            f"- 내 조건과 맞은 공지: {max(0, int(run_row[4] or 0))}건",
            f"- 완료 시각: {str(run_row[5])}",
        ]
        if bool(run_row[3]):
            lines.append(
                "- 주의: 일부 게시판을 읽지 못해 이전 저장 공지가 포함될 수 있습니다."
            )
        if readiness == "profile_stale":
            lines.append(
                "- 현재 설정이 이 결과 뒤에 바뀌었습니다. 다음 05시 수집부터 새 조건을 적용합니다."
            )
        elif readiness != "ready" and status in {"succeeded", "partial"}:
            lines.append("- 현재 설정과 결과의 일치 여부를 확인할 수 없어 전달하지 않습니다.")
        lines.extend(
            (
                "- 다음 정기 수집: 매일 05:00 (한국 시간)",
                "- 자동 DM: 새롭거나 수정된 관련 공지가 있을 때만 전송",
            )
        )
        await ctx.reply(
            "\n".join(lines),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def send_dashboard(self, ctx: commands.Context) -> None:
        """설정 상태와 자주 쓰는 동작을 한 화면에 모아 보여준다."""
        if ctx.guild:
            await ctx.reply(
                "🎓 학교 공지는 개인정보가 포함될 수 있어 DM에서 설정합니다. "
                "마사몽에게 DM으로 `학교 공지 설정`이라고 보내주세요."
            )
            return
        consented = await self._has_school_notice_consent(ctx.author.id)
        row = await self._profile_row(int(ctx.author.id)) if consented else None
        has_profile = row is not None
        state = (
            "동의 확인 필요"
            if not consented
            else ("사용 중" if row["enabled"] else "중지됨")
            if has_profile
            else "설정 전"
        )
        delivery_time = str(row["delivery_time"]) if has_profile else "기본 09:00"
        embed = discord.Embed(
            title="🎓 학교 공지",
            description=(
                "학교·과정·학년만 필수이며, 캠퍼스·학과·관심사는 원하는 경우에만 "
                "말하면 됩니다. 내용을 확인한 뒤에만 저장합니다.\n"
                "첫 등록 직후 공개 게시판을 한 번 확인하고, 이후 등록한 학교만 "
                "05:00(한국 시간)에 수집합니다."
            ),
            color=0x4F8EF7,
        )
        embed.add_field(name="현재 상태", value=state, inline=True)
        embed.add_field(name="알림 시각", value=delivery_time, inline=True)
        embed.add_field(
            name="가장 쉬운 사용법",
            value=(
                "`전북대 소프트웨어공학과 3학년이고 장학·인턴 공지를 "
                "오전 9시에 알려줘`처럼 DM으로 말해보세요."
            ),
            inline=False,
        )
        embed.add_field(
            name="개인정보 보호",
            value=(
                "학교 사이트에는 Discord ID·학과·학년·관심사를 보내지 않습니다. "
                "관련 공지가 없으면 DM도 보내지 않습니다."
            ),
            inline=False,
        )
        embed.set_footer(
            text="세부 명령은 !도움 공지 · 삭제는 !공지 삭제"
        )
        await ctx.reply(
            embed=embed,
            view=SchoolNoticeDashboardView(
                self,
                ctx,
                has_profile=has_profile,
                enabled=bool(row["enabled"]) if has_profile else False,
                delivery_time=(
                    str(row["delivery_time"])
                    if has_profile
                    else config.SCHOOL_NOTICE_DEFAULT_DELIVERY_TIME
                ),
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.group(name="공지", invoke_without_command=True)
    @commands.dm_only()
    async def school_notice(self, ctx: commands.Context, page: int = 0) -> None:
        """가장 최근 성공/부분 성공 digest의 요청 페이지를 보여줍니다."""
        if not config.SCHOOL_NOTICE_ENABLED:
            await ctx.reply("ℹ️ 이 마사몽 인스턴스에서는 학교 공지 기능을 운영하지 않습니다.")
            return
        if page == 0:
            await self.send_dashboard(ctx)
            return
        if ctx.guild:
            await ctx.reply("⚠️ 개인화 학교 공지는 DM에서만 확인할 수 있습니다.")
            return
        profile_row = await self._require_profile(ctx)
        if profile_row is None:
            return
        user_key = str(profile_row["user_key"])
        async with self.bot.db.execute(
            """
            SELECT run_date
            FROM school_notice_batch_runs
            WHERE user_key = ? AND status IN ('succeeded', 'partial')
            ORDER BY run_date DESC
            LIMIT 1
            """,
            (user_key,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            await ctx.reply(
                "아직 완료된 학교 공지 수집 결과가 없습니다. "
                "등록 학교만 한국 시간 05시에 수집합니다."
            )
            return
        try:
            digest_date = date.fromisoformat(str(row[0]))
            readiness = await self._batch_ready_for_profile(
                int(ctx.author.id),
                user_key,
                digest_date,
            )
            if readiness == "profile_stale":
                await ctx.reply(
                    "학교 공지 정보를 바꾼 뒤 아직 새로 수집하지 않았습니다. "
                    "오늘 05시 수집 이후 최신 조건으로 확인해주세요."
                )
                return
            if readiness != "ready":
                await ctx.reply("⚠️ 최근 학교 공지 수집 상태를 확인할 수 없습니다.")
                return
            digest = await asyncio.to_thread(
                self.load_user_digest,
                user_key,
                digest_date,
            )
        except (ValueError, DigestContractError):
            await ctx.reply("⚠️ 최근 학교 공지 결과를 안전하게 읽을 수 없습니다.")
            return
        if digest.is_empty and not (
            digest.collection_health and digest.collection_health.has_problem
        ):
            await ctx.reply("최근 수집 결과에는 내 조건에 맞는 공지가 없습니다.")
            return
        if page < 1:
            await ctx.reply("페이지는 1 이상의 숫자로 입력해주세요. 예: `!공지 2`")
            return
        visible = digest.visible_items()
        page_size = config.SCHOOL_NOTICE_MAX_ITEMS_PER_DM
        page_count = max(1, (len(visible) + page_size - 1) // page_size)
        if page > page_count:
            await ctx.reply(
                f"해당 페이지가 없습니다. 최근 결과는 총 {page_count}페이지입니다."
            )
            return
        start = (page - 1) * page_size
        page_digest = _digest_with_items(
            digest,
            visible[start : start + page_size],
        )
        embeds = render_digest(
            page_digest,
            max_items=page_size,
            today=_as_kst().date(),
        )
        for index, group in enumerate(chunk_embeds(embeds)):
            # 파일 read/parse나 앞 그룹 전송 중 철회된 경우 이후 개인화
            # 내용을 보내지 않는다.
            if not await self._has_school_notice_consent(ctx.author.id):
                await self._send_school_notice_consent_prompt(ctx)
                return
            readiness = await self._batch_ready_for_profile(
                int(ctx.author.id),
                user_key,
                digest_date,
            )
            if readiness != "ready":
                await ctx.reply(
                    "학교 공지 정보가 바뀌어 이전 조건의 나머지 결과는 "
                    "보내지 않았습니다. 다음 05시 수집 이후 다시 확인해주세요."
                )
                return
            if index == 0:
                await ctx.reply(
                    content=(
                        f"최근 학교 공지 · {page}/{page_count}페이지"
                        + (f"\n다음: `!공지 {page + 1}`" if page < page_count else "")
                    ),
                    embeds=group,
                )
            else:
                await ctx.send(embeds=group)

    @school_notice.command(name="등록")
    @commands.dm_only()
    async def register(
        self,
        ctx: commands.Context,
        *,
        profile_text: str = "",
    ) -> None:
        """학교·과정·관심사·알림 시각을 자연어로 확인 후 등록합니다."""
        await self.begin_profile_setup(
            ctx,
            initial_text=profile_text,
            prefer_existing=False,
        )

    @school_notice.command(name="수정")
    @commands.dm_only()
    async def modify_profile(
        self,
        ctx: commands.Context,
        *,
        correction_text: str = "",
    ) -> None:
        """현재 학교 공지 정보를 자연어로 고치고 확인 후 저장합니다."""
        await self.begin_profile_setup(
            ctx,
            initial_text=correction_text,
            prefer_existing=True,
        )

    async def _call_profile_llm(self, prompt: str, *, user_id: int) -> str | None:
        """동의 직후 라우팅 primary 한 곳만 한 번 호출한다."""
        if not config.SCHOOL_NOTICE_PROFILE_LLM_ENABLED:
            return None
        if not await self._has_school_notice_consent(user_id):
            raise ConsentRequiredError(SCHOOL_NOTICE_SCOPE)
        get_cog = getattr(self.bot, "get_cog", None)
        ai_handler = get_cog("AIHandler") if callable(get_cog) else None
        llm_client = getattr(ai_handler, "llm_client", None)
        if llm_client is None:
            return None
        get_targets = getattr(llm_client, "get_lane_targets", None)
        call_target = getattr(llm_client, "call_routing_lane_target", None)
        if not callable(get_targets) or not callable(call_target):
            return None
        targets = list(get_targets("routing") or [])
        if not targets:
            return None
        async with self._profile_llm_lock:
            # 학교 등록 세션끼리는 quota 확인→예약→provider 호출 전체를
            # 직렬화한다. 예약 기록이 실패하면 과금 호출도 fail-closed한다.
            if not await self._has_school_notice_consent(user_id):
                raise ConsentRequiredError(SCHOOL_NOTICE_SCOPE)
            calls = self._profile_llm_calls.get(int(user_id), 0)
            if calls >= min(3, int(config.SCHOOL_NOTICE_PROFILE_MAX_REVISIONS)):
                logger.info("학교 공지 프로필 세션 LLM 호출 상한 도달, 로컬 파서 사용")
                return None
            try:
                limited = bool(
                    self.bot.db
                    and await db_utils.check_api_rate_limit(
                        self.bot.db,
                        "cometapi",
                        config.COMETAPI_RPM_LIMIT,
                        config.COMETAPI_RPD_LIMIT,
                    )
                )
            except Exception:
                logger.warning(
                    "학교 공지 프로필 LLM quota 확인 실패, provider 호출 생략",
                    exc_info=True,
                )
                return None
            if limited:
                logger.info("학교 공지 프로필 LLM 공용 사용량 상한 도달, 로컬 파서 사용")
                return None
            if self.bot.db:
                try:
                    reserved_at = datetime.now(timezone.utc).isoformat()
                    await self.bot.db.executemany(
                        """
                        INSERT INTO api_call_log (api_type, called_at)
                        VALUES (?, ?)
                        """,
                        (
                            ("cometapi", reserved_at),
                            ("school_notice_profile", reserved_at),
                        ),
                    )
                    await self.bot.db.commit()
                except Exception:
                    try:
                        await self.bot.db.rollback()
                    except Exception:
                        pass
                    logger.warning(
                        "학교 공지 프로필 LLM quota 예약 실패, provider 호출 생략",
                        exc_info=True,
                    )
                    return None
            self._profile_llm_calls[int(user_id)] = calls + 1
            try:
                return await asyncio.wait_for(
                    call_target(
                        targets[0],
                        prompt=prompt,
                        log_extra={"feature": "school_notice_profile"},
                    ),
                    timeout=config.SCHOOL_NOTICE_PROFILE_LLM_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 프롬프트/응답/사용자 문장은 로그에 남기지 않는다.
                logger.info(
                    "학교 공지 프로필 LLM 1회 호출 실패, 로컬 파서 사용: %s",
                    type(exc).__name__,
                )
                return None

    async def _initial_profile_draft(
        self,
        text: str,
        *,
        user_id: int,
    ) -> dict[str, Any]:
        catalog = self._school_catalog()
        local_draft: dict[str, Any] | None = None
        local_error: SchoolProfileError | None = None
        try:
            local_draft = parse_profile_locally(
                text,
                catalog=catalog,
                require_complete=False,
            )
            if not missing_profile_fields(local_draft):
                return local_draft
        except SchoolProfileError as exc:
            local_error = exc

        # 결정론적 파서로 확정하지 못한 경우에만 동의된 원문을 외부에 한 번
        # 보낸다. 명확한 입력은 비용과 개인정보 외부 전달이 모두 0회다.
        response = await self._call_profile_llm(
            build_profile_extraction_prompt(text, catalog=catalog),
            user_id=user_id,
        )
        if response:
            try:
                return parse_llm_profile_json(
                    response,
                    catalog=catalog,
                    require_complete=False,
                    user_text=text,
                )
            except SchoolProfileError as exc:
                logger.info(
                    "학교 공지 프로필 LLM 응답 계약 거부, 로컬 파서 사용: %s",
                    type(exc).__name__,
                )
        if local_draft is not None:
            return local_draft
        if local_error is not None:
            raise local_error
        raise SchoolProfileError("학교 공지 프로필을 이해하지 못했습니다.")

    async def _correct_profile_draft(
        self,
        text: str,
        current_profile: Mapping[str, Any],
        *,
        user_id: int,
    ) -> dict[str, Any]:
        catalog = self._school_catalog()
        local_error: SchoolProfileError | None = None
        try:
            return parse_profile_correction_locally(
                text,
                current_profile,
                catalog=catalog,
                require_complete=False,
            )
        except SchoolProfileError as exc:
            local_error = exc

        response = await self._call_profile_llm(
            build_profile_correction_prompt(
                text,
                current_profile,
                catalog=catalog,
            ),
            user_id=user_id,
        )
        if response:
            try:
                patch = parse_llm_profile_patch(
                    response,
                    current_profile,
                    catalog=catalog,
                    user_text=text,
                )
                return merge_profile_correction(
                    current_profile,
                    patch,
                    catalog=catalog,
                    require_complete=False,
                )
            except SchoolProfileError as exc:
                logger.info(
                    "학교 공지 프로필 보정 LLM 응답 계약 거부, 로컬 파서 사용: %s",
                    type(exc).__name__,
                )
        if local_error is not None:
            raise local_error
        raise SchoolProfileError("학교 공지 수정 내용을 이해하지 못했습니다.")

    async def _wait_profile_message(self, ctx: commands.Context) -> str | None:
        author_id = int(ctx.author.id)
        channel_id = getattr(ctx.channel, "id", None)

        def check(message: discord.Message) -> bool:
            if int(getattr(message.author, "id", -1)) != author_id:
                return False
            incoming_id = getattr(getattr(message, "channel", None), "id", None)
            return incoming_id == channel_id

        try:
            message = await self.bot.wait_for(
                "message",
                check=check,
                timeout=config.SCHOOL_NOTICE_PROFILE_INPUT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            await ctx.reply(
                "⌛ 입력 시간이 지나 등록을 종료했습니다. 저장된 내용은 없습니다."
            )
            return None
        return str(getattr(message, "content", "") or "").strip()

    def _canonical_existing_profile(
        self,
        profile: Mapping[str, Any],
    ) -> dict[str, Any]:
        extracted = {
            key: value
            for key, value in dict(profile).items()
            if key in EXTRACTION_FIELDS
        }
        if "delivery_time" not in extracted:
            extracted["delivery_time"] = config.SCHOOL_NOTICE_DEFAULT_DELIVERY_TIME
        return canonicalize_profile(
            extracted,
            catalog=self._school_catalog(),
            require_complete=False,
        )

    async def _run_profile_session(
        self,
        ctx: commands.Context,
        *,
        initial_text: str,
        current_profile: Mapping[str, Any] | None,
    ) -> None:
        """한 사용자당 한 세션, 유한 보정, 확인 뒤에만 canonical 값을 저장."""
        user_id = int(ctx.author.id)
        if user_id in self._profile_sessions:
            await ctx.reply("⚠️ 이미 학교 공지 등록/수정을 진행 중입니다.")
            return
        now_monotonic = time.monotonic()
        self._profile_session_started_at = {
            tracked_user: started_at
            for tracked_user, started_at in self._profile_session_started_at.items()
            if now_monotonic - started_at < _PROFILE_SESSION_COOLDOWN_SECONDS
        }
        last_started = self._profile_session_started_at.get(user_id)
        if (
            last_started is not None
            and now_monotonic - last_started < _PROFILE_SESSION_COOLDOWN_SECONDS
        ):
            remaining = int(
                _PROFILE_SESSION_COOLDOWN_SECONDS
                - (now_monotonic - last_started)
            )
            await ctx.reply(
                f"⚠️ 방금 등록/수정을 진행했습니다. 약 {max(1, remaining)}초 뒤 "
                "다시 시도해주세요."
            )
            return

        locked_users = getattr(self.bot, "locked_users", None)
        if not isinstance(locked_users, set):
            locked_users = set()
            setattr(self.bot, "locked_users", locked_users)
        self._profile_sessions.add(user_id)
        self._profile_session_started_at[user_id] = now_monotonic
        self._profile_llm_calls[user_id] = 0
        locked_users.add(user_id)

        max_revisions = min(
            3,
            max(1, int(config.SCHOOL_NOTICE_PROFILE_MAX_REVISIONS)),
        )
        revisions = 0
        try:
            if not await self._has_school_notice_consent(user_id):
                await self._send_school_notice_consent_prompt(ctx)
                return

            if current_profile is None:
                draft: dict[str, Any] | None = None
                pending_text = initial_text.strip()
                if not pending_text:
                    await ctx.reply(
                        "학교, 캠퍼스/학과, 과정·학년, 관심 공지와 원하는 알림 시각을 "
                        "한 문장으로 말씀해주세요.\n"
                        "예: `전북대 소프트웨어공학과 3학년이고 장학·인턴 공지를 "
                        "오전 9시에 받고 싶어`"
                    )
                while draft is None:
                    if not pending_text:
                        pending_text = await self._wait_profile_message(ctx)
                        if pending_text is None:
                            return
                    if pending_text.casefold() in _CANCEL_WORDS:
                        await ctx.reply("학교 공지 등록을 취소했습니다. 저장된 내용은 없습니다.")
                        return
                    try:
                        draft = await self._initial_profile_draft(
                            pending_text,
                            user_id=user_id,
                        )
                    except ConsentRequiredError:
                        await self._send_school_notice_consent_prompt(ctx)
                        return
                    except SchoolProfileError as exc:
                        revisions += 1
                        if revisions >= max_revisions:
                            await ctx.reply(
                                "정보를 확정하지 못해 등록을 종료했습니다. "
                                "저장된 내용은 없습니다."
                            )
                            return
                        await ctx.reply(
                            f"아직 정보를 이해하지 못했습니다: {exc}\n"
                            "학교 이름과 과정·학년을 포함해 다시 말씀해주세요. "
                            "`취소`라고 해도 됩니다."
                        )
                        pending_text = ""
            else:
                try:
                    draft = self._canonical_existing_profile(current_profile)
                except SchoolProfileError:
                    await ctx.reply(
                        "기존 프로필 형식을 안전하게 읽지 못했습니다. "
                        "`!공지 삭제` 후 다시 등록해주세요."
                    )
                    return
                if initial_text.strip():
                    try:
                        draft = await self._correct_profile_draft(
                            initial_text.strip(),
                            draft,
                            user_id=user_id,
                        )
                    except ConsentRequiredError:
                        await self._send_school_notice_consent_prompt(ctx)
                        return
                    except SchoolProfileError as exc:
                        revisions += 1
                        await ctx.reply(f"수정 내용을 이해하지 못했습니다: {exc}")

            while True:
                await ctx.reply(build_confirmation_summary(draft))
                answer = await self._wait_profile_message(ctx)
                if answer is None:
                    return
                normalized = answer.casefold().strip()
                if normalized in _CANCEL_WORDS:
                    await ctx.reply("학교 공지 등록/수정을 취소했습니다. 저장된 내용은 없습니다.")
                    return
                if normalized in _CONFIRM_WORDS:
                    missing = missing_profile_fields(draft)
                    if missing:
                        revisions += 1
                        if revisions >= max_revisions:
                            await ctx.reply(
                                "필수 정보를 확정하지 못해 종료했습니다. "
                                "저장된 내용은 없습니다."
                            )
                            return
                        await ctx.reply(
                            "아직 " + ", ".join(missing)
                            + " 정보가 필요합니다. 자연스럽게 덧붙여주세요."
                        )
                        continue
                    # 세션 도중 철회된 경우 provider 호출뿐 아니라 저장도 막는다.
                    if not await self._has_school_notice_consent(user_id):
                        await self._send_school_notice_consent_prompt(ctx)
                        return
                    final_profile = canonicalize_profile(
                        {
                            key: value
                            for key, value in draft.items()
                            if key in EXTRACTION_FIELDS
                        },
                        catalog=self._school_catalog(),
                        require_complete=True,
                    )
                    upsert_result = await self.upsert_profile(
                        user_id,
                        final_profile,
                    )
                    run_initial_now = bool(
                        upsert_result.needs_initial_collection
                        and getattr(
                            config,
                            "SCHOOL_NOTICE_INITIAL_CRAWL_ENABLED",
                            True,
                        )
                    )
                    await ctx.reply(
                        "✅ 확인한 학교 공지 정보를 저장했습니다.\n"
                        + (
                            "아직 유효한 수집 기록이 없어 등록 학교의 공개 게시판을 "
                            "지금 첫 확인합니다. "
                            if run_initial_now
                            else (
                                "첫 수집은 다음 05:00(한국 시간)에 진행합니다. "
                                if upsert_result.needs_initial_collection
                                else ""
                            )
                        )
                        + "이후에는 등록한 학교만 매일 05:00(한국 시간)에 확인하고, "
                        f"새롭거나 수정된 관련 공지가 있을 때 {final_profile['delivery_time']}에 "
                        "알려드립니다. 관련 공지가 없으면 DM을 보내지 않습니다."
                    )
                    if run_initial_now:
                        await self._schedule_initial_collection(
                            ctx,
                            user_id=user_id,
                        )
                    return

                if revisions >= max_revisions:
                    await ctx.reply(
                        "수정 횟수 제한에 도달해 종료했습니다. 저장된 내용은 없습니다."
                    )
                    return
                if not await self._has_school_notice_consent(user_id):
                    await self._send_school_notice_consent_prompt(ctx)
                    return
                try:
                    draft = await self._correct_profile_draft(
                        answer,
                        draft,
                        user_id=user_id,
                    )
                except ConsentRequiredError:
                    await self._send_school_notice_consent_prompt(ctx)
                    return
                except SchoolProfileError as exc:
                    await ctx.reply(
                        f"수정 내용을 이해하지 못했습니다: {exc}\n"
                        "학교·과정·학년·관심사·알림 시각 중 바꿀 내용을 다시 말씀해주세요."
                    )
                finally:
                    revisions += 1
        finally:
            locked_users.discard(user_id)
            self._profile_sessions.discard(user_id)
            self._profile_llm_calls.pop(user_id, None)

    async def upsert_profile(
        self,
        user_id: int,
        profile: dict,
    ) -> ProfileUpsertResult:
        """프로필을 저장하고 최초 수집이 필요한 기존 행도 안전하게 승계합니다."""
        if not await self._has_school_notice_consent(user_id):
            raise ConsentRequiredError(SCHOOL_NOTICE_SCOPE)
        user_key = user_key_for(user_id)
        async with self.bot.db.execute(
            "SELECT 1 FROM school_notice_profiles WHERE user_id = ?",
            (int(user_id),),
        ) as cursor:
            created = await cursor.fetchone() is None
        async with self.bot.db.execute(
            """
            SELECT 1
            FROM school_notice_batch_runs
            WHERE user_key = ? AND status IN ('succeeded', 'partial')
            LIMIT 1
            """,
            (user_key,),
        ) as cursor:
            has_valid_collection = await cursor.fetchone() is not None
        extracted = {
            key: value
            for key, value in dict(profile).items()
            if key in EXTRACTION_FIELDS
        }
        extracted.setdefault(
            "delivery_time",
            config.SCHOOL_NOTICE_DEFAULT_DELIVERY_TIME,
        )
        canonical = canonicalize_profile(
            extracted,
            catalog=self._school_catalog(),
            require_complete=True,
        )
        delivery_time = normalize_delivery_time(canonical["delivery_time"])
        stored_profile = dict(canonical)
        stored_profile["user_key"] = user_key
        timestamp = _now_text()
        payload = json.dumps(
            stored_profile,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if config.DB_BACKEND == "tidb":
            query = """
                INSERT INTO school_notice_profiles
                    (user_id, user_key, school_id, profile_json, delivery_time,
                     enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                ON DUPLICATE KEY UPDATE
                    school_id = VALUES(school_id),
                    profile_json = VALUES(profile_json),
                    delivery_time = VALUES(delivery_time),
                    enabled = 1,
                    profile_version = profile_version + 1,
                    updated_at = VALUES(updated_at)
            """
        else:
            query = """
                INSERT INTO school_notice_profiles
                    (user_id, user_key, school_id, profile_json, delivery_time,
                     enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    school_id = excluded.school_id,
                    profile_json = excluded.profile_json,
                    delivery_time = excluded.delivery_time,
                    enabled = 1,
                    profile_version = school_notice_profiles.profile_version + 1,
                    updated_at = excluded.updated_at
            """
        await self.bot.db.execute(
            query,
            (
                int(user_id),
                user_key,
                str(canonical["school_id"]),
                payload,
                delivery_time,
                timestamp,
                timestamp,
            ),
        )
        await self.bot.db.commit()
        return ProfileUpsertResult(
            created=created,
            needs_initial_collection=created or not has_valid_collection,
        )

    @school_notice.command(name="정보")
    @commands.dm_only()
    async def profile_info(self, ctx: commands.Context) -> None:
        """저장된 최소 프로필과 전달 상태를 보여줍니다."""
        row = await self._require_profile(ctx)
        if row is None:
            return
        try:
            profile = self._canonical_existing_profile(row["profile"])
            lines = build_confirmation_summary(profile).splitlines()
            lines[0] = "현재 저장된 학교 공지 정보입니다."
            if lines and lines[-1].startswith("맞으면"):
                lines.pop()
        except SchoolProfileError:
            lines = [
                "현재 학교 공지 정보의 일부를 읽을 수 없습니다.",
                f"- 학교 ID: {row['school_id']}",
                f"- 알림 시각: {row['delivery_time']} (한국 시간)",
            ]
        lines.append("- 전달 상태: " + ("사용 중" if row["enabled"] else "중지됨"))
        lines.append(
            "- 수집/전달: 첫 등록 때 즉시 1회 확인, 이후 등록 학교만 매일 "
            "05:00 수집 → 다음 알림 시각 전달"
        )
        if not await self._has_school_notice_consent(ctx.author.id):
            await self._send_school_notice_consent_prompt(ctx)
            return
        await ctx.reply("\n".join(lines))

    @school_notice.command(name="상태")
    @commands.dm_only()
    async def collection_status_command(self, ctx: commands.Context) -> None:
        """최근 등록 학교 공개 게시판 수집 상태를 보여줍니다."""
        await self.send_collection_status(ctx)

    async def update_delivery_time(self, user_id: int, value: str) -> str:
        """명령과 Modal이 공유하는 동의 보호 알림 시각 갱신."""
        if not await self._has_school_notice_consent(user_id):
            raise ConsentRequiredError(SCHOOL_NOTICE_SCOPE)
        row = await self._profile_row(user_id)
        if row is None:
            raise SchoolProfileError("먼저 학교 공지 정보를 설정해주세요.")
        delivery_time = normalize_delivery_time(value)
        profile = self._canonical_existing_profile(row["profile"])
        profile["delivery_time"] = delivery_time
        profile = canonicalize_profile(
            {
                key: item
                for key, item in profile.items()
                if key in EXTRACTION_FIELDS
            },
            catalog=self._school_catalog(),
            require_complete=True,
        )
        if not await self._has_school_notice_consent(user_id):
            raise ConsentRequiredError(SCHOOL_NOTICE_SCOPE)
        stored = dict(profile)
        stored["user_key"] = row["user_key"]
        await self.bot.db.execute(
            """
            UPDATE school_notice_profiles
            SET profile_json = ?, delivery_time = ?
            WHERE user_id = ?
              AND EXISTS (
                  SELECT 1
                  FROM privacy_consents AS pc
                  WHERE pc.user_id = school_notice_profiles.user_id
                    AND pc.scope = ?
                    AND pc.policy_version = ?
                    AND pc.notice_hash = ?
                    AND pc.status = ?
                    AND pc.granted_at IS NOT NULL
                    AND pc.withdrawn_at IS NULL
              )
            """,
            (
                json.dumps(
                    stored,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                delivery_time,
                int(user_id),
                SCHOOL_NOTICE_CONSENT_POLICY.scope,
                SCHOOL_NOTICE_CONSENT_POLICY.version,
                SCHOOL_NOTICE_CONSENT_POLICY.notice_hash,
                CONSENT_GRANTED,
            ),
        )
        await self.bot.db.commit()
        return delivery_time

    @school_notice.command(name="시간")
    @commands.dm_only()
    async def set_delivery_time(
        self,
        ctx: commands.Context,
        value: str = "",
    ) -> None:
        """사용자별 KST 알림 시각을 HH:MM으로 변경합니다."""
        if not value.strip():
            row = await self._require_profile(ctx)
            if row is None:
                return
            await ctx.reply(
                f"현재 알림 시각은 `{row['delivery_time']}`입니다. "
                "`!공지 시간 09:00`처럼 입력하거나 `!공지` 메뉴의 "
                "**알림 시간** 버튼을 눌러주세요."
            )
            return
        try:
            delivery_time = await self.update_delivery_time(
                int(ctx.author.id),
                value,
            )
        except ConsentRequiredError:
            await self._send_school_notice_consent_prompt(
                ctx,
                on_granted=lambda _interaction: SchoolNoticeCog.set_delivery_time.callback(
                    self,
                    ctx,
                    value,
                ),
            )
            return
        except SchoolProfileError as exc:
            await ctx.reply(f"❌ 알림 시각이 올바르지 않습니다: {exc}")
            return
        await ctx.reply(
            f"✅ 학교 공지 알림을 매일 `{delivery_time}`(한국 시간)로 설정했습니다. "
            "관련 공지가 없으면 DM을 보내지 않습니다."
        )

    @school_notice.command(name="중지")
    @commands.dm_only()
    async def disable(self, ctx: commands.Context) -> None:
        """digest 전달을 중지합니다."""
        row = await self._require_profile(ctx)
        if row is None:
            return
        if not row["enabled"]:
            await ctx.reply("학교 공지 전달은 이미 중지되어 있습니다.")
            return
        if not await self._has_school_notice_consent(ctx.author.id):
            await self._send_school_notice_consent_prompt(ctx)
            return
        await self.bot.db.execute(
            "UPDATE school_notice_profiles SET enabled = 0 WHERE user_id = ?",
            (int(ctx.author.id),),
        )
        await self.bot.db.commit()
        await ctx.reply("✅ 학교 공지 전달을 중지했습니다. `!공지 재개`로 다시 켤 수 있습니다.")

    @school_notice.command(name="재개")
    @commands.dm_only()
    async def enable(self, ctx: commands.Context) -> None:
        """digest 전달을 다시 켭니다."""
        row = await self._require_profile(ctx)
        if row is None:
            return
        if row["enabled"]:
            await ctx.reply("학교 공지 전달은 이미 사용 중입니다.")
            return
        if not await self._has_school_notice_consent(ctx.author.id):
            await self._send_school_notice_consent_prompt(ctx)
            return
        await self.bot.db.execute(
            "UPDATE school_notice_profiles SET enabled = 1 WHERE user_id = ?",
            (int(ctx.author.id),),
        )
        await self.bot.db.commit()
        await ctx.reply("✅ 학교 공지 전달을 다시 시작합니다.")

    @school_notice.command(name="삭제")
    @commands.dm_only()
    async def delete_profile(self, ctx: commands.Context) -> None:
        """학교 공지 프로필과 연결된 개인화 데이터를 명시적으로 삭제합니다."""
        user_id = int(ctx.author.id)
        user_key = user_key_for(user_id)
        lock_descriptor = await asyncio.to_thread(
            _try_acquire_batch_lock,
            self.digest_dir,
        )
        if lock_descriptor is None:
            await ctx.reply(
                "⚠️ 지금 학교 공지를 수집 중이라 안전하게 삭제할 수 없습니다. "
                "수집이 끝난 뒤 다시 시도해주세요."
            )
            return
        try:
            try:
                # 데이터 삭제가 끝난 뒤 granted 상태만 남는 경우를 방지한다.
                await withdraw_consent(
                    self.bot.db,
                    user_id,
                    SCHOOL_NOTICE_SCOPE,
                )
                for table_name in (
                    "school_notice_feedback",
                    "school_notice_deliveries",
                    "school_notice_delivery_runs",
                    "school_notice_batch_runs",
                ):
                    await self.bot.db.execute(
                        f"DELETE FROM {table_name} WHERE user_key = ?",
                        (user_key,),
                    )
                await self.bot.db.execute(
                    "DELETE FROM school_notice_profiles WHERE user_id = ?",
                    (user_id,),
                )
                await self.bot.db.commit()
            except Exception:
                try:
                    await self.bot.db.rollback()
                except Exception:
                    pass
                logger.error(
                    "학교 공지 개인정보 삭제 실패: user_id=%s",
                    user_id,
                    exc_info=True,
                )
                await ctx.reply(
                    "❌ 학교 공지 개인정보를 삭제하지 못했습니다. "
                    "동의는 철회 상태로 유지되며, 잠시 후 삭제를 다시 시도해주세요."
                )
                return

            cleanup_errors = await asyncio.to_thread(
                _delete_local_school_notice_data,
                digest_dir=self.digest_dir,
                core_db_path=config.SCHOOL_NOTICE_CORE_DB,
                user_key=user_key,
            )
            if cleanup_errors:
                logger.error(
                    "학교 공지 로컬 파생 데이터 일부 삭제 실패: user_id=%s targets=%s",
                    user_id,
                    ",".join(cleanup_errors),
                )
                await ctx.reply(
                    "⚠️ 운영 DB의 학교 공지 프로필·피드백·전달 기록은 삭제했고 동의도 "
                    "철회했습니다. 다만 로컬 파생 파일 일부를 정리하지 못해 운영자 확인이 "
                    "필요합니다. 동의·철회 감사 이력은 보존됩니다."
                )
                return

            await ctx.reply(
                "🗑️ 학교 공지 프로필·피드백·전달/실행 기록과 사용자별 파생 파일을 "
                "삭제하고 개인정보 동의를 철회했습니다.\n"
                "동의·철회 감사 이력은 보존되며, 일반 Discord 대화와 서버 기록은 "
                "변경하지 않았습니다."
            )
        finally:
            await asyncio.to_thread(_release_batch_lock, lock_descriptor)

    @school_notice.command(name="음소거")
    @commands.dm_only()
    async def mute_topic(self, ctx: commands.Context, *, topic: str = "") -> None:
        """주제를 숨기거나, 인자 없이 부르면 현재 음소거 목록을 보여줍니다."""
        profile_row = await self._require_profile(ctx)
        if profile_row is None:
            return
        user_key = str(profile_row["user_key"])
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
            if not await self._has_school_notice_consent(ctx.author.id):
                await self._send_school_notice_consent_prompt(ctx)
                return
            muted = sorted({str(row[0]) for row in rows})
            if not muted:
                await ctx.reply("현재 음소거한 주제가 없습니다. `!공지 음소거 <주제>`로 숨길 수 있습니다.")
                return
            await ctx.reply(
                "음소거한 주제: " + ", ".join(muted)
                + "\n`!공지 음소거해제 <주제>`로 되돌릴 수 있습니다."
            )
            return
        if len(topic) > 80 or any(ord(character) < 32 for character in topic):
            await ctx.reply("❌ 음소거 주제는 80자 이내의 한 줄로 입력해주세요.")
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
    @commands.dm_only()
    async def unmute_topic(self, ctx: commands.Context, *, topic: str) -> None:
        """음소거한 주제를 되돌립니다."""
        profile_row = await self._require_profile(ctx)
        if profile_row is None:
            return
        topic = topic.strip()
        if not topic:
            await ctx.reply("❌ 해제할 주제를 입력해주세요.")
            return
        if not await self._has_school_notice_consent(ctx.author.id):
            await self._send_school_notice_consent_prompt(ctx)
            return
        cursor = await self.bot.db.execute(
            """
            DELETE FROM school_notice_feedback
            WHERE user_key = ? AND feedback_type = 'mute_topic' AND topic = ?
            """,
            (str(profile_row["user_key"]), topic),
        )
        await self.bot.db.commit()
        if int(getattr(cursor, "rowcount", 0) or 0) > 0:
            await ctx.reply(f"✅ `{topic}` 음소거를 해제했습니다.")
        else:
            await ctx.reply(f"`{topic}`은 현재 음소거 목록에 없습니다.")


def _digest_with_items(digest: Digest, items) -> Digest:
    """표시 대상만 남긴 digest 사본을 만듭니다."""
    return Digest(
        schema_version=digest.schema_version,
        user_key=digest.user_key,
        digest_date=digest.digest_date,
        summary=digest.summary,
        items=tuple(items),
        collection_health=digest.collection_health,
        warnings=digest.warnings,
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

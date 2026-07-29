# -*- coding: utf-8 -*-
"""목적별 개인정보 동의 조회·동의·철회 명령."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

import config
from logger_config import logger
from utils.discord_interactions import ReliableView
from utils.privacy_consent import (
    CONSENT_GRANTED,
    CONSENT_WITHDRAWN,
    FORTUNE_SCOPE,
    SCHOOL_NOTICE_SCOPE,
    TRANSFER_NOTICE_SCOPE,
    all_policies,
    consent_command_name,
    format_policy_notice,
    get_consent_state,
    get_policy,
    grant_consent,
    is_current_consent_state,
    normalize_scope,
    withdraw_consent,
)


class ConsentDecisionView(ReliableView):
    """정책 고지를 읽은 본인만 명시적으로 동의할 수 있는 버튼."""

    def __init__(
        self,
        cog: "PrivacyCog",
        *,
        user_id: int | None,
        scope: str,
        on_granted: Callable[[discord.Interaction], Awaitable[None]] | None = None,
        persistent_fallback: bool = False,
    ) -> None:
        # 명령 직후에는 사용자별 callback으로 원래 기능을 이어간다. 이 View가
        # 만료되거나 프로세스가 재시작된 뒤에는 PrivacyCog가 등록한 동일 custom_id의
        # stateless persistent fallback이 처리한다.
        super().__init__(timeout=None if persistent_fallback else 15 * 60)
        self._cog = cog
        self._user_id = int(user_id) if user_id is not None else None
        self._scope = normalize_scope(scope)
        self._on_granted = on_granted
        self._persistent_fallback = bool(persistent_fallback)

        agree = discord.ui.Button(
            label="동의합니다",
            style=discord.ButtonStyle.success,
            custom_id=f"privacy:grant:{self._scope}",
        )
        cancel = discord.ui.Button(
            label="동의하지 않습니다",
            style=discord.ButtonStyle.secondary,
            custom_id=f"privacy:cancel:{self._scope}",
        )
        agree.callback = self._grant  # type: ignore[method-assign]
        cancel.callback = self._cancel  # type: ignore[method-assign]
        self.add_item(agree)
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # 개인정보 동의는 DM에서만 받는다. persistent fallback에는 사용자 ID를
        # 저장하지 않고 실제 DM을 누른 당사자의 ID를 사용한다.
        if getattr(interaction, "guild", None) is not None:
            await interaction.response.send_message(
                "개인정보 동의는 마사몽 DM에서만 할 수 있어요.",
                ephemeral=True,
            )
            return False
        if self._user_id is None or int(interaction.user.id) == self._user_id:
            return True
        await interaction.response.send_message(
            "이 버튼은 명령을 부른 사람만 누를 수 있어요.",
            ephemeral=True,
        )
        return False

    def _disable(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        self.stop()

    async def _grant(self, interaction: discord.Interaction) -> None:
        policy = get_policy(self._scope)
        user_id = (
            self._user_id
            if self._user_id is not None
            else int(interaction.user.id)
        )

        # Discord component는 약 3초 안에 acknowledgement가 필요하다. 원격 TiDB
        # 왕복을 먼저 기다리면 저장은 성공해도 클라이언트에는 "적시에 응답하지
        # 않았어요"가 표시될 수 있으므로, 네트워크/DB 작업 전에 즉시 defer한다.
        await interaction.response.defer()
        try:
            await asyncio.wait_for(
                grant_consent(
                    self._cog.bot.db,
                    user_id,
                    policy.scope,
                ),
                timeout=10,
            )
        except Exception as exc:
            logger.error(
                "개인정보 동의 저장 실패: scope=%s user_id=%s error=%s",
                policy.scope,
                user_id,
                type(exc).__name__,
                exc_info=True,
            )
            await interaction.followup.send(
                "동의 상태를 저장하지 못했어요. 잠시 뒤 다시 해주세요.",
                ephemeral=True,
            )
            return

        if self._persistent_fallback:
            # 공유 persistent View 자체를 disable/stop하면 다른 사용자의 오래된
            # 동의 버튼까지 모두 멈춘다. 처리된 메시지의 버튼만 제거한다.
            rendered_view = None
        else:
            self._disable()
            rendered_view = self
        await interaction.edit_original_response(
            content=(
                f"✅ **{policy.display_name}** 개인정보 처리에 동의했습니다. "
                f"(정책 `{policy.version}`)\n"
                + (
                    "요청하신 기능을 이어서 진행할게요. "
                    if self._on_granted is not None
                    else "이제 원하는 기능을 바로 사용할 수 있습니다. "
                )
                + f"언제든 `!개인정보 철회 {consent_command_name(policy.scope)}`로 "
                "향후 이용을 중단할 수 있습니다."
            ),
            view=rendered_view,
        )
        if self._on_granted is not None:
            try:
                await self._on_granted(interaction)
            except Exception:
                logger.error(
                    "개인정보 동의 후 기능 이어하기 실패: scope=%s user_id=%s",
                    policy.scope,
                    self._user_id,
                    exc_info=True,
                )
                await interaction.followup.send(
                    "동의는 저장했는데 기능을 바로 열지 못했어요. `!메뉴`에서 다시 골라주세요.",
                    ephemeral=True,
                )

    async def _cancel(self, interaction: discord.Interaction) -> None:
        policy = get_policy(self._scope)
        if self._persistent_fallback:
            rendered_view = None
        else:
            self._disable()
            rendered_view = self
        await interaction.response.edit_message(
            content=(
                f"동의하지 않았어요. **{policy.display_name}** 개인정보는 "
                "새로 모으지도, 이미 있는 걸 쓰지도 않아요."
            ),
            view=rendered_view,
        )


class PrivacyCog(commands.Cog):
    """개인정보 동의를 기능별로 관리한다."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._legacy_prompt_lock = asyncio.Lock()
        self._persistent_consent_views: list[ConsentDecisionView] = []
        # 오래된 DM의 component는 View timeout 또는 봇 재시작 뒤에도 동일
        # custom_id로 복구한다. 사용자 ID는 버튼에 넣지 않고 DM을 누른 당사자를
        # 사용하므로 개인 식별정보가 Discord component payload에 남지 않는다.
        if hasattr(bot, "add_view"):
            for policy in all_policies():
                view = ConsentDecisionView(
                    self,
                    user_id=None,
                    scope=policy.scope,
                    persistent_fallback=True,
                )
                bot.add_view(view)
                self._persistent_consent_views.append(view)
        # 테스트용 최소 bot 객체에는 Discord readiness API가 없다. 실제 Bot에서만
        # 기존 활성 구독자 안내 worker를 시작해 import/단위 테스트 부작용을 막는다.
        if hasattr(bot, "wait_until_ready"):
            self.legacy_consent_prompt_task.start()

    def cog_unload(self) -> None:
        if self.legacy_consent_prompt_task.is_running():
            self.legacy_consent_prompt_task.cancel()

    @staticmethod
    def _now_text() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _prompt_scopes(self) -> tuple[str, ...]:
        """현재 인스턴스에서 실제 자동 발송 기능이 켜진 목적만 반환한다."""
        scopes: list[str] = []
        if (
            config.FORTUNE_MORNING_BRIEFING_ENABLED
            and "fortune_cog" not in config.DISABLED_COGS
        ):
            scopes.append(FORTUNE_SCOPE)
        if config.SCHOOL_NOTICE_ENABLED:
            scopes.append(SCHOOL_NOTICE_SCOPE)
        if config.TRANSFER_NOTICE_ENABLED:
            scopes.append(TRANSFER_NOTICE_SCOPE)
        return tuple(scopes)

    @staticmethod
    def _active_source(scope: str) -> tuple[str, str]:
        """목적별 활성 구독 테이블과 조건.

        사용자 프로필 내용은 읽지 않고 Discord 사용자 ID와 활성 여부만 조회한다.
        """
        if scope == FORTUNE_SCOPE:
            return "user_profiles", "subscription_active = 1"
        if scope == SCHOOL_NOTICE_SCOPE:
            return "school_notice_profiles", "enabled = 1"
        if scope == TRANSFER_NOTICE_SCOPE:
            return "transfer_notice_subscriptions", "enabled = 1"
        raise ValueError(f"지원하지 않는 개인정보 목적: {scope}")

    async def _next_legacy_prompt_candidate(
        self,
    ) -> tuple[int, str] | None:
        """활성 구독이지만 현재 동의가 없는 후보 한 명만 고른다.

        명시적으로 철회한 사용자는 제외한다. 예전 정책에 동의한 사용자는 새 정책
        재동의 대상이지만, 정책 버전당 이미 성공적으로 안내한 경우 다시 보내지 않는다.
        """
        now = self._now_text()
        for scope in self._prompt_scopes():
            policy = get_policy(scope)
            table_name, active_clause = self._active_source(scope)
            # table/column 이름은 내부 상수에서만 오며 사용자 입력을 받지 않는다.
            query = f"""
                SELECT source.user_id
                FROM {table_name} AS source
                LEFT JOIN privacy_consents AS pc
                  ON pc.user_id = source.user_id
                 AND pc.scope = ?
                WHERE {active_clause}
                  AND (
                        pc.user_id IS NULL
                        OR (
                            pc.status = ?
                            AND (
                                pc.policy_version <> ?
                                OR pc.notice_hash <> ?
                            )
                        )
                  )
                  AND NOT EXISTS (
                        SELECT 1
                        FROM privacy_consent_prompts AS prompt
                        WHERE prompt.user_id = source.user_id
                          AND prompt.scope = ?
                          AND prompt.policy_version = ?
                          AND prompt.notice_hash = ?
                          AND (
                                prompt.status IN ('sent', 'failed')
                                OR (
                                    prompt.status IN ('retry', 'processing')
                                    AND COALESCE(prompt.next_attempt_at, ?) > ?
                                )
                          )
                  )
                ORDER BY source.user_id
                LIMIT 1
            """
            async with self.bot.db.execute(
                query,
                (
                    policy.scope,
                    CONSENT_GRANTED,
                    policy.version,
                    policy.notice_hash,
                    policy.scope,
                    policy.version,
                    policy.notice_hash,
                    now,
                    now,
                ),
            ) as cursor:
                row = await cursor.fetchone()
            if row is not None:
                return int(row[0]), policy.scope
        return None

    async def _is_active_without_current_consent(
        self,
        user_id: int,
        scope: str,
    ) -> bool:
        """발송 직전 구독 취소·철회 경합을 다시 차단한다."""
        policy = get_policy(scope)
        table_name, active_clause = self._active_source(policy.scope)
        query = f"""
            SELECT 1
            FROM {table_name} AS source
            LEFT JOIN privacy_consents AS pc
              ON pc.user_id = source.user_id
             AND pc.scope = ?
            WHERE source.user_id = ?
              AND {active_clause}
              AND (
                    pc.user_id IS NULL
                    OR (
                        pc.status = ?
                        AND (
                            pc.policy_version <> ?
                            OR pc.notice_hash <> ?
                        )
                    )
              )
            LIMIT 1
        """
        async with self.bot.db.execute(
            query,
            (
                policy.scope,
                int(user_id),
                CONSENT_GRANTED,
                policy.version,
                policy.notice_hash,
            ),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def _prompt_row(self, user_id: int, scope: str):
        policy = get_policy(scope)
        async with self.bot.db.execute(
            """
            SELECT status, attempt_count, next_attempt_at
            FROM privacy_consent_prompts
            WHERE user_id = ? AND scope = ?
              AND policy_version = ? AND notice_hash = ?
            """,
            (
                int(user_id),
                policy.scope,
                policy.version,
                policy.notice_hash,
            ),
        ) as cursor:
            return await cursor.fetchone()

    async def _reserve_prompt(self, user_id: int, scope: str) -> bool:
        """동시/재시작 tick이 같은 동의 안내를 중복 발송하지 않게 예약한다."""
        policy = get_policy(scope)
        row = await self._prompt_row(user_id, policy.scope)
        now = datetime.now(timezone.utc)
        if row is not None:
            status = str(row[0])
            attempts = int(row[1] or 0)
            if status in {"sent", "failed"} or attempts >= 3:
                return False
            if row[2]:
                try:
                    if datetime.fromisoformat(str(row[2])) > now:
                        return False
                except ValueError:
                    pass
        attempt_count = int(row[1] or 0) + 1 if row is not None else 1
        # process가 발송 도중 종료돼도 즉시 중복 전송하지 않고 15분 뒤에만
        # 최대 3회 복구한다. 실제 성공 발송은 sent 상태로 영구 dedupe된다.
        next_attempt = (now + timedelta(minutes=15)).isoformat(timespec="seconds")
        backend = str(getattr(self.bot.db, "backend", config.DB_BACKEND))
        params = (
            int(user_id),
            policy.scope,
            policy.version,
            policy.notice_hash,
            attempt_count,
            next_attempt,
            self._now_text(),
        )
        if backend == "tidb":
            query = """
                INSERT INTO privacy_consent_prompts (
                    user_id, scope, policy_version, notice_hash, status,
                    attempt_count, next_attempt_at, updated_at
                ) VALUES (?, ?, ?, ?, 'processing', ?, ?, ?)
                ON DUPLICATE KEY UPDATE
                    status = 'processing',
                    attempt_count = VALUES(attempt_count),
                    next_attempt_at = VALUES(next_attempt_at),
                    updated_at = VALUES(updated_at)
            """
        else:
            query = """
                INSERT INTO privacy_consent_prompts (
                    user_id, scope, policy_version, notice_hash, status,
                    attempt_count, next_attempt_at, updated_at
                ) VALUES (?, ?, ?, ?, 'processing', ?, ?, ?)
                ON CONFLICT(user_id, scope, policy_version, notice_hash)
                DO UPDATE SET
                    status = 'processing',
                    attempt_count = excluded.attempt_count,
                    next_attempt_at = excluded.next_attempt_at,
                    updated_at = excluded.updated_at
            """
        await self.bot.db.execute(query, params)
        await self.bot.db.commit()
        return True

    async def _finish_prompt(
        self,
        user_id: int,
        scope: str,
        *,
        sent: bool,
        error: str | None = None,
    ) -> None:
        policy = get_policy(scope)
        row = await self._prompt_row(user_id, policy.scope)
        attempts = int(row[1] or 1) if row is not None else 1
        terminal = sent or error == "discord_forbidden" or attempts >= 3
        status = "sent" if sent else "failed" if terminal else "retry"
        next_attempt = None
        if not terminal:
            next_attempt = (
                datetime.now(timezone.utc) + timedelta(hours=6)
            ).isoformat(timespec="seconds")
        await self.bot.db.execute(
            """
            UPDATE privacy_consent_prompts
            SET status = ?, next_attempt_at = ?, sent_at = ?,
                last_error = ?, updated_at = ?
            WHERE user_id = ? AND scope = ?
              AND policy_version = ? AND notice_hash = ?
            """,
            (
                status,
                next_attempt,
                self._now_text() if sent else None,
                error,
                self._now_text(),
                int(user_id),
                policy.scope,
                policy.version,
                policy.notice_hash,
            ),
        )
        await self.bot.db.commit()

    async def _legacy_prompt_tick(self) -> str:
        """활성 기존 구독자 한 명에게만 현재 정책 동의를 요청한다."""
        candidate = await self._next_legacy_prompt_candidate()
        if candidate is None:
            return "idle"
        user_id, scope = candidate
        if not await self._is_active_without_current_consent(user_id, scope):
            return "inactive"
        if not await self._reserve_prompt(user_id, scope):
            return "deduped"
        # 예약 후 구독 취소나 명시 철회가 들어온 경우 발송하지 않는다.
        if not await self._is_active_without_current_consent(user_id, scope):
            await self._finish_prompt(
                user_id,
                scope,
                sent=False,
                error="inactive_before_send",
            )
            return "inactive"
        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await asyncio.wait_for(
                    self.bot.fetch_user(user_id),
                    timeout=5,
                )
            except Exception as exc:
                await self._finish_prompt(
                    user_id,
                    scope,
                    sent=False,
                    error=type(exc).__name__[:64],
                )
                return "fetch_failed"
        policy = get_policy(scope)
        prefix = (
            f"🔐 기존 **{policy.display_name}** 자동 알림 구독은 그대로 보존되어 있습니다.\n"
            "앞으로 개인정보를 이용하기 전에 명시적 동의가 필요해 현재 알림만 "
            "일시 정지했습니다. 아래 내용을 확인하고 원할 때 직접 선택해주세요."
        )
        try:
            await asyncio.wait_for(
                self.send_consent_prompt(
                    user,
                    user_id=user_id,
                    scope=scope,
                    prefix=prefix,
                ),
                timeout=12,
            )
        except discord.Forbidden:
            await self._finish_prompt(
                user_id,
                scope,
                sent=False,
                error="discord_forbidden",
            )
            return "forbidden"
        except Exception as exc:
            await self._finish_prompt(
                user_id,
                scope,
                sent=False,
                error=type(exc).__name__[:64],
            )
            return "retry"
        await self._finish_prompt(user_id, scope, sent=True)
        return "sent"

    @tasks.loop(minutes=1)
    async def legacy_consent_prompt_task(self) -> None:
        if self._legacy_prompt_lock.locked():
            return
        async with self._legacy_prompt_lock:
            try:
                await asyncio.wait_for(self._legacy_prompt_tick(), timeout=35)
            except asyncio.TimeoutError:
                try:
                    await self.bot.db.rollback()
                except Exception:
                    logger.critical(
                        "기존 동의 요청 시간초과 후 rollback도 실패했습니다.",
                        exc_info=True,
                    )
                logger.error("기존 활성 구독자 동의 요청 tick이 35초를 초과했습니다.")
            except Exception:
                try:
                    await self.bot.db.rollback()
                except Exception:
                    logger.critical(
                        "기존 동의 요청 실패 후 rollback도 실패했습니다.",
                        exc_info=True,
                    )
                logger.error("기존 활성 구독자 동의 요청 tick 실패", exc_info=True)

    @legacy_consent_prompt_task.before_loop
    async def before_legacy_consent_prompt_task(self) -> None:
        await self.bot.wait_until_ready()

    async def send_consent_prompt(
        self,
        destination,
        *,
        user_id: int,
        scope: str,
        prefix: str | None = None,
        on_granted: Callable[[discord.Interaction], Awaitable[None]] | None = None,
        replace_message: discord.Message | None = None,
    ):
        """다른 Cog도 동일한 정책 고지와 명시 동의 버튼을 사용하게 한다."""
        policy = get_policy(scope)
        content = format_policy_notice(policy.scope)
        if prefix:
            content = f"{prefix}\n\n{content}"
        view = ConsentDecisionView(
            self,
            user_id=int(user_id),
            scope=policy.scope,
            on_granted=on_granted,
        )
        if replace_message is not None:
            return await replace_message.edit(
                content=content,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return await destination.send(
            content,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def status_text(self, user_id: int) -> str:
        """명령과 통합 메뉴가 동일한 개인정보 상태 설명을 사용하게 한다."""
        lines = [
            "🔐 **개인정보 동의 현황**",
            "일반 Discord 대화와 서버가 제공하는 정보는 아래 동의 대상이 아닙니다.",
        ]
        for policy in all_policies():
            state = await get_consent_state(
                self.bot.db,
                int(user_id),
                policy.scope,
            )
            if is_current_consent_state(state, policy.scope):
                label = f"동의됨 (정책 {policy.version})"
            elif state and state.status == CONSENT_WITHDRAWN:
                label = "철회됨 — 기존 데이터는 보존되지만 이용은 중단됨"
            elif state and state.status == CONSENT_GRANTED:
                label = "재동의 필요 — 정책 버전 또는 고지문이 변경됨"
            else:
                label = "미동의"
            lines.append(f"- **{policy.display_name}**: {label}")
        lines.extend(
            (
                "",
                "기능을 시작하면 필요한 경우 동의 버튼이 바로 표시됩니다.",
                "철회: `!개인정보 철회 운세` / `!개인정보 철회 학교공지`",
                "편입 공지 철회: `!개인정보 철회 편입공지`",
                "철회는 향후 이용만 중단합니다. 저장 데이터 삭제는 "
                "`!운세 삭제`, `!공지 삭제`, `!편입 삭제`를 별도로 실행해야 합니다. "
                "동의·철회 증빙용 감사 이력은 기능 데이터 삭제 후에도 별도 보관됩니다.",
            )
        )
        return "\n".join(lines)

    @commands.group(name="개인정보", invoke_without_command=True)
    @commands.dm_only()
    async def privacy(self, ctx: commands.Context) -> None:
        """현재 목적별 동의 상태를 확인합니다."""
        if ctx.invoked_subcommand is not None:
            return

        await ctx.send(
            await self.status_text(ctx.author.id),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @privacy.command(name="동의")
    @commands.dm_only()
    async def consent(self, ctx: commands.Context, scope: str) -> None:
        """정책 고지를 표시하고 버튼으로 명시 동의를 받습니다."""
        try:
            normalized = normalize_scope(scope)
        except ValueError as exc:
            await ctx.send(f"❌ {exc}")
            return
        await self.send_consent_prompt(
            ctx,
            user_id=ctx.author.id,
            scope=normalized,
        )

    @privacy.command(name="철회")
    @commands.dm_only()
    async def withdraw(self, ctx: commands.Context, scope: str) -> None:
        """향후 목적별 개인정보 이용을 중단하되 누적 데이터는 보존합니다."""
        try:
            policy = get_policy(scope)
        except ValueError as exc:
            await ctx.send(f"❌ {exc}")
            return

        try:
            withdrawn = await withdraw_consent(
                self.bot.db,
                ctx.author.id,
                policy.scope,
            )
        except Exception:
            logger.error(
                "개인정보 동의 철회 저장 실패: scope=%s user_id=%s",
                policy.scope,
                ctx.author.id,
                exc_info=True,
            )
            await ctx.send("철회 상태를 저장하지 못했어요. 잠시 뒤 다시 해주세요.")
            return

        if withdrawn is None:
            await ctx.send(
                f"ℹ️ **{policy.display_name}** 개인정보에 동의한 적이 없어서 "
                "따로 철회 기록을 남기지 않았어요.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        delete_command = {
            FORTUNE_SCOPE: "`!운세 삭제`",
            SCHOOL_NOTICE_SCOPE: "`!공지 삭제`",
            TRANSFER_NOTICE_SCOPE: "`!편입 삭제`",
        }[policy.scope]
        await ctx.send(
            f"✅ **{policy.display_name}** 개인정보 동의를 철회했어요.\n"
            "지금부터 저장된 정보를 쓰지 않고 자동 발송도 멈춰요. "
            "쌓인 데이터와 구독 설정은 지우지 않아서, 다시 동의하면 그대로 이어져요.\n"
            f"데이터까지 지우려면 {delete_command}를 따로 실행해주세요. "
            "동의·철회 기록은 삭제 후에도 그대로 남아요.",
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PrivacyCog(bot))

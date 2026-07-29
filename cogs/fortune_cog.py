# -*- coding: utf-8 -*-
"""
사용자 개인 운세 및 비서 서비스를 담당하는 Cog입니다.
명령어 처리와 모닝 브리핑 자동 발송 스케줄러를 포함합니다.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Awaitable, Callable
import discord
from discord.ext import commands, tasks
import asyncio
from datetime import date, datetime, timedelta, timezone
import json
import math
import pytz
import re
import time
import weakref

import config
from database.compat_db import get_table_columns
from logger_config import logger
from utils import db as db_utils
from utils.constants import FORTUNE_DAILY_LIMIT
from utils.fortune import FortuneCalculator, get_sign_from_date
from utils.discord_helpers import (
    clip_discord_text,
    send_split_message,
    split_message_chunks,
)
from utils.privacy_consent import (
    CONSENT_GRANTED,
    FORTUNE_SCOPE,
    consent_command_name,
    get_policy,
    has_current_consent,
    withdraw_consent,
)

# 시간 유효성 검사 정규식 (HH:MM)
TIME_PATTERN = re.compile(r'^([01]\d|2[0-3]):([0-5]\d)$')
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FORTUNE_CONSENT_POLICY = get_policy(FORTUNE_SCOPE)
REGISTRATION_MAX_ATTEMPTS = 3
_CANCEL_INPUTS = {"취소", "중단", "그만", "cancel", "quit", "stop"}
_UNKNOWN_INPUTS = {
    "모름",
    "몰라",
    "unknown",
    "응답안함",
    "응답 안 함",
    "미제공",
    "비공개",
    "skip",
}

_MORNING_JOB_VERSION = 1
_MORNING_LOOP_SECONDS = 60
_MORNING_TICK_TIMEOUT_SECONDS = 50
_ZODIAC_ATTEMPT_API_TYPE = "fortune_zodiac_generation"
_ZODIAC_COOLDOWN_MAX_USERS = 4096


def _fortune_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    """운세 전용 런타임 값을 항상 유한한 안전 범위로 읽는다."""
    try:
        value = int(getattr(config, name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def parse_birth_date_input(value: str, *, today: date | None = None) -> str:
    """등록용 생년월일을 실제 달력·미래일·합리적 연령까지 검증한다."""
    text = str(value or "").strip()
    if not DATE_PATTERN.fullmatch(text):
        raise ValueError("생년월일은 `YYYY-MM-DD` 형식이어야 합니다.")
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("실제 달력에 존재하는 날짜를 입력해주세요.") from exc
    reference = today or datetime.now(pytz.timezone("Asia/Seoul")).date()
    if parsed > reference:
        raise ValueError("미래 날짜는 생년월일로 등록할 수 없습니다.")
    minimum = date(reference.year - 120, 1, 1)
    if parsed < minimum:
        raise ValueError(
            f"생년월일 연도는 {minimum.year}년 이후로 입력해주세요."
        )
    return parsed.isoformat()


def parse_birth_time_input(value: str) -> str | None:
    """출생 시간을 검증하되 미제공을 정오로 바꾸지 않는다."""
    text = str(value or "").strip()
    if text.lower() in _UNKNOWN_INPUTS:
        return None
    if not TIME_PATTERN.fullmatch(text):
        raise ValueError("시간은 `HH:MM` 형식이거나 `모름`이어야 합니다.")
    return text


def parse_gender_input(value: str) -> str | None:
    """성별 입력을 정규화하고 응답하지 않을 선택을 허용한다."""
    text = str(value or "").strip()
    lowered = text.lower()
    if lowered in _UNKNOWN_INPUTS:
        return None
    if text in {"남", "남자", "남성"} or lowered in {"m", "male"}:
        return "M"
    if text in {"여", "여자", "여성"} or lowered in {"f", "female"}:
        return "F"
    raise ValueError("`남성`, `여성`, `응답 안 함` 중 하나를 입력해주세요.")


def parse_birth_place_input(value: str) -> str | None:
    """출생지는 선택 항목이며 짧고 비정상적인 값만 거부한다."""
    text = str(value or "").strip()
    if text.lower() in _UNKNOWN_INPUTS:
        return None
    if not 2 <= len(text) <= 100:
        raise ValueError("출생지는 2~100자로 입력하거나 `모름`을 입력해주세요.")
    return text


def _gender_label(gender: str | None) -> str:
    return {"M": "남성", "F": "여성"}.get(str(gender or ""), "미제공")


def _morning_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    return _fortune_setting(name, default, minimum, maximum)


def _morning_max_generation_attempts() -> int:
    return _morning_setting(
        "FORTUNE_MORNING_MAX_GENERATION_ATTEMPTS",
        3,
        1,
        5,
    )


def _morning_max_send_attempts() -> int:
    return _morning_setting("FORTUNE_MORNING_MAX_SEND_ATTEMPTS", 3, 1, 5)


def _morning_retry_base_seconds() -> int:
    return _morning_setting(
        "FORTUNE_MORNING_RETRY_BASE_SECONDS",
        60,
        60,
        3600,
    )


def _morning_retry_at(now: datetime, attempt: int) -> str:
    delay = min(
        3600,
        _morning_retry_base_seconds() * (2 ** max(0, int(attempt) - 1)),
    )
    return (now + timedelta(seconds=delay)).isoformat(timespec="seconds")


def _new_morning_job(target_date: date) -> dict:
    return {
        "version": _MORNING_JOB_VERSION,
        "target_date": target_date.isoformat(),
        "state": "generation_retry",
        "content": None,
        "generation_attempts": 0,
        "send_attempts": 0,
        "next_attempt_at": None,
        "last_error": None,
    }


def _decode_morning_job(payload: str | None, target_date: date) -> dict:
    """기존 raw payload나 다른 날짜의 상태를 오늘 작업으로 재사용하지 않는다."""
    if not payload:
        return _new_morning_job(target_date)
    try:
        parsed = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        # 과거 버전의 raw 운세에는 생성 대상 날짜가 없어 자정 이후 오발송할
        # 수 있다. 누적 프로필은 보존하되 이 파생 캐시만 새 작업으로 교체한다.
        return _new_morning_job(target_date)
    if (
        not isinstance(parsed, dict)
        or parsed.get("version") != _MORNING_JOB_VERSION
        or parsed.get("target_date") != target_date.isoformat()
    ):
        return _new_morning_job(target_date)
    job = _new_morning_job(target_date)
    job.update(
        {
            key: parsed.get(key)
            for key in (
                "state",
                "content",
                "generation_attempts",
                "send_attempts",
                "next_attempt_at",
                "last_error",
            )
        }
    )
    try:
        job["generation_attempts"] = max(
            0,
            int(job["generation_attempts"] or 0),
        )
        job["send_attempts"] = max(0, int(job["send_attempts"] or 0))
    except (TypeError, ValueError):
        return _new_morning_job(target_date)
    if job["state"] not in {
        "generation_retry",
        "generated",
        "send_retry",
        "terminal_failed",
    }:
        return _new_morning_job(target_date)
    if job["state"] in {"generated", "send_retry"} and not isinstance(
        job["content"],
        str,
    ):
        return _new_morning_job(target_date)
    return job


def _encode_morning_job(job: dict) -> str:
    return json.dumps(job, ensure_ascii=False, separators=(",", ":"))


def _morning_job_ready(job: dict, now: datetime) -> bool:
    if job.get("state") == "terminal_failed":
        return False
    raw = job.get("next_attempt_at")
    if not raw:
        return True
    try:
        next_attempt = datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    if next_attempt.tzinfo is None:
        next_attempt = pytz.timezone("Asia/Seoul").localize(next_attempt)
    return next_attempt <= now

class FortuneCog(commands.Cog):
    """운세 관련 기능을 제공하는 Cog입니다."""

    def __init__(self, bot: commands.Bot):
        """FortuneCog를 초기화하고 백그라운드 태스크를 시작합니다."""
        self.bot = bot
        self.calculator = FortuneCalculator()
        self._ready = False
        self._registration_users: set[int] = set()
        self._fortune_user_locks: weakref.WeakValueDictionary[
            int,
            asyncio.Lock,
        ] = weakref.WeakValueDictionary()
        self._zodiac_cache: OrderedDict[
            tuple[str, ...],
            tuple[float, str | None],
        ] = OrderedDict()
        self._zodiac_key_locks: weakref.WeakValueDictionary[
            tuple[str, ...],
            asyncio.Lock,
        ] = weakref.WeakValueDictionary()
        self._zodiac_attempt_lock = asyncio.Lock()
        self._zodiac_user_cooldowns: OrderedDict[int, float] = OrderedDict()
        self._zodiac_users_inflight: set[int] = set()
        # 비동기 초기화 작업을 위해 별도 태스크로 실행
        self._schema_task = self.bot.loop.create_task(self._ensure_db_schema())
        if config.FORTUNE_MORNING_BRIEFING_ENABLED:
            self.morning_briefing_task.start()
        else:
            logger.info("운세 모닝 브리핑 스케줄러가 비활성화되었습니다.")
        logger.info("FortuneCog가 성공적으로 초기화되었습니다.")

    async def _ensure_db_schema(self):
        """pending_payload 컬럼이 없으면 추가합니다."""
        await self.bot.wait_until_ready()
        if not bool(getattr(config, "AUTO_MIGRATE", True)):
            logger.info(
                "자동 migration이 비활성화되어 FortuneCog의 runtime ALTER를 건너뜁니다."
            )
            self._ready = True
            return
        if config.DB_BACKEND == "tidb":
            logger.info("FortuneCog 스키마 점검을 건너뜁니다. TiDB 스키마는 중앙 스키마 파일 기준으로 관리됩니다.")
            self._ready = True
            return
        try:
            columns = await get_table_columns(self.bot.db, "user_profiles")
                
            if 'pending_payload' not in columns:
                logger.info("필요한 컬럼(pending_payload)이 없어 추가합니다.")
                await self.bot.db.execute("ALTER TABLE user_profiles ADD COLUMN pending_payload TEXT")
                await self.bot.db.commit()
                logger.info("Added 'pending_payload' column to user_profiles")

            if 'gender' not in columns:
                logger.info("필요한 컬럼(gender)이 없어 추가합니다.")
                await self.bot.db.execute("ALTER TABLE user_profiles ADD COLUMN gender TEXT")
                await self.bot.db.commit()
                logger.info("Added 'gender' column to user_profiles")

            if 'last_fortune_content' not in columns:
                logger.info("필요한 컬럼(last_fortune_content)이 없어 추가합니다.")
                await self.bot.db.execute("ALTER TABLE user_profiles ADD COLUMN last_fortune_content TEXT")
                await self.bot.db.commit()
                logger.info("Added 'last_fortune_content' column to user_profiles")

            if 'birth_place' not in columns:
                logger.info("필요한 컬럼(birth_place)이 없어 추가합니다.")
                await self.bot.db.execute("ALTER TABLE user_profiles ADD COLUMN birth_place TEXT")
                await self.bot.db.commit()
                logger.info("Added 'birth_place' column to user_profiles")
        except Exception as e:
            logger.error(f"Failed to check/add column: {e}")
        finally:
            self._ready = True

    def cog_unload(self):
        """Cog 언로드 시 아침 브리핑 태스크를 취소합니다."""
        if not self._schema_task.done():
            self._schema_task.cancel()
        if self.morning_briefing_task.is_running():
            self.morning_briefing_task.cancel()

    async def _has_fortune_consent(self, user_id: int) -> bool:
        """동의 저장소 오류도 허용으로 해석하지 않고 fail-closed 처리한다."""
        try:
            return await has_current_consent(
                self.bot.db,
                int(user_id),
                FORTUNE_SCOPE,
            )
        except Exception:
            logger.error(
                "운세 개인정보 동의 상태 확인 실패: user_id=%s",
                user_id,
                exc_info=True,
            )
            return False

    async def _send_fortune_consent_prompt(
        self,
        ctx: commands.Context,
        *,
        status_msg: discord.Message | None = None,
        on_granted: Callable[[discord.Interaction], Awaitable[None]] | None = None,
    ) -> None:
        """개인정보를 읽기 전에 중앙 동의 흐름으로 안내한다."""
        message = (
            "🔐 운세 프로필을 새로 수집하거나 기존 프로필에서 이용하려면 "
            "현재 개인정보 정책에 대한 명시적 동의가 필요합니다."
        )
        if ctx.guild:
            content = (
                f"{message}\n동의 처리는 공개 채널이 아닌 DM에서 "
                f"`!메뉴`를 열고 **오늘 운세**를 선택해주세요."
            )
            if status_msg is not None:
                await status_msg.edit(content=content)
            else:
                await ctx.reply(content, mention_author=True)
            return

        privacy_cog = self.bot.get_cog("PrivacyCog")
        if privacy_cog is not None:
            await privacy_cog.send_consent_prompt(
                ctx,
                user_id=ctx.author.id,
                scope=FORTUNE_SCOPE,
                prefix=message,
                on_granted=on_granted,
                replace_message=status_msg,
            )
            return
        content = (
            f"{message}\n`!개인정보 동의 {consent_command_name(FORTUNE_SCOPE)}`를 "
            "실행해주세요."
        )
        if status_msg is not None:
            await status_msg.edit(content=content)
        else:
            await ctx.send(content)

    async def _collect_registration_input(
        self,
        ctx: commands.Context,
        *,
        prompt: str,
        parser,
    ) -> tuple[bool, object | None]:
        """등록 한 단계를 최대 3회만 묻고 취소/timeout을 유한하게 처리한다."""

        def check(message):
            return message.author == ctx.author and message.channel == ctx.channel

        for attempt in range(1, REGISTRATION_MAX_ATTEMPTS + 1):
            await ctx.send(
                f"{prompt}\n"
                f"`취소`를 입력하면 등록을 중단합니다. "
                f"({attempt}/{REGISTRATION_MAX_ATTEMPTS})"
            )
            try:
                message = await self.bot.wait_for(
                    "message",
                    check=check,
                    timeout=60.0,
                )
            except asyncio.TimeoutError:
                await ctx.send(
                    "⏰ 시간이 지나서 등록을 닫았어요. `!운세 등록`으로 다시 시작해주세요."
                )
                return False, None

            raw_value = message.content.strip()
            if raw_value.lower() in _CANCEL_INPUTS:
                await ctx.send("등록을 취소했어요. 입력한 내용은 저장하지 않았어요.")
                return False, None
            try:
                return True, parser(raw_value)
            except ValueError as exc:
                if attempt >= REGISTRATION_MAX_ATTEMPTS:
                    await ctx.send(
                        f"❌ {exc}\n{REGISTRATION_MAX_ATTEMPTS}번 잘못 입력해서 "
                        "등록을 닫았어요. 저장된 건 없어요."
                    )
                    return False, None
                await ctx.send(f"❌ {exc}")
        return False, None  # pragma: no cover - for 범위가 보장

    @commands.group(name='운세', invoke_without_command=True)
    async def fortune(self, ctx: commands.Context, *, option: str = None):
        """
        운세 관련 종합 기능을 제공합니다. 🔮
        
        사용법:
        - `!운세` : 오늘의 운세 (서버=요약, DM=상세)
        - `!운세 상세` : DM에서 상세 운세
        - `!운세 등록` : 생년월일/시간/성별/출생지 등록 (DM 전용)
        - `!운세 구독 HH:MM` : 매일 아침 운세 브리핑 (DM 전용)
        - `!운세 구독취소` : 구독 해제
        - `!운세 삭제` : 등록된 정보 삭제 (DM 전용)
        - AI 개인 운세는 기본/상세/월/년을 합쳐 하루 3회이며,
          외부 AI 호출이 시작된 실패 요청도 횟수에 포함됩니다.

        예시:
        - `!운세`
        - `!운세 상세`
        - `!운세 구독 07:30`
        """
        if ctx.invoked_subcommand is None:
            # 기존 !운세 (check_fortune) 로직 호출
            status_msg = await ctx.send("🔮 운세를 살펴보는 중이야...")
            await self._check_fortune_logic(ctx, option, status_msg=status_msg)

    @fortune.command(name='등록')
    @commands.dm_only()
    async def fortune_register(self, ctx: commands.Context):
        """
        생년월일/시간/성별/출생지를 대화형으로 등록합니다. (DM 전용)

        사용법:
        - `!운세 등록`

        예시:
        - `!운세 등록`
        """
        if not await self._has_fortune_consent(ctx.author.id):
            await self._send_fortune_consent_prompt(
                ctx,
                on_granted=lambda _interaction: FortuneCog.fortune_register.callback(
                    self,
                    ctx,
                ),
            )
            return

        registration_users = getattr(self, "_registration_users", None)
        if registration_users is None:
            registration_users = set()
            self._registration_users = registration_users
        if ctx.author.id in registration_users:
            await ctx.send("⚠️ 이미 운세 등록을 진행 중입니다.")
            return
        registration_users.add(ctx.author.id)

        # [Safety Lock] 다른 명령어/AI 응답 방지
        self.bot.locked_users.add(ctx.author.id)
        
        try:
            completed, birth_date = await self._collect_registration_input(
                ctx,
                prompt=(
                    "📝 생년월일을 입력해주세요. "
                    "(예: `1990-01-01`, 필수 항목)"
                ),
                parser=parse_birth_date_input,
            )
            if not completed:
                return
            completed, birth_time = await self._collect_registration_input(
                ctx,
                prompt=(
                    "🕒 태어난 시간을 입력해주세요. (예: `14:30`)\n"
                    "모르거나 제공하지 않으려면 `모름` 또는 `응답 안 함`을 입력하세요. "
                    "미제공 값을 `12:00`으로 바꾸지 않습니다."
                ),
                parser=parse_birth_time_input,
            )
            if not completed:
                return
            completed, gender = await self._collect_registration_input(
                ctx,
                prompt=(
                    "⚧ 성별을 입력해주세요. (`남성` / `여성`)\n"
                    "제공하지 않으려면 `응답 안 함`을 입력하세요. "
                    "미제공 성별을 추측하지 않습니다."
                ),
                parser=parse_gender_input,
            )
            if not completed:
                return
            completed, birth_place = await self._collect_registration_input(
                ctx,
                prompt=(
                    "🌍 출생지를 시/군 단위로 입력해주세요. (예: `서울`, `부산`)\n"
                    "제공하지 않으려면 `모름` 또는 `응답 안 함`을 입력하세요."
                ),
                parser=parse_birth_place_input,
            )
            if not completed:
                return

            # DB 저장 (기본적으로 구독은 비활성화 상태로 저장)
            if not await self._has_fortune_consent(ctx.author.id):
                await ctx.send(
                    "개인정보 동의가 등록 도중 철회되어 입력 내용을 저장하지 않았어요."
                )
                return
            await self._save_user_profile(ctx.author.id, birth_date, birth_time, gender, birth_place)
            await ctx.send(
                "✅ 등록 끝났어요!\n"
                "이제 `!운세`로 언제든 오늘의 운세를 볼 수 있어요.\n\n"
                "🔔 **매일 아침 운세 브리핑**을 받고 싶다면 `!운세 구독 [시간]`을 보내주세요. (예: `!운세 구독 07:30`)"
            )
            
        except Exception as e:
            logger.error(f"운세 등록 중 오류: {e}", exc_info=True)
            await ctx.send("❌ 등록 중 오류가 발생했어요.")
        finally:
            # [Safety Lock Release] 작업 종료 후 반드시 잠금 해제
            self.bot.locked_users.discard(ctx.author.id)
            registration_users.discard(ctx.author.id)

    async def _save_user_profile(self, user_id, birth_date, birth_time, gender, birth_place):
        """사용자 운세 프로필(생년월일/시간/성별/출생지)을 DB에 저장하거나 갱신합니다."""
        if config.DB_BACKEND == "tidb":
            query = """
                INSERT INTO user_profiles (user_id, birth_date, birth_time, gender, birth_place, created_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP(6))
                ON DUPLICATE KEY UPDATE
                    birth_date = VALUES(birth_date),
                    birth_time = VALUES(birth_time),
                    gender = VALUES(gender),
                    birth_place = VALUES(birth_place),
                    pending_payload = NULL,
                    last_fortune_content = NULL
            """
        else:
            query = """
                INSERT INTO user_profiles (user_id, birth_date, birth_time, gender, birth_place, created_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(user_id) DO UPDATE SET
                    birth_date = excluded.birth_date,
                    birth_time = excluded.birth_time,
                    gender = excluded.gender,
                    birth_place = excluded.birth_place,
                    pending_payload = NULL,
                    last_fortune_content = NULL
            """
        async with self.bot.db.execute(
            query,
            (user_id, birth_date, birth_time, gender, birth_place)
        ):
            await self.bot.db.commit()

    async def _update_last_fortune_context(self, user_id: int, content: str):
        """사용자가 마지막으로 받은 운세 내용을 DB에 저장하여 이후 대화 컨텍스트로 활용합니다."""
        try:
             # LLM 처리 도중 철회될 수 있으므로 쓰기 시점에도 다시 확인한다.
             if not await self._has_fortune_consent(user_id):
                 return
             await self.bot.db.execute(
                "UPDATE user_profiles SET last_fortune_content = ? WHERE user_id = ?",
                (content, user_id)
            )
             await self.bot.db.commit()
        except Exception as e:
            logger.error(f"운세 컨텍스트 저장 실패: {e}")

    @fortune.command(name='삭제')
    async def fortune_delete(self, ctx: commands.Context):
        """
        등록된 운세 프로필과 구독 설정을 삭제합니다. (DM 전용)

        사용법:
        - `!운세 삭제`

        예시:
        - `!운세 삭제`
        """
        # DM 체크
        if ctx.guild:
            await ctx.reply("⚠️ 개인 정보 보호를 위해 이 명령어는 DM에서만 사용할 수 있어요.")
            return

        try:
             # 삭제 뒤 기존 granted 상태가 남아 향후 프로필을 묵시적으로 다시
             # 사용하지 않도록 먼저 철회를 append-only 이력에 기록한다.
             await withdraw_consent(
                 self.bot.db,
                 ctx.author.id,
                 FORTUNE_SCOPE,
             )
             await self.bot.db.execute(
                 "DELETE FROM user_profiles WHERE user_id = ?",
                 (ctx.author.id,),
             )
             await self.bot.db.execute(
                 "DELETE FROM api_call_log WHERE api_type = ?",
                 (f"fortune_detail_{ctx.author.id}",),
             )
             await self.bot.db.commit()
             await ctx.send(
                 "🗑️ 운세 정보와 구독 설정을 모두 지우고 동의도 철회했어요.\n평소 대화와 서버 기록은 그대로예요. 동의·철회 기록은 그대로 남아요."
             )
        except Exception as e:
             logger.error(f"운세 정보 삭제 중 오류: {e}", exc_info=True)
             await ctx.send("❌ 삭제 중 오류가 발생했어요.")

    @fortune.command(name='구독', aliases=['구독시간', '알림시간'])
    async def fortune_subscribe(self, ctx: commands.Context, time_str: str):
        """
        매일 아침 오늘의 운세 브리핑 구독을 설정합니다. (DM 전용)

        사용법:
        - `!운세 구독 HH:MM`

        예시:
        - `!운세 구독 07:30`
        """
        # DM 체크
        if ctx.guild:
            await ctx.reply("⚠️ 구독 설정은 DM에서만 가능해요.")
            return
        if not config.FORTUNE_MORNING_BRIEFING_ENABLED:
            await ctx.send("ℹ️ 여기서는 자동 아침 운세 구독을 운영하지 않아요.")
            return

        if time_str in ["취소", "해제", "off", "cancel", "중단", "비활성", "비활성화"]:
            await self.fortune_unsubscribe(ctx)
            return

        if not await self._has_fortune_consent(ctx.author.id):
            await self._send_fortune_consent_prompt(
                ctx,
                on_granted=lambda _interaction: FortuneCog.fortune_subscribe.callback(
                    self,
                    ctx,
                    time_str,
                ),
            )
            return

        if not TIME_PATTERN.match(time_str):
            await ctx.send("❌ 올바른 시간 형식이 아닙니다. `HH:MM` (24시간제)로 입력해주세요.\n혹시 구독을 취소하시려면 `!구독 취소`라고 입력해주세요.")
            return
        
        # 5분 여유 확인
        now = datetime.now(pytz.timezone('Asia/Seoul'))
        try:
             target_time = datetime.strptime(time_str, '%H:%M').replace(year=now.year, month=now.month, day=now.day, tzinfo=now.tzinfo)
             if target_time <= now:
                 target_time += timedelta(days=1)
                 
             diff_minutes = (target_time - now).total_seconds() / 60
             if diff_minutes < 5:
                 await ctx.send(f"⚠️ **시간 설정 주의**\n원활한 발송 준비를 위해, 현재 시간보다 최소 5분 이후의 시간으로 설정해주세요.\n(현재 시각: {now.strftime('%H:%M')})")
                 return
        except Exception as e:
             logger.error(f"시간 계산 오류: {e}")

        try:
             # 프로필 존재 여부 확인
             cursor = await self.bot.db.execute("SELECT 1 FROM user_profiles WHERE user_id = ?", (ctx.author.id,))
             if not await cursor.fetchone():
                 await ctx.send("⚠️ 먼저 `!운세 등록`으로 정보를 등록해주세요.")
                 return

             # 프로필 조회와 설정 저장 사이에 동의가 철회될 수 있으므로
             # 개인정보 기반 자동 처리 활성화 직전에 최신 상태를 다시 확인한다.
             if not await self._has_fortune_consent(ctx.author.id):
                 await self._send_fortune_consent_prompt(ctx)
                 return

             await self.bot.db.execute(
                 """
                 UPDATE user_profiles
                 SET subscription_time = ?, subscription_active = 1
                 WHERE user_id = ?
                   AND EXISTS (
                       SELECT 1
                       FROM privacy_consents AS pc
                       WHERE pc.user_id = user_profiles.user_id
                         AND pc.scope = ?
                         AND pc.policy_version = ?
                         AND pc.notice_hash = ?
                         AND pc.status = ?
                         AND pc.granted_at IS NOT NULL
                         AND pc.withdrawn_at IS NULL
                   )
                 """,
                 (
                     time_str,
                     ctx.author.id,
                     FORTUNE_CONSENT_POLICY.scope,
                     FORTUNE_CONSENT_POLICY.version,
                     FORTUNE_CONSENT_POLICY.notice_hash,
                     CONSENT_GRANTED,
                 ),
             )
             await self.bot.db.commit()
             # MySQL/TiDB rowcount는 동일 값 재설정 시 0일 수 있으므로 성공
             # 판정에 사용하지 않는다. 저장값과 현재 동의를 JOIN으로 다시 읽어
             # 유효한 활성 상태만 사용자에게 성공으로 알린다.
             async with self.bot.db.execute(
                 """
                 SELECT 1
                 FROM user_profiles AS up
                 JOIN privacy_consents AS pc
                   ON pc.user_id = up.user_id
                  AND pc.scope = ?
                  AND pc.policy_version = ?
                  AND pc.notice_hash = ?
                  AND pc.status = ?
                  AND pc.granted_at IS NOT NULL
                  AND pc.withdrawn_at IS NULL
                 WHERE up.user_id = ?
                   AND up.subscription_active = 1
                   AND up.subscription_time = ?
                 """,
                 (
                     FORTUNE_CONSENT_POLICY.scope,
                     FORTUNE_CONSENT_POLICY.version,
                     FORTUNE_CONSENT_POLICY.notice_hash,
                     CONSENT_GRANTED,
                     ctx.author.id,
                     time_str,
                 ),
             ) as verify_cursor:
                 activated = await verify_cursor.fetchone()
             if not activated:
                 # 실제 UPDATE 문도 현재 정책 동의를 조건으로 삼아 두 번째
                 # 애플리케이션 검사 직후 발생한 철회 경합까지 fail-closed한다.
                 await self._send_fortune_consent_prompt(ctx)
                 return
             await ctx.send(f"✅ 구독이 활성화됐어요! 매일 아침 `{time_str}`에 브리핑을 보내드릴게요.")
        except Exception as e:
             logger.error(f"구독 설정 중 오류: {e}", exc_info=True)
             await ctx.send("❌ 설정 변경 중 오류가 발생했어요.")

    @fortune.command(name='구독취소')
    async def fortune_unsubscribe(self, ctx: commands.Context):
        """
        운세 브리핑 구독을 중단합니다. (정보는 유지됨)

        사용법:
        - `!운세 구독취소`

        예시:
        - `!운세 구독취소`
        """
        try:
             await self.bot.db.execute(
                 "UPDATE user_profiles SET subscription_active = 0 WHERE user_id = ?",
                 (ctx.author.id,)
             )
             await self.bot.db.commit()
             await ctx.send("🔕 오늘의 운세 브리핑 구독이 취소됐어요. (등록된 정보는 그대로 남아요.)")
        except Exception as e:
             logger.error(f"구독 취소 중 오류: {e}", exc_info=True)
             await ctx.send("❌ 구독 취소 중 오류가 발생했어요.")

    @commands.command(name='구독', aliases=['구독시간', '알림시간'])
    async def global_subscribe(self, ctx: commands.Context, time_str: str):
        """
        `!운세 구독`의 별칭 명령어입니다. (DM 전용)

        사용법:
        - `!구독 HH:MM`

        예시:
        - `!구독 08:00`
        """
        await self.fortune_subscribe(ctx, time_str)
    
    @commands.command(name='이번달운세', aliases=['이번달'])
    @commands.dm_only()
    async def monthly_fortune(self, ctx: commands.Context, arg: str = None):
        """
        이번 달의 운세를 확인합니다. (DM 전용, 하루 3회 제한)

        사용법:
        - `!이번달운세`

        예시:
        - `!이번달운세`
        """
        # !이번달 운세 <- 이렇게 띄어쓰기 한 경우 처리
        if arg and arg not in ['운세']:
             return # 다른 명령어일 수 있음
        status_msg = await ctx.send("📅 이번달 운세를 분석 중이야...")
        await self._check_fortune_logic(ctx, mode='month', status_msg=status_msg)

    @commands.command(name='올해운세', aliases=['올해', '신년운세'])
    @commands.dm_only()
    async def yearly_fortune(self, ctx: commands.Context, arg: str = None):
        """
        올해의 운세를 확인합니다. (DM 전용, 하루 3회 제한)

        사용법:
        - `!올해운세`

        예시:
        - `!올해운세`
        """
        # !올해 운세 <- 띄어쓰기 대응
        if arg and arg not in ['운세']:
             return
        status_msg = await ctx.send("🗓️ 올해 운세를 살펴보는 중이야...")
        await self._check_fortune_logic(ctx, mode='year', status_msg=status_msg)

    def _fortune_lock_for_user(self, user_id: int) -> asyncio.Lock:
        """개인 운세 quota 임계구역을 직렬화하되 lock을 무한 보관하지 않는다."""
        locks = getattr(self, "_fortune_user_locks", None)
        if locks is None:
            locks = weakref.WeakValueDictionary()
            self._fortune_user_locks = locks
        lock = locks.get(int(user_id))
        if lock is None:
            lock = asyncio.Lock()
            locks[int(user_id)] = lock
        return lock

    async def _send_fortune_status(
        self,
        ctx: commands.Context,
        status_msg: discord.Message | None,
        message: str,
    ) -> None:
        if status_msg is not None:
            await status_msg.edit(content=message)
        else:
            await ctx.send(message)

    async def _reserve_personal_fortune_attempt(
        self,
        ctx: commands.Context,
        *,
        status_msg: discord.Message | None,
    ) -> int | None:
        """동의·3회 상한 확인과 실패 포함 물리 시도 예약을 한곳에서 수행한다.

        호출자는 반드시 사용자별 운세 lock을 잡은 상태여야 한다. 이 헬퍼를
        provider 호출 바로 앞에서 호출해 check→INSERT 사이의 동일 프로세스
        경쟁과 동의 철회 TOCTOU 창을 최소화한다.
        """
        user_id = int(ctx.author.id)
        if not await self._has_fortune_consent(user_id):
            await self._send_fortune_consent_prompt(
                ctx,
                status_msg=status_msg,
            )
            return None

        is_limited, remaining = await db_utils.check_fortune_daily_limit(
            self.bot.db,
            user_id,
        )
        if is_limited:
            await self._send_fortune_status(
                ctx,
                status_msg,
                (
                    "⛔ **일일 운세 조회 한도 초과!**\n"
                    f"AI 개인 운세(기본/상세/월/년)는 합쳐서 하루 "
                    f"{FORTUNE_DAILY_LIMIT}회까지 "
                    "이용할 수 있어요.\n내일 다시 찾아와주세요! 🌙"
                ),
            )
            return None

        # 외부 호출 전에 저장해 timeout·빈 응답·provider 오류도 실제 시도로
        # 차감한다. 저장소 장애는 무제한 호출로 이어지지 않도록 fail-closed다.
        if not await db_utils.log_fortune_usage(self.bot.db, user_id):
            await self._send_fortune_status(
                ctx,
                status_msg,
                (
                    "운세 사용량을 안전하게 기록하지 못해 AI 호출을 시작하지 "
                    "않았습니다. 잠시 후 다시 시도해주세요."
                ),
            )
            return None
        return max(0, int(remaining) - 1)

    def _zodiac_cache_get(
        self,
        key: tuple[str, ...],
        *,
        now: float | None = None,
    ) -> tuple[bool, str | None]:
        cache = getattr(self, "_zodiac_cache", None)
        if cache is None:
            cache = OrderedDict()
            self._zodiac_cache = cache
        current = time.monotonic() if now is None else float(now)
        entry = cache.get(key)
        if entry is None:
            return False, None
        expires_at, value = entry
        if expires_at <= current:
            cache.pop(key, None)
            return False, None
        cache.move_to_end(key)
        return True, value

    def _zodiac_cache_put(
        self,
        key: tuple[str, ...],
        value: str | None,
        *,
        negative: bool,
        now: float | None = None,
    ) -> None:
        cache = getattr(self, "_zodiac_cache", None)
        if cache is None:
            cache = OrderedDict()
            self._zodiac_cache = cache
        current = time.monotonic() if now is None else float(now)
        ttl = _fortune_setting(
            (
                "FORTUNE_ZODIAC_NEGATIVE_CACHE_TTL_SECONDS"
                if negative
                else "FORTUNE_ZODIAC_CACHE_TTL_SECONDS"
            ),
            30 if negative else 90000,
            5 if negative else 300,
            300 if negative else 172800,
        )
        cache[key] = (current + ttl, value)
        cache.move_to_end(key)
        maximum = _fortune_setting(
            "FORTUNE_ZODIAC_CACHE_MAX_ENTRIES",
            32,
            1,
            128,
        )
        while len(cache) > maximum:
            cache.popitem(last=False)

    def _zodiac_lock_for_key(self, key: tuple[str, ...]) -> asyncio.Lock:
        locks = getattr(self, "_zodiac_key_locks", None)
        if locks is None:
            locks = weakref.WeakValueDictionary()
            self._zodiac_key_locks = locks
        lock = locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            locks[key] = lock
        return lock

    def _zodiac_cooldown_remaining(
        self,
        user_id: int,
        *,
        now: float | None = None,
    ) -> int:
        """요청 시각을 먼저 예약해 cache hit Discord 도배도 제한한다."""
        cooldowns = getattr(self, "_zodiac_user_cooldowns", None)
        if cooldowns is None:
            cooldowns = OrderedDict()
            self._zodiac_user_cooldowns = cooldowns
        current = time.monotonic() if now is None else float(now)
        user_id = int(user_id)
        expires_at = float(cooldowns.get(user_id, 0.0))
        if expires_at > current:
            cooldowns.move_to_end(user_id)
            return max(1, math.ceil(expires_at - current))

        duration = _fortune_setting(
            "FORTUNE_ZODIAC_USER_COOLDOWN_SECONDS",
            5,
            1,
            60,
        )
        cooldowns[user_id] = current + duration
        cooldowns.move_to_end(user_id)
        while len(cooldowns) > _ZODIAC_COOLDOWN_MAX_USERS:
            cooldowns.popitem(last=False)
        return 0

    async def _reserve_zodiac_physical_attempt(self) -> tuple[bool, str]:
        """KST 날짜별 별자리 provider 시도를 전역 상한 안에서 먼저 기록한다."""
        lock = getattr(self, "_zodiac_attempt_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._zodiac_attempt_lock = lock
        async with lock:
            kst = pytz.timezone("Asia/Seoul")
            now_kst = datetime.now(kst)
            today_start = now_kst.replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            ).astimezone(timezone.utc)
            limit = _fortune_setting(
                "FORTUNE_ZODIAC_DAILY_PHYSICAL_LIMIT",
                30,
                1,
                120,
            )
            try:
                async with self.bot.db.execute(
                    """
                    SELECT COUNT(*) FROM api_call_log
                    WHERE api_type = ? AND called_at >= ?
                    """,
                    (_ZODIAC_ATTEMPT_API_TYPE, today_start.isoformat()),
                ) as cursor:
                    row = await cursor.fetchone()
                if int(row[0] if row else 0) >= limit:
                    return False, "limit"
                await self.bot.db.execute(
                    "INSERT INTO api_call_log (api_type, called_at) VALUES (?, ?)",
                    (
                        _ZODIAC_ATTEMPT_API_TYPE,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                await self.bot.db.commit()
                return True, "reserved"
            except Exception:
                try:
                    await self.bot.db.rollback()
                except Exception:
                    pass
                logger.error("별자리 물리 호출 예약 실패", exc_info=True)
                return False, "unavailable"

    async def _bounded_zodiac_generation(
        self,
        **kwargs,
    ) -> tuple[str | None, str]:
        """한 사용자가 cooldown 뒤에도 여러 cache miss를 겹쳐 만들지 못하게 한다."""
        ctx = kwargs["ctx"]
        user_id = int(ctx.author.id)
        inflight = getattr(self, "_zodiac_users_inflight", None)
        if inflight is None:
            inflight = set()
            self._zodiac_users_inflight = inflight
        if user_id in inflight:
            return None, "user_busy"
        inflight.add(user_id)
        try:
            return await self._cached_zodiac_generation(**kwargs)
        finally:
            inflight.discard(user_id)

    async def _cached_zodiac_generation(
        self,
        *,
        ctx: commands.Context,
        key: tuple[str, ...],
        system_prompt: str,
        user_prompt: str,
        log_extra: dict,
        requires_consent: bool = False,
    ) -> tuple[str | None, str]:
        """bounded cache와 key singleflight 뒤 최대 한 번만 provider를 호출한다."""
        found, cached = self._zodiac_cache_get(key)
        if found:
            return cached, "cached" if cached else "negative_cached"

        lock = self._zodiac_lock_for_key(key)
        wait_seconds = _fortune_setting(
            "FORTUNE_ZODIAC_LLM_TIMEOUT_SECONDS",
            35,
            5,
            45,
        ) + 2
        try:
            await asyncio.wait_for(lock.acquire(), timeout=wait_seconds)
        except asyncio.TimeoutError:
            return None, "busy"
        try:
            found, cached = self._zodiac_cache_get(key)
            if found:
                return cached, "cached" if cached else "negative_cached"

            if requires_consent and not await self._has_fortune_consent(
                ctx.author.id
            ):
                return None, "consent_required"
            ai_handler = self.bot.get_cog("AIHandler")
            if ai_handler is None:
                self._zodiac_cache_put(key, None, negative=True)
                return None, "unavailable"
            reserved, reservation_outcome = (
                await self._reserve_zodiac_physical_attempt()
            )
            if not reserved:
                return None, reservation_outcome

            timeout_seconds = _fortune_setting(
                "FORTUNE_ZODIAC_LLM_TIMEOUT_SECONDS",
                35,
                5,
                45,
            )
            try:
                response = await asyncio.wait_for(
                    ai_handler._cometapi_generate_content(
                        system_prompt,
                        user_prompt,
                        log_extra=log_extra,
                    ),
                    timeout=timeout_seconds,
                )
            except Exception:
                logger.warning(
                    "별자리 LLM 생성 실패: key=%s",
                    key,
                    exc_info=True,
                )
                response = None
            normalized = (
                response.strip()
                if isinstance(response, str) and response.strip()
                else None
            )
            self._zodiac_cache_put(
                key,
                normalized,
                negative=normalized is None,
            )
            return normalized, "generated" if normalized else "failed"
        finally:
            lock.release()

    async def _check_fortune_logic(
        self,
        ctx: commands.Context,
        option: str = None,
        mode: str = "day",
        status_msg: discord.Message = None,
    ):
        """모든 즉시 개인 운세의 동의·quota·물리 호출을 사용자별 직렬화한다."""
        async with self._fortune_lock_for_user(ctx.author.id):
            return await self._check_fortune_logic_unlocked(
                ctx,
                option,
                mode,
                status_msg,
            )

    async def _check_fortune_logic_unlocked(
        self,
        ctx: commands.Context,
        option: str = None,
        mode: str = "day",
        status_msg: discord.Message = None,
    ):
        """운세 조회의 핵심 로직: 일일 한도 확인 → 프로필 조회 → 별자리/사주 데이터 → LLM 운세 생성."""
        user_id = ctx.author.id
        is_dm = isinstance(ctx.channel, discord.DMChannel)

        if not await self._has_fortune_consent(user_id):
            await self._send_fortune_consent_prompt(
                ctx,
                status_msg=status_msg,
                on_granted=lambda _interaction: self._check_fortune_logic(
                    ctx,
                    option,
                    mode,
                    status_msg,
                ),
            )
            return

        is_detail_request = bool(
            option and option.strip() in {"상세", "detail"}
        )

        # 2. 프로필 조회
        cursor = await self.bot.db.execute("SELECT birth_date, birth_time, gender, birth_place FROM user_profiles WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        
        if not row:
            if status_msg:
                msg = "🔮 개인 운세를 보려면 DM으로 `!운세 등록`을 먼저 해주세요!" if ctx.guild else "🔮 아직 정보가 없네요. `!운세 등록`으로 생년월일을 알려주세요!"
                await status_msg.edit(content=msg)
            elif ctx.guild: # 서버에서는 안내만
                await ctx.reply("🔮 개인 운세를 보려면 DM으로 `!운세 등록`을 먼저 해주세요!", mention_author=True)
            else: # DM에서는 바로 유도
                await ctx.send("🔮 아직 정보가 없네요. `!운세 등록`으로 생년월일을 알려주세요!")
            return

        birth_date, birth_time, gender, birth_place = row
        # 선택 항목 미제공을 남성/정오/대한민국으로 추측하지 않는다.
        gender_text = _gender_label(gender)
        birth_place_text = birth_place or "미제공"
        
        # Typing indicator (작성 중 표시)
        async with ctx.typing():
            # 운세 데이터 생성
            fortune_data = self.calculator.get_comprehensive_info(birth_date, birth_time)
            fortune_data += f"\n[Birth Place]: {birth_place_text}"
            
            # 3. AI 핸들러 호출
            ai_handler = self.bot.get_cog('AIHandler')
            if not ai_handler:
                if status_msg: await status_msg.edit(content="AI 모듈을 불러올 수 없어요.")
                else: await ctx.send("AI 모듈을 불러올 수 없어요.")
                return
            
            # 모델명 매핑 (환경변수/설정으로 오버라이드 가능)
            MODEL_LITE = getattr(config, "FORTUNE_MODEL_LITE", "DeepSeek-V3.2-Exp-nothinking")
            MODEL_PRO = getattr(config, "FORTUNE_MODEL_PRO", "DeepSeek-V3.2-Exp-thinking")

            # 별자리 데이터 추가
            try:
                 b_year, b_month, b_day = map(int, birth_date.split('-'))
                 user_sign = get_sign_from_date(b_month, b_day)
                 now = datetime.now(pytz.timezone('Asia/Seoul'))
                 astro_chart = self.calculator._get_astrology_chart(now)
                 fortune_data += f"\n[User Zodiac]: {user_sign}\n[Gender]: {gender_text}\n[Astro Chart]: {astro_chart}"
            except Exception as e:
                 logger.error(f"Zodiac integration error: {e}")
                 user_sign = "알 수 없음"

            # 프롬프트 설정 (통합)
            display_name = ctx.author.display_name
            
            if mode == 'month':
                period_str = "이번 달"
                prompt_focus = "이번 달의 전반적인 흐름과 주의사항을 알려줘."
            elif mode == 'year':
                period_str = "올해"
                prompt_focus = "올해의 총운과 월별 흐름을 간략히 포함해줘."
            else:
                period_str = "오늘"
                prompt_focus = "오늘의 구체적인 운세 흐름을 알려줘."

            # 채널 vs DM 및 상세 옵션 처리
            # 1. 서버 채널: 무조건 3줄 요약
            # 2. DM (기본): 적당한 요약 (Moderate Summary)
            # 3. DM (상세): 풀버전 상세 분석 (Full Detail)
            
            is_detail_request = (option and option.strip() in ['상세', 'detail'])
            
            if not is_dm and mode == 'day': # [Case 1] 서버 채널
                model_name = MODEL_LITE
                system_prompt = (
                    "너는 '마사몽'이야. 채널(공개된 공간)에서 사용자의 운세를 3줄로 핵심만 요약해서 알려줘. "
                    "구체적인 내용은 DM으로 확인하라고 안내해야 해."
                )
                user_prompt = (
                    f"{fortune_data}\n\n"
                    f"사용자: {display_name} (성별: {gender_text})\n"
                    f"이 사용자의 오늘의 운세를 **3줄 요약**해줘.\n"
                    f"마지막 줄에는 반드시 '✨ 더 자세한 운세는 저에게 DM으로 `!운세 상세`라고 보내주세요!' 라고 덧붙여줘."
                )
            
            elif is_dm and not is_detail_request and mode == 'day': # [Case 2] DM 기본 (적당한 요약)
                model_name = MODEL_LITE
                system_prompt = (
                    "너는 사용자의 친구이자 개인 비서인 '마사몽'이야. "
                    "운세는 재미와 자기 성찰을 위한 참고 정보임을 전제로, "
                    "오늘 바로 활용할 수 있는 구체적인 브리핑을 제공해. "
                    "Discord에서 안정적으로 보이도록 굵은 소제목과 한 단계 불릿만 "
                    "사용하고 표, # 헤더, HTML, 복잡한 중첩 목록은 쓰지 마. "
                    "건강·금전·관계의 결과를 확정적으로 단언하지 마."
                )
                user_prompt = (
                    f"{fortune_data}\n\n"
                    f"사용자: {display_name} (성별: {gender_text})\n"
                    "오늘의 운세를 다음 순서로 간결하게 작성해줘.\n"
                    "1. **오늘의 흐름**: 100점 만점 지수와 근거 2문장\n"
                    "2. **일·학업 / 재물 / 관계 / 컨디션**: 각 1문장\n"
                    "3. **오늘의 행동**: 하면 좋은 일 2개, 피하면 좋은 일 1개\n"
                    "4. **행운 포인트**: 색, 아이템, 시간대 각 1개\n"
                    "마지막 줄에 '✨ 더 깊은 분석은 `!운세 상세`에서 확인할 수 "
                    "있어요.'라고 안내해줘."
                )

            else: # [Case 3] DM 상세 or 월/년 운세
                model_name = MODEL_PRO
                system_prompt = (
                    "너는 전문 점성가이자 명리하자인 '마사몽'이야. "
                    "사용자의 운세와 별자리 정보를 깊이 있게 분석해서 상세한 답변을 제공해줘. "
                    "동양(사주)과 서양(별자리) 관점을 종합하고, 사용자가 성별을 제공한 경우에만 이를 고려해. "
                    "미제공 항목을 추측하지 마. "
                    "운세는 재미와 자기 성찰을 위한 참고 정보이며 의료·투자·법률 "
                    "판단을 대신하지 않는다는 점을 짧게 밝혀. 결과를 확정적으로 "
                    "단언하거나 불안을 조장하지 마. 서로 충돌하는 해석이 있으면 "
                    "가능성과 주의점으로 구분해. "
                    "Discord에서 안정적으로 보이도록 굵은 제목과 단순 불릿만 사용해. "
                    "마크다운 표, # 헤더, HTML, 복잡한 중첩 목록은 사용하지 마."
                )
                if mode == "month":
                    structure = (
                        "[이번 달 핵심 지수와 총평], [초반·중반·후반 흐름], "
                        "[일·학업], [재물], [연애·대인관계], [건강·생활 리듬], "
                        "[중요한 날짜대 3개], [실천 체크리스트], [행운 포인트]"
                    )
                elif mode == "year":
                    structure = (
                        "[올해 핵심 지수와 총평], [분기별 흐름 1~4분기], "
                        "[일·학업], [재물], [연애·대인관계], [건강·생활 리듬], "
                        "[기회와 주의 시기], [올해의 실천 체크리스트], [행운 포인트]"
                    )
                else:
                    structure = (
                        "[오늘의 핵심 지수와 총평], [시간대별 흐름: 아침·낮·저녁], "
                        "[일·학업], [재물], [연애·대인관계], [건강·생활 리듬], "
                        "[하면 좋은 일 3개], [피하면 좋은 일 2개], "
                        "[행운 색·숫자·아이템·시간대], [한 줄 결론]"
                    )
                user_prompt = (
                    f"{fortune_data}\n\n"
                    f"사용자 닉네임: {display_name}\n"
                    f"성별: {gender_text}\n"
                    f"위 데이터를 바탕으로 {user_sign} 사용자({birth_date})의 {period_str} 운세를 아주 상세하게 분석해줘.\n"
                    f"{prompt_focus}\n"
                    f"항목: {structure}\n"
                    "각 해석에는 사용자가 실제로 해볼 수 있는 짧은 행동 제안을 "
                    "붙이고, 같은 말을 반복하지 마."
                )

            # 프로필을 읽고 prompt를 만든 뒤에도 최신 동의를 다시 확인한다.
            # 모든 즉시 개인 운세는 같은 3회 check→실패 포함 예약을 사용한다.
            remaining_after_attempt = await self._reserve_personal_fortune_attempt(
                ctx,
                status_msg=status_msg,
            )
            if remaining_after_attempt is None:
                return
            quota_reserved = True

            # 모델 라우팅
            try:
                 response = await ai_handler._cometapi_generate_content(
                     system_prompt, 
                     user_prompt, 
                     log_extra={
                         'guild_id': ctx.guild.id if ctx.guild else None,
                         'user_id': user_id,
                         'mode': f'fortune_{mode}',
                     },
                     model=model_name
                 )
                 
                 if response:
                     if not await self._has_fortune_consent(user_id):
                         message = (
                             "개인정보 동의가 처리 도중 철회되어 운세 결과 전송과 "
                             "컨텍스트 저장을 중단했습니다."
                         )
                         if status_msg:
                             await status_msg.edit(content=message)
                         else:
                             await ctx.send(message)
                         return
                     if status_msg:
                         # 자연 경계 분할 헬퍼로 통일(마크다운/단어 중간 절단 방지).
                         # 첫 청크는 기존 상태 메시지를 편집하고, 나머지는 이어서 전송한다.
                         chunks = split_message_chunks(response) or [response]
                         for index, chunk in enumerate(chunks):
                             if not await self._has_fortune_consent(user_id):
                                 return
                             if index == 0:
                                 await status_msg.edit(
                                     content=chunk,
                                     allowed_mentions=discord.AllowedMentions.none(),
                                 )
                             else:
                                 await ctx.send(
                                     chunk,
                                     allowed_mentions=discord.AllowedMentions.none(),
                                 )
                                 await asyncio.sleep(0.5)
                     else:
                         chunks = split_message_chunks(response) or [response]
                         for chunk in chunks:
                             if not await self._has_fortune_consent(user_id):
                                 return
                             await ctx.send(
                                 chunk,
                                 allowed_mentions=discord.AllowedMentions.none(),
                             )
                             await asyncio.sleep(0.5)
                     # 저장/후속 전송 시점에도 최신 상태를 사용한다.
                     if not await self._has_fortune_consent(user_id):
                         return
                     # DM이고 상세 운세(오늘)인 경우 컨텍스트 저장
                     if is_dm and mode == 'day' and is_detail_request:
                         await self._update_last_fortune_context(user_id, response)
                     
                     # 사용량은 물리 호출 전에 이미 예약했다.
                     await ctx.send(
                         f"💡 남은 횟수: {remaining_after_attempt}회 "
                         "(AI 개인 운세 기본/상세/월/년 합산)"
                     )
                 else:
                     failure_message = "운세 분석에 실패했습니다. (AI 응답 없음)"
                     if quota_reserved:
                         failure_message += (
                             "\n이 요청은 외부 AI 호출이 시작되어 일일 한도에 "
                             f"포함됩니다. 남은 횟수: {remaining_after_attempt}회"
                         )
                     if status_msg: await status_msg.edit(content=failure_message)
                     else: await ctx.send(failure_message)
                     
            except Exception as e:
                 logger.error(f"운세 요청 처리 중 오류: {e}", exc_info=True)
                 failure_message = "운세 시스템에 문제가 발생했습니다."
                 if quota_reserved:
                     failure_message += (
                         "\n외부 AI 호출이 시작된 요청이므로 일일 한도에 "
                         f"포함됩니다. 남은 횟수: {remaining_after_attempt}회"
                     )
                 if status_msg: await status_msg.edit(content=failure_message)
                 else: await ctx.send(failure_message)



    @commands.group(name='별자리', aliases=['운세전체'])
    async def zodiac(self, ctx: commands.Context):
        """
        별자리 운세를 확인합니다. 🌌
        
        사용법:
        - `!별자리` : 내 별자리 운세 (등록 정보가 있으면 자동)
        - `!별자리 <이름>` : 특정 별자리 운세
        - `!별자리 순위` : 오늘의 12별자리 랭킹

        예시:
        - `!별자리`
        - `!별자리 물병자리`
        - `!별자리 순위`
        """
        if ctx.invoked_subcommand is None:
            content = ctx.message.content.strip()
            params = content.split()
            
            # 1. 인자가 있는 경우 (기존 로직 유지)
            if len(params) > 1:
                arg = params[1]
                if arg in ['순위', '랭킹', 'ranking']:
                    await self._show_zodiac_ranking(ctx)
                else:
                    target_sign = arg
                    await self._show_zodiac_fortune(ctx, target_sign)
                return

            # 2. 인자가 없는 경우 -> DB 확인
            target_sign = None

            if not await self._has_fortune_consent(ctx.author.id):
                await self._send_fortune_consent_prompt(ctx)
                return
            
            # DB에서 생년월일 조회
            cursor = await self.bot.db.execute("SELECT birth_date FROM user_profiles WHERE user_id = ?", (ctx.author.id,))
            row = await cursor.fetchone()
            
            if row and row[0]:
                try:
                    b_year, b_month, b_day = map(int, row[0].split('-'))
                    target_sign = get_sign_from_date(b_month, b_day)
                    if not await self._has_fortune_consent(ctx.author.id):
                        await self._send_fortune_consent_prompt(ctx)
                        return
                    # 등록된 정보로 바로 운세 출력
                    await self._show_zodiac_fortune(
                        ctx,
                        target_sign,
                        requires_consent=True,
                    )
                    return
                except Exception as e:
                    logger.error(f"별자리 자동 조회 실패: {e}")
            
            # 3. 등록된 정보도 없고 인자도 없는 경우 -> 안내 메시지
            embed = discord.Embed(
                title="🌌 오늘의 별자리 운세",
                description=(
                    "**내 별자리 운세를 보고 싶다면?**\n👉 `!운세 등록` 으로 생년월일을 알려주세요! (자동으로 인식돼요)\n\n**특정 별자리를 보고 싶다면?**\n👉 `!별자리 <이름>` (예: `!별자리 물병자리`)\n\n**12별자리 순위가 궁금하다면?**\n👉 `!별자리 순위`\n\n**목록**: 양, 황소, 쌍둥이, 게, 사자, 처녀\n천칭, 전갈, 사수, 염소, 물병, 물고기"
                ),
                color=0x6a0dad
            )
            await ctx.send(embed=embed)

    async def _show_zodiac_ranking(self, ctx: commands.Context):
        """공용 12별자리 순위를 KST 날짜별 singleflight cache로 보여준다."""
        cooldown = self._zodiac_cooldown_remaining(ctx.author.id)
        if cooldown:
            await ctx.send(
                f"⏳ 별자리 운세는 {cooldown}초 뒤에 다시 요청해주세요."
            )
            return

        now = datetime.now(pytz.timezone('Asia/Seoul'))
        astro_chart = self.calculator._get_astrology_chart(now)
        system_prompt = (
            "너는 점성술사 '마사몽'이야. 현재 천체 배치를 분석해서 12별자리의 오늘의 운세 순위를 매겨줘. "
            "1위부터 12위까지 순위를 매기고, 각 별자리에 대해 한 줄 코멘트를 달아줘. "
            "Discord에서 안정적으로 보이도록 굵은 제목과 한 단계 불릿만 사용하고, "
            "마크다운 표, # 헤더, HTML은 사용하지 마."
        )
        user_prompt = (
            f"[현재 천체 배치]\n{astro_chart}\n\n"
            f"오늘의 12별자리 운세 순위를 알려줘. "
            f"상위권(1~3위)은 🌟, 중위권(4~9위)은 😐, 하위권(10~12위)은 ☁️ 이모지를 사용하여 리스트 형식으로 분류해줘. "
            f"각 별자리마다 행운의 팁(색상, 숫자)도 포함해줘."
        )

        async with ctx.typing():
            response, outcome = await self._bounded_zodiac_generation(
                ctx=ctx,
                key=("ranking", now.date().isoformat()),
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                log_extra={
                    "user_id": ctx.author.id,
                    "mode": "zodiac_ranking",
                },
            )
        if response:
            embed = discord.Embed(
                title=f"🏆 오늘의 별자리 운세 랭킹 ({now.strftime('%m/%d')})",
                description=clip_discord_text(response, 4096),
                color=0xffd700
            )
            await ctx.send(embed=embed)
        elif outcome == "limit":
            await ctx.send(
                "🌙 오늘 만들 수 있는 별자리 운세를 다 썼어요. 이미 생성된 별자리 결과는 계속 확인할 수 있어요."
            )
        elif outcome in {"busy", "user_busy"}:
            await ctx.send(
                "별자리 순위를 생성 중인 요청이 오래 걸리고 있어요. "
                "잠시 후 다시 시도해주세요."
            )
        else:
            await ctx.send(
                "별자리 순위를 매기다가 문제가 생겼어요. 잠시 후 다시 시도해주세요."
            )

    async def _show_zodiac_fortune(
        self,
        ctx: commands.Context,
        sign_name: str,
        *,
        requires_consent: bool = False,
    ):
        """특정 별자리의 공용 운세를 날짜·별자리별 cache로 출력한다."""
        # 1. 별자리 이름 정규화
        normalized_sign = self._normalize_zodiac_name(sign_name)
        if not normalized_sign:
            await ctx.send(f"🤔 '{sign_name}'은(는) 올바른 별자리 이름이 아니에요. (예: 물병자리, 사자자리)")
            return
        if requires_consent and not await self._has_fortune_consent(
            ctx.author.id
        ):
            await self._send_fortune_consent_prompt(ctx)
            return
        cooldown = self._zodiac_cooldown_remaining(ctx.author.id)
        if cooldown:
            await ctx.send(
                f"⏳ 별자리 운세는 {cooldown}초 뒤에 다시 요청해주세요."
            )
            return

        is_dm = isinstance(ctx.channel, discord.DMChannel)
        now = datetime.now(pytz.timezone('Asia/Seoul'))
        astro_chart = self.calculator._get_astrology_chart(now)

        # 2. 채널 vs DM 분기 (프롬프트 차별화)
        if not is_dm:
            # [Channel] 요약 버전
            system_prompt = (
                "너는 '마사몽'이야. 공개된 채널에서는 별자리 운세를 **3줄로 핵심만 요약**해서 알려줘. "
                "구체적인 내용은 DM으로 확인하라고 안내해."
            )
            user_prompt = (
                f"[현재 천체 배치]\n{astro_chart}\n\n"
                f"[타겟 별자리]: {normalized_sign}\n"
                f"오늘 {normalized_sign}의 운세를 3줄로 요약해줘.\n"
                f"마지막에는 '✨ 더 자세한 별자리 분석은 DM으로 `!별자리 {normalized_sign}`을 입력해보세요!' 라고 덧붙여줘."
            )
        else:
            # [DM] 상세 버전
            system_prompt = (
                "당신은 친절하고 통찰력 있는 '점성술사 마사몽'입니다. "
                "현재 천체 배치(Transit)를 바탕으로 특정 별자리의 오늘 운세를 상세히 분석해줍니다. "
                "추상적인 표현보다는 실질적인 조언 위주로, 다정하고 희망찬 어조를 유지하세요. "
                "Discord에서 안정적으로 보이도록 굵은 제목과 한 단계 불릿만 사용하고, "
                "마크다운 표, # 헤더, HTML은 사용하지 마세요."
            )
            user_prompt = (
                f"[현재 천체 배치]\n{astro_chart}\n\n"
                f"[타겟 별자리]: {normalized_sign}\n"
                f"오늘 {normalized_sign} 사람들을 위한 상세한 운세를 작성해주세요. "
                f"각 항목명은 굵게 표시하고 단순 불릿으로 작성하며 다음 항목을 포함하세요:\n"
                f"1. 🌟 오늘의 기운 (총평)\n"
                f"2. 💘 사랑과 인간관계\n"
                f"3. 💰 일과 금전\n"
                f"4. 🍀 마사몽의 행운 팁 (행운의 색, 물건 등)"
            )

        async with ctx.typing():
            response, outcome = await self._bounded_zodiac_generation(
                ctx=ctx,
                key=(
                    "sign",
                    now.date().isoformat(),
                    normalized_sign,
                    "dm" if is_dm else "public",
                ),
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                log_extra={
                    "user_id": ctx.author.id,
                    "mode": "zodiac_fortune",
                    "sign": normalized_sign,
                },
                requires_consent=requires_consent,
            )

        if outcome == "consent_required" or (
            requires_consent
            and not await self._has_fortune_consent(ctx.author.id)
        ):
            await self._send_fortune_consent_prompt(ctx)
            return
        if response:
            embed = discord.Embed(
                title=f"✨ {normalized_sign}의 오늘 운세 ({'요약' if not is_dm else '상세'})",
                description=clip_discord_text(response, 4096),
                color=0x9b59b6
            )
            embed.set_footer(text=f"기준 시각: {now.strftime('%Y-%m-%d %H:%M')}")
            if len(response) > 4000: # 임베드 제한 초과 시 분할 텍스트로 보냄
                 await self._send_split_message(ctx, response)
            else:
                 await ctx.send(embed=embed)
        elif outcome == "limit":
            await ctx.send(
                "🌙 오늘 만들 수 있는 별자리 운세를 다 썼어요. 이미 생성된 별자리 결과는 계속 확인할 수 있어요."
            )
        elif outcome in {"busy", "user_busy"}:
            await ctx.send(
                "같은 별자리 운세를 생성 중인 요청이 오래 걸리고 있어요. "
                "잠시 후 다시 시도해주세요."
            )
        else:
            await ctx.send(
                "별들의 목소리가 오늘따라 희미하네요... "
                "잠시 후 다시 시도해주세요."
            )

    def _normalize_zodiac_name(self, name: str) -> str | None:
        """사용자 입력을 표준 별자리 이름으로 변환합니다."""
        name = name.replace("자리", "").strip()
        mapping = {
            "양": "양자리", "황소": "황소자리", "쌍둥이": "쌍둥이자리", "게": "게자리",
            "사자": "사자자리", "처녀": "처녀자리", "천칭": "천칭자리", "전갈": "전갈자리",
            "사수": "사수자리", "염소": "염소자리", "물병": "물병자리", "물고기": "물고기자리",
            "궁수": "사수자리", "물염소": "염소자리" # 이명 처리
        }
        return mapping.get(name)

    def _get_system_prompt(self, key: str) -> str:
        """운세 유형별 AI 시스템 프롬프트 템플릿을 반환합니다."""
        prompts = {
            "fortune_summary": (
                "너는 사용자의 친구이자 개인 비서인 '마사몽'이야. 제공된 운세 데이터를 바탕으로, "
                "오늘의 핵심 운세를 요약해줘. Discord용 굵은 제목과 단순 불릿만 사용하고 "
                "표, # 헤더, HTML은 사용하지 마. 이모지는 적절히 사용해."
            ),
            "fortune_detail": (
                "너는 전문 점성가이자 명리하자인 '마사몽'이야. 제공된 데이터를 깊이 있게 분석해서 "
                "[총평], [재물운], [연애/대인관계], [오늘의 조언] 항목으로 나누어 자세히 설명해줘. "
                "항목은 굵은 제목으로 표시하고 단순 불릿만 사용해. 표, # 헤더, HTML은 사용하지 마."
            ),
            "fortune_morning": (
                "너는 사용자의 아침을 여는 든든한 비서 '마사몽'이야. 오늘 하루의 흐름을 예측하고, "
                "주의할 점과 행운의 포인트를 짚어줘. 닉네임을 꼭 부르며 다정하게 인사해.\n"
                "중요: '행운의 시간'을 추천할 때는 7시 30분에 집착하지 말고, 천체 배치나 운세 기운에 맞춰 매번 다르게 추천해줘. "
                "Discord용 굵은 제목과 단순 불릿만 사용하고 표, # 헤더, HTML은 사용하지 마."
            )
        }
        return prompts.get(key, prompts['fortune_summary'])

    async def _send_split_message(self, destination, text: str):
        """Discord 메시지 2000자 제한을 고려해 텍스트를 분할 전송합니다."""
        await send_split_message(destination, text)


    async def _persist_morning_job(self, user_id: int, job: dict) -> None:
        await self.bot.db.execute(
            "UPDATE user_profiles SET pending_payload = ? WHERE user_id = ?",
            (_encode_morning_job(job), int(user_id)),
        )
        await self.bot.db.commit()

    async def _morning_profiles(
        self,
        *,
        target_date: date,
        scheduled_time: str,
        due: bool,
    ) -> list[tuple]:
        comparison = "<=" if due else "="
        query = f"""
            SELECT up.user_id, up.birth_date, up.birth_time, up.gender,
                   up.birth_place, up.subscription_time, up.pending_payload
            FROM user_profiles AS up
            JOIN privacy_consents AS pc
              ON pc.user_id = up.user_id
             AND pc.scope = ?
             AND pc.policy_version = ?
             AND pc.notice_hash = ?
             AND pc.status = ?
             AND pc.granted_at IS NOT NULL
             AND pc.withdrawn_at IS NULL
            WHERE up.subscription_active = 1
              AND up.subscription_time {comparison} ?
              AND (up.last_fortune_sent IS NULL OR up.last_fortune_sent != ?)
            ORDER BY up.subscription_time, up.user_id
        """
        async with self.bot.db.execute(
            query,
            (
                FORTUNE_CONSENT_POLICY.scope,
                FORTUNE_CONSENT_POLICY.version,
                FORTUNE_CONSENT_POLICY.notice_hash,
                CONSENT_GRANTED,
                scheduled_time,
                target_date.isoformat(),
            ),
        ) as cursor:
            return list(await cursor.fetchall())

    async def _morning_display_name(self, user_id: int) -> str:
        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await asyncio.wait_for(
                    self.bot.fetch_user(user_id),
                    timeout=5,
                )
            except Exception:
                user = None
        return str(getattr(user, "display_name", "사용자") or "사용자")

    async def _record_morning_failure(
        self,
        *,
        user_id: int,
        job: dict,
        stage: str,
        error_name: str,
        now: datetime,
    ) -> str:
        attempts_key = (
            "generation_attempts" if stage == "generation" else "send_attempts"
        )
        maximum = (
            _morning_max_generation_attempts()
            if stage == "generation"
            else _morning_max_send_attempts()
        )
        attempts = int(job.get(attempts_key) or 0)
        terminal = attempts >= maximum or error_name == "discord_forbidden"
        job["state"] = "terminal_failed" if terminal else f"{stage}_retry"
        job["last_error"] = error_name
        job["next_attempt_at"] = (
            None if terminal else _morning_retry_at(now, attempts)
        )
        await self._persist_morning_job(user_id, job)
        logger.warning(
            "모닝 브리핑 %s 실패: user_id=%s target=%s attempt=%s/%s error=%s terminal=%s",
            stage,
            user_id,
            job["target_date"],
            attempts,
            maximum,
            error_name,
            terminal,
        )
        return "terminal_failed" if terminal else f"{stage}_retry"

    async def _generate_morning_job(
        self,
        row: tuple,
        job: dict,
        *,
        target_date: date,
        now: datetime,
    ) -> str:
        (
            user_id,
            birth_date,
            birth_time,
            gender,
            birth_place,
            _subscription_time,
            _pending_payload,
        ) = row
        if not await self._has_fortune_consent(user_id):
            return "consent_required"

        attempts = int(job.get("generation_attempts") or 0)
        maximum = _morning_max_generation_attempts()
        if attempts >= maximum:
            job["state"] = "terminal_failed"
            job["last_error"] = "generation_attempt_limit"
            job["next_attempt_at"] = None
            await self._persist_morning_job(user_id, job)
            return "terminal_failed"

        # 외부 호출 전에 시도 횟수와 다음 가능 시각을 내구 저장한다. 호출 뒤
        # 프로세스가 죽어도 같은 사용자를 매분 무제한 재호출하지 않는다.
        job["generation_attempts"] = attempts + 1
        job["state"] = "generation_retry"
        job["last_error"] = "generation_in_progress"
        job["next_attempt_at"] = _morning_retry_at(
            now,
            job["generation_attempts"],
        )
        await self._persist_morning_job(user_id, job)

        ai_handler = self.bot.get_cog("AIHandler")
        if ai_handler is None:
            return await self._record_morning_failure(
                user_id=user_id,
                job=job,
                stage="generation",
                error_name="ai_handler_unavailable",
                now=now,
            )

        display_name = await self._morning_display_name(user_id)
        fortune_data = self.calculator.get_comprehensive_info(
            birth_date,
            birth_time,
            target_date=target_date,
        )
        fortune_data += f"\n[Birth Place]: {birth_place or '미제공'}"
        system_prompt = self._get_system_prompt("fortune_morning")
        user_prompt = (
            f"[대상 날짜: {target_date.isoformat()}]\n"
            f"{fortune_data}\n\n"
            f"사용자: {display_name} (성별: {_gender_label(gender)})\n\n"
            f"{target_date.isoformat()} 모닝 브리핑을 작성해줘. "
            f"첫머리에 '{display_name}님, 좋은 아침이에요!'와 같은 인사를 포함해줘. "
            "사용자가 제공하지 않은 출생 시간·성별·출생지는 추측하지 말고, "
            "제공된 데이터만으로 구체적이고 다정한 조언을 해줘."
        )
        llm_timeout = _morning_setting(
            "FORTUNE_MORNING_LLM_TIMEOUT_SECONDS",
            35,
            5,
            40,
        )
        # 프로필 조회·prompt 구성 중 철회될 수 있으므로 provider 호출과
        # 맞닿은 지점에서 최신 동의를 다시 확인한다.
        if not await self._has_fortune_consent(user_id):
            return "consent_required"
        try:
            briefing = await asyncio.wait_for(
                ai_handler._cometapi_generate_content(
                    system_prompt,
                    user_prompt,
                    log_extra={
                        "user_id": user_id,
                        "mode": "morning_briefing_generation",
                        "target_date": target_date.isoformat(),
                        "attempt": job["generation_attempts"],
                    },
                ),
                timeout=llm_timeout,
            )
            if not isinstance(briefing, str) or not briefing.strip():
                raise RuntimeError("empty_response")
        except asyncio.TimeoutError:
            return await self._record_morning_failure(
                user_id=user_id,
                job=job,
                stage="generation",
                error_name="llm_timeout",
                now=now,
            )
        except Exception as exc:
            error_name = (
                "empty_response"
                if str(exc) == "empty_response"
                else type(exc).__name__
            )
            return await self._record_morning_failure(
                user_id=user_id,
                job=job,
                stage="generation",
                error_name=error_name,
                now=now,
            )

        if not await self._has_fortune_consent(user_id):
            return "consent_required"
        job["state"] = "generated"
        job["content"] = briefing.strip()
        job["send_attempts"] = 0
        job["next_attempt_at"] = now.isoformat(timespec="seconds")
        job["last_error"] = None
        await self._persist_morning_job(user_id, job)
        logger.info(
            "모닝 브리핑 생성 완료: user_id=%s target=%s attempt=%s",
            user_id,
            target_date.isoformat(),
            job["generation_attempts"],
        )
        return "generated"

    async def _deliver_morning_job(
        self,
        row: tuple,
        job: dict,
        *,
        target_date: date,
        now: datetime,
    ) -> str:
        user_id = int(row[0])
        content = job.get("content")
        if not isinstance(content, str) or not content:
            return await self._generate_morning_job(
                row,
                job,
                target_date=target_date,
                now=now,
            )
        if not await self._has_fortune_consent(user_id):
            return "consent_required"

        attempts = int(job.get("send_attempts") or 0)
        maximum = _morning_max_send_attempts()
        if attempts >= maximum:
            job["state"] = "terminal_failed"
            job["last_error"] = "send_attempt_limit"
            job["next_attempt_at"] = None
            await self._persist_morning_job(user_id, job)
            return "terminal_failed"

        # 발송 시도도 전송 전에 저장한다. 이후 재시도는 job.content만 사용하며
        # 이미 성공한 LLM 생성을 절대 다시 호출하지 않는다.
        job["send_attempts"] = attempts + 1
        job["state"] = "send_retry"
        job["last_error"] = "send_in_progress"
        job["next_attempt_at"] = _morning_retry_at(now, job["send_attempts"])
        await self._persist_morning_job(user_id, job)

        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await asyncio.wait_for(
                    self.bot.fetch_user(user_id),
                    timeout=5,
                )
            except Exception as exc:
                return await self._record_morning_failure(
                    user_id=user_id,
                    job=job,
                    stage="send",
                    error_name=type(exc).__name__,
                    now=now,
                )
        if not await self._has_fortune_consent(user_id):
            return "consent_required"

        full_message = (
            "🌞 **좋은 아침이에요! 오늘의 모닝 브리핑**\n\n" + content
        )
        send_timeout = _morning_setting(
            "FORTUNE_MORNING_SEND_TIMEOUT_SECONDS",
            10,
            3,
            15,
        )
        # 사용자 fetch와 상태 저장 사이에도 철회될 수 있으므로 실제 Discord
        # DM 전송 직전에 한 번 더 fail-closed로 확인한다.
        if not await self._has_fortune_consent(user_id):
            return "consent_required"
        try:
            await asyncio.wait_for(
                self._send_split_message(user, full_message),
                timeout=send_timeout,
            )
        except discord.Forbidden:
            return await self._record_morning_failure(
                user_id=user_id,
                job=job,
                stage="send",
                error_name="discord_forbidden",
                now=now,
            )
        except asyncio.TimeoutError:
            return await self._record_morning_failure(
                user_id=user_id,
                job=job,
                stage="send",
                error_name="send_timeout",
                now=now,
            )
        except Exception as exc:
            return await self._record_morning_failure(
                user_id=user_id,
                job=job,
                stage="send",
                error_name=type(exc).__name__,
                now=now,
            )

        # 실제 발송 사실과 파생 캐시 정리는 기록한다. 컨텍스트는 발송 중에도
        # 현재 동의가 유지된 경우에만 새로 저장한다.
        if await self._has_fortune_consent(user_id):
            await self.bot.db.execute(
                """
                UPDATE user_profiles
                SET last_fortune_sent = ?, pending_payload = NULL,
                    last_fortune_content = ?
                WHERE user_id = ?
                """,
                (target_date.isoformat(), content, user_id),
            )
        else:
            await self.bot.db.execute(
                """
                UPDATE user_profiles
                SET last_fortune_sent = ?, pending_payload = NULL
                WHERE user_id = ?
                """,
                (target_date.isoformat(), user_id),
            )
        await self.bot.db.commit()
        logger.info(
            "모닝 브리핑 발송 완료: user_id=%s target=%s send_attempt=%s",
            user_id,
            target_date.isoformat(),
            job["send_attempts"],
        )
        return "sent"

    async def _process_one_morning_profile(
        self,
        rows: list[tuple],
        *,
        target_date: date,
        now: datetime,
    ) -> str | None:
        for row in rows:
            job = _decode_morning_job(row[6], target_date)
            if not _morning_job_ready(job, now):
                continue
            if job["state"] in {"generated", "send_retry"} and job.get(
                "content"
            ):
                return await self._deliver_morning_job(
                    row,
                    job,
                    target_date=target_date,
                    now=now,
                )
            return await self._generate_morning_job(
                row,
                job,
                target_date=target_date,
                now=now,
            )
        return None

    async def _run_morning_briefing_tick(
        self,
        *,
        now: datetime | None = None,
    ) -> str:
        """한 tick에 한 사용자·한 단계만 처리한다."""
        kst = pytz.timezone("Asia/Seoul")
        current = now.astimezone(kst) if now is not None else datetime.now(kst)
        today = current.date()

        due_rows = await self._morning_profiles(
            target_date=today,
            scheduled_time=current.strftime("%H:%M"),
            due=True,
        )
        result = await self._process_one_morning_profile(
            due_rows,
            target_date=today,
            now=current,
        )
        if result is not None:
            return result

        pregen_at = current + timedelta(minutes=3)
        pregen_target = pregen_at.date()
        pregen_rows = await self._morning_profiles(
            target_date=pregen_target,
            scheduled_time=pregen_at.strftime("%H:%M"),
            due=False,
        )
        result = await self._process_one_morning_profile(
            pregen_rows,
            target_date=pregen_target,
            now=current,
        )
        return result or "idle"

    @tasks.loop(seconds=_MORNING_LOOP_SECONDS)
    async def morning_briefing_task(self):
        """한 번에 한 단계만 실행하고 전체 tick 시간을 루프 주기보다 짧게 제한한다."""
        for _ in range(30):
            if self._ready:
                break
            await asyncio.sleep(1)
        if not self._ready:
            logger.error(
                "운세 schema 준비가 30초 안에 끝나지 않아 이번 tick을 건너뜁니다."
            )
            return
        try:
            await asyncio.wait_for(
                self._run_morning_briefing_tick(),
                timeout=_MORNING_TICK_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.error(
                "모닝 브리핑 tick이 %s초 제한을 초과해 취소되었습니다.",
                _MORNING_TICK_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.error(
                "모닝 브리핑 tick 실패: %s",
                exc,
                exc_info=True,
            )

    @morning_briefing_task.before_loop
    async def before_morning_briefing(self):
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot):
    await bot.add_cog(FortuneCog(bot))

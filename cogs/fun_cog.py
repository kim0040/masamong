# -*- coding: utf-8 -*-
"""
`!운세`, `!요약` 등 재미와 편의를 위한 기능을 제공하는 Cog입니다.
명령어뿐만 아니라, 특정 키워드에 반응하여 기능을 실행하기도 합니다.
"""

import discord
from discord.ext import commands
from typing import Dict
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass

import config
from logger_config import logger
from utils.discord_helpers import split_message_chunks
from .ai_handler import AIHandler


@dataclass
class SummaryCacheEntry:
    """채널별 요약 캐시 데이터를 저장하는 데이터클래스입니다."""
    anchor_message_id: int
    summary_text: str
    updated_at: datetime


class FunCog(commands.Cog):
    """재미, 편의 목적의 명령어 및 키워드 기반 기능을 그룹화하는 클래스입니다."""

    def __init__(self, bot: commands.Bot):
        """FunCog를 초기화하고 키워드 쿨다운 및 요약 캐시를 설정합니다."""
        self.bot = bot
        self.ai_handler: AIHandler | None = None # main.py에서 주입됨
        # 채널별 키워드 기능 쿨다운을 관리하는 딕셔너리
        self.keyword_cooldowns: Dict[int, datetime] = {}
        # 빠른 재사용용 메모리 캐시. 기준점 원본은 channel_summary_state에 영속화한다.
        self.summary_cache: Dict[int, SummaryCacheEntry] = {}
        logger.info("FunCog가 성공적으로 초기화되었습니다.")

    # --- 쿨다운 관리 ---

    def is_on_cooldown(self, channel_id: int) -> bool:
        """지정된 채널이 키워드 트리거 쿨다운 기간인지 확인합니다."""
        cooldown_seconds = config.FUN_KEYWORD_TRIGGERS.get("cooldown_seconds", 60)
        last_time = self.keyword_cooldowns.get(channel_id)
        if last_time and (datetime.now() - last_time) < timedelta(seconds=cooldown_seconds):
            return True
        return False

    def update_cooldown(self, channel_id: int):
        """지정된 채널의 키워드 트리거 쿨다운을 현재 시간으로 갱신합니다."""
        self.keyword_cooldowns[channel_id] = datetime.now()
        logger.debug(f"FunCog: 채널({channel_id})의 키워드 응답 쿨다운이 갱신되었습니다.")

    def _trim_summary_cache(self):
        """요약 캐시가 설정 최대 개수를 초과하면 가장 오래된 항목부터 제거합니다."""
        max_channels = max(1, int(getattr(config, "SUMMARY_CACHE_MAX_CHANNELS", 300)))
        if len(self.summary_cache) <= max_channels:
            return
        overflow = len(self.summary_cache) - max_channels
        for channel_id, _ in sorted(self.summary_cache.items(), key=lambda item: item[1].updated_at)[:overflow]:
            self.summary_cache.pop(channel_id, None)

    def _update_summary_cache(self, channel_id: int, anchor_message_id: int, summary_text: str):
        """채널별 요약 결과를 메모리 캐시에 기록하고 초과 시 트리밍합니다."""
        self.summary_cache[channel_id] = SummaryCacheEntry(
            anchor_message_id=int(anchor_message_id),
            summary_text=(summary_text or "").strip(),
            updated_at=datetime.now(),
        )
        self._trim_summary_cache()

    async def _load_summary_state(
        self,
        guild_id: int,
        channel_id: int,
    ) -> SummaryCacheEntry | None:
        """재시작 전 저장한 요약 기준점을 읽어 메모리 캐시에 복원합니다."""
        if not getattr(self.bot, "db", None):
            return None
        try:
            async with self.bot.db.execute(
                """
                SELECT anchor_message_id, summary_text, updated_at
                FROM channel_summary_state
                WHERE guild_id = ? AND channel_id = ?
                LIMIT 1
                """,
                (int(guild_id), int(channel_id)),
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                return None
            updated_raw = str(row["updated_at"] or "")
            try:
                updated_at = datetime.fromisoformat(
                    updated_raw.replace("Z", "+00:00")
                ).replace(tzinfo=None)
            except (TypeError, ValueError):
                updated_at = datetime.now()
            entry = SummaryCacheEntry(
                anchor_message_id=int(row["anchor_message_id"]),
                summary_text=str(row["summary_text"] or "").strip(),
                updated_at=updated_at,
            )
            if not entry.summary_text:
                return None
            self.summary_cache[int(channel_id)] = entry
            self._trim_summary_cache()
            return entry
        except Exception as exc:
            logger.warning(
                "요약 상태 조회 실패: guild=%s channel=%s error=%s",
                guild_id,
                channel_id,
                exc,
            )
            return None

    async def _persist_summary_state(
        self,
        guild_id: int,
        channel_id: int,
        anchor_message_id: int,
        summary_text: str,
    ) -> bool:
        """요약 상태 한 행만 additive upsert합니다. 대화/기존 요약을 삭제하지 않습니다."""
        if not getattr(self.bot, "db", None):
            return False
        backend = str(getattr(self.bot.db, "backend", config.DB_BACKEND))
        updated_at = datetime.now(timezone.utc).isoformat()
        if backend == "tidb":
            query = """
                INSERT INTO channel_summary_state
                    (guild_id, channel_id, anchor_message_id, summary_text, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON DUPLICATE KEY UPDATE
                    anchor_message_id = VALUES(anchor_message_id),
                    summary_text = VALUES(summary_text),
                    updated_at = VALUES(updated_at)
            """
        else:
            query = """
                INSERT INTO channel_summary_state
                    (guild_id, channel_id, anchor_message_id, summary_text, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(guild_id, channel_id) DO UPDATE SET
                    anchor_message_id = excluded.anchor_message_id,
                    summary_text = excluded.summary_text,
                    updated_at = excluded.updated_at
            """
        try:
            await self.bot.db.execute(
                query,
                (
                    int(guild_id),
                    int(channel_id),
                    int(anchor_message_id),
                    str(summary_text).strip(),
                    updated_at,
                ),
            )
            await self.bot.db.commit()
            return True
        except Exception as exc:
            try:
                await self.bot.db.rollback()
            except Exception:
                pass
            logger.error(
                "요약 상태 저장 실패: guild=%s channel=%s error=%s",
                guild_id,
                channel_id,
                exc,
            )
            return False

    # --- 핵심 실행 로직 ---

    async def execute_summarize(self, channel: discord.TextChannel, author: discord.User, status_msg: discord.Message = None):
        """
        AI를 호출하여 최근 대화를 요약하고 채널에 전송하는 핵심 로직입니다.
        `!요약` 명령어 또는 키워드 트리거에 의해 호출됩니다.
        """
        if not self.ai_handler or not self.ai_handler.is_ready or not config.AI_MEMORY_ENABLED:
            if status_msg: await status_msg.edit(content="지금은 대화 요약을 쓸 수 없어요. 잠시 뒤 다시 시도해주세요.")
            else: await channel.send("지금은 대화 요약을 쓸 수 없어요. 잠시 뒤 다시 시도해주세요.")
            return

        # [Safety] DM Support Check
        if not channel.guild:
            if status_msg: await status_msg.edit(content="이건 서버 채널에서만 쓸 수 있어요!")
            else: await channel.send("이건 서버 채널에서만 쓸 수 있어요!")
            return

        async with channel.typing():
            try:
                guild_id = channel.guild.id
                channel_id = channel.id
                latest_message_id = await self.ai_handler.get_latest_conversation_message_id(guild_id, channel_id)
                if latest_message_id is None:
                    if status_msg: await status_msg.edit(content="요약할 만한 대화가 충분히 쌓이지 않았어요.")
                    else: await channel.send("요약할 만한 대화가 충분히 쌓이지 않았어요.")
                    return

                cache_entry = self.summary_cache.get(channel_id)
                if cache_entry is None:
                    cache_entry = await self._load_summary_state(
                        guild_id,
                        channel_id,
                    )
                response_text = None
                should_persist = True

                # 1) 캐시 앵커 이후 신규 대화가 적으면 증분 요약
                if (
                    getattr(config, "SUMMARY_INCREMENTAL_ENABLED", True)
                    and cache_entry
                    and cache_entry.summary_text
                ):
                    new_count = await self.ai_handler.count_recent_conversation_messages(
                        guild_id,
                        channel_id,
                        after_message_id=cache_entry.anchor_message_id,
                        include_bot=True,
                    )

                    if new_count <= 0:
                        response_text = cache_entry.summary_text
                        should_persist = False
                    else:
                        delta_lookback = getattr(
                            config,
                            "SUMMARY_INCREMENTAL_DELTA_LOOKBACK",
                            48,
                        )
                        if new_count > getattr(
                            config,
                            "SUMMARY_INCREMENTAL_MAX_NEW_MESSAGES",
                            24,
                        ):
                            delta_lookback = max(
                                delta_lookback,
                                getattr(config, "SUMMARY_MAX_LOOKBACK", 120),
                            )
                        delta_context = await self.ai_handler.get_recent_conversation_text(
                            guild_id,
                            channel_id,
                            look_back=delta_lookback,
                            max_chars=getattr(config, "SUMMARY_MAX_CONTEXT_CHARS", 3200),
                            include_bot=True,
                            after_message_id=cache_entry.anchor_message_id,
                        )
                        if delta_context:
                            response_text = await self.ai_handler.generate_creative_text(
                                channel=channel,
                                author=author,
                                prompt_key='summarize_incremental',
                                context={
                                    'previous_summary': cache_entry.summary_text,
                                    'new_conversation': delta_context,
                                }
                            )

                # 2) 증분이 불가하거나 신규 대화량이 많으면 전체 압축 요약
                if not response_text:
                    history_str = await self.ai_handler.get_recent_conversation_text(
                        guild_id,
                        channel_id,
                        look_back=getattr(config, "SUMMARY_MAX_LOOKBACK", 120),
                        max_chars=getattr(config, "SUMMARY_MAX_CONTEXT_CHARS", 3200),
                        include_bot=True,
                    )

                    if not history_str:
                        if status_msg: await status_msg.edit(content="요약할 만한 대화가 충분히 쌓이지 않았어요.")
                        else: await channel.send("요약할 만한 대화가 충분히 쌓이지 않았어요.")
                        return

                    response_text = await self.ai_handler.generate_creative_text(
                        channel=channel,
                        author=author,
                        prompt_key='summarize',
                        context={'conversation': history_str}
                    )
                
                # AI 응답 생성 실패 시 기본 메시지 전송
                if not response_text or response_text in [config.MSG_AI_ERROR, config.MSG_CMD_ERROR]:
                    if status_msg: await status_msg.edit(content=response_text or "대화 내용을 요약하다가 머리에 쥐났어요. 다시 시도해주세요.")
                    else: await channel.send(response_text or "대화 내용을 요약하다가 머리에 쥐났어요. 다시 시도해주세요.")
                else:
                    self._update_summary_cache(channel_id, latest_message_id, response_text)
                    if should_persist:
                        await self._persist_summary_state(
                            guild_id,
                            channel_id,
                            latest_message_id,
                            response_text,
                        )
                    rendered = (
                        "**📈 최근 대화 요약 (마사몽 ver.)**\n"
                        f"{response_text}"
                    )
                    chunks = split_message_chunks(rendered) or [rendered]
                    if status_msg:
                        await status_msg.edit(
                            content=chunks[0],
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        chunks = chunks[1:]
                    for chunk in chunks:
                        await channel.send(
                            chunk,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
            except Exception as e:
                # [Fix] Handle logs safely even if guild is None (though we return early above, good for safety)
                guild_id = channel.guild.id if channel.guild else 'DM'
                logger.error(f"요약 기능 실행 중 오류: {e}", exc_info=True, extra={'guild_id': guild_id})
                if status_msg: await status_msg.edit(content=config.MSG_CMD_ERROR)
                else: await channel.send(config.MSG_CMD_ERROR)

    # --- 명령어 정의 ---

    @commands.command(name='요약', aliases=['summarize', 'summary', '3줄요약', 'sum'])
    async def summarize(self, ctx: commands.Context):
        """
        최근 대화를 압축 컨텍스트로 요약합니다. (서버 전용)

        사용법:
        - `!요약`

        예시:
        - `!요약`

        참고:
        - 대화 기록이 충분히 쌓여 있어야 합니다.
        """
        status_msg = await ctx.send("📋 대화 내용을 분석해서 요약 중이야...")
        await self.execute_summarize(ctx.channel, ctx.author, status_msg=status_msg)

async def setup(bot: commands.Bot):
    """Cog를 봇에 등록하는 함수입니다."""
    await bot.add_cog(FunCog(bot))

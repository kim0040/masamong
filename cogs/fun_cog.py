# -*- coding: utf-8 -*-
"""
`!운세`, `!요약` 등 재미와 편의를 위한 기능을 제공하는 Cog입니다.
명령어뿐만 아니라, 특정 키워드에 반응하여 기능을 실행하기도 합니다.
"""

import discord
from discord.ext import commands
from typing import Dict
from datetime import datetime, timedelta
from dataclasses import dataclass

import config
from logger_config import logger
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
        # DB 변경 없이 증분 요약을 지원하기 위한 메모리 캐시 (채널 단위)
        self.summary_cache: Dict[int, SummaryCacheEntry] = {}
        logger.info("FunCog가 성공적으로 초기화되었습니다.")

    # --- 쿨다운 관리 ---

    def is_on_cooldown(self, channel_id: int) -> bool:
        """특정 채널이 키워드 기능 쿨다운 상태인지 확인합니다."""
        cooldown_seconds = config.FUN_KEYWORD_TRIGGERS.get("cooldown_seconds", 60)
        last_time = self.keyword_cooldowns.get(channel_id)
        if last_time and (datetime.now() - last_time) < timedelta(seconds=cooldown_seconds):
            return True
        return False

    def update_cooldown(self, channel_id: int):
        """특정 채널의 키워드 기능 쿨다운을 현재 시간으로 갱신합니다."""
        self.keyword_cooldowns[channel_id] = datetime.now()
        logger.debug(f"FunCog: 채널({channel_id})의 키워드 응답 쿨다운이 갱신되었습니다.")

    def _trim_summary_cache(self):
        """캐시가 설정 개수를 초과하면 오래된 항목부터 제거합니다."""
        max_channels = max(1, int(getattr(config, "SUMMARY_CACHE_MAX_CHANNELS", 300)))
        if len(self.summary_cache) <= max_channels:
            return
        overflow = len(self.summary_cache) - max_channels
        for channel_id, _ in sorted(self.summary_cache.items(), key=lambda item: item[1].updated_at)[:overflow]:
            self.summary_cache.pop(channel_id, None)

    def _update_summary_cache(self, channel_id: int, anchor_message_id: int, summary_text: str):
        self.summary_cache[channel_id] = SummaryCacheEntry(
            anchor_message_id=int(anchor_message_id),
            summary_text=(summary_text or "").strip(),
            updated_at=datetime.now(),
        )
        self._trim_summary_cache()

    # --- 핵심 실행 로직 ---

    # async def execute_fortune(self, channel: discord.TextChannel, author: discord.User):
    #     """
    #     AI를 호출하여 오늘의 운세를 생성하고 채널에 전송하는 핵심 로직입니다.
    #     `!운세` 명령어 또는 키워드 트리거에 의해 호출됩니다.
    #     (fortune_cog.py로 이전됨)
    #     """
    #     if not self.ai_handler or not self.ai_handler.is_ready:
    #         await channel.send("죄송합니다, AI 운세 기능이 현재 준비되지 않았습니다.")
    #         return

    #     async with channel.typing():
    #         try:
    #             response_text = await self.ai_handler.generate_creative_text(
    #                 channel=channel,
    #                 author=author,
    #                 prompt_key='fortune',
    #                 context={'user_name': author.display_name}
    #             )
    #             # AI 응답 생성 실패 시 기본 메시지 전송
    #             if not response_text or response_text in [config.MSG_AI_ERROR, config.MSG_CMD_ERROR]:
    #                 await channel.send(response_text or "운세를 보다가 깜빡 졸았네요. 다시 물어봐 주세요.")
    #             else:
    #                 await channel.send(response_text)
    #         except Exception as e:
    #             logger.error(f"운세 기능 실행 중 오류: {e}", exc_info=True, extra={'guild_id': channel.guild.id})
    #             await channel.send(config.MSG_CMD_ERROR)

    async def execute_summarize(self, channel: discord.TextChannel, author: discord.User):
        """
        AI를 호출하여 최근 대화를 요약하고 채널에 전송하는 핵심 로직입니다.
        `!요약` 명령어 또는 키워드 트리거에 의해 호출됩니다.
        """
        if not self.ai_handler or not self.ai_handler.is_ready or not config.AI_MEMORY_ENABLED:
            await channel.send("죄송합니다, 대화 요약 기능이 현재 준비되지 않았습니다.")
            return

        # [Safety] DM Support Check
        if not channel.guild:
            await channel.send("이 명령어는 개인 메시지(DM)에서는 사용할 수 없어요! 서버(채널)에서 사용해주세요.")
            return

        async with channel.typing():
            try:
                guild_id = channel.guild.id
                channel_id = channel.id
                latest_message_id = await self.ai_handler.get_latest_conversation_message_id(guild_id, channel_id)
                if latest_message_id is None:
                    await channel.send("요약할 만한 대화가 충분히 쌓이지 않았어요.")
                    return

                cache_entry = self.summary_cache.get(channel_id)
                response_text = None

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
                    elif new_count <= getattr(config, "SUMMARY_INCREMENTAL_MAX_NEW_MESSAGES", 24):
                        delta_context = await self.ai_handler.get_recent_conversation_text(
                            guild_id,
                            channel_id,
                            look_back=getattr(config, "SUMMARY_INCREMENTAL_DELTA_LOOKBACK", 48),
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
                        await channel.send("요약할 만한 대화가 충분히 쌓이지 않았어요.")
                        return

                    response_text = await self.ai_handler.generate_creative_text(
                        channel=channel,
                        author=author,
                        prompt_key='summarize',
                        context={'conversation': history_str}
                    )
                
                # AI 응답 생성 실패 시 기본 메시지 전송
                if not response_text or response_text in [config.MSG_AI_ERROR, config.MSG_CMD_ERROR]:
                    await channel.send(response_text or "대화 내용을 요약하다가 머리에 쥐났어요. 다시 시도해주세요.")
                else:
                    self._update_summary_cache(channel_id, latest_message_id, response_text)
                    await channel.send(f"**📈 최근 대화 요약 (마사몽 ver.)**\n{response_text}")
            except Exception as e:
                # [Fix] Handle logs safely even if guild is None (though we return early above, good for safety)
                guild_id = channel.guild.id if channel.guild else 'DM'
                logger.error(f"요약 기능 실행 중 오류: {e}", exc_info=True, extra={'guild_id': guild_id})
                await channel.send(config.MSG_CMD_ERROR)

    # --- 명령어 정의 ---

    # @commands.command(name='운세', aliases=['fortune'])
    # async def fortune(self, ctx: commands.Context):
    #     """'마사몽' 페르소나로 오늘의 운세를 알려줍니다. (fortune_cog.py로 이전됨)"""
    #     # await self.execute_fortune(ctx.channel, ctx.author)
    #     pass

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
        await self.execute_summarize(ctx.channel, ctx.author)

async def setup(bot: commands.Bot):
    """Cog를 봇에 등록하는 함수입니다."""
    await bot.add_cog(FunCog(bot))

# -*- coding: utf-8 -*-
import logging
import sys
from datetime import datetime
import pytz
import asyncio
import discord
from discord.ext import commands
from collections import deque
import traceback
import json

import config

# KST 시간대 객체 생성
KST = pytz.timezone('Asia/Seoul')

# 로깅 시간 변환 함수 (KST 기준)
def time_converter(*args):
    return datetime.now(KST).timetuple()

_discord_log_queue = asyncio.Queue()
_bot_instance = None

class JsonFormatter(logging.Formatter):
    """
    로그 레코드를 JSON 형식으로 포맷팅하는 포맷터.
    """
    def format(self, record):
        log_object = {
            "timestamp": datetime.fromtimestamp(record.created, tz=KST).isoformat(),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        }
        # 예외 정보가 있는 경우 추가
        if record.exc_info:
            log_object['exc_info'] = self.formatException(record.exc_info)

        # 추가 컨텍스트 정보가 있는 경우 (예: guild_id, user_id)
        # 참고: 로깅 호출 시 extra={'guild_id': ..., 'user_id': ...} 형태로 전달해야 함
        extra_fields = ['guild_id', 'user_id', 'channel_id', 'author_id']
        for field in extra_fields:
            if hasattr(record, field):
                log_object[field] = getattr(record, field)

        return json.dumps(log_object, ensure_ascii=False)

class DiscordLogHandler(logging.Handler):
    """
    로그 레코드를 asyncio.Queue에 넣어 Discord로 비동기적으로 전송하는 핸들러.
    """
    def __init__(self):
        super().__init__()
        self.level_colors = {
            logging.CRITICAL: discord.Color.dark_red(),
            logging.ERROR: discord.Color.red(),
            logging.WARNING: discord.Color.gold(),
            logging.INFO: discord.Color.blue(),
            logging.DEBUG: discord.Color.dark_grey(),
        }

    def emit(self, record):
        """로그 레코드를 받아 큐에 넣습니다."""
        if not _bot_instance or _bot_instance.is_closed():
            return

        try:
            _discord_log_queue.put_nowait(record)
        except asyncio.QueueFull:
            pass

    def format_embed(self, record: logging.LogRecord) -> discord.Embed:
        """로그 레코드를 discord.Embed 객체로 포맷팅합니다."""
        embed_color = self.level_colors.get(record.levelno, discord.Color.default())

        title = f"📄 {record.levelname.upper()} Log"
        if record.levelno >= logging.ERROR:
            title = f"🚨 {record.levelname.upper()} Log"

        description = f"**Logger:** `{record.name}`"

        embed = discord.Embed(
            title=title,
            description=description,
            color=embed_color,
            timestamp=datetime.fromtimestamp(record.created).astimezone(KST)
        )

        message_content = record.getMessage()
        if record.exc_info:
            exc_text = "".join(traceback.format_exception(*record.exc_info))
            message_content += f"\n\n**Traceback:**\n```python\n{exc_text}\n```"

        if len(message_content) > 1000:
            message_content = message_content[:1000] + "..."

        embed.add_field(name="Message", value=f"```\n{message_content}\n```", inline=False)
        embed.set_footer(text="Logged at")
        return embed

async def discord_logging_task():
    """큐에서 로그를 꺼내 Discord로 전송하는 백그라운드 작업."""
    global _bot_instance
    await _bot_instance.wait_until_ready()
    logger.info("Discord 로깅 태스크 시작.")

    log_channel_cache = {}

    while not _bot_instance.is_closed():
        try:
            record = await _discord_log_queue.get()
            handler = None
            for h in logging.getLogger().handlers:
                if isinstance(h, DiscordLogHandler):
                    handler = h
                    break
            if not handler:
                continue

            embed = handler.format_embed(record)
            guild_id = getattr(record, 'guild_id', None)
            if guild_id:
                guild = _bot_instance.get_guild(guild_id)
                if guild:
                    log_channel = log_channel_cache.get(guild.id)
                    if not log_channel:
                        for channel in guild.text_channels:
                            if channel.name == 'logs':
                                bot_permissions = channel.permissions_for(guild.me)
                                if bot_permissions.send_messages and bot_permissions.embed_links:
                                    log_channel_cache[guild.id] = channel
                                    log_channel = channel
                                    break
                    if log_channel:
                        try:
                            await log_channel.send(embed=embed)
                        except discord.Forbidden:
                            log_channel_cache.pop(guild.id, None)
                        except Exception as e:
                            logging.getLogger(__name__).error(f"Discord 로그 채널({log_channel.name}) 전송 중 오류: {e}", exc_info=False)

            _discord_log_queue.task_done()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.getLogger(__name__).error(f"Discord 로깅 태스크에서 심각한 오류 발생: {e}", exc_info=True)
            await asyncio.sleep(5)


def setup_logger():
    """로거 객체를 설정하고 반환합니다."""
    # logging.Formatter.converter = time_converter # JsonFormatter가 타임스탬프를 직접 처리하므로 필요 없음
    json_formatter = JsonFormatter()

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    if logger.hasHandlers():
        logger.handlers.clear()

    # 1. 콘솔 핸들러 (JSON 포맷)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(json_formatter)
    console_handler.setLevel(logging.DEBUG)
    logger.addHandler(console_handler)

    # 2. 일반 파일 핸들러 (JSON 포맷)
    try:
        file_handler = logging.FileHandler(config.LOG_FILE_NAME, encoding='utf-8', mode='a')
        file_handler.setFormatter(json_formatter)
        logger.addHandler(file_handler)
    except Exception as e:
        print(f"**[심각] 일반 로그 파일 핸들러 설정 오류:** {e}", file=sys.stderr)

    # 3. 오류 파일 핸들러 (JSON 포맷)
    try:
        error_handler = logging.FileHandler(config.ERROR_LOG_FILE_NAME, encoding='utf-8', mode='a')
        error_handler.setFormatter(json_formatter)
        error_handler.setLevel(logging.ERROR)
        logger.addHandler(error_handler)
    except Exception as e:
        print(f"**[심각] 오류 로그 파일 핸들러 설정 오류:** {e}", file=sys.stderr)

    logging.getLogger('discord').setLevel(logging.WARNING)
    logging.getLogger('websockets').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)

    return logger

def register_discord_logging(bot: commands.Bot):
    """
    Discord 로깅 핸들러를 루트 로거에 추가하고 백그라운드 태스크를 시작합니다.
    """
    global _bot_instance
    _bot_instance = bot

    discord_handler = DiscordLogHandler()
    discord_handler.setLevel(logging.WARNING)
    logging.getLogger().addHandler(discord_handler)
    asyncio.create_task(discord_logging_task())
    logging.info("Discord 로깅 핸들러가 등록되었고, 전송 태스크가 시작될 예정입니다.")


# 로거 인스턴스 생성
logger = setup_logger()

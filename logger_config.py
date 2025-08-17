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

import config

# KST 시간대 객체 생성
KST = pytz.timezone('Asia/Seoul')

# 로깅 시간 변환 함수 (KST 기준)
def time_converter(*args):
    return datetime.now(KST).timetuple()

_discord_log_queue = asyncio.Queue()
_bot_instance = None

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
            # 큐가 가득 차면, 가장 오래된 로그를 버리고 새 로그를 넣음 (선택적)
            # 여기서는 간단히 무시
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

        # 메시지 본문 추가 (1024자 제한 엄수)
        message_content = record.getMessage()
        if record.exc_info:
            exc_text = "".join(traceback.format_exception(*record.exc_info))
            message_content += f"\n\n**Traceback:**\n```python\n{exc_text}\n```"

        # Discord 필드 값 제한인 1024자에 맞게 자르기
        if len(message_content) > 1000:
            message_content = message_content[:1000] + "..."

        embed.add_field(name="Message", value=f"```\n{message_content}\n```", inline=False)
        embed.set_footer(text="Logged at")
        return embed

async def discord_logging_task():
    """큐에서 로그를 꺼내 Discord 'logs' 채널로 전송하는 백그라운드 작업."""
    global _bot_instance
    await _bot_instance.wait_until_ready()
    logger.info("Discord 로깅 태스크 시작.")

    log_channel_cache = {}

    while not _bot_instance.is_closed():
        try:
            record = await _discord_log_queue.get()

            # 로그 레코드에 guild_id가 있는지 확인
            guild_id = getattr(record, 'guild_id', None)
            target_guilds = []

            if guild_id:
                guild = _bot_instance.get_guild(guild_id)
                if guild:
                    target_guilds.append(guild)
            else:
                # guild_id가 없으면 모든 서버의 'logs' 채널에 보낸다 (또는 특정 채널에만).
                # 여기서는 모든 서버에 보내는 것으로 가정.
                # 실제 운영 시에는 특정 서버나 채널 ID로 제한하는 것이 좋음.
                target_guilds = _bot_instance.guilds

            handler = logging.getLogger().handlers[-1] # Get this handler instance
            if not isinstance(handler, DiscordLogHandler): continue

            embed = handler.format_embed(record)

            for guild in target_guilds:
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
                        # 권한 문제 발생 시 캐시에서 제거하여 다음 번에 다시 찾도록 함
                        log_channel_cache.pop(guild.id, None)
                    except Exception as e:
                        # 전송 실패 시 파일 로그에 기록
                        logging.getLogger(__name__).error(f"Discord 로그 채널({log_channel.name}) 전송 중 오류: {e}", exc_info=False)

            _discord_log_queue.task_done()
        except asyncio.CancelledError:
            break
        except Exception as e:
            # 로깅 태스크 자체의 오류는 파일에만 기록
            logging.getLogger(__name__).error(f"Discord 로깅 태스크에서 심각한 오류 발생: {e}", exc_info=True)
            await asyncio.sleep(5)


def setup_logger():
    """로거 객체를 설정하고 반환합니다."""
    logging.Formatter.converter = time_converter
    log_formatter = logging.Formatter('%(asctime)s [%(levelname)s] [%(name)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    logger = logging.getLogger()
    logger.setLevel(logging.INFO) # 기본 로그 레벨을 INFO로 설정

    if logger.hasHandlers():
        logger.handlers.clear()

    # 1. 콘솔 핸들러
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(log_formatter)
    console_handler.setLevel(logging.DEBUG) # 콘솔에서는 DEBUG까지 모두 표시
    logger.addHandler(console_handler)

    # 2. 일반 파일 핸들러
    try:
        file_handler = logging.FileHandler(config.LOG_FILE_NAME, encoding='utf-8', mode='a')
        file_handler.setFormatter(log_formatter)
        logger.addHandler(file_handler)
    except Exception as e:
        print(f"**[심각] 일반 로그 파일 핸들러 설정 오류:** {e}", file=sys.stderr)

    # 3. 오류 파일 핸들러
    try:
        error_handler = logging.FileHandler(config.ERROR_LOG_FILE_NAME, encoding='utf-8', mode='a')
        error_handler.setFormatter(log_formatter)
        error_handler.setLevel(logging.ERROR)
        logger.addHandler(error_handler)
    except Exception as e:
        print(f"**[심각] 오류 로그 파일 핸들러 설정 오류:** {e}", file=sys.stderr)

    # discord.py 라이브러리 자체의 로그가 너무 많이 뜨는 것을 방지
    logging.getLogger('discord').setLevel(logging.WARNING)
    logging.getLogger('websockets').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)

    return logger

def register_discord_logging(bot: commands.Bot):
    """
    Discord 로깅 핸들러를 루트 로거에 추가하고 백그라운드 태스크를 시작합니다.
    이 함수는 봇이 초기화된 후 main.py에서 호출되어야 합니다.
    """
    global _bot_instance
    _bot_instance = bot

    discord_handler = DiscordLogHandler()
    # 사용자의 요청에 따라 WARNING 레벨 이상의 로그만 Discord로 전송
    discord_handler.setLevel(logging.WARNING)

    # 포매터는 핸들러 내에서 Embed를 생성하므로 필요 없음
    logging.getLogger().addHandler(discord_handler)

    # 백그라운드에서 로그 전송 태스크 시작
    asyncio.create_task(discord_logging_task())
    logging.info("Discord 로깅 핸들러가 등록되었고, 전송 태스크가 시작될 예정입니다.")


# 로거 인스턴스 생성
logger = setup_logger()

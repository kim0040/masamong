# -*- coding: utf-8 -*-
"""
마사몽 봇의 로깅 시스템을 설정하고 관리하는 모듈입니다.

주요 기능:
- KST 시간대 기준으로 로그 시간 기록
- 콘솔 출력을 위한 색상 포맷터 (ColoredFormatter)
- 파일 저장을 위한 JSON 포맷터 (JsonFormatter)
- Discord 채널로 로그를 비동기 전송하는 핸들러 및 태스크
- 로거 초기 설정 (콘솔, 파일, 오류 파일 핸들러)
"""

import logging
import sys
from datetime import datetime
import pytz
import asyncio
import discord
from discord.ext import commands
import traceback
import json

import config

# 한국 시간대 (Asia/Seoul) 객체
KST = pytz.timezone('Asia/Seoul')

# 로깅 시간 포맷을 KST로 변환하는 함수
def time_converter(*args):
    """logging.Formatter의 시간 변환기로 사용될 함수."""
    return datetime.now(KST).timetuple()

# Discord로 보낼 로그를 임시 저장하는 비동기 큐
_discord_log_queue = asyncio.Queue()
# Discord 봇 인스턴스를 저장하기 위한 전역 변수
_bot_instance = None

class ColoredFormatter(logging.Formatter):
    """
    콘솔(stdout) 로그 출력에 레벨별로 색상을 적용하는 포맷터입니다.
    디버깅 시 가독성을 높여줍니다.
    """
    GREY = "\x1b[38;20m"
    YELLOW = "\x1b[33;20m"
    RED = "\x1b[31;20m"
    BOLD_RED = "\x1b[31;1m"
    RESET = "\x1b[0m"
    BLUE = "\x1b[34;20m"

    FORMAT = "%(asctime)s [%(levelname)s] [%(name)s:%(funcName)s:%(lineno)d] - %(message)s"

    FORMATS = {
        logging.DEBUG: GREY + FORMAT + RESET,
        logging.INFO: BLUE + FORMAT + RESET,
        logging.WARNING: YELLOW + FORMAT + RESET,
        logging.ERROR: RED + FORMAT + RESET,
        logging.CRITICAL: BOLD_RED + FORMAT + RESET
    }

    def format(self, record):
        """로그 레코드를 색상 포맷에 맞게 변환합니다."""
        log_fmt = self.FORMATS.get(record.levelno, self.FORMAT)
        formatter = logging.Formatter(log_fmt, datefmt='%Y-%m-%d %H:%M:%S')
        formatter.converter = time_converter
        return formatter.format(record)

class JsonFormatter(logging.Formatter):
    """
    로그 레코드를 구조화된 JSON 형식으로 변환하는 포맷터입니다.
    파일로 저장하여 로그를 분석하거나 다른 시스템과 연동할 때 유용합니다.
    """
    def format(self, record):
        """로그 레코드를 JSON 문자열로 변환합니다."""
        log_object = {
            "timestamp": datetime.fromtimestamp(record.created, tz=KST).isoformat(),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
            "source": f"{record.filename}:{record.lineno}",
            "function": record.funcName,
        }
        # 예외 정보가 있는 경우, 스택 트레이스를 추가합니다.
        if record.exc_info:
            log_object['exc_info'] = self.formatException(record.exc_info)

        # 로깅 호출 시 extra에 추가된 커스텀 필드를 로그 객체에 포함시킵니다.
        extra_fields = ['guild_id', 'user_id', 'channel_id', 'author_id', 'trace_id']
        for field in extra_fields:
            if hasattr(record, field):
                log_object[field] = getattr(record, field)

        return json.dumps(log_object, ensure_ascii=False)

class DiscordLogHandler(logging.Handler):
    """
    로그 레코드를 Discord 채널로 비동기 전송하는 핸들러입니다.
    _discord_log_queue를 통해 로그 메시지를 받아 백그라운드 태스크로 처리합니다.
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
        """로그 레코드를 받아 큐에 안전하게 넣습니다."""
        if not _bot_instance or _bot_instance.is_closed():
            return

        try:
            _discord_log_queue.put_nowait(record)
        except asyncio.QueueFull:
            # 큐가 가득 차면 해당 로그는 버려집니다. (블로킹 방지)
            pass

    def format_embed(self, record: logging.LogRecord) -> discord.Embed:
        """로그 레코드를 Discord에 보내기 좋은 Embed 객체로 포맷팅합니다."""
        embed_color = self.level_colors.get(record.levelno, discord.Color.default())
        title = f"📄 {record.levelname.upper()} Log"
        if record.levelno >= logging.ERROR:
            title = f"🚨 {record.levelname.upper()} Log"

        embed = discord.Embed(
            title=title,
            description=f"**Logger:** `{record.name}`",
            color=embed_color,
            timestamp=datetime.fromtimestamp(record.created, KST)
        )

        message_content = record.getMessage()
        if record.exc_info:
            exc_text = "".join(traceback.format_exception(*record.exc_info))
            exc_text = exc_text[:800]
            message_content += f"\n\n**Traceback:**\n```python\n{exc_text}\n```"

        # 임베드 필드 길이 제한 (1024자) 준수
        if len(message_content) > 1000:
            message_content = message_content[:1000] + "..."

        embed.add_field(name="Message", value=f"```\n{message_content}\n```", inline=False)
        embed.set_footer(text=f"{record.filename}:{record.lineno}")
        return embed

async def discord_logging_task():
    """_discord_log_queue에서 로그를 지속적으로 꺼내 Discord 'logs' 채널로 전송하는 백그라운드 작업입니다."""
    global _bot_instance
    await _bot_instance.wait_until_ready()
    logger.info("Discord 로깅 태스크를 시작합니다.")

    log_channel_cache = {}

    while not _bot_instance.is_closed():
        try:
            record = await _discord_log_queue.get()
            
            # 루트 로거에서 DiscordLogHandler 인스턴스를 찾습니다.
            handler = next((h for h in logging.getLogger().handlers if isinstance(h, DiscordLogHandler)), None)
            if not handler:
                continue

            embed = handler.format_embed(record)
            guild_id = getattr(record, 'guild_id', None)
            if not guild_id:
                continue

            guild = _bot_instance.get_guild(guild_id)
            if not guild:
                continue

            # 채널 캐시를 확인하고, 없으면 'logs' 채널을 찾아 캐시에 추가합니다.
            log_channel = log_channel_cache.get(guild.id)
            if not log_channel:
                channel = discord.utils.get(guild.text_channels, name='logs')
                if channel and channel.permissions_for(guild.me).send_messages:
                    log_channel_cache[guild.id] = channel
                    log_channel = channel

            if log_channel:
                try:
                    await log_channel.send(embed=embed)
                except discord.Forbidden:
                    # 권한 문제 발생 시 캐시에서 제거하여 다음 시도에 다시 찾도록 함
                    log_channel_cache.pop(guild.id, None)
                except Exception as e:
                    # 로깅 실패가 다른 로깅을 유발하지 않도록 exc_info=False 설정
                    logging.getLogger(__name__).error(f"Discord 로그 채널({log_channel.name}) 전송 중 오류: {e}", exc_info=False)

            _discord_log_queue.task_done()
        except asyncio.CancelledError:
            logger.info("Discord 로깅 태스크가 취소되었습니다.")
            break
        except Exception as e:
            # 로깅 태스크 자체의 심각한 오류는 콘솔에 직접 출력
            print(f"[CRITICAL] Discord 로깅 태스크에서 심각한 오류 발생: {e}", file=sys.stderr)
            traceback.print_exc()
            await asyncio.sleep(5) # 오류 발생 후 잠시 대기

def setup_logger() -> logging.Logger:
    """
    루트 로거를 설정하고 핸들러들을 연결합니다.
    
    - 핸들러 종류:
        1. 콘솔 핸들러 (색상 포맷)
        2. 일반 로그 파일 핸들러 (JSON 포맷)
        3. 오류 로그 파일 핸들러 (JSON 포맷, ERROR 레벨 이상)
    - 서드파티 라이브러리 로그 레벨을 WARNING으로 조정하여 노이즈를 줄입니다.
    """
    logger = logging.getLogger()
    logger.setLevel(logging.INFO) # 기본 로그 레벨을 INFO로 설정

    # 기존 핸들러가 있다면 모두 제거하여 중복 로깅 방지
    if logger.hasHandlers():
        logger.handlers.clear()

    # 1. 콘솔 핸들러 (가독성을 위한 색상 포맷)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(ColoredFormatter())
    logger.addHandler(console_handler)

    # 2. 일반 로그 파일 핸들러 (분석을 위한 JSON 포맷)
    try:
        file_handler = logging.FileHandler(config.LOG_FILE_NAME, encoding='utf-8', mode='a')
        file_handler.setFormatter(JsonFormatter())
        logger.addHandler(file_handler)
    except Exception as e:
        print(f"**[심각] 일반 로그 파일 핸들러 설정 오류:** {e}", file=sys.stderr)

    # 3. 오류 로그 파일 핸들러 (오류만 별도 저장)
    try:
        error_handler = logging.FileHandler(config.ERROR_LOG_FILE_NAME, encoding='utf-8', mode='a')
        error_handler.setFormatter(JsonFormatter())
        error_handler.setLevel(logging.ERROR)
        logger.addHandler(error_handler)
    except Exception as e:
        print(f"**[심각] 오류 로그 파일 핸들러 설정 오류:** {e}", file=sys.stderr)

    # 서드파티 라이브러리의 로그 레벨을 조정하여 불필요한 로그 줄이기
    logging.getLogger('discord').setLevel(logging.WARNING)
    logging.getLogger('websockets').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)

    return logger

def register_discord_logging(bot: commands.Bot):
    """
    Discord 로깅을 활성화합니다.
    
    루트 로거에 DiscordLogHandler를 추가하고,
    로그 메시지를 Discord로 전송하는 백그라운드 태스크를 시작합니다.
    """
    global _bot_instance
    _bot_instance = bot

    discord_handler = DiscordLogHandler()
    discord_handler.setLevel(logging.WARNING) # WARNING 레벨 이상의 로그만 Discord로 전송
    logging.getLogger().addHandler(discord_handler)
    
    asyncio.create_task(discord_logging_task())
    logging.info("Discord 로깅 핸들러가 등록되었으며, 전송 태스크가 시작될 예정입니다.")

# --- 로거 인스턴스 생성 ---
# 이 모듈을 임포트하는 모든 파일에서 `from logger_config import logger`로 사용 가능
logger = setup_logger()

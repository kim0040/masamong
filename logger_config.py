# -*- coding: utf-8 -*-
import logging
import sys
from datetime import datetime
import pytz
import asyncio
import discord
import config

# KST 시간대 객체 생성
KST = pytz.timezone('Asia/Seoul')

# 로깅 시간 변환 함수 (KST 기준)
def time_converter(*args):
  return datetime.now(KST).timetuple()

# [신규 기능] 디스코드 채널로 로그를 보내는 커스텀 핸들러
class DiscordLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.bot = None
        self.channel_id = 0
        self.log_queue = asyncio.Queue()
        self.task = None

    def set_bot(self, bot: discord.Client, channel_id: int):
        """봇 인스턴스와 채널 ID를 설정하고, 로그 전송 태스크를 시작합니다."""
        self.bot = bot
        self.channel_id = channel_id
        if self.channel_id != 0 and self.task is None:
            print(f"[정보] DiscordLogHandler가 채널 ID {self.channel_id}에 대해 활성화됩니다.")
            self.task = self.bot.loop.create_task(self._send_logs())

    async def _send_logs(self):
        """큐에 쌓인 로그를 비동기적으로 디스코드 채널에 전송합니다."""
        await self.bot.wait_until_ready()
        channel = self.bot.get_channel(self.channel_id)
        if not channel:
            print(f"[심각] DiscordLogHandler: 로그 채널(ID: {self.channel_id})을 찾을 수 없습니다. 채널 전송이 비활성화됩니다.", file=sys.stderr)
            return

        print(f"[정보] DiscordLogHandler: '{channel.name}' 채널로 로그 전송을 시작합니다.")

        while not self.bot.is_closed():
            try:
                record = await self.log_queue.get()
                log_entry = self.format(record)
                
                # 로그 레벨에 따라 색상을 다르게 하여 가독성 향상
                color = discord.Color.dark_grey()
                if record.levelno == logging.INFO:
                    color = discord.Color.blue()
                elif record.levelno == logging.WARNING:
                    color = discord.Color.gold()
                elif record.levelno >= logging.ERROR:
                    color = discord.Color.red()

                # 가독성을 높인 새로운 Embed 포맷
                embed = discord.Embed(
                    description=f"```\n{record.getMessage()}\n```",
                    color=color,
                    timestamp=datetime.fromtimestamp(record.created, tz=KST)
                )
                embed.set_author(name=f"[{record.levelname}] in [{record.name}]")

                # 에러 로그의 경우 Traceback 정보 추가
                if record.exc_info:
                    exc_text = self.formatter.formatException(record.exc_info)
                    embed.add_field(name="Traceback", value=f"```python\n{exc_text[:1000]}\n```", inline=False)

                await channel.send(embed=embed)
            except discord.errors.Forbidden:
                print(f"[심각] DiscordLogHandler: 채널(ID: {self.channel_id})에 메시지를 보낼 권한이 없습니다.", file=sys.stderr)
            except Exception as e:
                # 디스코드 전송 실패 시, 콘솔에 오류 출력 (무한 루프 방지)
                print(f"[심각] DiscordLogHandler: 로그 전송 중 예기치 않은 오류 발생: {e}", file=sys.stderr)

    def emit(self, record):
        """로그 레코드를 큐에 추가합니다."""
        if self.bot and self.channel_id != 0:
            self.log_queue.put_nowait(record)

# 이 핸들러 인스턴스를 외부(events.py)에서 사용할 수 있도록 전역 변수로 생성
discord_log_handler = DiscordLogHandler()

def setup_logger():
    """로거 객체를 설정하고 반환합니다."""
    logging.Formatter.converter = time_converter
    log_formatter = logging.Formatter('%(asctime)s [%(levelname)s] [%(name)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    discord_formatter = logging.Formatter('[%(levelname)s] [%(name)s] %(message)s')

    # 기본 로거 설정
    logger = logging.getLogger() # 루트 로거를 가져와서 모든 라이브러리의 로그를 제어
    logger.setLevel(logging.DEBUG) # 모든 로그를 일단 받도록 설정

    # 핸들러 중복 추가 방지
    if logger.hasHandlers():
        logger.handlers.clear()

    # 1. 콘솔 핸들러 (모든 DEBUG 레벨 이상 로그 출력)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(log_formatter)
    console_handler.setLevel(logging.DEBUG)
    logger.addHandler(console_handler)

    # 2. 일반 파일 핸들러 (모든 DEBUG 레벨 이상 로그를 discord_logs.txt에 저장)
    try:
        file_handler = logging.FileHandler(config.LOG_FILE_NAME, encoding='utf-8', mode='a')
        file_handler.setFormatter(log_formatter)
        file_handler.setLevel(logging.DEBUG)
        logger.addHandler(file_handler)
    except Exception as e:
        print(f"**[심각] 일반 로그 파일 핸들러 설정 오류:** {e}", file=sys.stderr)

    # 3. [신규 기능] 오류 파일 핸들러 (ERROR 레벨 이상 로그만 error_logs.txt에 저장)
    try:
        error_handler = logging.FileHandler(config.ERROR_LOG_FILE_NAME, encoding='utf-8', mode='a')
        error_handler.setFormatter(log_formatter)
        error_handler.setLevel(logging.ERROR)
        logger.addHandler(error_handler)
    except Exception as e:
        print(f"**[심각] 오류 로그 파일 핸들러 설정 오류:** {e}", file=sys.stderr)

    # 4. [신규 기능] 디스코드 채널 핸들러
    # discord_log_handler는 이미 생성되었으므로, 포매터와 레벨만 설정하여 추가
    discord_log_handler.setFormatter(discord_formatter)
    try:
        log_level = getattr(logging, config.DISCORD_LOG_LEVEL.upper())
    except AttributeError:
        log_level = logging.INFO
    discord_log_handler.setLevel(log_level)
    logger.addHandler(discord_log_handler)
    
    # discord.py 라이브러리 자체의 로그가 너무 많이 뜨는 것을 방지
    logging.getLogger('discord').setLevel(logging.INFO)
    logging.getLogger('websockets').setLevel(logging.INFO)

    return logger

# 로거 인스턴스 생성
logger = setup_logger()

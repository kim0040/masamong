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

    # discord.py 라이브러리 자체의 로그가 너무 많이 뜨는 것을 방지
    logging.getLogger('discord').setLevel(logging.INFO)
    logging.getLogger('websockets').setLevel(logging.INFO)

    return logger

# 로거 인스턴스 생성
logger = setup_logger()

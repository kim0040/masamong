# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
import os
from datetime import datetime
import pytz

# 설정, 로거, 유틸리티 가져오기
import config
from logger_config import logger
import utils

def get_current_time() -> str:
    """
    현재 시간을 '년-월-일 시:분:초' 형식의 문자열로 반환합니다.
    항상 대한민국 표준시(KST, UTC+9)를 기준으로 시간을 반환합니다.
    """
    now_kst = datetime.now(pytz.timezone('Asia/Seoul'))
    return now_kst.strftime("%Y년 %m월 %d일 %H시 %M분 %S초")

class UserCommands(commands.Cog):
    """사용자가 호출할 수 있는 명령어를 포함하는 Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name='delete_log', aliases=['로그삭제'])
    @commands.has_permissions(administrator=True) # 관리자 권한 확인
    @commands.guild_only() # 서버에서만 사용 가능
    async def delete_log(self, ctx: commands.Context):
        """로그 파일(`discord_logs.txt`)을 삭제합니다. (관리자 전용)"""
        log_filename = config.LOG_FILE_NAME
        try:
            if os.path.exists(log_filename):
                os.remove(log_filename)
                await ctx.send(config.MSG_DELETE_LOG_SUCCESS.format(filename=log_filename))
                logger.info(f"[{ctx.guild.name}/{ctx.channel.name}] 로그 파일 삭제됨 | 요청자:{ctx.author}")
                # 주기적 로그 출력 태스크의 위치 초기화 (필요 시)
                if hasattr(utils.print_log_periodically_task, 'last_pos'):
                    utils.print_log_periodically_task.last_pos = 0
            else:
                await ctx.send(config.MSG_DELETE_LOG_NOT_FOUND.format(filename=log_filename))
                logger.warning(f"[{ctx.guild.name}/{ctx.channel.name}] 로그 파일 삭제 시도 - 파일 없음 | 요청자:{ctx.author}")
        except Exception as e:
            await ctx.send(config.MSG_DELETE_LOG_ERROR)
            logger.error(f"[{ctx.guild.name}/{ctx.channel.name}] 로그 파일 삭제 중 오류 발생 | 요청자:{ctx.author} | 오류: {e}", exc_info=True)

    # delete_log 명령어의 에러 핸들러
    @delete_log.error
    async def delete_log_error(self, ctx: commands.Context, error):
        """delete_log 명령어 처리 중 발생하는 오류를 핸들링합니다."""
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(config.MSG_CMD_NO_PERM)
            logger.warning(f"[{ctx.guild.name}/{ctx.channel.name}] 권한 없는 로그 삭제 시도 | 요청자:{ctx.author}")
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send(config.MSG_CMD_GUILD_ONLY)
        elif isinstance(error, commands.CheckFailure): # has_permissions 외 다른 Check 실패 시
             await ctx.send(config.MSG_CMD_NO_PERM) # 일단 동일 메시지 사용
             logger.warning(f"[{ctx.guild.name}/{ctx.channel.name}] 로그 삭제 권한 확인 실패 (CheckFailure) | 요청자:{ctx.author}")
        else:
            logger.error(f"[{ctx.guild.name}/{ctx.channel.name}] delete_log 명령어 처리 중 예기치 않은 오류 발생: {error}", exc_info=True)
            await ctx.send(config.MSG_CMD_ERROR)

# Cog를 로드하기 위한 setup 함수 (main.py에서 호출)
async def setup(bot: commands.Bot):
    await bot.add_cog(UserCommands(bot))
    logger.info("UserCommands Cog 로드 완료.")
# -*- coding: utf-8 -*-
"""
사용자가 직접 호출할 수 있는 일반 명령어들을 관리하는 Cog입니다.
주로 관리 및 정보 조회용 명령어가 포함됩니다.
"""

import discord
from discord.ext import commands
import os
import io

import config
from logger_config import logger

class UserCommands(commands.Cog):
    """사용자 명령어들을 그룹화하는 클래스입니다."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        logger.info("UserCommands Cog가 성공적으로 초기화되었습니다.")

    @commands.command(name='delete_log', aliases=['로그삭제'])
    @commands.has_permissions(administrator=True) # 관리자 권한이 있는 사용자만 실행 가능
    @commands.guild_only() # 서버 채널에서만 사용 가능
    async def delete_log(self, ctx: commands.Context):
        """
        봇의 로그 파일을 삭제합니다. (관리자 전용)
        `config.LOG_FILE_NAME`에 정의된 로그 파일을 대상으로 합니다.
        """
        log_filename = config.LOG_FILE_NAME
        log_extra = {'guild_id': ctx.guild.id, 'author_id': ctx.author.id}
        try:
            if os.path.exists(log_filename):
                os.remove(log_filename)
                await ctx.send(config.MSG_DELETE_LOG_SUCCESS.format(filename=log_filename))
                logger.info(f"로그 파일 '{log_filename}'이(가) 삭제되었습니다.", extra=log_extra)
            else:
                await ctx.send(config.MSG_DELETE_LOG_NOT_FOUND.format(filename=log_filename))
                logger.warning(f"삭제할 로그 파일 '{log_filename}'을(를) 찾을 수 없습니다.", extra=log_extra)
        except Exception as e:
            await ctx.send(config.MSG_DELETE_LOG_ERROR)
            logger.error(f"로그 파일 삭제 중 오류 발생: {e}", exc_info=True, extra=log_extra)

    @delete_log.error
    async def delete_log_error(self, ctx: commands.Context, error):
        """`delete_log` 명령어에서 발생하는 특정 오류를 처리합니다."""
        log_extra = {'guild_id': ctx.guild.id, 'author_id': ctx.author.id}
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(config.MSG_CMD_NO_PERM)
            logger.warning(f"사용자가 권한 없이 `delete_log` 명령어를 시도했습니다.", extra=log_extra)
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send(config.MSG_CMD_GUILD_ONLY)
        else:
            logger.error(f"`delete_log` 명령어 처리 중 예기치 않은 오류 발생: {error}", exc_info=True, extra=log_extra)
            await ctx.send(config.MSG_CMD_ERROR)

    @commands.command(name='도움', aliases=['헬프', '명령어'])
    async def help_command(self, ctx: commands.Context):
        """사용 가능한 명령어 목록을 표시합니다."""
        embed = discord.Embed(
            title="📚 마사몽 명령어 도움말",
            description="사용 가능한 명령어 목록이에요!",
            color=discord.Color.blue()
        )
        
        # 이미지 생성
        embed.add_field(
            name="🎨 이미지 생성",
            value=(
                "`!이미지 <설명>` - AI가 이미지를 생성해줘요\n"
                "예시: `!이미지 귀여운 고양이`\n"
                f"⏱️ 제한: {config.IMAGE_USER_LIMIT}장/{config.IMAGE_USER_RESET_HOURS}시간"
            ),
            inline=False
        )
        
        # AI 대화
        embed.add_field(
            name="💬 AI 대화",
            value=(
                "`@마사몽 <질문>` - 마사몽에게 물어봐요\n"
                "예시: `@마사몽 오늘 날씨 어때?`"
            ),
            inline=False
        )
        
        # 관리자 명령어
        embed.add_field(
            name="⚙️ 관리자 전용",
            value="`!로그삭제` - 로그 파일 삭제 (관리자)",
            inline=False
        )
        
        embed.set_footer(text="💡 이미지 생성은 선정적인 내용이 필터링됩니다.")
        
        await ctx.send(embed=embed)
    
    @commands.command(name='이미지', aliases=['image', 'img', '그림', '생성'])
    @commands.guild_only()
    async def generate_image_command(self, ctx: commands.Context, *, prompt: str = None):
        """AI를 사용하여 이미지를 생성합니다.
        
        사용법: !이미지 <원하는 이미지 설명>
        예시: !이미지 귀여운 고양이
        """
        log_extra = {'guild_id': ctx.guild.id, 'author_id': ctx.author.id}
        
        if not prompt:
            await ctx.send("❌ 어떤 이미지를 생성할지 알려줘! 예: `!이미지 귀여운 고양이`")
            return
        
        # 이미지 생성 기능 활성화 확인
        if not getattr(config, 'COMETAPI_IMAGE_ENABLED', False):
            await ctx.send("❌ 이미지 생성 기능이 현재 비활성화되어 있어요.")
            return
        
        # AI 핸들러 가져오기
        ai_handler = self.bot.get_cog('AIHandler')
        if not ai_handler or not ai_handler.tools_cog:
            await ctx.send("❌ AI 핸들러가 준비되지 않았어요. 잠시 후 다시 시도해줘!")
            return
        
        async with ctx.typing():
            try:
                # 생성 중 메시지 전송 (LLM 호출 없음)
                status_msg = await ctx.send("🎨 이미지 생성 중이에요... 잠시만 기다려줘!")
                
                # 1. 프롬프트 생성 (LLM 1회 호출)
                image_prompt = await ai_handler._generate_image_prompt(
                    prompt, 
                    log_extra,
                    rag_context=None  # 명령어에서는 RAG 컨텍스트 없음
                )
                
                if not image_prompt:
                    await status_msg.edit(content="❌ 이미지 프롬프트 생성에 실패했어요. 다시 시도해줘!")
                    return
                
                # 2. 이미지 생성 (tools_cog 직접 호출)
                result = await ai_handler.tools_cog.generate_image(
                    prompt=image_prompt,
                    user_id=ctx.author.id
                )
                
                # 3. 결과 처리
                if result.get('image_data') or result.get('image_url'):
                    remaining = result.get('remaining', 0)
                    
                    # 상태 메시지 삭제
                    try:
                        await status_msg.delete()
                    except:
                        pass
                    
                    # 이미지 바이너리가 있으면 파일로 직접 업로드 (URL 만료 방지)
                    if result.get('image_data'):
                        image_file = discord.File(
                            io.BytesIO(result['image_data']),
                            filename="generated_image.jpg"
                        )
                        await ctx.reply(
                            f"짜잔~ 이미지 생성했어! 🎨\n(남은 이미지 생성 횟수: {remaining}장)",
                            file=image_file,
                            mention_author=False
                        )
                    else:
                        # 폴백: URL로 전송
                        await ctx.reply(
                            f"짜잔~ 이미지 생성했어! 🎨\n{result['image_url']}\n\n(남은 이미지 생성 횟수: {remaining}장)",
                            mention_author=False
                        )
                    
                    logger.info(f"이미지 생성 성공 (명령어): user={ctx.author.id}", extra=log_extra)
                    
                elif result.get('error'):
                    await status_msg.edit(content=f"😅 {result['error']}")
                else:
                    await status_msg.edit(content="❌ 이미지 생성 중 알 수 없는 오류가 발생했어요.")
                    
            except Exception as e:
                logger.error(f"이미지 생성 명령어 오류: {e}", exc_info=True, extra=log_extra)
                try:
                    await status_msg.edit(content="❌ 이미지 생성 중 오류가 발생했어요. 잠시 후 다시 시도해줘!")
                except:
                    await ctx.send("❌ 이미지 생성 중 오류가 발생했어요. 잠시 후 다시 시도해줘!")
    
    @generate_image_command.error
    async def generate_image_error(self, ctx: commands.Context, error):
        """`이미지` 명령어의 오류를 처리합니다."""
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.send("❌ 이 명령어는 서버에서만 사용할 수 있어요!")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("❌ 어떤 이미지를 생성할지 알려줘! 예: `!이미지 귀여운 고양이`")

async def setup(bot: commands.Bot):
    """Cog를 봇에 등록하는 함수입니다."""
    await bot.add_cog(UserCommands(bot))

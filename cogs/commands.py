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
        봇의 로그 파일을 삭제합니다. (관리자 전용, 서버 전용)

        사용법:
        - `!delete_log`

        예시:
        - `!delete_log`

        참고:
        - `config.LOG_FILE_NAME`에 정의된 파일을 삭제합니다.
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


    
    @commands.command(name='이미지', aliases=['image', 'img', '그림', '생성'])
    @commands.guild_only()
    async def generate_image_command(self, ctx: commands.Context, *, prompt: str = None):
        """
        AI로 이미지를 생성합니다. (서버 전용)

        사용법:
        - `!이미지 <설명>`

        예시:
        - `!이미지 파란 하늘을 나는 귀여운 아기 고양이`
        - `!이미지 사이버펑크 스타일의 서울 야경`

        참고:
        - 이미지 생성은 `COMETAPI_KEY`가 필요합니다.
        - 유저/전역 생성 횟수 제한이 있습니다.
        """
        log_extra = {'guild_id': ctx.guild.id, 'author_id': ctx.author.id}
        
        if not prompt:
            await ctx.send("❌ 그림에 대한 설명이 빠졌어요!\n**올바른 사용법**: `!이미지 <설명>`\n(예: `!이미지 우주복을 입은 햄스터`)")
            return
        
        # 이미지 생성 기능 활성화 확인
        if not getattr(config, 'COMETAPI_IMAGE_ENABLED', False):
            await ctx.send("❌ 이미지 생성 기능이 현재 관리자에 의해 비활성화되어 있어요.")
            return
        
        # AI 핸들러 가져오기
        ai_handler = self.bot.get_cog('AIHandler')
        if not ai_handler or not ai_handler.tools_cog:
            await ctx.send("❌ AI 시스템이 아직 준비되지 않았어요. 잠시 후 다시 시도해주세요!")
            return
        
        async with ctx.typing():
            try:
                # 생성 중 메시지 전송 (LLM 호출 없음)
                status_msg = await ctx.send(f"🎨 **'{prompt}'**\n위 설명으로 그림을 그리고 있어요... (약 10~20초 소요)")
                
                # 1. 프롬프트 세팅 (Seedream 5.0은 자체 추론이 뛰어나 번역/최적화 과정 생략)
                image_prompt = prompt
                
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
                            f"짜잔~ 요청하신 이미지가 완성되었어요! 🎨\n(남은 이미지 생성 횟수: {remaining}장)",
                            file=image_file,
                            mention_author=False
                        )
                    else:
                        # 폴백: URL로 전송
                        await ctx.reply(
                            f"짜잔~ 요청하신 이미지가 완성되었어요! 🎨\n{result['image_url']}\n\n(남은 이미지 생성 횟수: {remaining}장)",
                            mention_author=False
                        )
                    
                    logger.info(f"이미지 생성 성공 (명령어): user={ctx.author.id}", extra=log_extra)
                    
                elif result.get('error'):
                    await status_msg.edit(content=f"😅 이미지 생성 실패: {result['error']}")
                else:
                    await status_msg.edit(content="❌ 이미지 생성 중 알 수 없는 오류가 발생했어요.")
                    
            except Exception as e:
                logger.error(f"이미지 생성 명령어 오류: {e}", exc_info=True, extra=log_extra)
                try:
                    await status_msg.edit(content="❌ 처리 중 오류가 발생했어요. 잠시 후 다시 시도해 주세요!")
                except:
                    await ctx.send("❌ 처리 중 오류가 발생했어요. 잠시 후 다시 시도해 주세요!")
    
    @generate_image_command.error
    async def generate_image_error(self, ctx: commands.Context, error):
        """`이미지` 명령어의 오류를 처리합니다."""
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.send("❌ 이 명령어는 서버 채널에서만 사용할 수 있어요!")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("❌ 그림에 대한 설명이 빠졌어요!\n**사용법**: `!이미지 <설명>` (예: `!이미지 귀여운 고양이`)")

    @commands.command(name='업데이트', aliases=['update', '패치노트'])
    async def update_info(self, ctx: commands.Context):
        """
        최근 추가된 기능과 변경 사항을 알려줍니다.

        사용법:
        - `!업데이트`

        예시:
        - `!업데이트`
        """
        embed = discord.Embed(
            title="🚀 마사몽 업데이트 소식",
            description="최근 추가된 따끈따끈한 기능들을 소개할게요!",
            color=0xff6b6b # Rose Color
        )
        
        embed.add_field(
            name="🎨 차세대 AI 이미지 생성 탑재 (Seedream 5.0)",
            value=(
                "**최고 품질의 3K 해상도로 그림을 그려드려요!** (`!이미지`)\n"
                "- 🚀 **ByteDance의 초거대 비전 모델 Seedream 5.0** 적용\n"
                "- 🧠 **지능형 추론**: 복잡한 지시나 다중 객체를 더 똑똑하게 이해합니다.\n"
                "- ✨ **압도적 디테일**: 상업용 수준의 포토리얼리즘과 풍부한 분위기 연출!\n"
                "- 🔡 **향상된 텍스트 렌더링**: 한글/영문 등 이미지 내 글자 표현력이 향상되었습니다.\n"
                "\n*기존보다 훨씬 높은 품질의 결과물을 경험해보세요!*\n\n"
                "🎉 **업데이트!** 이미지 생성 한도가 기존 12시간 당 5장에서 **6시간 당 10장**으로 대폭 상향되었습니다!"
            ),
            inline=False
        )
        
        embed.set_footer(text="자세한 내용은 !도움 명령어를 참고해주세요.")
        await ctx.send(embed=embed)

async def setup(bot: commands.Bot):
    """Cog를 봇에 등록하는 함수입니다."""
    await bot.add_cog(UserCommands(bot))

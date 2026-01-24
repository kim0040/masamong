# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
from logger_config import logger

class MasamongHelpCommand(commands.HelpCommand):
    """
    마사몽 전용 커스텀 도움말 커맨드입니다.
    기본 텍스트 대신 Embed를 사용하여 가독성을 높이고,
    관리자 전용 명령어(hidden=True)를 일반 사용자에게서 숨깁니다.
    """
    
    def __init__(self):
        super().__init__()
        self.command_attrs["help"] = "명령어 목록과 사용법을 자세히 보여줍니다."
        self.command_attrs["aliases"] = ["도움", "도움말", "h"]

    async def send_bot_help(self, mapping):
        """!도움 입력 시 전체 명령어 목록 출력"""
        embed = discord.Embed(
            title="📖 친절한 마사몽의 사용 설명서",
            description=(
                f"안녕하세요! 여러분의 AI 친구이자 비서, **{self.context.bot.user.display_name}**입니다. 🤖\n"
                "궁금한 점이 있거나 도움이 필요하면 언제든 불러주세요.\n\n"
                "💡 **팁**: 명령어의 자세한 사용법을 보려면 `!도움 [명령어]`를 입력하세요.\n"
                "예: `!도움 운세`, `!도움 별자리`"
            ),
            color=0x66ccff # Sky Blue
        )
        embed.set_thumbnail(url=self.context.bot.user.avatar.url if self.context.bot.user.avatar else None)
        
        for cog, cmds in mapping.items():
            # Cog가 없거나(No Category), 숨겨진 명령어만 있는 경우 스킵
            filtered_cmds = [c for c in cmds if not c.hidden]
            filtered_cmds.sort(key=lambda c: c.name)
            
            if not filtered_cmds:
                continue

            cog_name = cog.qualified_name if cog else "기타 기능"
            # 카테고리 이름 직관적으로 변경
            if cog_name == "FortuneCog": cog_name = "🔮 운세 및 사주"
            elif cog_name == "UserCommands": cog_name = "🛠 일반 기능"
            elif cog_name == "ActivityCog": cog_name = "📊 활동 기록"
            elif cog_name == "FunCog": cog_name = "🎉 재미 기능"
            
            # Cog 설명의 첫 줄만 가져오기
            cog_desc = (cog.description.split('\n')[0]) if cog and cog.description else "다양한 기능들이에요!"

            cmd_list = [f"`!{c.name}`" for c in filtered_cmds]
            embed.add_field(
                name=f"**{cog_name}**",
                value=", ".join(cmd_list) + f"\n*{cog_desc}*",
                inline=False
            )

        destination = self.get_destination()
        await destination.send(embed=embed)

    async def send_command_help(self, command):
        """!도움 <명령어> 입력 시 상세 설명 출력"""
        embed = discord.Embed(
            title=f"📘 명령어 가이드: !{command.name}",
            description=command.help or "상세 설명이 준비되어 있지 않아요.",
            color=0x00ff00 # Green
        )
        
        # 별칭(Alias) 표시
        if command.aliases:
            embed.add_field(name="✨ 다른 이름 (별칭)", value=", ".join([f"`!{alias}`" for alias in command.aliases]), inline=False)
            
        # 사용법(Usage) 표시
        signature = command.signature if command.signature else ""
        usage = f"!{command.name} {signature}"
        embed.add_field(name="📝 사용법", value=f"`{usage}`", inline=False)
        
        # 예시 (자동 생성은 어렵지만 힌트 제공)
        if command.name == '운세':
            examples = "`!운세` (오늘의 운세)\n`!운세 구독 08:00` (매일 아침 8시 알림)"
        elif command.name == '별자리':
            examples = "`!별자리` (전체 목록)\n`!별자리 물병자리` (특정 별자리 운세)"
        elif command.name == '이미지':
            examples = "`!이미지 귀여운 아기 고양이`"
        else:
            examples = None
            
        if examples:
             embed.add_field(name="💡 예시", value=examples, inline=False)

        destination = self.get_destination()
        await destination.send(embed=embed)

    async def send_group_help(self, group):
        """그룹 명령어 도움말 (예: !debug)"""
        embed = discord.Embed(
            title=f"🔧 그룹 명령어: !{group.name}",
            description=group.help or "설명이 없습니다.",
            color=0xffaa00
        )
        
        # 여기서도 hidden 체크만 수행
        filtered_cmds = [c for c in group.commands if not c.hidden]
        filtered_cmds.sort(key=lambda c: c.name)

        cmd_list = [f"`!{c.qualified_name}`: {c.short_doc}" for c in filtered_cmds]
        
        embed.add_field(name="하위 명령어", value="\n".join(cmd_list) if cmd_list else "없음", inline=False)
        destination = self.get_destination()
        await destination.send(embed=embed)

    async def send_error_message(self, error):
        """없는 명렁어 검색 시 오류 메시지"""
        destination = self.get_destination()
        await destination.send(f"❌ {error}")

class HelpCog(commands.Cog):
    """도움말 기능을 담당하는 Cog입니다."""
    def __init__(self, bot):
        self.bot = bot
        self._original_help_command = bot.help_command
        bot.help_command = MasamongHelpCommand()
        bot.help_command.cog = self
        logger.info("Custom HelpCog initialized and HelpCommand replaced.")

    def cog_unload(self):
        """Cog 언로드 시 원래 도움말 커맨드로 복구"""
        self.bot.help_command = self._original_help_command

async def setup(bot):
    await bot.add_cog(HelpCog(bot))

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
            title="🤖 마사몽 명령어 가이드",
            description=f"안녕하세요! {self.context.bot.user.display_name}입니다.\n사용 가능한 명령어는 아래와 같습니다.",
            color=0x66ccff # Sky Blue
        )
        embed.set_thumbnail(url=self.context.bot.user.avatar.url if self.context.bot.user.avatar else None)
        embed.set_footer(text="!도움 <명령어>를 입력하면 상세 설명을 볼 수 있어요!")

        for cog, cmds in mapping.items():
            # Cog가 없거나(No Category), 숨겨진 명령어만 있는 경우 스킵
            # 기본 filter_commands는 실행 불가능한(예: DM전용) 명령어를 숨겨버리므로,
            # hidden 속성만 확인하여 모든 명령어를 보여주도록 변경합니다.
            filtered_cmds = [c for c in cmds if not c.hidden]
            filtered_cmds.sort(key=lambda c: c.name)
            
            if not filtered_cmds:
                continue

            cog_name = cog.qualified_name if cog else "기타 명령어"
            # Cog 설명의 첫 줄만 가져오기
            cog_desc = (cog.description.split('\n')[0]) if cog and cog.description else "일반 기능"

            cmd_list = [f"`!{c.name}`" for c in filtered_cmds]
            embed.add_field(
                name=f"📂 {cog_name} - {cog_desc}",
                value=", ".join(cmd_list),
                inline=False
            )

        destination = self.get_destination()
        await destination.send(embed=embed)

    async def send_command_help(self, command):
        """!도움 <명령어> 입력 시 상세 설명 출력"""
        embed = discord.Embed(
            title=f"📖 명령어: !{command.name}",
            description=command.help or "설명이 없습니다.",
            color=0x00ff00 # Green
        )
        
        # 별칭(Alias) 표시
        if command.aliases:
            embed.add_field(name="별칭", value=", ".join([f"!{alias}" for alias in command.aliases]), inline=False)
            
        # 사용법(Usage) 표시
        usage = f"!{command.name} {command.signature}"
        embed.add_field(name="사용법", value=f"`{usage}`", inline=False)

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

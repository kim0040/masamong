# -*- coding: utf-8 -*-
"""
도움말 명령어를 제공하는 Cog 모듈입니다.
"""
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
        """커스텀 도움말 커맨드를 초기화하고 별칭을 설정합니다."""
        super().__init__()
        self.command_attrs["help"] = "명령어 목록과 사용법을 자세히 보여줍니다."
        self.command_attrs["aliases"] = ["도움", "도움말", "h"]

    async def send_bot_help(self, mapping):
        """!도움 입력 시 전체 명령어 목록 출력"""
        prefix = self.context.clean_prefix or "!"
        embed = discord.Embed(
            title="📖 친절한 마사몽의 사용 설명서",
            description=(
                f"안녕하세요! 여러분의 AI 친구이자 비서, **{self.context.bot.user.display_name}**입니다. 🤖\n"
                f"이 인스턴스의 명령어는 **`{prefix}`로 시작**합니다.\n\n"
                "**💬 AI 대화**\n"
                "- 서버: `@마사몽 할 말` (멘션으로 호출)\n"
                "- DM: 그냥 메시지를 보내면 1:1 대화 가능\n"
                "- 자연어로 날씨, 뉴스, 이미지 생성, 운세 등 요청 가능\n\n"
                "**📋 명령어 목록**\n"
                f"- 전체 목록: `{prefix}도움`\n"
                f"- 자세한 설명: `{prefix}도움 <명령어>`\n"
                f"- 별칭도 동일하게 동작합니다. (예: `{prefix}도움 help`)\n\n"
                "**예시**\n"
                f"- `{prefix}도움 날씨`\n"
                f"- `{prefix}도움 운세`\n"
                f"- `{prefix}도움 개인정보`\n\n"
                "⚠️ 일부 명령어는 **서버 전용/DM 전용/권한 제한**이 있습니다."
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
            elif cog_name == "PrivacyCog": cog_name = "🔐 개인정보 동의"
            elif cog_name == "SchoolNoticeCog": cog_name = "🎓 학교 공지"
            
            # Cog 설명의 첫 줄만 가져오기
            cog_desc = (cog.description.split('\n')[0]) if cog and cog.description else "다양한 기능들이에요!"

            cmd_list = [f"`{prefix}{c.name}`" for c in filtered_cmds]
            embed.add_field(
                name=f"**{cog_name}**",
                value=", ".join(cmd_list) + f"\n*{cog_desc}*",
                inline=False
            )

        destination = self.get_destination()
        await destination.send(embed=embed)

    async def send_command_help(self, command):
        """개별 명령어에 대한 상세 도움말을 Embed로 출력합니다."""
        prefix = self.context.clean_prefix or "!"
        embed = discord.Embed(
            title=f"📘 명령어 가이드: {prefix}{command.name}",
            description=command.help or "상세 설명이 준비되어 있지 않아요.",
            color=0x00ff00 # Green
        )
        
        # 별칭(Alias) 표시
        if command.aliases:
            embed.add_field(name="✨ 다른 이름 (별칭)", value=", ".join([f"`!{alias}`" for alias in command.aliases]), inline=False)
            
        # 사용법(Usage) 표시
        signature = command.signature if command.signature else ""
        usage = f"{prefix}{command.name} {signature}".rstrip()
        embed.add_field(name="📝 사용법", value=f"`{usage}`", inline=False)
        
        # 예시 (자동 생성은 어렵지만 힌트 제공)
        examples = None
        if command.name == '운세':
            examples = (
                f"`{prefix}운세` (오늘 운세)\n"
                f"`{prefix}운세 상세` (DM 상세 운세)\n"
                f"`{prefix}운세 구독 08:00` (매일 아침 운세)"
            )
        elif command.name == '별자리':
            examples = (
                f"`{prefix}별자리` (내 별자리 운세)\n"
                f"`{prefix}별자리 물병자리` (특정 별자리)\n"
                f"`{prefix}별자리 순위` (12별자리 랭킹)"
            )
        elif command.name == '날씨':
            examples = (
                f"`{prefix}날씨` (기본 지역)\n"
                f"`{prefix}날씨 서울` (지역 지정)\n"
                f"`{prefix}날씨 내일 부산` (날짜+지역)\n"
                f"`{prefix}날씨 이번주 광주`"
            )
        elif command.name == '이미지':
            examples = f"`{prefix}이미지 별이 가득한 밤하늘`"
        elif command.name == '요약':
            examples = f"`{prefix}요약` (최근 대화 요약)"
        elif command.name == '랭킹':
            examples = (
                f"`{prefix}랭킹` (현재 채널 누적 랭킹)\n"
                f"`{prefix}랭킹 오늘` (오늘 기준)\n"
                f"`{prefix}랭킹 이번주` (주간 기준)\n"
                f"`{prefix}랭킹 이번달` (월간 기준)\n"
                f"`{prefix}랭킹 전체` (전체 누적)"
            )
        elif command.name == '투표':
            examples = (
                f"`{prefix}투표 \"점심 메뉴\" \"피자\" \"라멘\" \"국밥\"`\n"
                f"`{prefix}투표 \"회식할까?\"` (찬반 투표)"
            )
        elif command.name == '업데이트':
            examples = f"`{prefix}업데이트`"
        elif command.name == 'delete_log':
            examples = f"`{prefix}delete_log` (관리자 전용)"
        elif command.name == '구독':
            examples = f"`{prefix}구독 07:30` (운세 브리핑 구독)"
        elif command.name in {'이번달운세', '올해운세'}:
            examples = f"`{prefix}{command.name}`"
        elif command.name == '개인정보':
            examples = (
                f"`{prefix}개인정보` (현재 동의 상태)\n"
                f"`{prefix}개인정보 동의 운세`\n"
                f"`{prefix}개인정보 동의 학교공지`"
            )
        elif command.name == '공지':
            examples = (
                f"`{prefix}공지 등록 전북대 소프트웨어공학과 3학년, 오전 9시 알림`\n"
                f"`{prefix}공지 정보`\n"
                f"`{prefix}공지 시간 09:00`"
            )
            
        if examples:
             embed.add_field(name="💡 예시", value=examples, inline=False)

        destination = self.get_destination()
        await destination.send(embed=embed)

    async def send_group_help(self, group):
        """그룹 명령어(예: !debug)의 하위 명령어 목록을 Embed로 출력합니다."""
        prefix = self.context.clean_prefix or "!"
        embed = discord.Embed(
            title=f"🔧 그룹 명령어: {prefix}{group.name}",
            description=group.help or "설명이 없습니다.",
            color=0xffaa00
        )
        
        # 여기서도 hidden 체크만 수행
        filtered_cmds = [c for c in group.commands if not c.hidden]
        filtered_cmds.sort(key=lambda c: c.name)

        cmd_list = [
            f"`{prefix}{c.qualified_name}`: {c.short_doc}"
            for c in filtered_cmds
        ]
        
        embed.add_field(name="하위 명령어", value="\n".join(cmd_list) if cmd_list else "없음", inline=False)
        destination = self.get_destination()
        await destination.send(embed=embed)

    async def send_error_message(self, error):
        """존재하지 않는 명령어 입력 시 오류 메시지를 출력합니다."""
        destination = self.get_destination()
        await destination.send(f"❌ {error}")

class HelpCog(commands.Cog):
    """도움말 기능을 담당하는 Cog입니다."""
    def __init__(self, bot):
        """Cog를 초기화하고 봇의 help_command를 커스텀 구현으로 교체합니다."""
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

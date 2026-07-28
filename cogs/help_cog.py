# -*- coding: utf-8 -*-
"""
도움말 명령어를 제공하는 Cog 모듈입니다.
"""
import discord
from discord.ext import commands
import config
from logger_config import logger
from utils.discord_helpers import clip_discord_text

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
        first_embed = discord.Embed(
            title="📖 친절한 마사몽의 사용 설명서",
            description=(
                f"안녕하세요! 여러분의 AI 친구이자 비서, **{self.context.bot.user.display_name}**입니다. 🤖\n"
                f"이 인스턴스의 명령어는 **`{prefix}`로 시작**합니다.\n\n"
                f"처음이라면 `{prefix}메뉴`에서 버튼으로 기능을 골라보세요.\n\n"
                "**💬 AI 대화**\n"
                "- 서버: `@마사몽 할 말` (멘션으로 호출)\n"
                "- DM: 그냥 메시지를 보내면 1:1 대화 가능\n"
                "- 자연어로 날씨, 뉴스, 이미지 생성 등을 요청 가능\n"
                "- 학교 공지는 DM에서 `학교 공지 설정`이라고 말하면 시작\n\n"
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
        first_embed.set_thumbnail(
            url=(
                self.context.bot.user.avatar.url
                if self.context.bot.user.avatar
                else None
            )
        )
        embeds = [first_embed]
        embed = first_embed
        
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
            field_name = clip_discord_text(f"**{cog_name}**", 256)
            field_value = clip_discord_text(
                ", ".join(cmd_list) + f"\n*{cog_desc}*",
                1024,
            )
            if len(embed.fields) >= 25 or len(embed) + len(field_name) + len(
                field_value
            ) > 5800:
                embed = discord.Embed(
                    title="📖 사용 설명서 · 계속",
                    description=(
                        f"명령어는 `{prefix}`로 시작합니다. "
                        f"처음이라면 `{prefix}메뉴`를 이용하세요."
                    ),
                    color=0x66ccff,
                )
                embeds.append(embed)
            embed.add_field(
                name=field_name,
                value=field_value,
                inline=False
            )

        destination = self.get_destination()
        for help_embed in embeds:
            await destination.send(embed=help_embed)

    async def send_command_help(self, command):
        """개별 명령어에 대한 상세 도움말을 Embed로 출력합니다."""
        prefix = self.context.clean_prefix or "!"
        embed = discord.Embed(
            title=clip_discord_text(
                f"📘 명령어 가이드: {prefix}{command.name}",
                256,
            ),
            description=clip_discord_text(
                command.help or "상세 설명이 준비되어 있지 않아요.",
                4096,
            ),
            color=0x00ff00 # Green
        )
        
        # 별칭(Alias) 표시
        if command.aliases:
            embed.add_field(
                name="✨ 다른 이름 (별칭)",
                value=clip_discord_text(
                    ", ".join([f"`{prefix}{alias}`" for alias in command.aliases]),
                    1024,
                ),
                inline=False,
            )
            
        # 사용법(Usage) 표시
        signature = command.signature if command.signature else ""
        usage = f"{prefix}{command.name} {signature}".rstrip()
        embed.add_field(
            name="📝 사용법",
            value=clip_discord_text(f"`{usage}`", 1024),
            inline=False,
        )
        
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
             embed.add_field(
                 name="💡 예시",
                 value=clip_discord_text(examples, 1024),
                 inline=False,
             )

        destination = self.get_destination()
        await destination.send(embed=embed)

    async def send_group_help(self, group):
        """그룹 명령어(예: !debug)의 하위 명령어 목록을 Embed로 출력합니다."""
        prefix = self.context.clean_prefix or "!"
        embed = discord.Embed(
            title=clip_discord_text(
                f"🔧 그룹 명령어: {prefix}{group.name}",
                256,
            ),
            description=clip_discord_text(
                group.help or "설명이 없습니다.",
                4096,
            ),
            color=0xffaa00
        )
        
        # 여기서도 hidden 체크만 수행
        filtered_cmds = [c for c in group.commands if not c.hidden]
        filtered_cmds.sort(key=lambda c: c.name)

        cmd_list = [
            f"`{prefix}{c.qualified_name}`: {c.short_doc}"
            for c in filtered_cmds
        ]
        
        embed.add_field(
            name="하위 명령어",
            value=clip_discord_text(
                "\n".join(cmd_list) if cmd_list else "없음",
                1024,
            ),
            inline=False,
        )
        destination = self.get_destination()
        await destination.send(embed=embed)

    async def send_error_message(self, error):
        """존재하지 않는 명령어 입력 시 오류 메시지를 출력합니다."""
        destination = self.get_destination()
        await destination.send(f"❌ {error}")


class MasamongHomeView(discord.ui.View):
    """통합 메뉴를 연 사용자만 조작할 수 있는 짧은 수명의 홈 화면."""

    def __init__(self, bot: commands.Bot, ctx: commands.Context) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.ctx = ctx
        self.user_id = int(ctx.author.id)
        self.school_notice.disabled = bot.get_cog("SchoolNoticeCog") is None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) == self.user_id:
            return True
        await interaction.response.send_message(
            "이 메뉴는 연 사용자만 사용할 수 있습니다.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(
        label="학교 공지",
        style=discord.ButtonStyle.primary,
        emoji="🎓",
    )
    async def school_notice(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        if self.ctx.guild:
            await interaction.response.send_message(
                "개인화 학교 공지는 DM에서 설정합니다. 마사몽에게 DM으로 "
                "`학교 공지 설정`이라고 보내주세요.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "학교 공지 메뉴를 아래에 열었습니다.",
            ephemeral=True,
        )
        cog = self.bot.get_cog("SchoolNoticeCog")
        if cog is None:
            await interaction.followup.send(
                "이 인스턴스에서는 학교 공지를 사용할 수 없습니다.",
                ephemeral=True,
            )
            return
        await cog.send_dashboard(self.ctx)

    @discord.ui.button(
        label="오늘 운세",
        style=discord.ButtonStyle.primary,
        emoji="🔮",
    )
    async def fortune(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        if self.ctx.guild:
            await interaction.response.send_message(
                "개인 운세는 DM에서 이용합니다. 마사몽에게 DM으로 `!메뉴`를 "
                "보내고 **오늘 운세**를 눌러주세요.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "오늘 운세를 아래에서 이어서 확인합니다.",
            ephemeral=True,
        )
        cog = self.bot.get_cog("FortuneCog")
        if cog is None:
            await interaction.followup.send(
                "이 인스턴스에서는 운세를 사용할 수 없습니다.",
                ephemeral=True,
            )
            return
        await type(cog).fortune.callback(cog, self.ctx, option=None)

    @discord.ui.button(
        label="개인정보",
        style=discord.ButtonStyle.secondary,
        emoji="🔐",
    )
    async def privacy(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        cog = self.bot.get_cog("PrivacyCog")
        if cog is None:
            await interaction.response.send_message(
                "개인정보 상태 기능을 불러오지 못했습니다.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            await cog.status_text(self.user_id),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(
        label="전체 도움말",
        style=discord.ButtonStyle.secondary,
        emoji="📖",
    )
    async def help(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        prefix = self.ctx.clean_prefix or config.COMMAND_PREFIX or "!"
        await interaction.response.send_message(
            f"`{prefix}도움`은 전체 기능, `{prefix}도움 <기능>`은 상세 사용법을 보여줍니다.\n"
            f"예: `{prefix}도움 공지`, `{prefix}도움 운세`",
            ephemeral=True,
        )


class HelpCog(commands.Cog):
    """도움말 기능을 담당하는 Cog입니다."""
    def __init__(self, bot):
        """Cog를 초기화하고 봇의 help_command를 커스텀 구현으로 교체합니다."""
        self.bot = bot
        self._original_help_command = bot.help_command
        bot.help_command = MasamongHelpCommand()
        bot.help_command.cog = self
        logger.info("Custom HelpCog initialized and HelpCommand replaced.")

    @commands.command(name="메뉴", aliases=["시작", "기능", "홈"])
    async def menu(self, ctx: commands.Context) -> None:
        """자주 쓰는 기능을 버튼으로 시작하는 통합 메뉴를 엽니다."""
        prefix = ctx.clean_prefix or config.COMMAND_PREFIX or "!"
        embed = discord.Embed(
            title="🤖 마사몽 메뉴",
            description=(
                "원하는 기능을 버튼으로 선택하세요. 개인정보가 필요한 기능은 "
                "동의 화면이 이어서 열리고, 동의 후 원래 요청도 자동으로 계속됩니다."
            ),
            color=0x66CCFF,
        )
        embed.add_field(
            name="💬 대화",
            value=(
                "DM에서는 그냥 말하고, 서버에서는 마사몽을 멘션하세요. "
                "날씨·뉴스·검색·이미지도 자연어로 요청할 수 있습니다."
            ),
            inline=False,
        )
        embed.add_field(
            name="🎓 학교 공지",
            value="등록 학교만 23시에 수집해 관련 공지가 있을 때 선택 시각에 알립니다.",
            inline=False,
        )
        embed.add_field(
            name="🔮 운세",
            value="프로필 등록·오늘 운세·알림 설정을 개인정보 동의와 함께 진행합니다.",
            inline=False,
        )
        embed.add_field(
            name="🧰 서버 편의 기능",
            value=(
                f"`{prefix}날씨`, `{prefix}랭킹`, `{prefix}투표`, "
                f"`{prefix}요약`, `{prefix}이미지` · 자세한 예시는 "
                f"`{prefix}도움 <기능>`"
            ),
            inline=False,
        )
        embed.set_footer(text=f"전체 명령과 상세 설명: {prefix}도움")
        await ctx.send(
            embed=embed,
            view=MasamongHomeView(self.bot, ctx),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def cog_unload(self):
        """Cog 언로드 시 원래 도움말 커맨드로 복구"""
        self.bot.help_command = self._original_help_command

async def setup(bot):
    await bot.add_cog(HelpCog(bot))

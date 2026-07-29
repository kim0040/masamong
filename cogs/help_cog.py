# -*- coding: utf-8 -*-
"""
도움말 명령어를 제공하는 Cog 모듈입니다.
"""
import discord
from discord.ext import commands
import config
from logger_config import logger
from utils.admin_policy import is_superadmin
from utils.discord_interactions import ReliableModal, ReliableView
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
            title="📖 마사몽 사용 안내",
            description=(
                f"처음이라면 `{prefix}메뉴`에서 원하는 기능을 골라보세요.\n"
                "- 서버 대화: 마사몽을 멘션하고 질문\n"
                "- DM 대화: 멘션 없이 바로 질문\n"
                f"- 전체 명령어: `{prefix}도움`\n"
                f"- 자세한 사용법: `{prefix}도움 <명령어>`\n"
                "- 개인 구독과 개인정보 설정은 DM에서만 열립니다."
            ),
            color=0x66CCFF,
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
            elif cog_name == "TransferNoticeCog": cog_name = "📚 편입 공지"
            
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
                f"`{prefix}개인정보 동의 학교공지`\n"
                f"`{prefix}개인정보 동의 편입공지`"
            )
        elif command.name == '공지':
            examples = (
                f"`{prefix}공지 등록 전북대 소프트웨어공학과 3학년, 오전 9시 알림`\n"
                f"`{prefix}공지 상태`\n"
                f"`{prefix}공지 정보`\n"
                f"`{prefix}공지 시간 09:00`"
            )
        elif command.name == '편입':
            examples = (
                f"`{prefix}편입` (대학 선택 메뉴)\n"
                f"`{prefix}편입 최근`\n"
                f"`{prefix}편입 상태`\n"
                f"`{prefix}편입 구독취소` / `{prefix}편입 재개`"
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
                group.help or "설명이 없어요.",
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


def _build_home_embed(
    bot: commands.Bot,
    ctx: commands.Context,
    *,
    server_private: bool,
) -> discord.Embed:
    """DM/서버 맥락에 맞는 통합 메뉴 본문을 구성합니다."""
    prefix = ctx.clean_prefix or config.COMMAND_PREFIX or "!"
    if server_private:
        description = (
            "이 메뉴는 나에게만 보여요. 범주를 고른 뒤 기능을 실행하면 "
            "날씨·이미지·요약·랭킹·투표 같은 결과는 채널에 함께 표시됩니다. "
            "개인 설정과 관리 결과는 계속 비공개로 처리합니다."
        )
    else:
        description = (
            "하고 싶은 일을 범주에서 골라주세요. 개인정보가 필요한 기능은 "
            "먼저 동의를 확인한 뒤 원래 요청을 이어갑니다."
        )
    embed = discord.Embed(
        title="🤖 마사몽 메뉴",
        description=description,
        color=0x66CCFF,
    )
    embed.add_field(
        name="무엇을 도와드릴까요?",
        value=(
            "🎓 학교·편입  ·  💬 AI·검색  ·  🌦️ 날씨·재난\n"
            "🔮 운세  ·  🗳️ 커뮤니티  ·  🔐 개인 설정\n"
            + (
                "⚙️ 간편 설정  ·  "
                if is_superadmin(ctx.author.id)
                else ""
            )
            + "📖 전체 도움말"
        ),
        inline=False,
    )
    embed.add_field(
        name="이용 방법",
        value=(
            "아래에서 범주를 고르고 원하는 버튼을 누르면 됩니다.\n"
            f"명령어 목록은 `{prefix}도움`에서 볼 수 있어요."
        ),
        inline=False,
    )
    embed.set_footer(
        text=(
            "서버 메뉴의 상세 화면은 호출자에게만 표시돼요."
            if server_private
            else f"전체 명령과 상세 설명: {prefix}도움"
        )
    )
    return embed


class MasamongHomeView(ReliableView):
    """통합 메뉴를 연 사용자만 조작할 수 있는 짧은 수명의 홈 화면."""

    def __init__(self, bot: commands.Bot, ctx: commands.Context) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.ctx = ctx
        self.user_id = int(ctx.author.id)
        self.server_mode = bool(ctx.guild)
        # 데코레이터로 남아 있는 구버전 평면 버튼은 노출하지 않는다. 홈은
        # 범주 선택 하나만 제공하고, 선택 뒤 CategoryView에서 관련 기능만 연다.
        self.clear_items()
        self.add_item(HomeCategorySelect(self))

    @discord.ui.select(
        placeholder="기능별 빠른 사용법 보기",
        min_values=1,
        max_values=1,
        row=1,
        options=[
            discord.SelectOption(
                label="AI 대화·검색",
                value="ai",
                emoji="💬",
                description="대화, 최신 검색, 뉴스 질문",
            ),
            discord.SelectOption(
                label="날씨·재난",
                value="weather",
                emoji="🌦️",
                description="기상청 날씨와 공통 재난 알림",
            ),
            discord.SelectOption(
                label="학교 공지",
                value="school",
                emoji="🎓",
                description="등록, 첫 확인, 상태, 알림",
            ),
            discord.SelectOption(
                label="편입 공지",
                value="transfer",
                emoji="📚",
                description="20개 대학 자동 알림과 공식 바로가기",
            ),
            discord.SelectOption(
                label="운세",
                value="fortune",
                emoji="🔮",
                description="등록, 오늘·월간·연간, 구독",
            ),
            discord.SelectOption(
                label="이미지·요약",
                value="creative",
                emoji="🎨",
                description="이미지 생성과 채널 대화 요약",
            ),
            discord.SelectOption(
                label="랭킹·투표",
                value="community",
                emoji="🗳️",
                description="서버 커뮤니티 편의 기능",
            ),
            discord.SelectOption(
                label="간편 설정",
                value="admin",
                emoji="⚙️",
                description="현재 서버 AI와 채널 설정",
            ),
            discord.SelectOption(
                label="개인정보 관리",
                value="privacy",
                emoji="🔐",
                description="동의 현황, 철회, 기능 데이터 삭제",
            ),
        ],
    )
    async def quick_guide(
        self,
        interaction: discord.Interaction,
        select: discord.ui.Select,
    ) -> None:
        prefix = self.ctx.clean_prefix or config.COMMAND_PREFIX or "!"
        guides = {
            "ai": (
                "💬 **AI 대화·검색**\n"
                "- 서버: 마사몽을 멘션하고 자연스럽게 질문\n"
                "- DM: 멘션 없이 바로 질문\n"
                "- 최신 정보가 필요하면 `최신 자료를 검색해서 출처와 함께 알려줘`처럼 요청\n"
                "- 처리 중에는 단계와 경과 시간이 한 메시지에서 갱신됩니다."
            ),
            "weather": (
                "🌦️ **날씨·재난**\n"
                f"- `{prefix}날씨 서울`, `{prefix}날씨 내일 부산`, "
                f"`{prefix}날씨 이번주 제주`\n"
                "- 지진은 기상청 발표 후 약 1분 주기로 확인하며 공통 공식 문구만 사용\n"
                "- 같은 지진군의 후속 이벤트는 기존 Discord 메시지를 수정해 누적 표시"
            ),
            "school": (
                "🎓 **학교 공지**\n"
                "- DM에서 **학교 공지** 버튼을 누르거나 `학교 공지 설정`이라고 말하기\n"
                "- 학교·과정·학년만 필수, 나머지는 선택\n"
                "- 첫 등록 직후 한 번 확인, 이후 매일 05시 수집\n"
                f"- 상태 확인: `{prefix}공지 상태` · 저장 정보: `{prefix}공지 정보`"
            ),
            "transfer": (
                "📚 **편입 공지**\n"
                f"- DM에서 `{prefix}편입`을 실행하고 관심 대학 1~20곳 선택\n"
                "- 매일 05:35에 선택한 대학의 공식 입학처와 필요한 상세 본문 확인\n"
                "- 새 글이나 제목 수정이 있을 때만 활성 구독자에게 DM\n"
                f"- 최근 보기: `{prefix}편입 최근` · 취소: "
                f"`{prefix}편입 구독취소` · 재개: `{prefix}편입 재개`\n"
                "- 지원 자격과 반영 기준은 원문 모집요강에서 최종 확인해 주세요."
            ),
            "fortune": (
                "🔮 **운세**\n"
                f"- 오늘: `{prefix}운세` · 상세: `{prefix}운세 상세`\n"
                f"- 등록: `{prefix}운세 등록` · 아침 알림: `{prefix}운세 구독 08:00`\n"
                f"- 월간/연간: `{prefix}이번달운세`, `{prefix}올해운세`\n"
                "- 개인정보는 고지 후 본인이 동의 버튼을 눌러야 이용합니다."
            ),
            "creative": (
                "🎨 **이미지·요약**\n"
                f"- 이미지: `{prefix}이미지 우주복을 입은 고양이` (서버 전용)\n"
                f"- 최근 대화 요약: `{prefix}요약` (서버 전용)\n"
                "- 긴 작업은 상태 메시지를 유지하고 완료되면 같은 자리에 결과를 표시합니다."
            ),
            "community": (
                "🗳️ **랭킹·투표**\n"
                f"- `{prefix}랭킹 오늘`, `{prefix}랭킹 이번주`, `{prefix}랭킹 전체`\n"
                f"- `{prefix}투표 \"점심 메뉴\" \"국밥\" \"라멘\"`\n"
                "- 랭킹은 현재 채널 범위이며, 투표의 공백 포함 항목은 큰따옴표로 묶습니다."
            ),
            "admin": (
                "⚙️ **간편 설정**\n"
                f"- `{prefix}관리`: 현재 서버 AI와 현재 채널 응답 켜기·끄기\n"
                "- 초대 링크 열기\n"
                "- 현재 인스턴스의 최고 관리자에게만 표시\n"
                "- 모델·DB·수집 주기·말투 같은 운영 설정은 Discord에서 변경하지 않습니다."
            ),
            "privacy": (
                "🔐 **개인정보 관리**\n"
                f"- 상태: `{prefix}개인정보`\n"
                f"- 철회: `{prefix}개인정보 철회 운세` / "
                f"`{prefix}개인정보 철회 학교공지` / "
                f"`{prefix}개인정보 철회 편입공지`\n"
                f"- 기능 데이터 삭제: `{prefix}운세 삭제` / `{prefix}공지 삭제` / "
                f"`{prefix}편입 삭제`\n"
                "- 철회와 삭제는 다르며, 일반 Discord 대화·서버 기록은 그대로 유지됩니다."
            ),
            "dm_only": (
                "🔒 **DM 전용 기능**\n"
                "- 학교 공지 등록·수정·삭제와 편입 공지 구독은 개인 DM에서만 가능합니다.\n"
                "- 운세 프로필 등록·상세 운세·아침 알림도 DM에서 진행합니다.\n"
                "- 마사몽 프로필을 열어 **메시지 보내기**를 누른 뒤 "
                f"`{prefix}메뉴`를 입력하세요.\n"
                "- 서버 메뉴에서는 실수로 개인 설정이 공개되지 않도록 해당 버튼이 비활성화됩니다."
            ),
        }
        await interaction.response.send_message(
            guides.get(select.values[0], "그 안내는 찾지 못했어요."),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) == self.user_id:
            return True
        await interaction.response.send_message(
            "이 메뉴는 메뉴를 연 사람만 쓸 수 있어요.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(
        label="학교 공지",
        style=discord.ButtonStyle.primary,
        emoji="🎓",
        row=0,
    )
    async def school_notice(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        if self.ctx.guild:
            await interaction.response.send_message(
                "개인화 학교 공지는 DM에서 설정해요. 마사몽에게 DM으로 `학교 공지 설정`이라고 보내주세요.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "학교 공지 메뉴를 아래에 열었어요.",
            ephemeral=True,
        )
        cog = self.bot.get_cog("SchoolNoticeCog")
        if cog is None:
            await interaction.followup.send(
                "여기서는 학교 공지를 사용할 수 없어요.",
                ephemeral=True,
            )
            return
        await cog.send_dashboard(self.ctx)

    @discord.ui.button(
        label="편입 공지",
        style=discord.ButtonStyle.primary,
        emoji="📚",
        row=0,
    )
    async def transfer_notice(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        if self.ctx.guild:
            prefix = self.ctx.clean_prefix or config.COMMAND_PREFIX or "!"
            await interaction.response.send_message(
                "개인 편입 공지 구독은 DM에서 설정합니다. 마사몽에게 DM으로 "
                f"`{prefix}편입`을 보내주세요.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "편입 공지 구독 메뉴를 아래에 열었어요.",
            ephemeral=True,
        )
        cog = self.bot.get_cog("TransferNoticeCog")
        if cog is None:
            await interaction.followup.send(
                "여기서는 편입 공지를 사용할 수 없어요.",
                ephemeral=True,
            )
            return
        await cog.send_dashboard(self.ctx)

    @discord.ui.button(
        label="오늘 운세",
        style=discord.ButtonStyle.primary,
        emoji="🔮",
        row=0,
    )
    async def fortune(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        if self.ctx.guild:
            await interaction.response.send_message(
                f"서버에서는 개인 프로필을 쓰지 않는 요약 운세 `{self.ctx.clean_prefix or '!'}운세`를 "
                "사용할 수 있어요. 상세 운세·등록·아침 알림은 마사몽 DM에서 "
                f"`{self.ctx.clean_prefix or '!'}메뉴`를 열어 진행하세요.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "오늘 운세를 아래에서 이어서 확인해요.",
            ephemeral=True,
        )
        cog = self.bot.get_cog("FortuneCog")
        if cog is None:
            await interaction.followup.send(
                "여기서는 운세를 사용할 수 없어요.",
                ephemeral=True,
            )
            return
        await type(cog).fortune.callback(cog, self.ctx, option=None)

    @discord.ui.button(
        label="개인정보",
        style=discord.ButtonStyle.secondary,
        emoji="🔐",
        row=0,
    )
    async def privacy(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        cog = self.bot.get_cog("PrivacyCog")
        if cog is None:
            await interaction.response.send_message(
                "개인정보 상태 기능을 불러오지 못했어요.",
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
        row=0,
    )
    async def help(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        prefix = self.ctx.clean_prefix or config.COMMAND_PREFIX or "!"
        await interaction.response.send_message(
            f"`{prefix}도움`은 전체 기능, `{prefix}도움 <기능>`은 상세 사용법을 보여줍니다.\n"
            f"예: `{prefix}도움 공지`, `{prefix}도움 편입`, `{prefix}도움 운세`",
            ephemeral=True,
        )


_MENU_CATEGORIES = {
    "school": {
        "label": "학교·편입",
        "emoji": "🎓",
        "description": "학교·편입 공지와 알림 설정",
    },
    "ai": {
        "label": "AI·검색·창작",
        "emoji": "💬",
        "description": "대화, 검색, 이미지와 요약",
    },
    "weather": {
        "label": "날씨·재난",
        "emoji": "🌦️",
        "description": "날씨 조회와 공식 재난 안내",
    },
    "fortune": {
        "label": "운세",
        "emoji": "🔮",
        "description": "운세 조회와 아침 알림",
    },
    "community": {
        "label": "커뮤니티",
        "emoji": "🗳️",
        "description": "랭킹, 투표와 채널 요약",
    },
    "personal": {
        "label": "개인 설정",
        "emoji": "🔐",
        "description": "동의 상태와 개인 알림",
    },
    "admin": {
        "label": "간편 설정",
        "emoji": "⚙️",
        "description": "현재 서버 AI와 채널 설정",
    },
    "help": {
        "label": "전체 도움말",
        "emoji": "📖",
        "description": "명령어와 사용 예시",
    },
}


def _category_embed(ctx: commands.Context, category: str) -> discord.Embed:
    prefix = ctx.clean_prefix or config.COMMAND_PREFIX or "!"
    server_mode = bool(ctx.guild)
    copy = {
        "school": (
            "학교·편입",
            "내게 필요한 공지만 골라 DM으로 알려드려요.",
            (
                "- 학교 공지: 학교·학과·학년·관심사에 맞춰 확인\n"
                "- 편입 공지: 선택한 대학의 공식 입학처를 확인\n"
                "- 수집: 매일 05:00부터 순차 진행\n"
                "- 알림: 관련 새 글이 있을 때만 설정 시각에 DM"
            ),
        ),
        "ai": (
            "AI·검색·창작",
            "자연스럽게 질문하면 필요한 자료와 기억을 찾아 답해요.",
            (
                "- 서버: 마사몽을 멘션해 질문, DM: 바로 질문\n"
                "- 최신 정보는 검색 결과와 출처를 함께 사용\n"
                "- 기억은 현재 DM 또는 현재 서버 안에서만 활용\n"
                "- 이미지와 요약은 아래 버튼에서 바로 실행"
            ),
        ),
        "weather": (
            "날씨·재난",
            "기상청 자료를 우선해 날씨와 재난 정보를 전해요.",
            (
                "- 현재·내일·주간 날씨 조회\n"
                "- 지진 발표는 약 1분마다 확인\n"
                "- 후속 발표는 기존 메시지에 이어서 정리\n"
                "- 재난 알림은 공통 공식 문체로 표시"
            ),
        ),
        "fortune": (
            "운세",
            "오늘 운세와 상세 흐름을 확인하고 알림도 받을 수 있어요.",
            (
                "- 서버: 개인정보 없는 오늘 요약\n"
                "- DM: 동의 후 상세·월간·연간 운세\n"
                "- 원하는 시각에 아침 운세 알림\n"
                f"- 직접 입력: `{prefix}운세`, `{prefix}운세 등록`"
            ),
        ),
        "community": (
            "커뮤니티",
            "현재 서버에서 함께 쓰는 기능이에요.",
            (
                "- 오늘·주간 활동 랭킹\n"
                "- 찬반·다중 선택 투표\n"
                "- 최근 채널 대화 요약\n"
                "- 결과는 현재 채널에 공개"
            ),
        ),
        "personal": (
            "개인 설정",
            "내 동의 상태와 알림 설정을 한곳에서 관리해요.",
            (
                "- 기능별 개인정보 동의 상태 확인\n"
                "- 학교·편입·운세 알림 설정\n"
                "- 동의 철회와 기능 데이터 삭제\n"
                "- 개인 설정은 DM에서만 이용"
            ),
        ),
        "admin": (
            "간편 설정",
            "현재 인스턴스의 최고 관리자에게만 보여요.",
            (
                "- 현재 서버 AI 응답 켜기·끄기\n"
                "- 현재 채널 응답 켜기·끄기\n"
                "- 마사몽 초대 링크\n"
                "- 모델·DB·수집·말투 같은 운영 설정은 변경 불가"
            ),
        ),
        "help": (
            "전체 도움말",
            "필요한 명령과 예시를 찾아볼 수 있어요.",
            (
                f"- 전체 목록: `{prefix}도움`\n"
                f"- 기능 설명: `{prefix}도움 <명령어>`\n"
                f"- 예: `{prefix}도움 공지`, `{prefix}도움 편입`, "
                f"`{prefix}도움 운세`\n"
                "- 뒤로 버튼을 누르면 다른 범주를 고를 수 있어요."
            ),
        ),
    }
    title, subtitle, body = copy.get(category, copy["help"])
    if server_mode and category in {"school", "personal"}:
        body += (
            "\n\n🔒 개인 식별 정보와 구독 설정은 이 서버 화면에서 실행되지 "
            "않습니다. 마사몽에게 DM으로 `!메뉴`를 보내세요."
        )
    embed = discord.Embed(
        title=f"{_MENU_CATEGORIES[category]['emoji']} {title}",
        description=subtitle,
        color=0x66CCFF,
    )
    embed.add_field(name="이용할 수 있는 기능", value=body, inline=False)
    embed.set_footer(text="아래 버튼으로 시작할 수 있어요.")
    return embed


class _MenuContextProxy:
    """명령 콜백의 출력을 현재 interaction 응답으로 돌리는 최소 Context 프록시."""

    def __init__(
        self,
        source: commands.Context,
        interaction: discord.Interaction,
        *,
        result_ephemeral: bool = False,
    ) -> None:
        self._source = source
        self._interaction = interaction
        self._result_ephemeral = bool(result_ephemeral)

    def __getattr__(self, name):
        return getattr(self._source, name)

    async def send(self, content=None, **kwargs):
        kwargs.pop("ephemeral", None)
        ephemeral = self._result_ephemeral
        response = self._interaction.response
        if not response.is_done():
            await response.send_message(
                content,
                ephemeral=ephemeral,
                **kwargs,
            )
            return await self._interaction.original_response()
        return await self._interaction.followup.send(
            content,
            ephemeral=ephemeral,
            wait=True,
            **kwargs,
        )

    async def reply(self, content=None, **kwargs):
        kwargs.pop("mention_author", None)
        return await self.send(content, **kwargs)

    def typing(self):
        class _Typing:
            async def __aenter__(self):
                return None

            async def __aexit__(self, exc_type, exc, tb):
                return False

        return _Typing()


class HomeCategorySelect(discord.ui.Select):
    """홈에서는 기능 범주만 노출합니다."""

    def __init__(self, home: MasamongHomeView) -> None:
        self.home = home
        options = [
            discord.SelectOption(
                label=data["label"],
                value=key,
                emoji=data["emoji"],
                description=data["description"],
            )
            for key, data in _MENU_CATEGORIES.items()
            if key != "admin" or is_superadmin(home.user_id)
        ]
        super().__init__(
            placeholder="무엇을 해볼까요?",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        category = self.values[0]
        if category == "admin" and not is_superadmin(interaction.user.id):
            await interaction.response.send_message(
                "여기서는 사용할 수 없는 메뉴예요.",
                ephemeral=True,
            )
            return
        await interaction.response.edit_message(
            embed=_category_embed(self.home.ctx, category),
            view=CategoryView(self.home.bot, self.home.ctx, category),
            allowed_mentions=discord.AllowedMentions.none(),
        )


class FeatureInputModal(ReliableModal):
    """범주 안에서 인자가 필요한 명령을 입력받습니다."""

    def __init__(self, parent: "CategoryView", action: str) -> None:
        titles = {
            "weather_query": "날씨 조회",
            "image": "이미지 생성",
            "poll": "투표 만들기",
            "fortune_time": "운세 알림 시간",
        }
        super().__init__(title=titles[action], timeout=300)
        self.parent_view = parent
        self.action = action
        if action == "weather_query":
            self.primary = discord.ui.TextInput(
                label="지역과 날짜",
                placeholder="예: 내일 부산 / 이번주 제주",
                max_length=100,
            )
            self.add_item(self.primary)
        elif action == "image":
            self.primary = discord.ui.TextInput(
                label="그리고 싶은 장면",
                placeholder="예: 기억 속 철수를 따뜻한 수채화로 그려줘",
                style=discord.TextStyle.paragraph,
                max_length=1000,
            )
            self.add_item(self.primary)
        elif action == "poll":
            self.primary = discord.ui.TextInput(
                label="투표 질문",
                placeholder="예: 점심 메뉴는?",
                max_length=200,
            )
            self.secondary = discord.ui.TextInput(
                label="선택지 (쉼표로 구분, 비우면 찬반)",
                placeholder="국밥, 라멘, 샐러드",
                required=False,
                max_length=500,
            )
            self.add_item(self.primary)
            self.add_item(self.secondary)
        else:
            self.primary = discord.ui.TextInput(
                label="알림 시각 (24시간제)",
                placeholder="09:00",
                min_length=5,
                max_length=5,
            )
            self.add_item(self.primary)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        values = {"primary": str(self.primary.value).strip()}
        if hasattr(self, "secondary"):
            values["secondary"] = str(self.secondary.value).strip()
        await self.parent_view.execute_action(interaction, self.action, values)


class CategoryActionButton(discord.ui.Button):
    def __init__(
        self,
        parent: "CategoryView",
        *,
        label: str,
        action: str,
        emoji: str,
        dm_only: bool = False,
    ) -> None:
        self.parent_view = parent
        self.action = action
        guild_only = action in {
            "image",
            "summary",
            "ranking_today",
            "ranking_week",
            "poll",
        }
        suffix = ""
        if dm_only and parent.server_mode:
            suffix = " · DM"
        elif guild_only and not parent.server_mode:
            suffix = " · 서버"
        super().__init__(
            label=f"{label}{suffix}",
            emoji=emoji,
            style=discord.ButtonStyle.primary,
            disabled=bool(
                (dm_only and parent.server_mode)
                or (guild_only and not parent.server_mode)
            ),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.action in {"weather_query", "image", "poll", "fortune_time"}:
            await interaction.response.send_modal(
                FeatureInputModal(self.parent_view, self.action)
            )
            return
        await self.parent_view.execute_action(interaction, self.action, {})


class CategoryView(ReliableView):
    """선택한 범주의 기능만 표시하는 2단계 메뉴."""

    _ACTIONS = {
        "school": [
            ("학교 공지", "school_dashboard", "🎓", True),
            ("편입 공지", "transfer_dashboard", "📚", True),
            ("개인 설정", "open_personal", "🔐", False),
        ],
        "ai": [
            ("이미지 생성", "image", "🎨", False),
            ("대화 요약", "summary", "📋", False),
            ("업데이트", "update", "🧾", False),
        ],
        "weather": [
            ("기본 지역 날씨", "weather_now", "🌤️", False),
            ("지역·날짜 입력", "weather_query", "🗺️", False),
        ],
        "fortune": [
            ("오늘 운세", "fortune_today", "🔮", False),
            ("상세 운세", "fortune_detail", "🧭", True),
            ("이번 달", "fortune_month", "📅", True),
            ("올해", "fortune_year", "🗓️", True),
            ("알림 시간", "fortune_time", "⏰", True),
        ],
        "community": [
            ("오늘 랭킹", "ranking_today", "🏆", False),
            ("주간 랭킹", "ranking_week", "📈", False),
            ("투표 만들기", "poll", "🗳️", False),
            ("대화 요약", "summary", "📋", False),
        ],
        "personal": [
            ("동의 상태", "privacy_status", "🔐", True),
            ("학교 설정", "school_dashboard", "🎓", True),
            ("편입 설정", "transfer_dashboard", "📚", True),
            ("운세 알림", "fortune_time", "⏰", True),
        ],
        "admin": [
            ("간편 설정 열기", "admin_panel", "⚙️", False),
        ],
        "help": [("전체 도움말 보기", "help_text", "📖", False)],
    }

    def __init__(
        self,
        bot: commands.Bot,
        ctx: commands.Context,
        category: str,
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.ctx = ctx
        self.category = category
        self.user_id = int(ctx.author.id)
        self.server_mode = bool(ctx.guild)
        if category == "admin" and not is_superadmin(self.user_id):
            category = "help"
            self.category = "help"
        for label, action, emoji, dm_only in self._ACTIONS.get(category, []):
            button = CategoryActionButton(
                self,
                label=label,
                action=action,
                emoji=emoji,
                dm_only=dm_only,
            )
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) == self.user_id:
            return True
        await interaction.response.send_message(
            "이 메뉴는 메뉴를 연 사람만 쓸 수 있어요.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(
        label="뒤로",
        emoji="↩️",
        style=discord.ButtonStyle.secondary,
        row=4,
    )
    async def back(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            embed=_build_home_embed(
                self.bot,
                self.ctx,
                server_private=self.server_mode,
            ),
            view=MasamongHomeView(self.bot, self.ctx),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _invoke_command(
        self,
        interaction: discord.Interaction,
        command_name: str,
        **kwargs,
    ) -> None:
        command = self.bot.get_command(command_name)
        if command is None or command.cog is None:
            await interaction.response.send_message(
                "여기서는 해당 기능을 사용할 수 없어요.",
                ephemeral=True,
            )
            return
        # 원격 DB/API 조회가 3초를 넘더라도 Discord가 "응답하지 않음"으로
        # 표시하지 않게 먼저 승인한다. 이후 명령의 ctx.send/edit 흐름은
        # _MenuContextProxy의 followup 메시지로 그대로 이어진다.
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=False, thinking=True)
        proxy = _MenuContextProxy(
            self.ctx,
            interaction,
            result_ephemeral=False,
        )
        await command.callback(command.cog, proxy, **kwargs)

    async def execute_action(
        self,
        interaction: discord.Interaction,
        action: str,
        values: dict[str, str],
    ) -> None:
        if action == "open_personal":
            await interaction.response.edit_message(
                embed=_category_embed(self.ctx, "personal"),
                view=CategoryView(self.bot, self.ctx, "personal"),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if action == "admin_panel":
            if not is_superadmin(interaction.user.id):
                await interaction.response.send_message(
                    "여기서는 사용할 수 없는 메뉴예요.",
                    ephemeral=True,
                )
                return
            cog = self.bot.get_cog("AdminCog")
            if cog is None:
                await interaction.response.send_message(
                    "간편 설정을 불러오지 못했어요.",
                    ephemeral=True,
                )
                return
            await cog.send_interaction_panel(interaction, self.ctx)
            return
        if action in {"school_dashboard", "transfer_dashboard"}:
            if self.server_mode:
                await interaction.response.send_message(
                    "개인 공지 설정은 DM에서만 가능해요. 마사몽에게 DM으로 `!메뉴`를 보내주세요.",
                    ephemeral=True,
                )
                return
            cog_name = (
                "SchoolNoticeCog"
                if action == "school_dashboard"
                else "TransferNoticeCog"
            )
            cog = self.bot.get_cog(cog_name)
            await interaction.response.send_message(
                "고르신 설정 화면을 아래에 열었어요.",
                ephemeral=False,
            )
            if cog is None:
                await interaction.followup.send(
                    "여기서는 해당 공지 기능을 사용할 수 없어요."
                )
                return
            await cog.send_dashboard(self.ctx)
            return
        if action == "privacy_status":
            await interaction.response.defer(
                ephemeral=self.server_mode,
                thinking=True,
            )
            cog = self.bot.get_cog("PrivacyCog")
            text = (
                await cog.status_text(self.user_id)
                if cog is not None
                else "개인정보 상태 기능을 불러오지 못했어요."
            )
            await interaction.followup.send(
                text,
                ephemeral=self.server_mode,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if action == "weather_now":
            await self._invoke_command(
                interaction,
                "날씨",
                location_query="",
            )
            return
        if action == "weather_query":
            await self._invoke_command(
                interaction,
                "날씨",
                location_query=values.get("primary", ""),
            )
            return
        if action == "image":
            if not self.server_mode:
                await interaction.response.send_message(
                    "이미지 생성은 서버에서만 사용할 수 있어요.",
                    ephemeral=False,
                )
                return
            await self._invoke_command(
                interaction,
                "이미지",
                prompt=values.get("primary", ""),
            )
            return
        if action == "summary":
            if not self.server_mode:
                await interaction.response.send_message(
                    "채널 대화 요약은 서버에서만 사용할 수 있어요.",
                    ephemeral=False,
                )
                return
            await self._invoke_command(interaction, "요약")
            return
        if action == "update":
            await self._invoke_command(interaction, "업데이트")
            return
        if action.startswith("fortune_"):
            if action == "fortune_today":
                await self._invoke_command(
                    interaction,
                    "운세",
                    option=None,
                )
            elif action == "fortune_detail":
                await self._invoke_command(
                    interaction,
                    "운세",
                    option="상세",
                )
            elif action == "fortune_month":
                await self._invoke_command(interaction, "이번달운세", arg=None)
            elif action == "fortune_year":
                await self._invoke_command(interaction, "올해운세", arg=None)
            else:
                command = self.bot.get_command("운세 구독")
                if command is None or command.cog is None:
                    await interaction.response.send_message(
                        "운세 알림 기능을 불러오지 못했어요.",
                        ephemeral=bool(self.server_mode),
                    )
                    return
                await interaction.response.defer(
                    ephemeral=self.server_mode,
                    thinking=True,
                )
                proxy = _MenuContextProxy(self.ctx, interaction)
                await command.callback(
                    command.cog,
                    proxy,
                    values.get("primary", ""),
                )
            return
        if action in {"ranking_today", "ranking_week"}:
            if not self.server_mode:
                await interaction.response.send_message(
                    "활동 랭킹은 서버에서만 사용할 수 있어요.",
                    ephemeral=False,
                )
                return
            await self._invoke_command(
                interaction,
                "랭킹",
                period_arg=("오늘" if action == "ranking_today" else "이번주"),
            )
            return
        if action == "poll":
            if not self.server_mode:
                await interaction.response.send_message(
                    "투표는 서버에서만 만들 수 있어요.",
                    ephemeral=False,
                )
                return
            command = self.bot.get_command("투표")
            if command is None or command.cog is None:
                await interaction.response.send_message(
                    "투표 기능을 불러오지 못했어요.",
                    ephemeral=True,
                )
                return
            await interaction.response.defer(
                ephemeral=False,
                thinking=True,
            )
            choices = tuple(
                part.strip()
                for part in values.get("secondary", "").split(",")
                if part.strip()
            )
            proxy = _MenuContextProxy(
                self.ctx,
                interaction,
                result_ephemeral=False,
            )
            await command.callback(
                command.cog,
                proxy,
                values.get("primary", ""),
                *choices,
            )
            return
        if action == "help_text":
            prefix = self.ctx.clean_prefix or config.COMMAND_PREFIX or "!"
            await interaction.response.send_message(
                f"전체 목록은 `{prefix}도움`, 개별 설명은 "
                f"`{prefix}도움 <명령어>`로 확인할 수 있어요.",
                ephemeral=bool(self.server_mode),
            )


class ServerMenuLauncherView(ReliableView):
    """서버 채널에서는 공개 명령 뒤 호출자 전용(ephemeral) 메뉴를 엽니다."""

    def __init__(self, bot: commands.Bot, ctx: commands.Context) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.ctx = ctx
        self.user_id = int(ctx.author.id)
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) == self.user_id:
            return True
        await interaction.response.send_message(
            "이 버튼은 메뉴를 호출한 사람만 사용할 수 있어요. "
            f"`{self.ctx.clean_prefix or '!'}메뉴`를 직접 입력해 주세요.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(
        label="나만의 메뉴 열기",
        style=discord.ButtonStyle.primary,
        emoji="🧭",
    )
    async def open_private_menu(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_message(
            embed=_build_home_embed(
                self.bot,
                self.ctx,
                server_private=True,
            ),
            view=MasamongHomeView(self.bot, self.ctx),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        # prefix 명령은 Discord 프로토콜상 처음부터 ephemeral 응답을 만들 수
        # 없다. 대신 소유자가 버튼을 눌러 실제 ephemeral 메뉴를 받은 즉시
        # 잠깐 노출됐던 공개 launcher를 제거한다.
        launcher_message = getattr(interaction, "message", None) or self.message
        if launcher_message is not None:
            try:
                await launcher_message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        self.message = None
        self.stop()

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


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
        if ctx.guild:
            launcher = ServerMenuLauncherView(self.bot, ctx)
            launcher_embed = discord.Embed(
                title="🧭 마사몽 메뉴",
                description=(
                    "아래 버튼을 누르면 메뉴는 본인에게만 보여요. 기능을 실행한 결과는 함께 볼 수 있도록 채널에 표시하고, 개인 설정과 관리 내용만 비공개로 유지해요."
                ),
                color=0x66CCFF,
            )
            launcher_embed.set_footer(
                text=f"버튼은 3분간 유효 · 전체 텍스트 도움말: {prefix}도움"
            )
            launcher.message = await ctx.send(
                embed=launcher_embed,
                view=launcher,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            # 사용자가 입력한 prefix 메시지도 권한이 있으면 정리한다. 실제
            # 결과는 메뉴 액션의 interaction followup으로 채널에 공개된다.
            invocation = getattr(ctx, "message", None)
            if invocation is not None:
                try:
                    await invocation.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
            return

        await ctx.send(
            embed=_build_home_embed(
                self.bot,
                ctx,
                server_private=False,
            ),
            view=MasamongHomeView(self.bot, ctx),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def cog_unload(self):
        """Cog 언로드 시 원래 도움말 커맨드로 복구"""
        self.bot.help_command = self._original_help_command

async def setup(bot):
    await bot.add_cog(HelpCog(bot))

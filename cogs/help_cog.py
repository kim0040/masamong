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
                f"- `{prefix}도움 편입`\n"
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
            "이 화면은 메뉴를 연 본인에게만 보여요. 서버에서 바로 쓸 수 있는 "
            "기능과 명령 예시를 확인하세요. 🔒 표시는 DM 전용이라 이 메뉴에서는 "
            "실행되지 않습니다."
        )
    else:
        description = (
            "바로 시작하려면 버튼을, 사용법을 먼저 보려면 아래 선택 메뉴를 "
            "이용하세요. 개인정보가 필요한 기능은 동의 화면이 이어서 열리고, "
            "동의 후 원래 요청도 자동으로 계속됩니다."
        )
    embed = discord.Embed(
        title="🤖 마사몽 메뉴",
        description=description,
        color=0x66CCFF,
    )
    embed.add_field(
        name="💬 AI 대화·웹 검색",
        value=(
            "마사몽을 멘션하고 자연스럽게 질문하세요. 최신 정보는 출처를 본문에 "
            "함께 표시합니다.\n"
            f"예: `@마사몽 오늘 주요 AI 소식 찾아줘` · `{prefix}날씨 내일 서울`"
        ),
        inline=False,
    )
    if server_private:
        embed.add_field(
            name="🧰 서버에서 바로 사용",
            value=(
                f"`{prefix}운세` · `{prefix}랭킹 오늘` · `{prefix}투표 점심 먹을까?`\n"
                f"`{prefix}요약` · `{prefix}이미지 우주복 고양이` · `{prefix}업데이트`"
            ),
            inline=False,
        )
        dm_features = ["학교 공지", "편입 공지 구독", "운세 등록·상세·알림"]
        if bot.get_cog("PrivacyCog") is not None:
            dm_features.append("개인정보 철회·기능 데이터 삭제")
        embed.add_field(
            name="🔒 DM에서만 설정",
            value=(
                " · ".join(dm_features)
                + "\n마사몽 프로필을 열어 **메시지 보내기** 후 "
                f"`{prefix}메뉴`를 입력하세요."
            ),
            inline=False,
        )
    else:
        embed.add_field(
            name="🎓 학교·편입 공지",
            value=(
                "등록한 학교만 확인하며 학교 공지는 첫 등록 직후 1회, 이후 23시 "
                "수집입니다. 편입은 선택한 20개 대학 중 새 공식 공지만 DM으로 받습니다."
            ),
            inline=False,
        )
        embed.add_field(
            name="🔮 개인 운세",
            value="프로필 등록·오늘 운세·알림 설정을 개인정보 동의와 함께 진행합니다.",
            inline=False,
        )
    embed.add_field(
        name="📖 더 자세히",
        value=(
            f"`{prefix}도움`은 전체 목록, `{prefix}도움 <기능>`은 상세 설명입니다.\n"
            f"예: `{prefix}도움 운세`, `{prefix}도움 공지`, `{prefix}도움 편입`"
        ),
        inline=False,
    )
    embed.set_footer(
        text=(
            "서버 메뉴의 상세 화면은 호출자에게만 표시됩니다."
            if server_private
            else f"전체 명령과 상세 설명: {prefix}도움"
        )
    )
    return embed


class MasamongHomeView(discord.ui.View):
    """통합 메뉴를 연 사용자만 조작할 수 있는 짧은 수명의 홈 화면."""

    def __init__(self, bot: commands.Bot, ctx: commands.Context) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.ctx = ctx
        self.user_id = int(ctx.author.id)
        self.server_mode = bool(ctx.guild)
        self.school_notice.disabled = (
            self.server_mode or bot.get_cog("SchoolNoticeCog") is None
        )
        self.transfer_notice.disabled = (
            self.server_mode or bot.get_cog("TransferNoticeCog") is None
        )
        self.privacy.disabled = (
            self.server_mode or bot.get_cog("PrivacyCog") is None
        )
        if self.server_mode:
            self.school_notice.label = "학교 공지 · DM"
            self.transfer_notice.label = "편입 공지 · DM"
            self.privacy.label = "개인정보 · DM"
            self.fortune.label = "오늘 운세 안내"
            self.quick_guide.options = [
                option
                for option in self.quick_guide.options
                if option.value not in {"school", "transfer", "privacy"}
            ]
            self.quick_guide.options.append(
                discord.SelectOption(
                    label="DM 전용 기능",
                    value="dm_only",
                    emoji="🔒",
                    description="학교·편입 구독과 개인 정보 설정",
                )
            )

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
                description="20개 대학 선택 구독과 새 공지 DM",
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
                label="서버 관리자 설정",
                value="admin",
                emoji="⚙️",
                description="AI 채널, 말투, 언어 설정",
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
                "- 첫 등록 직후 한 번 확인, 이후 매일 23시 수집\n"
                f"- 상태 확인: `{prefix}공지 상태` · 저장 정보: `{prefix}공지 정보`"
            ),
            "transfer": (
                "📚 **TOEIC·공인영어 편입 공지**\n"
                f"- DM에서 `{prefix}편입`을 실행하고 관심 대학 1~20곳 선택\n"
                "- 매일 23:35에 선택한 대학의 공식 입학처 목록만 한 번 확인\n"
                "- 새 글이나 제목 수정이 있을 때만 활성 구독자에게 DM\n"
                f"- 최근 보기: `{prefix}편입 최근` · 취소: "
                f"`{prefix}편입 구독취소` · 재개: `{prefix}편입 재개`\n"
                "- TOEIC 인정·환산·모집단위는 매년 달라질 수 있어 공식 최종 "
                "모집요강을 반드시 확인해야 합니다."
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
                "⚙️ **서버 관리자 설정**\n"
                "- `/config set_ai`: 서버 AI 켜기/끄기\n"
                "- `/config channel`: 응답 허용 채널 추가/제거\n"
                "- `/config language`: 서버 언어\n"
                "- `/persona view`, `/persona set`: 이 서버 전용 말투\n"
                "- 관리자 권한이 필요하며 다른 서버의 설정과 섞이지 않습니다."
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
            guides.get(select.values[0], "해당 안내를 찾지 못했습니다."),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

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
        row=0,
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
            "편입 공지 구독 메뉴를 아래에 열었습니다.",
            ephemeral=True,
        )
        cog = self.bot.get_cog("TransferNoticeCog")
        if cog is None:
            await interaction.followup.send(
                "이 인스턴스에서는 편입 공지를 사용할 수 없습니다.",
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


class ServerMenuLauncherView(discord.ui.View):
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
                title="🧭 마사몽 개인 메뉴",
                description=(
                    "아래 버튼을 누르면 **호출한 사람에게만** 전체 기능 메뉴가 "
                    "보입니다. 학교·편입 구독처럼 DM 전용인 기능은 서버 메뉴에서 "
                    "잠겨 있어 개인 설정이 채널에 노출되지 않습니다."
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

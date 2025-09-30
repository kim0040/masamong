# -*- coding: utf-8 -*-
"""
서버 관리자가 슬래시 커맨드를 사용하여 서버별 AI 설정을 관리하는 기능을 제공하는 Cog입니다.

주요 기능:
- AI 기능 활성화/비활성화
- AI 응답 허용 채널 관리
- AI 페르소나 조회 및 설정 (Modal UI 사용)
"""

import discord
from discord.ext import commands
from discord import app_commands
from discord.ui import Modal, TextInput
import aiosqlite
import json

from logger_config import logger
from utils import db as db_utils

class PersonaSetModal(Modal, title="AI 페르소나 설정"):
    """AI의 페르소나를 입력받기 위한 Modal(팝업창) UI 클래스입니다."""
    persona_input = TextInput(
        label="새로운 페르소나",
        style=discord.TextStyle.paragraph,
        placeholder="AI의 새로운 정체성, 행동 원칙, 규칙 등을 여기에 입력하세요...",
        max_length=2000,
        required=True
    )

    def __init__(self, bot: commands.Bot, current_persona: str = ""):
        super().__init__()
        self.bot = bot
        # 현재 설정된 페르소나가 있으면 기본값으로 보여줍니다.
        if current_persona:
            self.persona_input.default = current_persona

    async def on_submit(self, interaction: discord.Interaction):
        """사용자가 Modal에서 '제출' 버튼을 눌렀을 때 실행됩니다."""
        new_persona = self.persona_input.value
        guild_id = interaction.guild_id
        log_extra = {'guild_id': guild_id, 'user_id': interaction.user.id}

        try:
            # UPSERT 구문을 사용하여 설정이 없으면 INSERT, 있으면 UPDATE를 수행합니다.
            await db_utils.set_guild_setting(self.bot.db, guild_id, 'persona_text', new_persona)
            await interaction.response.send_message("✅ AI 페르소나를 성공적으로 변경했습니다.", ephemeral=True)
            logger.info(f"AI 페르소나가 변경되었습니다.", extra=log_extra)
        except aiosqlite.Error as e:
            logger.error(f"AI 페르소나 설정 중 DB 오류: {e}", exc_info=True, extra=log_extra)
            await interaction.response.send_message("❌ 페르소나 변경 중 오류가 발생했습니다.", ephemeral=True)

class SettingsCog(commands.Cog):
    """서버별 설정을 관리하는 슬래시 커맨드 그룹입니다."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        logger.info("SettingsCog가 성공적으로 초기화되었습니다.")

    # 슬래시 커맨드를 그룹화하여 /config set_ai, /config channel 등으로 사용할 수 있게 합니다.
    config_group = app_commands.Group(name="config", description="서버의 일반 설정을 관리합니다.")
    persona_group = app_commands.Group(name="persona", description="AI의 페르소나를 관리합니다.")

    @config_group.command(name="set_ai", description="이 서버에서 AI 기능 활성화 여부를 설정합니다.")
    @app_commands.describe(enabled="AI 기능을 활성화하려면 True, 비활성화하려면 False")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_ai_enabled(self, interaction: discord.Interaction, enabled: bool):
        """서버 전체의 AI 기능 활성화/비활성화를 설정합니다."""
        guild_id = interaction.guild_id
        try:
            await db_utils.set_guild_setting(self.bot.db, guild_id, 'ai_enabled', enabled)
            status = "활성화" if enabled else "비활성화"
            await interaction.response.send_message(f"✅ AI 기능을 성공적으로 **{status}**했습니다.", ephemeral=True)
            logger.info(f"AI 기능이 {status}되었습니다.", extra={'guild_id': guild_id, 'user_id': interaction.user.id})
        except aiosqlite.Error as e:
            logger.error(f"AI 기능 설정 중 DB 오류: {e}", exc_info=True, extra={'guild_id': guild_id})
            await interaction.response.send_message("❌ 설정 변경 중 오류가 발생했습니다.", ephemeral=True)

    @config_group.command(name="channel", description="AI 응답 허용 채널을 관리합니다.")
    @app_commands.describe(action="수행할 작업 (추가/제거)", channel="대상 채널")
    @app_commands.choices(action=[
        app_commands.Choice(name="추가", value="add"),
        app_commands.Choice(name="제거", value="remove"),
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def set_allowed_channel(self, interaction: discord.Interaction, action: str, channel: discord.TextChannel):
        """AI가 응답할 수 있는 채널 목록을 관리합니다."""
        guild_id = interaction.guild_id
        log_extra = {'guild_id': guild_id, 'user_id': interaction.user.id}

        try:
            # DB에서 현재 채널 목록(JSON)을 불러옵니다.
            current_channels_json = await db_utils.get_guild_setting(self.bot.db, guild_id, 'ai_allowed_channels')
            allowed_channels = json.loads(current_channels_json) if current_channels_json else []

            if action == "add":
                if channel.id not in allowed_channels:
                    allowed_channels.append(channel.id)
                    message = f"✅ 이제 <#{channel.id}> 채널에서 AI가 응답합니다."
                else:
                    await interaction.response.send_message(f"ℹ️ <#{channel.id}> 채널은 이미 허용된 채널입니다.", ephemeral=True)
                    return
            elif action == "remove":
                if channel.id in allowed_channels:
                    allowed_channels.remove(channel.id)
                    message = f"✅ 이제 <#{channel.id}> 채널에서 AI가 응답하지 않습니다."
                else:
                    await interaction.response.send_message(f"ℹ️ <#{channel.id}> 채널은 원래 허용된 채널이 아닙니다.", ephemeral=True)
                    return

            # 변경된 채널 목록을 다시 JSON으로 저장합니다.
            await db_utils.set_guild_setting(self.bot.db, guild_id, 'ai_allowed_channels', json.dumps(allowed_channels))
            await interaction.response.send_message(message, ephemeral=True)
            logger.info(f"AI 허용 채널 목록 변경: {action} {channel.name}", extra=log_extra)

        except (aiosqlite.Error, json.JSONDecodeError) as e:
            logger.error(f"AI 허용 채널 설정 중 오류: {e}", exc_info=True, extra=log_extra)
            await interaction.response.send_message("❌ 설정 변경 중 오류가 발생했습니다.", ephemeral=True)

    @persona_group.command(name="view", description="현재 서버에 설정된 AI 페르소나를 확인합니다.")
    @app_commands.checks.has_permissions(administrator=True)
    async def view_persona(self, interaction: discord.Interaction):
        """DB에 저장된 현재 서버의 커스텀 페르소나를 보여줍니다."""
        guild_id = interaction.guild_id
        persona_text = await db_utils.get_guild_setting(self.bot.db, guild_id, 'persona_text')

        if persona_text:
            embed = discord.Embed(title="🎨 현재 AI 페르소나", description=persona_text, color=discord.Color.green())
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message("ℹ️ 이 서버에 설정된 커스텀 페르소나가 없습니다. `config.py`의 기본 페르소나를 사용합니다.", ephemeral=True)

    @persona_group.command(name="set", description="이 서버의 AI 페르소나를 새로 설정합니다.")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_persona(self, interaction: discord.Interaction):
        """페르소나 설정을 위한 Modal을 띄웁니다."""
        guild_id = interaction.guild_id
        current_persona = await db_utils.get_guild_setting(self.bot.db, guild_id, 'persona_text', default="")
        await interaction.response.send_modal(PersonaSetModal(self.bot, current_persona=current_persona))

async def setup(bot: commands.Bot):
    """Cog를 봇에 등록하는 함수입니다."""
    await bot.add_cog(SettingsCog(bot))
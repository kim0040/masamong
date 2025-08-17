# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
from discord import app_commands
from discord.ui import Modal, TextInput
import aiosqlite
import json

import config
from logger_config import logger
import utils

class PersonaSetModal(Modal, title="AI 페르소나 설정"):
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
        if current_persona:
            self.persona_input.default = current_persona

    async def on_submit(self, interaction: discord.Interaction):
        new_persona = self.persona_input.value
        guild_id = interaction.guild_id
        log_extra = {'guild_id': guild_id}

        try:
            await self.bot.db.execute("""
                INSERT INTO guild_settings (guild_id, persona_text, updated_at) VALUES (?, ?, strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc'))
                ON CONFLICT(guild_id) DO UPDATE SET
                    persona_text = excluded.persona_text,
                    updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc')
            """, (guild_id, new_persona))
            await self.bot.db.commit()

            await interaction.response.send_message("✅ AI 페르소나를 성공적으로 변경했습니다.", ephemeral=True)
            logger.info(f"AI 페르소나 변경됨 (요청자: {interaction.user})", extra=log_extra)

        except aiosqlite.Error as e:
            logger.error(f"AI 페르소나 설정 중 DB 오류: {e}", exc_info=True, extra=log_extra)
            await interaction.response.send_message("❌ 페르소나를 변경하는 중 오류가 발생했습니다.", ephemeral=True)


class SettingsCog(commands.Cog):
    """서버별 설정을 관리하는 Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    config_group = app_commands.Group(name="config", description="서버의 일반 설정을 관리합니다.")
    persona_group = app_commands.Group(name="persona", description="AI의 페르소나를 관리합니다.")

    @config_group.command(name="set_ai", description="AI 기능 활성화 여부를 설정합니다.")
    @app_commands.describe(enabled="AI 기능 활성화 여부 (True/False)")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_ai_enabled(self, interaction: discord.Interaction, enabled: bool):
        """AI 기능 활성화/비활성화를 설정합니다."""
        guild_id = interaction.guild_id
        log_extra = {'guild_id': guild_id}

        try:
            await self.bot.db.execute("""
                INSERT INTO guild_settings (guild_id, ai_enabled, updated_at) VALUES (?, ?, strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc'))
                ON CONFLICT(guild_id) DO UPDATE SET
                    ai_enabled = excluded.ai_enabled,
                    updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc')
            """, (guild_id, enabled))
            await self.bot.db.commit()

            status = "활성화" if enabled else "비활성화"
            await interaction.response.send_message(f"✅ AI 기능을 성공적으로 **{status}**했습니다.", ephemeral=True)
            logger.info(f"AI 기능이 {status}되었습니다. (요청자: {interaction.user})", extra=log_extra)

        except aiosqlite.Error as e:
            logger.error(f"AI 기능 설정 중 DB 오류: {e}", exc_info=True, extra=log_extra)
            await interaction.response.send_message("❌ 설정을 변경하는 중 오류가 발생했습니다.", ephemeral=True)

    @config_group.command(name="channel", description="AI 응답 허용 채널을 관리합니다.")
    @app_commands.describe(action="수행할 작업 (추가/제거)", channel="대상 채널")
    @app_commands.choices(action=[
        app_commands.Choice(name="추가", value="add"),
        app_commands.Choice(name="제거", value="remove"),
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def set_allowed_channel(self, interaction: discord.Interaction, action: str, channel: discord.TextChannel):
        """AI 응답 허용 채널 목록을 추가하거나 제거합니다."""
        guild_id = interaction.guild_id
        log_extra = {'guild_id': guild_id}

        try:
            current_channels_json = await utils.get_guild_setting(self.bot.db, guild_id, 'ai_allowed_channels')

            allowed_channels = []
            if current_channels_json:
                try:
                    allowed_channels = json.loads(current_channels_json)
                except (json.JSONDecodeError, TypeError):
                    # DB에 잘못된 데이터가 있는 경우, 초기화
                    logger.warning(f"Guild({guild_id})의 ai_allowed_channels가 잘못된 JSON 형식이라 초기화합니다.", extra=log_extra)
                    allowed_channels = []

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

            new_allowed_channels_json = json.dumps(allowed_channels)
            await self.bot.db.execute("""
                INSERT INTO guild_settings (guild_id, ai_allowed_channels, updated_at) VALUES (?, ?, strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc'))
                ON CONFLICT(guild_id) DO UPDATE SET
                    ai_allowed_channels = excluded.ai_allowed_channels,
                    updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc')
            """, (guild_id, new_allowed_channels_json))
            await self.bot.db.commit()

            await interaction.response.send_message(message, ephemeral=True)
            logger.info(f"AI 허용 채널 목록 변경: {action} {channel.name} (요청자: {interaction.user})", extra=log_extra)

        except aiosqlite.Error as e:
            logger.error(f"AI 허용 채널 설정 중 DB 오류: {e}", exc_info=True, extra=log_extra)
            await interaction.response.send_message("❌ 설정을 변경하는 중 오류가 발생했습니다.", ephemeral=True)

    @persona_group.command(name="view", description="현재 서버의 AI 페르소나를 확인합니다.")
    @app_commands.checks.has_permissions(administrator=True)
    async def view_persona(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        persona_text = await utils.get_guild_setting(self.bot.db, guild_id, 'persona_text')

        if persona_text:
            embed = discord.Embed(title="🎨 현재 AI 페르소나", description=persona_text, color=discord.Color.green())
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message("ℹ️ 이 서버에 설정된 커스텀 페르소나가 없습니다. `config.py`의 기본 페르소나를 사용합니다.", ephemeral=True)

    @persona_group.command(name="set", description="이 서버의 AI 페르소나를 새로 설정합니다.")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_persona(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        current_persona = await utils.get_guild_setting(self.bot.db, guild_id, 'persona_text', default="")
        await interaction.response.send_modal(PersonaSetModal(self.bot, current_persona=current_persona))

async def setup(bot: commands.Bot):
    await bot.add_cog(SettingsCog(bot))
    logger.info("SettingsCog 로드 완료.")

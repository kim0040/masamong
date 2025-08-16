# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
from discord import app_commands
import sqlite3

import config
from logger_config import logger

class SettingsCog(commands.Cog):
    """서버별 설정을 관리하는 Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    config_group = app_commands.Group(name="config", description="서버 설정을 관리합니다.")

    @config_group.command(name="set_ai", description="AI 기능 활성화 여부를 설정합니다.")
    @app_commands.describe(enabled="AI 기능 활성화 여부 (True/False)")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_ai_enabled(self, interaction: discord.Interaction, enabled: bool):
        """AI 기능 활성화/비활성화를 설정합니다."""
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message("이 명령어는 서버에서만 사용할 수 있습니다.", ephemeral=True)
            return

        conn = None
        try:
            conn = sqlite3.connect(config.DATABASE_FILE)
            cursor = conn.cursor()
            # guild_settings 테이블에 guild_id가 없으면 INSERT, 있으면 UPDATE
            cursor.execute("""
                INSERT INTO guild_settings (guild_id, ai_enabled) VALUES (?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET ai_enabled = excluded.ai_enabled
            """, (guild_id, enabled))
            conn.commit()

            status = "활성화" if enabled else "비활성화"
            await interaction.response.send_message(f"✅ AI 기능을 성공적으로 **{status}**했습니다.", ephemeral=True)
            logger.info(f"[{interaction.guild.name}] AI 기능이 {status}되었습니다. (요청자: {interaction.user})")

        except sqlite3.Error as e:
            logger.error(f"[{interaction.guild.name}] AI 기능 설정 중 DB 오류: {e}", exc_info=True)
            await interaction.response.send_message("❌ 설정을 변경하는 중 오류가 발생했습니다.", ephemeral=True)
        finally:
            if conn:
                conn.close()

async def setup(bot: commands.Bot):
    await bot.add_cog(SettingsCog(bot))
    logger.info("SettingsCog 로드 완료.")

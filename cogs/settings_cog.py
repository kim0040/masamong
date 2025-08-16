# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
from discord import app_commands

from logger_config import logger

class SettingsCog(commands.Cog):
    """서버별 설정을 관리하는 Cog (슬래시 커맨드 사용)"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        logger.info("SettingsCog 준비 완료.")

    persona_group = app_commands.Group(name="persona", description="이 서버의 AI 페르소나를 관리합니다.")

    @persona_group.command(name="set", description="이 서버의 AI 페르소나를 새로 설정합니다. (관리자 전용)")
    @app_commands.describe(new_persona="새로운 페르소나 설명. AI의 역할, 말투, 정체성 등을 상세히 적어주세요.")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_persona(self, interaction: discord.Interaction, new_persona: str):
        if not self.bot.db:
            await interaction.response.send_message("오류: 데이터베이스가 준비되지 않았습니다.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        await interaction.response.defer(ephemeral=True)

        try:
            sql = """
                INSERT INTO guild_settings (guild_id, setting_name, setting_value)
                VALUES (?, 'persona', ?)
                ON CONFLICT(guild_id, setting_name) DO UPDATE SET
                    setting_value = excluded.setting_value,
                    updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now', 'utc');
            """
            await self.bot.db.execute(sql, (guild_id, new_persona))
            await self.bot.db.commit()

            logger.info(f"서버({guild_id}) 페르소나 업데이트됨.")

            embed = discord.Embed(title="✅ 페르소나 설정 완료", description="이제부터 이 서버에서 마사몽은 아래 페르소나에 따라 행동합니다.", color=discord.Color.green())
            embed.add_field(name="새로운 페르소나", value=new_persona[:1024], inline=False)
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"페르소나 설정 중 DB 오류 발생 (서버: {guild_id}): {e}", exc_info=True)
            await interaction.followup.send("오류: 페르소나를 설정하는 중에 문제가 발생했습니다.")

    @persona_group.command(name="view", description="이 서버에 설정된 현재 AI 페르소나를 확인합니다.")
    async def view_persona(self, interaction: discord.Interaction):
        if not self.bot.db:
            await interaction.response.send_message("오류: 데이터베이스가 준비되지 않았습니다.", ephemeral=True)
            return

        guild_id = interaction.guild_id
        await interaction.response.defer(ephemeral=True)

        try:
            sql = "SELECT setting_value FROM guild_settings WHERE guild_id = ? AND setting_name = 'persona'"
            async with self.bot.db.execute(sql, (guild_id,)) as cursor:
                result = await cursor.fetchone()

            if result and result[0]:
                persona = result[0]
                embed = discord.Embed(title=f"📜 {interaction.guild.name} 서버의 현재 페르소나", description=persona, color=discord.Color.blue())
            else:
                embed = discord.Embed(title="ℹ️ 커스텀 페르소나 없음", description="이 서버에는 커스텀 페르소나가 설정되지 않았습니다. 기본 페르소나로 작동하고 있습니다.\n`/persona set` 명령어로 새로운 페르소나를 설정할 수 있습니다.", color=discord.Color.default())

            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"페르소나 조회 중 DB 오류 발생 (서버: {guild_id}): {e}", exc_info=True)
            await interaction.followup.send("오류: 페르소나를 조회하는 중에 문제가 발생했습니다.")

    @set_persona.error
    async def set_persona_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message("이 명령어를 사용하려면 서버 관리자 권한이 필요해요.", ephemeral=True)
        else:
            await interaction.response.send_message(f"명령어 처리 중 오류가 발생했어요: {error}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SettingsCog(bot))

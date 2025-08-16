# -*- coding: utf-8 -*-
import discord
from discord.ext import commands, tasks
import json
from collections import Counter
import re

import config
from logger_config import logger
from .ai_handler import AIHandler

class ActivityCog(commands.Cog):
    """서버 멤버의 활동량을 기록하고 랭킹을 보여주는 Cog - 개인화 기능 추가"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ai_handler: AIHandler | None = None
        self.activity_data = {}
        self.bot.loop.create_task(self.initialize())

    async def initialize(self):
        """Cog 초기화 작업을 수행합니다."""
        await self.bot.wait_until_ready()
        self.load_activity_data()
        self.save_activity_loop.start()
        logger.info("ActivityCog 초기화 완료.")

    def load_activity_data(self):
        """
        파일에서 활동 데이터를 불러옵니다.
        [수정] 이전 버전의 데이터 형식과 호환되도록 로직 개선
        """
        try:
            with open(config.ACTIVITY_DATA_FILE, 'r', encoding='utf-8') as f:
                loaded_data = json.load(f)
            
            # 새로운 데이터 구조로 변환
            converted_data = {}
            for gid_str, gdata in loaded_data.items():
                gid = int(gid_str)
                converted_data[gid] = {}
                for uid_str, udata in gdata.items():
                    # 옛날 데이터 형식 (값이 정수인 경우) 감지
                    if isinstance(udata, int):
                        converted_data[gid][uid_str] = {
                            'message_count': udata,
                            'keywords': Counter() # 키워드는 새로 시작
                        }
                    # 새로운 데이터 형식 (값이 딕셔너리인 경우)
                    elif isinstance(udata, dict):
                         converted_data[gid][uid_str] = {
                            'message_count': udata.get('message_count', 0),
                            'keywords': Counter(udata.get('keywords', {}))
                        }
            self.activity_data = converted_data
            logger.info("활동 데이터를 파일에서 성공적으로 불러왔습니다 (호환성 모드).")
        except FileNotFoundError:
            logger.warning("활동 데이터 파일이 없어 새로 시작합니다.")
            self.activity_data = {}
        except (json.JSONDecodeError, TypeError, AttributeError) as e:
            logger.error(f"활동 데이터 파일 파싱 오류 또는 형식 오류. 데이터를 초기화합니다. 오류: {e}")
            self.activity_data = {}


    def save_activity_data(self):
        """활동 데이터를 파일에 저장합니다."""
        try:
            with open(config.ACTIVITY_DATA_FILE, 'w', encoding='utf-8') as f:
                # Counter 객체는 json으로 직접 저장되지 않으므로 dict로 변환
                serializable_data = {
                    gid: {
                        uid: {
                            'message_count': udata['message_count'],
                            'keywords': dict(udata['keywords'])
                        } for uid, udata in gdata.items()
                    } for gid, gdata in self.activity_data.items()
                }
                json.dump(serializable_data, f, ensure_ascii=False, indent=4)
            logger.debug("활동 데이터를 파일에 저장했습니다.")
        except Exception as e:
            logger.error(f"활동 데이터 저장 중 오류: {e}", exc_info=True)

    @tasks.loop(seconds=config.ACTIVITY_SAVE_INTERVAL_SECONDS)
    async def save_activity_loop(self):
        """주기적으로 활동 데이터를 파일에 저장합니다."""
        self.save_activity_data()

    def record_message(self, message: discord.Message):
        """메시지 활동(메시지 수, 키워드)을 기록합니다."""
        if not message.guild: return
        
        guild_id = message.guild.id
        user_id = str(message.author.id)

        if guild_id not in self.activity_data:
            self.activity_data[guild_id] = {}
        if user_id not in self.activity_data[guild_id]:
            self.activity_data[guild_id][user_id] = {
                'message_count': 0,
                'keywords': Counter()
            }
        
        self.activity_data[guild_id][user_id]['message_count'] += 1
        keywords = re.findall(r'\b[가-힣]{2,}\b', message.content)
        self.activity_data[guild_id][user_id]['keywords'].update(keywords)


    @commands.command(name='랭킹', aliases=['수다왕', 'ranking'])
    @commands.guild_only()
    async def ranking(self, ctx: commands.Context):
        """서버 활동 랭킹(메시지 수 기준)을 보여줍니다."""
        if not self.ai_handler:
            await ctx.send("죄송합니다, AI 기능이 현재 준비되지 않았습니다.")
            return

        guild_data = self.activity_data.get(ctx.guild.id)
        if not guild_data:
            await ctx.send("아직 서버 활동 데이터가 충분하지 않아. 다들 분발하라구!")
            return

        sorted_users = sorted(
            guild_data.items(), 
            key=lambda item: item[1].get('message_count', 0), 
            reverse=True
        )[:5]

        if not sorted_users:
            await ctx.send("아직 랭킹을 매길 만큼 데이터가 쌓이지 않았어.")
            return

        async with ctx.typing():
            ranking_list = []
            for i, (user_id, data) in enumerate(sorted_users):
                try:
                    user = await self.bot.fetch_user(int(user_id))
                    user_name = user.display_name
                except discord.NotFound:
                    user_name = f"알수없는유저({user_id[-4:]})"
                
                count = data.get('message_count', 0)
                ranking_list.append(f"{i+1}위: {user_name} ({count}회)")

            ranking_str = "\n".join(ranking_list)
            response_text = await self.ai_handler.generate_creative_text(
                channel=ctx.channel,
                author=ctx.author,
                prompt_key='ranking',
                context={'ranking_list': ranking_str}
            )
            
            if response_text and response_text not in [config.MSG_AI_ERROR, config.MSG_CMD_ERROR]:
                await ctx.send(response_text)
            else:
                await ctx.send(response_text or f"**🏆 이번 주 수다왕 랭킹! 🏆**\n\n{ranking_str}")

async def setup(bot: commands.Bot):
    await bot.add_cog(ActivityCog(bot))

# -*- coding: utf-8 -*-
"""
사용자 개인 운세 및 비서 서비스를 담당하는 Cog입니다.
명령어 처리와 모닝 브리핑 자동 발송 스케줄러를 포함합니다.
"""

import discord
from discord.ext import commands, tasks
import asyncio
from datetime import datetime, timedelta
import pytz
import re

import config
from logger_config import logger
from utils import db as db_utils
from utils.fortune import FortuneCalculator, get_sign_from_date

# 시간 유효성 검사 정규식 (HH:MM)
TIME_PATTERN = re.compile(r'^([01]\d|2[0-3]):([0-5]\d)$')

class FortuneCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.calculator = FortuneCalculator()
        # 비동기 초기화 작업을 위해 별도 태스크로 실행
        self.bot.loop.create_task(self._ensure_db_schema())
        self.morning_briefing_task.start()
        logger.info("FortuneCog가 성공적으로 초기화되었습니다.")

    async def _ensure_db_schema(self):
        """pending_payload 컬럼이 없으면 추가합니다."""
        await self.bot.wait_until_ready()
        try:
            # PRAGMA는 row factory에 따라 다를 수 있으므로 인덱스 사용
            async with self.bot.db.execute("PRAGMA table_info(user_profiles)") as cursor:
                rows = await cursor.fetchall()
                # row[1]이 name 컬럼 (sqlite3.Row 객체일 수도 있고 튜플일 수도 있음)
                columns = [row['name'] if isinstance(row, dict) else row[1] for row in rows]
                
                if 'pending_payload' not in columns:
                    await self.bot.db.execute("ALTER TABLE user_profiles ADD COLUMN pending_payload TEXT")
                    await self.bot.db.commit()
                    logger.info("Added 'pending_payload' column to user_profiles")
        except Exception as e:
            logger.error(f"Failed to check/add column: {e}")

    def cog_unload(self):
        self.morning_briefing_task.cancel()

    @commands.group(name='사주')
    @commands.dm_only()
    async def saju(self, ctx: commands.Context):
        """
        사주 및 비서 서비스를 관리하는 명령어입니다.
        
        사용법:
        - `!사주 등록`: 생년월일을 등록하고 서비스를 시작합니다.
        - `!사주 삭제`: 등록된 정보를 삭제하고 구독을 취소합니다.
        """
        if ctx.invoked_subcommand is None:
            await ctx.send("📋 사용법: `!사주 등록`, `!사주 삭제`")

    @saju.command(name='등록')
    async def saju_register(self, ctx: commands.Context):
        """
        사용자의 생년월일 정보를 대화형으로 입력받아 등록합니다. (DM 전용)
        이미 등록된 경우 덮어쓸지 묻습니다.
        """
        try:
            # 1. 생년월일 입력
            await ctx.send("📝 비서 서비스를 위해 생년월일을 입력해주세요. (예: 1990-01-01)")
            
            def check(m):
                return m.author == ctx.author and m.channel == ctx.channel

            try:
                msg = await self.bot.wait_for('message', check=check, timeout=60.0)
                birth_date = msg.content.strip()
                # 날짜 형식 검증
                datetime.strptime(birth_date, '%Y-%m-%d')
            except ValueError:
                await ctx.send("❌ 형식이 올바르지 않아요. `YYYY-MM-DD` 형식으로 다시 시도해주세요.")
                return
            except asyncio.TimeoutError:
                await ctx.send("⏰ 시간이 초과되었어요. 다시 명령어를 입력해주세요.")
                return

            # 2. 태어난 시간 입력
            await ctx.send("🕒 태어난 시간도 알려주세요. 모르면 `모름`이라고 입력해주세요. (예: 14:30)")
            try:
                msg = await self.bot.wait_for('message', check=check, timeout=60.0)
                birth_time_input = msg.content.strip()
                if birth_time_input in ['모름', '몰라', 'unknown']:
                    birth_time = "12:00"
                else:
                    if not TIME_PATTERN.match(birth_time_input):
                         await ctx.send("❌ 시간 형식이 올바르지 않아요. `HH:MM` 형식으로 입력하거나 `모름`이라고 해주세요.")
                         return
                    birth_time = birth_time_input
            except asyncio.TimeoutError:
                 await ctx.send("⏰ 시간이 초과되었어요. 다시 명령어를 입력해주세요.")
                 return

            # 3. 양력/음력 확인 (간소화를 위해 일단 양력 기본, 추후 확장 가능)
            # await ctx.send("📅 양력인가요? (예/아니오)") ... (생략)

            # DB 저장
            await self._save_user_profile(ctx.author.id, birth_date, birth_time)
            await ctx.send(f"✅ 등록이 완료되었습니다!\n이제 매일 아침 설정된 시간(기본 07:30)에 브리핑을 보내드릴게요.\n`!운세` 명령어로 언제든 확인 가능합니다.")
            
        except Exception as e:
            logger.error(f"사주 등록 중 오류: {e}", exc_info=True)
            await ctx.send("❌ 등록 중 오류가 발생했습니다.")

    async def _save_user_profile(self, user_id, birth_date, birth_time):
        """DB에 사용자 프로필 저장/업데이트"""
        async with self.bot.db.execute(
            """
            INSERT OR REPLACE INTO user_profiles (user_id, birth_date, birth_time, created_at)
            VALUES (?, ?, ?, datetime('now'))
            """,
            (user_id, birth_date, birth_time)
        ):
            await self.bot.db.commit()

    @saju.command(name='삭제')
    async def saju_delete(self, ctx: commands.Context):
        """
        등록된 사주 정보와 구독 설정을 완전히 삭제합니다.
        더 이상 모닝 브리핑을 받지 않게 됩니다.
        """
        # DM 체크
        if ctx.guild:
            await ctx.reply("⚠️ 개인 정보 보호를 위해 이 명령어는 DM에서만 사용할 수 있습니다.")
            return

        try:
             async with self.bot.db.execute("DELETE FROM user_profiles WHERE user_id = ?", (ctx.author.id,)):
                 await self.bot.db.commit()
             await ctx.send("🗑️ 모든 개인 정보와 구독 설정이 삭제되었습니다.")
        except Exception as e:
             logger.error(f"사주 삭제 중 오류: {e}", exc_info=True)
             await ctx.send("❌ 삭제 중 오류가 발생했습니다.")

    @commands.command(name='구독시간', aliases=['알림시간', '구독'])
    async def set_subscription_time(self, ctx: commands.Context, time_str: str):
        """
        모닝 브리핑을 받을 시간을 변경합니다. (DM 전용)
        
        사용법:
        `!구독시간 07:00`
        `!구독시간 23:30`
        """
        # DM 체크
        if ctx.guild:
            await ctx.reply("⚠️ 알림 시간 설정은 DM에서만 가능합니다.")
            return

        if not TIME_PATTERN.match(time_str):
            await ctx.send("❌ 올바른 시간 형식이 아닙니다. `HH:MM` (24시간제)로 입력해주세요.")
            return
        
        # 5분 여유 확인
        now = datetime.now(pytz.timezone('Asia/Seoul'))
        try:
             target_time = datetime.strptime(time_str, '%H:%M').replace(year=now.year, month=now.month, day=now.day, tzinfo=now.tzinfo)
             # 만약 설정 시간이 이미 지났다면 내일로 계산
             if target_time <= now:
                 target_time += timedelta(days=1)
                 
             diff_minutes = (target_time - now).total_seconds() / 60
             if diff_minutes < 5:
                 await ctx.send(f"⚠️ **시간 설정 주의**\n원활한 발송 준비를 위해, 현재 시간보다 최소 5분 이후의 시간으로 설정해주세요.\n(현재 시각: {now.strftime('%H:%M')})")
                 return
        except Exception as e:
             logger.error(f"시간 계산 오류: {e}")

        try:
             # 프로필 존재 여부 확인
             cursor = await self.bot.db.execute("SELECT 1 FROM user_profiles WHERE user_id = ?", (ctx.author.id,))
             if not await cursor.fetchone():
                 await ctx.send("⚠️ 먼저 `!사주 등록`으로 정보를 등록해주세요.")
                 return
             
             await self.bot.db.execute(
                 "UPDATE user_profiles SET subscription_time = ? WHERE user_id = ?",
                 (time_str, ctx.author.id)
             )
             await self.bot.db.commit()
             await ctx.send(f"✅ 매일 아침 `{time_str}`에 브리핑을 보내드릴게요!")
        except Exception as e:
             logger.error(f"구독 시간 변경 중 오류: {e}", exc_info=True)
             await ctx.send("❌ 설정 변경 중 오류가 발생했습니다.")

    @commands.command(name='운세')
    async def check_fortune(self, ctx: commands.Context, *, option: str = None):
        """
        오늘의 운세를 확인합니다.
        옵션: `상세` 를 붙이면 더 자세한(Thinking 모델) 분석을 제공합니다.
        """
        user_id = ctx.author.id
        
        # 1. 프로필 조회
        cursor = await self.bot.db.execute("SELECT birth_date, birth_time FROM user_profiles WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        
        if not row:
            if ctx.guild: # 서버에서는 안내만
                 await ctx.reply("🔮 개인 운세를 보려면 DM으로 `!사주 등록`을 먼저 해주세요!", mention_author=True)
            else: # DM에서는 바로 유도
                 await ctx.send("🔮 아직 정보가 없네요. `!사주 등록`으로 생년월일을 알려주세요!")
            return

        birth_date, birth_time = row
        
        # Typing indicator (작성 중 표시)
        async with ctx.typing():
            # 2. 운세 데이터 생성
            fortune_data = self.calculator.get_comprehensive_info(birth_date, birth_time)
            
            # 3. AI 핸들러 호출
            ai_handler = self.bot.get_cog('AIHandler')
            if not ai_handler:
                await ctx.send("AI 모듈을 불러올 수 없습니다.")
                return
            
            # 모델명 매핑
            MODEL_LITE = "DeepSeek-V3.2-Exp-nothinking"
            MODEL_PRO = "DeepSeek-V3.2-Exp-thinking"

            # 별자리 데이터 추가
            try:
                 b_year, b_month, b_day = map(int, birth_date.split('-'))
                 user_sign = get_sign_from_date(b_month, b_day)
                 now = datetime.now(pytz.timezone('Asia/Seoul'))
                 astro_chart = self.calculator._get_astrology_chart(now)
                 fortune_data += f"\n[User Zodiac]: {user_sign}\n[Astro Chart]: {astro_chart}"
            except Exception as e:
                 logger.error(f"Zodiac integration error: {e}")
                 user_sign = "알 수 없음"

            # 프롬프트 설정 (통합)
            # 프롬프트 설정 (통합)
            display_name = ctx.author.display_name
            if option and '상세' in option:
                prompt_key = 'fortune_detail_combined'
                model_name = MODEL_PRO
                system_prompt = (
                    "너는 전문 점성가이자 명리하자인 '마사몽'이야. "
                    "사용자의 사주와 별자리 정보를 깊이 있게 분석해서 상세한 운세를 제공해줘. "
                    "각 관점(동양/서양)에서 보이는 특징을 설명하고, 이를 종합한 결론을 내려줘. "
                    "출력 형식은 가독성 좋은 마크다운(Markdown)을 사용해. (## 소제목, **강조**, - 리스트 등)"
                )
                user_prompt = (
                    f"{fortune_data}\n\n"
                    f"사용자 닉네임: {display_name}\n"
                    f"위 데이터를 바탕으로 {user_sign} 사용자({birth_date})의 오늘 운세를 아주 상세하게 분석해줘.\n"
                    f"항목: [총평], [재물운], [연애/인간관계], [건강운], [마사몽의 심층 조언]"
                )
            else:
                prompt_key = 'fortune_summary_combined'
                model_name = MODEL_LITE
                system_prompt = (
                    "너는 '마사몽'이야. 사용자의 사주(일진)와 별자리 운세를 종합해서 오늘의 운세를 알려줘. "
                    "일반 사용자는 사주와 별자리를 잘 구별하지 못하므로, 두 가지 관점을 자연스럽게 섞어서 설명해줘. "
                    "내용은 너무 짧지 않게, 하지만 가독성 있게 작성해. "
                    "말투는 친근하고 다정한 존댓말을 써. "
                    "출력 형식은 마크다운(Markdown)을 꼭 지켜줘."
                )
                user_prompt = (
                    f"{fortune_data}\n\n"
                    f"사용자 닉네임: {display_name}\n"
                    f"위 데이터를 바탕으로 {user_sign} 사용자({birth_date})의 오늘 운세를 종합적으로 분석해줘. "
                    f"닉네임을 부르며 대답해줘.\n"
                    f"다음 항목을 포함해줘:\n"
                    f"1. 🌟 오늘의 흐름 (사주와 별자리의 공통적인 기운)\n"
                    f"2. 💬 조언 (주의할 점이나 추천 행동)\n"
                    f"3. 🍀 행운의 팁\n"
                    f"내용은 너무 어렵지 않게, 적당한 길이로 작성해."
                )

            # 모델 라우팅
            try:
                 response = await ai_handler._cometapi_generate_content(
                     system_prompt, 
                     user_prompt, 
                     log_extra={'user_id': user_id, 'mode': 'fortune_combined'},
                     model=model_name
                 )
                 
                 if response:
                     await ctx.send(response)
                 else:
                     await ctx.send("운세 분석에 실패했습니다. (AI 응답 없음)")
                     
            except Exception as e:
                 logger.error(f"운세 요청 처리 중 오류: {e}", exc_info=True)
                 await ctx.send("운세 시스템에 문제가 발생했습니다.")


    @commands.group(name='별자리', aliases=['운세전체'])
    async def zodiac(self, ctx: commands.Context):
        """별자리 운세 관련 명령어 그룹입니다."""
        if ctx.invoked_subcommand is None:
            # 1. 서브커맨드 없이 호출 시: 전체 요약해줄지, 특정 별자리 알려줄지 안내
            # 혹은 인자가 있으면 그것을 별자리 이름으로 간주하고 처리
            content = ctx.message.content.strip()
            # 명령어 부분 제외하고 파라미터 확인
            params = content.split()
            
            if len(params) > 1:
                arg = params[1]
                if arg in ['순위', '랭킹', 'ranking']:
                    await self._show_zodiac_ranking(ctx)
                else:
                    target_sign = arg
                    await self._show_zodiac_fortune(ctx, target_sign)
            else:
                embed = discord.Embed(
                    title="🌌 오늘의 별자리 운세",
                    description="특정 별자리의 운세를 보고 싶다면 `!별자리 <이름>`을 입력해주세요!\n예: `!별자리 물병`, `!별자리 순위`\n\n**12별자리 목록**\n양, 황소, 쌍둥이, 게, 사자, 처녀\n천칭, 전갈, 사수, 염소, 물병, 물고기",
                    color=0x6a0dad
                )
                await ctx.send(embed=embed)

    async def _show_zodiac_ranking(self, ctx: commands.Context):
        """12별자리 운세 순위를 보여줍니다."""
        now = datetime.now(pytz.timezone('Asia/Seoul'))
        astro_chart = self.calculator._get_astrology_chart(now)
        
        system_prompt = (
            "너는 점성술사 '마사몽'이야. 현재 천체 배치를 분석해서 12별자리의 오늘의 운세 순위를 매겨줘. "
            "1위부터 12위까지 순위를 매기고, 각 별자리에 대해 한 줄 코멘트를 달아줘. "
            "출력 형식은 마크다운을 사용해 깔끔하게 보여줘."
        )
        user_prompt = (
            f"[현재 천체 배치]\n{astro_chart}\n\n"
            f"오늘의 12별자리 운세 순위를 알려줘. "
            f"상위권(1~3위)은 🌟, 중위권(4~9위)은 😐, 하위권(10~12위)은 ☁️ 이모지를 사용해 분류해줘. "
            f"각 별자리마다 행운의 팁(색상, 숫자)도 간단히 덧붙여줘."
        )

        async with ctx.typing():
            ai_handler = self.bot.get_cog('AIHandler')
            if ai_handler:
                response = await ai_handler._cometapi_generate_content(
                    system_prompt, user_prompt, 
                    log_extra={'user_id': ctx.author.id, 'mode': 'zodiac_ranking'}
                )
                if response:
                    embed = discord.Embed(
                        title=f"🏆 오늘의 별자리 운세 랭킹 ({now.strftime('%m/%d')})",
                        description=response,
                        color=0xffd700
                    )
                    await ctx.send(embed=embed)
                else:
                    await ctx.send("별들의 순위를 매기는 중 오류가 발생했습니다.")
            else:
                await ctx.send("AI 모듈 오류")

    async def _show_zodiac_fortune(self, ctx: commands.Context, sign_name: str):
        """특정 별자리의 오늘의 운세를 풍부하게 출력합니다."""
        # 1. 별자리 이름 정규화
        normalized_sign = self._normalize_zodiac_name(sign_name)
        if not normalized_sign:
            await ctx.send(f"🤔 '{sign_name}'은(는) 올바른 별자리 이름이 아니에요. (예: 물병자리, 사자자리)")
            return

        # 2. 현재 천체 배치 가져오기 (Context)
        now = datetime.now(pytz.timezone('Asia/Seoul'))
        astro_chart = self.calculator._get_astrology_chart(now)

        # 3. AI 프롬프트 구성
        system_prompt = (
            "당신은 친절하고 통찰력 있는 '점성술사 마사몽'입니다. "
            "현재 천체 배치(Transit)를 바탕으로 특정 별자리의 오늘 운세를 분석해줍니다. "
            "너무 추상적이거나 난해한 표현은 피하고, 누구나 이해하기 쉽게 명확하고 구체적으로 설명하세요. "
            "비유보다는 실질적인 조언 위주로 작성하되, 다정하고 희망찬 어조를 유지하세요. "
            "출력은 마크다운 형식을 사용하여 가독성을 높이세요."
        )
        
        user_prompt = (
            f"[현재 천체 배치]\n{astro_chart}\n\n"
            f"[타겟 별자리]: {normalized_sign}\n\n"
            f"오늘 {normalized_sign} 사람들을 위한 상세한 운세를 작성해주세요. "
            f"가독성을 위해 각 항목은 짧고 명료하게 작성하고, 중요한 키워드는 강조하세요. "
            f"다음 항목을 포함하세요:\n"
            f"1. 🌟 오늘의 기운 (총평)\n"
            f"2. 💘 사랑과 인간관계\n"
            f"3. 💰 일과 금전\n"
            f"4. 🍀 마사몽의 행운 팁 (행운의 색, 물건 등)"
        )

        async with ctx.typing():
            ai_handler = self.bot.get_cog('AIHandler')
            if ai_handler:
                response = await ai_handler._cometapi_generate_content(
                    system_prompt,
                    user_prompt,
                    log_extra={'user_id': ctx.author.id, 'mode': 'zodiac_fortune', 'sign': normalized_sign}
                )
            else:
                response = None

            if response:
                embed = discord.Embed(
                    title=f"✨ {normalized_sign}의 오늘 운세",
                    description=response,
                    color=0x9b59b6
                )
                embed.set_footer(text=f"기준 시각: {now.strftime('%Y-%m-%d %H:%M')}")
                await ctx.send(embed=embed)
            else:
                await ctx.send("별들의 목소리가 오늘따라 희미하네요... 잠시 후 다시 시도해주세요.")

    def _normalize_zodiac_name(self, name: str) -> str | None:
        """사용자 입력을 표준 별자리 이름으로 변환합니다."""
        name = name.replace("자리", "").strip()
        mapping = {
            "양": "양자리", "황소": "황소자리", "쌍둥이": "쌍둥이자리", "게": "게자리",
            "사자": "사자자리", "처녀": "처녀자리", "천칭": "천칭자리", "전갈": "전갈자리",
            "사수": "사수자리", "염소": "염소자리", "물병": "물병자리", "물고기": "물고기자리",
            "궁수": "사수자리", "물염소": "염소자리" # 이명 처리
        }
        return mapping.get(name)

    def _get_system_prompt(self, key: str) -> str:
        """프롬프트 템플릿 반환 (추후 prompts.json 연동 가능)"""
        prompts = {
            "fortune_summary": "너는 사용자의 친구이자 개인 비서인 '마사몽'이야. 제공된 운세 데이터를 바탕으로, 오늘의 핵심 운세를 3문장 이내로 밝고 희망차게 요약해줘. 이모지를 적절히 사용해.",
            "fortune_detail": "너는 전문 점성가이자 사주 분석가 '마사몽'이야. 제공된 데이터를 깊이 있게 분석해서 [총평], [재물운], [연애/대인관계], [오늘의 조언] 항목으로 나누어 자세히 설명해줘. 말투는 정중하면서도 친근한 존댓말을 써.",
            "fortune_morning": "너는 사용자의 아침을 여는 든든한 비서 '마사몽'이야. 오늘 하루의 흐름을 예측하고, 주의할 점과 행운의 포인트를 짚어줘. 활기찬 아침 인사를 포함해."
        }
        return prompts.get(key, prompts['fortune_summary'])


    @tasks.loop(minutes=1)
    async def morning_briefing_task(self):
        """
        1. 3분 뒤 전송해야 할 브리핑을 미리 생성 (Pre-generation)
        2. 전송 시간이 된 브리핑을 전송 (Delivery)
        """
        now = datetime.now(pytz.timezone('Asia/Seoul'))
        current_time_str = now.strftime('%H:%M')
        # 3분 뒤 시간 계산
        pre_gen_time_str = (now + timedelta(minutes=3)).strftime('%H:%M')
        today_str = now.strftime('%Y-%m-%d')
        
        try:
            # === [Task 1: Pre-generation] ===
            # 구독 시간이 pre_gen_time_str이고, 오늘 아직 안 보냈고, pending 데이터가 없는 사람
            cursor = await self.bot.db.execute(
                """
                SELECT user_id, birth_date, birth_time 
                FROM user_profiles 
                WHERE subscription_active = 1 
                  AND subscription_time = ? 
                  AND (last_fortune_sent IS NULL OR last_fortune_sent != ?)
                  AND (pending_payload IS NULL)
                """,
                (pre_gen_time_str, today_str)
            )
            pre_gen_users = await cursor.fetchall()
            
            ai_handler = self.bot.get_cog('AIHandler')

            if pre_gen_users and ai_handler:
                for user_id, birth_date, birth_time in pre_gen_users:
                    try:
                        # 유저 정보 가져오기 (닉네임용)
                        user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                        display_name = user.display_name if user else "사용자"

                        # 운세 데이터 생성
                        fortune_data = self.calculator.get_comprehensive_info(birth_date, birth_time)
                        system_prompt = self._get_system_prompt("fortune_morning")
                        user_prompt = f"{fortune_data}\n\n사용자 닉네임: {display_name}\n\n오늘자 모닝 브리핑을 작성해줘. 닉네임을 부르며 친근하게 시작해."
                        
                        briefing = await ai_handler._cometapi_generate_content(
                            system_prompt,
                            user_prompt,
                            log_extra={'user_id': user_id, 'mode': 'morning_briefing_pregen'}
                        )

                        if briefing:
                            # DB에 미리 저장
                            await self.bot.db.execute(
                                "UPDATE user_profiles SET pending_payload = ? WHERE user_id = ?",
                                (briefing, user_id)
                            )
                            await self.bot.db.commit()
                            logger.info(f"브리핑 미리 생성 완료: user={user_id}, time={pre_gen_time_str}")

                    except Exception as e:
                        logger.error(f"브리핑 생성 실패(pre-gen): {user_id}, {e}")

            # === [Task 2: Delivery] ===
            # 구독 시간이 current_time_str이고, 오늘 아직 안 보낸 사람
            cursor = await self.bot.db.execute(
                 """
                SELECT user_id, birth_date, birth_time, pending_payload
                FROM user_profiles 
                WHERE subscription_active = 1 
                  AND subscription_time = ? 
                  AND (last_fortune_sent IS NULL OR last_fortune_sent != ?)
                """,
                (current_time_str, today_str)
            )
            delivery_users = await cursor.fetchall()

            if not delivery_users:
                return

            for user_id, birth_date, birth_time, pending_payload in delivery_users:
                try:
                    user = self.bot.get_user(user_id)
                    if not user:
                         # 캐시에 없으면 fetch 시도
                        try:
                            user = await self.bot.fetch_user(user_id)
                        except:
                            continue
                    
                    final_msg = pending_payload

                    # 만약 미리 생성된 게 없다면(갑자기 시간을 바꿨거나 생성이 실패한 경우) 지금 생성
                    if not final_msg and ai_handler:
                        # ... (동일한 생성 로직 fallback)
                        fortune_data = self.calculator.get_comprehensive_info(birth_date, birth_time)
                        system_prompt = self._get_system_prompt("fortune_morning")
                        user_prompt = f"{fortune_data}\n\n사용자 닉네임: {user.display_name}\n\n오늘자 모닝 브리핑을 작성해줘. 닉네임을 부르며 친근하게 시작해."
                        final_msg = await ai_handler._cometapi_generate_content(
                            system_prompt,
                            user_prompt,
                            log_extra={'user_id': user_id, 'mode': 'morning_briefing_fallback'}
                        )

                    if final_msg:
                        await user.send(f"🌞 **좋은 아침이에요! 오늘의 모닝 브리핑**\n\n{final_msg}")
                        
                        # 전송 완료 처리 및 pending 초기화
                        await self.bot.db.execute(
                            "UPDATE user_profiles SET last_fortune_sent = ?, pending_payload = NULL WHERE user_id = ?",
                            (today_str, user_id)
                        )
                        await self.bot.db.commit()
                        logger.info(f"모닝 브리핑 전송 완료: user={user_id}, time={current_time_str}")

                except Exception as ue:
                    logger.error(f"유저({user_id}) 브리핑 전송 실패: {ue}")

        except Exception as e:
            logger.error(f"모닝 브리핑 태스크 에러: {e}", exc_info=True)

    @morning_briefing_task.before_loop
    async def before_morning_briefing(self):
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot):
    await bot.add_cog(FortuneCog(bot))

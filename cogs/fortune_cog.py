# -*- coding: utf-8 -*-
"""
사용자 개인 운세 및 비서 서비스를 담당하는 Cog입니다.
명령어 처리와 모닝 브리핑 자동 발송 스케줄러를 포함합니다.
"""
from __future__ import annotations

import discord
from discord.ext import commands, tasks
import asyncio
from datetime import datetime, timedelta
import pytz
import re

import config
from database.compat_db import get_table_columns
from logger_config import logger
from utils import db as db_utils
from utils.fortune import FortuneCalculator, get_sign_from_date

# 시간 유효성 검사 정규식 (HH:MM)
TIME_PATTERN = re.compile(r'^([01]\d|2[0-3]):([0-5]\d)$')

class FortuneCog(commands.Cog):
    """운세 관련 기능을 제공하는 Cog입니다."""

    def __init__(self, bot: commands.Bot):
        """FortuneCog를 초기화하고 백그라운드 태스크를 시작합니다."""
        self.bot = bot
        self.calculator = FortuneCalculator()
        self._ready = False
        # 비동기 초기화 작업을 위해 별도 태스크로 실행
        self.bot.loop.create_task(self._ensure_db_schema())
        self.morning_briefing_task.start()
        logger.info("FortuneCog가 성공적으로 초기화되었습니다.")

    async def _ensure_db_schema(self):
        """pending_payload 컬럼이 없으면 추가합니다."""
        await self.bot.wait_until_ready()
        if config.DB_BACKEND == "tidb":
            logger.info("FortuneCog 스키마 점검을 건너뜁니다. TiDB 스키마는 중앙 스키마 파일 기준으로 관리됩니다.")
            self._ready = True
            return
        try:
            columns = await get_table_columns(self.bot.db, "user_profiles")
                
            if 'pending_payload' not in columns:
                logger.info("필요한 컬럼(pending_payload)이 없어 추가합니다.")
                await self.bot.db.execute("ALTER TABLE user_profiles ADD COLUMN pending_payload TEXT")
                await self.bot.db.commit()
                logger.info("Added 'pending_payload' column to user_profiles")

            if 'gender' not in columns:
                logger.info("필요한 컬럼(gender)이 없어 추가합니다.")
                await self.bot.db.execute("ALTER TABLE user_profiles ADD COLUMN gender TEXT")
                await self.bot.db.commit()
                logger.info("Added 'gender' column to user_profiles")

            if 'last_fortune_content' not in columns:
                logger.info("필요한 컬럼(last_fortune_content)이 없어 추가합니다.")
                await self.bot.db.execute("ALTER TABLE user_profiles ADD COLUMN last_fortune_content TEXT")
                await self.bot.db.commit()
                logger.info("Added 'last_fortune_content' column to user_profiles")

            if 'birth_place' not in columns:
                logger.info("필요한 컬럼(birth_place)이 없어 추가합니다.")
                await self.bot.db.execute("ALTER TABLE user_profiles ADD COLUMN birth_place TEXT")
                await self.bot.db.commit()
                logger.info("Added 'birth_place' column to user_profiles")
        except Exception as e:
            logger.error(f"Failed to check/add column: {e}")
        finally:
            self._ready = True

    def cog_unload(self):
        """Cog 언로드 시 아침 브리핑 태스크를 취소합니다."""
        self.morning_briefing_task.cancel()

    @commands.group(name='운세', invoke_without_command=True)
    async def fortune(self, ctx: commands.Context, *, option: str = None):
        """
        운세 관련 종합 기능을 제공합니다. 🔮
        
        사용법:
        - `!운세` : 오늘의 운세 (서버=요약, DM=상세)
        - `!운세 상세` : DM에서 상세 운세
        - `!운세 등록` : 생년월일/시간/성별/출생지 등록 (DM 전용)
        - `!운세 구독 HH:MM` : 매일 아침 운세 브리핑 (DM 전용)
        - `!운세 구독취소` : 구독 해제
        - `!운세 삭제` : 등록된 정보 삭제 (DM 전용)

        예시:
        - `!운세`
        - `!운세 상세`
        - `!운세 구독 07:30`
        """
        if ctx.invoked_subcommand is None:
            # 기존 !운세 (check_fortune) 로직 호출
            status_msg = await ctx.send("🔮 운세를 살펴보는 중이야...")
            await self._check_fortune_logic(ctx, option, status_msg=status_msg)

    @fortune.command(name='등록')
    @commands.dm_only()
    async def fortune_register(self, ctx: commands.Context):
        """
        생년월일/시간/성별/출생지를 대화형으로 등록합니다. (DM 전용)

        사용법:
        - `!운세 등록`

        예시:
        - `!운세 등록`
        """
        # [Safety Lock] 다른 명령어/AI 응답 방지
        self.bot.locked_users.add(ctx.author.id)
        
        try:
            def check(m):
                return m.author == ctx.author and m.channel == ctx.channel

            # 1. 생년월일 입력
            birth_date = None
            while birth_date is None:
                await ctx.send("📝 운세 서비스를 위해 생년월일을 입력해주세요.\n(예: `1990-01-01` - 연도-월-일 순서, 하이픈 필수!)")
                try:
                    msg = await self.bot.wait_for('message', check=check, timeout=60.0)
                    input_date = msg.content.strip()
                    # 날짜 형식 검증
                    datetime.strptime(input_date, '%Y-%m-%d')
                    birth_date = input_date
                except ValueError:
                    await ctx.send("❌ 날짜 형식이 올바르지 않아요!\n**올바른 예시**: `1999-12-25` (반드시 하이픈 `-`을 넣어주세요)\n다시 입력해주세요.")
                except asyncio.TimeoutError:
                    await ctx.send("⏰ 입력 시간이 초과되었어요. `!운세 등록`을 처음부터 다시 시도해주세요.")
                    return

            # 2. 태어난 시간 입력
            birth_time = None
            while birth_time is None:
                await ctx.send("🕒 태어난 시간도 알려주세요. (예: `14:30` - 오후 2시 30분)\n정확히 모르면 `모름`이라고 입력해주세요.")
                try:
                    msg = await self.bot.wait_for('message', check=check, timeout=60.0)
                    birth_time_input = msg.content.strip()
                    if birth_time_input in ['모름', '몰라', 'unknown']:
                        birth_time = "12:00"
                    else:
                        if not TIME_PATTERN.match(birth_time_input):
                             await ctx.send("❌ 시간 형식이 올바르지 않아요!\n**올바른 예시**: `09:30` (오전 9시 반), `23:00` (밤 11시)\n혹은 `모름`이라고 입력해주세요.")
                             continue
                        birth_time = birth_time_input
                except asyncio.TimeoutError:
                     await ctx.send("⏰ 입력 시간이 초과되었어요. `!운세 등록`을 처음부터 다시 시도해주세요.")
                     return



            # 3. 성별 입력
            gender = None
            while gender is None:
                await ctx.send("⚧ 성별을 알려주세요. (입력: `남성` 또는 `여성`)")
                try:
                    msg = await self.bot.wait_for('message', check=check, timeout=60.0)
                    gender_input = msg.content.strip()
                    if gender_input in ['남', '남자', '남성', 'M', 'Male']:
                        gender = 'M'
                    elif gender_input in ['여', '여자', '여성', 'F', 'Female']:
                        gender = 'F'
                    else:
                        await ctx.send("❌ 성별을 정확히 입력해주세요. (`남성` 또는 `여성` 으로만 대답해주세요)")
                        continue
                except asyncio.TimeoutError:
                     await ctx.send("⏰ 입력 시간이 초과되었어요. `!운세 등록`을 처음부터 다시 시도해주세요.")
                     return

            # 4. 태어난 지역 (New)
            birth_place = None
            while birth_place is None:
                await ctx.send("🌍 태어난 지역도 알려주세요. (예: `서울`, `부산`, `뉴욕`, `도쿄`)\n동/읍/면 단위가 아닌 **시/군** 단위로 적어주시면 충분합니다!")
                try:
                    msg = await self.bot.wait_for('message', check=check, timeout=60.0)
                    place_input = msg.content.strip()
                    if len(place_input) < 2:
                        await ctx.send("❌ 지역 이름이 너무 짧아요. 다시 입력해주세요.")
                        continue
                    birth_place = place_input
                except asyncio.TimeoutError:
                     await ctx.send("⏰ 입력 시간이 초과되었어요. `!운세 등록`을 처음부터 다시 시도해주세요.")
                     return

            # DB 저장 (기본적으로 구독은 비활성화 상태로 저장)
            await self._save_user_profile(ctx.author.id, birth_date, birth_time, gender, birth_place)
            await ctx.send(
                f"✅ 정보 등록이 완료되었습니다!\n"
                f"이제 언제든 `!운세` 명령어로 오늘의 운세를 확인하실 수 있습니다.\n\n"
                f"🔔 **매일 아침 운세 브리핑**을 받고 싶다면 `!운세 구독 [시간]` (예: `!운세 구독 07:30`)을 입력해주세요!"
            )
            
        except Exception as e:
            logger.error(f"운세 등록 중 오류: {e}", exc_info=True)
            await ctx.send("❌ 등록 중 오류가 발생했습니다.")
        finally:
            # [Safety Lock Release] 작업 종료 후 반드시 잠금 해제
            self.bot.locked_users.discard(ctx.author.id)

    async def _save_user_profile(self, user_id, birth_date, birth_time, gender, birth_place):
        """DB에 사용자 프로필 저장/업데이트"""
        if config.DB_BACKEND == "tidb":
            query = """
                REPLACE INTO user_profiles (user_id, birth_date, birth_time, gender, birth_place, created_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP(6))
            """
        else:
            query = """
                INSERT OR REPLACE INTO user_profiles (user_id, birth_date, birth_time, gender, birth_place, created_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
            """
        async with self.bot.db.execute(
            query,
            (user_id, birth_date, birth_time, gender, birth_place)
        ):
            await self.bot.db.commit()

    async def _update_last_fortune_context(self, user_id: int, content: str):
        """사용자가 마지막으로 받은 운세 내용을 DB에 저장하여 컨텍스트로 활용"""
        try:
             await self.bot.db.execute(
                "UPDATE user_profiles SET last_fortune_content = ? WHERE user_id = ?",
                (content, user_id)
            )
             await self.bot.db.commit()
        except Exception as e:
            logger.error(f"운세 컨텍스트 저장 실패: {e}")

    @fortune.command(name='삭제')
    async def fortune_delete(self, ctx: commands.Context):
        """
        등록된 모든 정보와 구독 설정을 삭제합니다. (DM 전용)

        사용법:
        - `!운세 삭제`

        예시:
        - `!운세 삭제`
        """
        # DM 체크
        if ctx.guild:
            await ctx.reply("⚠️ 개인 정보 보호를 위해 이 명령어는 DM에서만 사용할 수 있습니다.")
            return

        try:
             async with self.bot.db.execute("DELETE FROM user_profiles WHERE user_id = ?", (ctx.author.id,)):
                 await self.bot.db.commit()
             await ctx.send("🗑️ 모든 개인 정보와 운세 구독 설정이 삭제되었습니다.")
        except Exception as e:
             logger.error(f"운세 정보 삭제 중 오류: {e}", exc_info=True)
             await ctx.send("❌ 삭제 중 오류가 발생했습니다.")

    @fortune.command(name='구독', aliases=['구독시간', '알림시간'])
    async def fortune_subscribe(self, ctx: commands.Context, time_str: str):
        """
        매일 아침 오늘의 운세 브리핑 구독을 설정합니다. (DM 전용)

        사용법:
        - `!운세 구독 HH:MM`

        예시:
        - `!운세 구독 07:30`
        """
        # DM 체크
        if ctx.guild:
            await ctx.reply("⚠️ 구독 설정은 DM에서만 가능합니다.")
            return

        if time_str in ["취소", "해제", "off", "cancel", "중단", "비활성", "비활성화"]:
            await self.fortune_unsubscribe(ctx)
            return

        if not TIME_PATTERN.match(time_str):
            await ctx.send("❌ 올바른 시간 형식이 아닙니다. `HH:MM` (24시간제)로 입력해주세요.\n혹시 구독을 취소하시려면 `!구독 취소`라고 입력해주세요.")
            return
        
        # 5분 여유 확인
        now = datetime.now(pytz.timezone('Asia/Seoul'))
        try:
             target_time = datetime.strptime(time_str, '%H:%M').replace(year=now.year, month=now.month, day=now.day, tzinfo=now.tzinfo)
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
                 await ctx.send("⚠️ 먼저 `!운세 등록`으로 정보를 등록해주세요.")
                 return
             
             await self.bot.db.execute(
                 "UPDATE user_profiles SET subscription_time = ?, subscription_active = 1 WHERE user_id = ?",
                 (time_str, ctx.author.id)
             )
             await self.bot.db.commit()
             await ctx.send(f"✅ 구독이 활성화되었습니다! 매일 아침 `{time_str}`에 브리핑을 보내드릴게요.")
        except Exception as e:
             logger.error(f"구독 설정 중 오류: {e}", exc_info=True)
             await ctx.send("❌ 설정 변경 중 오류가 발생했습니다.")

    @fortune.command(name='구독취소')
    async def fortune_unsubscribe(self, ctx: commands.Context):
        """
        운세 브리핑 구독을 중단합니다. (정보는 유지됨)

        사용법:
        - `!운세 구독취소`

        예시:
        - `!운세 구독취소`
        """
        try:
             await self.bot.db.execute(
                 "UPDATE user_profiles SET subscription_active = 0 WHERE user_id = ?",
                 (ctx.author.id,)
             )
             await self.bot.db.commit()
             await ctx.send("🔕 오늘의 운세 브리핑 구독이 취소되었습니다. (등록된 정보는 유지됩니다.)")
        except Exception as e:
             logger.error(f"구독 취소 중 오류: {e}", exc_info=True)
             await ctx.send("❌ 구독 취소 중 오류가 발생했습니다.")

    @commands.command(name='구독', aliases=['구독시간', '알림시간'])
    async def global_subscribe(self, ctx: commands.Context, time_str: str):
        """
        `!운세 구독`의 별칭 명령어입니다. (DM 전용)

        사용법:
        - `!구독 HH:MM`

        예시:
        - `!구독 08:00`
        """
        await self.fortune_subscribe(ctx, time_str)
    
    @commands.command(name='이번달운세', aliases=['이번달'])
    @commands.dm_only()
    async def monthly_fortune(self, ctx: commands.Context, arg: str = None):
        """
        이번 달의 운세를 확인합니다. (DM 전용, 하루 3회 제한)

        사용법:
        - `!이번달운세`

        예시:
        - `!이번달운세`
        """
        # !이번달 운세 <- 이렇게 띄어쓰기 한 경우 처리
        if arg and arg not in ['운세']:
             return # 다른 명령어일 수 있음
        status_msg = await ctx.send("📅 이번달 운세를 분석 중이야...")
        await self._check_fortune_logic(ctx, mode='month', status_msg=status_msg)

    @commands.command(name='올해운세', aliases=['올해', '신년운세'])
    @commands.dm_only()
    async def yearly_fortune(self, ctx: commands.Context, arg: str = None):
        """
        올해의 운세를 확인합니다. (DM 전용, 하루 3회 제한)

        사용법:
        - `!올해운세`

        예시:
        - `!올해운세`
        """
        # !올해 운세 <- 띄어쓰기 대응
        if arg and arg not in ['운세']:
             return
        status_msg = await ctx.send("🗓️ 올해 운세를 살펴보는 중이야...")
        await self._check_fortune_logic(ctx, mode='year', status_msg=status_msg)

    async def _check_fortune_logic(self, ctx: commands.Context, option: str = None, mode: str = 'day', status_msg: discord.Message = None):
        """오늘의 운세를 분석하여 출력하는 핵심 로직"""
        user_id = ctx.author.id
        is_dm = isinstance(ctx.channel, discord.DMChannel)
        
        # 1. 운세 상세 / 월 / 년 조회 시 제한 체크
        is_detail_request = (option and option.strip() in ['상세', 'detail'])
        usage_check_needed = (mode in ['month', 'year']) or (is_dm and is_detail_request)
        
        if usage_check_needed:
            is_limited, remaining = await db_utils.check_fortune_daily_limit(self.bot.db, user_id)
            if is_limited:
                if status_msg: await status_msg.edit(content=f"⛔ **일일 운세 조회 한도 초과!**\n상세 운세(월/년/상세)는 하루 3회까지만 가능해요.\n내일 다시 찾아와주세요! 🌙")
                else: await ctx.send(f"⛔ **일일 운세 조회 한도 초과!**\n상세 운세(월/년/상세)는 하루 3회까지만 가능해요.\n내일 다시 찾아와주세요! 🌙")
                return

        # 2. 프로필 조회
        cursor = await self.bot.db.execute("SELECT birth_date, birth_time, gender, birth_place FROM user_profiles WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        
        if not row:
            if status_msg:
                msg = "🔮 개인 운세를 보려면 DM으로 `!운세 등록`을 먼저 해주세요!" if ctx.guild else "🔮 아직 정보가 없네요. `!운세 등록`으로 생년월일을 알려주세요!"
                await status_msg.edit(content=msg)
            elif ctx.guild: # 서버에서는 안내만
                await ctx.reply("🔮 개인 운세를 보려면 DM으로 `!운세 등록`을 먼저 해주세요!", mention_author=True)
            else: # DM에서는 바로 유도
                await ctx.send("🔮 아직 정보가 없네요. `!운세 등록`으로 생년월일을 알려주세요!")
            return

        birth_date, birth_time, gender, birth_place = row
        # gender/place fallback for old records
        gender = gender or 'M' 
        birth_place = birth_place or "대한민국" 
        
        # Typing indicator (작성 중 표시)
        async with ctx.typing():
            # 운세 데이터 생성
            fortune_data = self.calculator.get_comprehensive_info(birth_date, birth_time)
            fortune_data += f"\n[Birth Place]: {birth_place}"
            
            # 3. AI 핸들러 호출
            ai_handler = self.bot.get_cog('AIHandler')
            if not ai_handler:
                if status_msg: await status_msg.edit(content="AI 모듈을 불러올 수 없습니다.")
                else: await ctx.send("AI 모듈을 불러올 수 없습니다.")
                return
            
            # 모델명 매핑 (환경변수/설정으로 오버라이드 가능)
            MODEL_LITE = getattr(config, "FORTUNE_MODEL_LITE", "DeepSeek-V3.2-Exp-nothinking")
            MODEL_PRO = getattr(config, "FORTUNE_MODEL_PRO", "DeepSeek-V3.2-Exp-thinking")

            # 별자리 데이터 추가
            try:
                 b_year, b_month, b_day = map(int, birth_date.split('-'))
                 user_sign = get_sign_from_date(b_month, b_day)
                 now = datetime.now(pytz.timezone('Asia/Seoul'))
                 astro_chart = self.calculator._get_astrology_chart(now)
                 fortune_data += f"\n[User Zodiac]: {user_sign}\n[Gender]: {gender}\n[Astro Chart]: {astro_chart}"
            except Exception as e:
                 logger.error(f"Zodiac integration error: {e}")
                 user_sign = "알 수 없음"

            # 프롬프트 설정 (통합)
            display_name = ctx.author.display_name
            
            if mode == 'month':
                period_str = "이번 달"
                prompt_focus = "이번 달의 전반적인 흐름과 주의사항을 알려줘."
            elif mode == 'year':
                period_str = "올해"
                prompt_focus = "올해의 총운과 월별 흐름을 간략히 포함해줘."
            else:
                period_str = "오늘"
                prompt_focus = "오늘의 구체적인 운세 흐름을 알려줘."

            # 채널 vs DM 및 상세 옵션 처리
            # 1. 서버 채널: 무조건 3줄 요약
            # 2. DM (기본): 적당한 요약 (Moderate Summary)
            # 3. DM (상세): 풀버전 상세 분석 (Full Detail)
            
            is_detail_request = (option and option.strip() in ['상세', 'detail'])
            
            if not is_dm and mode == 'day': # [Case 1] 서버 채널
                model_name = MODEL_LITE
                system_prompt = (
                    "너는 '마사몽'이야. 채널(공개된 공간)에서 사용자의 운세를 3줄로 핵심만 요약해서 알려줘. "
                    "구체적인 내용은 DM으로 확인하라고 안내해야 해."
                )
                user_prompt = (
                    f"{fortune_data}\n\n"
                    f"사용자: {display_name} ({gender})\n"
                    f"이 사용자의 오늘의 운세를 **3줄 요약**해줘.\n"
                    f"마지막 줄에는 반드시 '✨ 더 자세한 운세는 저에게 DM으로 `!운세 상세`라고 보내주세요!' 라고 덧붙여줘."
                )
            
            elif is_dm and not is_detail_request and mode == 'day': # [Case 2] DM 기본 (적당한 요약)
                model_name = MODEL_LITE # 또는 PRO 사용하되 프롬프트로 조절
                system_prompt = (
                    "너는 사용자의 친구이자 개인 비서인 '마사몽'이야. "
                    "오늘의 운세를 5~6문장 내외로 핵심만 짚어서 브리핑해줘. "
                    "너무 길지 않게, 하지만 다정하고 명확하게 전달해."
                )
                user_prompt = (
                    f"{fortune_data}\n\n"
                    f"사용자: {display_name} ({gender})\n"
                    f"오늘의 운세 핵심만 브리핑해줘. (총평, 주의할 점, 행운 요소 위주)\n"
                    f"마지막 줄에 '✨ 아주 상세한 전체 분석을 보고 싶다면 `!운세 상세`를 입력해주세요!' 라고 안내해줘."
                )

            else: # [Case 3] DM 상세 or 월/년 운세
                model_name = MODEL_PRO
                system_prompt = (
                    "너는 전문 점성가이자 명리하자인 '마사몽'이야. "
                    "사용자의 운세와 별자리 정보를 깊이 있게 분석해서 상세한 답변을 제공해줘. "
                    "동양(사주)과 서양(별자리) 관점을 종합하고, 성별을 고려하여 섬세하게 조언해줘. "
                    "출력 형식은 가독성 좋은 마크다운(Markdown)을 사용해."
                )
                user_prompt = (
                    f"{fortune_data}\n\n"
                    f"사용자 닉네임: {display_name}\n"
                    f"성별: {gender}\n"
                    f"위 데이터를 바탕으로 {user_sign} 사용자({birth_date})의 {period_str} 운세를 아주 상세하게 분석해줘.\n"
                    f"{prompt_focus}\n"
                    f"항목: [총평], [재물운], [연애/인간관계], [건강운], [마사몽의 행운 팁]"
                )

            # 모델 라우팅
            try:
                 response = await ai_handler._cometapi_generate_content(
                     system_prompt, 
                     user_prompt, 
                     log_extra={'user_id': user_id, 'mode': f'fortune_{mode}'},
                     model=model_name
                 )
                 
                 if response:
                     if status_msg:
                         if len(response) > 1900:
                             await status_msg.edit(content=response[:1900])
                             for i in range(1900, len(response), 1900):
                                 await ctx.send(response[i:i + 1900])
                                 await asyncio.sleep(0.5)
                         else:
                             await status_msg.edit(content=response)
                     else:
                         await self._send_split_message(ctx, response)
                     # DM이고 상세 운세(오늘)인 경우 컨텍스트 저장
                     if is_dm and mode == 'day' and is_detail_request:
                         await self._update_last_fortune_context(user_id, response)
                     
                     # 상세/월/년 운세 성공 시 카운트 증가
                     if usage_check_needed:
                         await db_utils.log_fortune_usage(self.bot.db, user_id)
                         await ctx.send(f"💡 남은 일일 조회 횟수: {remaining - 1}회")
                 else:
                     if status_msg: await status_msg.edit(content="운세 분석에 실패했습니다. (AI 응답 없음)")
                     else: await ctx.send("운세 분석에 실패했습니다. (AI 응답 없음)")
                     
            except Exception as e:
                 logger.error(f"운세 요청 처리 중 오류: {e}", exc_info=True)
                 if status_msg: await status_msg.edit(content="운세 시스템에 문제가 발생했습니다.")
                 else: await ctx.send("운세 시스템에 문제가 발생했습니다.")



    @commands.group(name='별자리', aliases=['운세전체'])
    async def zodiac(self, ctx: commands.Context):
        """
        별자리 운세를 확인합니다. 🌌
        
        사용법:
        - `!별자리` : 내 별자리 운세 (등록 정보가 있으면 자동)
        - `!별자리 <이름>` : 특정 별자리 운세
        - `!별자리 순위` : 오늘의 12별자리 랭킹

        예시:
        - `!별자리`
        - `!별자리 물병자리`
        - `!별자리 순위`
        """
        if ctx.invoked_subcommand is None:
            content = ctx.message.content.strip()
            params = content.split()
            
            # 1. 인자가 있는 경우 (기존 로직 유지)
            if len(params) > 1:
                arg = params[1]
                if arg in ['순위', '랭킹', 'ranking']:
                    await self._show_zodiac_ranking(ctx)
                else:
                    target_sign = arg
                    await self._show_zodiac_fortune(ctx, target_sign)
                return

            # 2. 인자가 없는 경우 -> DB 확인
            target_sign = None
            
            # DB에서 생년월일 조회
            cursor = await self.bot.db.execute("SELECT birth_date FROM user_profiles WHERE user_id = ?", (ctx.author.id,))
            row = await cursor.fetchone()
            
            if row and row[0]:
                try:
                    b_year, b_month, b_day = map(int, row[0].split('-'))
                    target_sign = get_sign_from_date(b_month, b_day)
                    # 등록된 정보로 바로 운세 출력
                    await self._show_zodiac_fortune(ctx, target_sign)
                    return
                except Exception as e:
                    logger.error(f"별자리 자동 조회 실패: {e}")
            
            # 3. 등록된 정보도 없고 인자도 없는 경우 -> 안내 메시지
            embed = discord.Embed(
                title="🌌 오늘의 별자리 운세",
                description=(
                    "**내 별자리 운세를 보고 싶다면?**\n"
                    "👉 `!운세 등록` 으로 생년월일을 알려주세요! (자동으로 인식됩니다)\n\n"
                    "**특정 별자리를 보고 싶다면?**\n"
                    "👉 `!별자리 <이름>` (예: `!별자리 물병자리`)\n\n"
                    "**12별자리 순위가 궁금하다면?**\n"
                    "👉 `!별자리 순위`\n\n"
                    "**목록**: 양, 황소, 쌍둥이, 게, 사자, 처녀\n천칭, 전갈, 사수, 염소, 물병, 물고기"
                ),
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
            "출력 형식은 마크다운(##, **)을 사용하여 매우 깔끔하고 보기 좋게 보여줘."
        )
        user_prompt = (
            f"[현재 천체 배치]\n{astro_chart}\n\n"
            f"오늘의 12별자리 운세 순위를 알려줘. "
            f"상위권(1~3위)은 🌟, 중위권(4~9위)은 😐, 하위권(10~12위)은 ☁️ 이모지를 사용하여 리스트 형식으로 분류해줘. "
            f"각 별자리마다 행운의 팁(색상, 숫자)도 포함해줘."
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

        is_dm = isinstance(ctx.channel, discord.DMChannel)
        now = datetime.now(pytz.timezone('Asia/Seoul'))
        astro_chart = self.calculator._get_astrology_chart(now)

        # 2. 채널 vs DM 분기 (프롬프트 차별화)
        if not is_dm:
            # [Channel] 요약 버전
            system_prompt = (
                "너는 '마사몽'이야. 공개된 채널에서는 별자리 운세를 **3줄로 핵심만 요약**해서 알려줘. "
                "구체적인 내용은 DM으로 확인하라고 안내해."
            )
            user_prompt = (
                f"[현재 천체 배치]\n{astro_chart}\n\n"
                f"[타겟 별자리]: {normalized_sign}\n"
                f"[사용자 이름]: {ctx.author.display_name}\n\n"
                f"오늘 {normalized_sign}의 운세를 3줄로 요약해줘.\n"
                f"마지막에는 '✨ 더 자세한 별자리 분석은 DM으로 `!별자리 {normalized_sign}`을 입력해보세요!' 라고 덧붙여줘."
            )
        else:
            # [DM] 상세 버전
            system_prompt = (
                "당신은 친절하고 통찰력 있는 '점성술사 마사몽'입니다. "
                "현재 천체 배치(Transit)를 바탕으로 특정 별자리의 오늘 운세를 상세히 분석해줍니다. "
                "추상적인 표현보다는 실질적인 조언 위주로, 다정하고 희망찬 어조를 유지하세요. "
                "출력은 마크다운 형식을 사용하여 가독성을 높이세요."
            )
            user_prompt = (
                f"[현재 천체 배치]\n{astro_chart}\n\n"
                f"[타겟 별자리]: {normalized_sign}\n"
                f"[사용자 이름]: {ctx.author.display_name}\n\n"
                f"오늘 {normalized_sign} 사람들을 위한 상세한 운세를 작성해주세요. "
                f"사용자 이름을 자연스럽게 불러주세요.\n"
                f"가독성을 위해 마크다운(##, **, -)을 적극 활용하고, 다음 항목을 포함하세요:\n"
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
                    title=f"✨ {normalized_sign}의 오늘 운세 ({'요약' if not is_dm else '상세'})",
                    description=response,
                    color=0x9b59b6
                )
                embed.set_footer(text=f"기준 시각: {now.strftime('%Y-%m-%d %H:%M')}")
                if len(response) > 4000: # 임베드 제한 초과 시 분할 텍스트로 보냄
                     await self._send_split_message(ctx, response)
                else:
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
            "fortune_summary": (
                "너는 사용자의 친구이자 개인 비서인 '마사몽'이야. 제공된 운세 데이터를 바탕으로, "
                "오늘의 핵심 운세를 요약해줘. 마크다운(**)을 사용해. 이모지를 적절히 사용해."
            ),
            "fortune_detail": (
                "너는 전문 점성가이자 명리하자인 '마사몽'이야. 제공된 데이터를 깊이 있게 분석해서 "
                "[총평], [재물운], [연애/대인관계], [오늘의 조언] 항목으로 나누어 자세히 설명해줘. "
                "마크다운(##, **)을 사용하여 가독성 있게 작성해."
            ),
            "fortune_morning": (
                "너는 사용자의 아침을 여는 든든한 비서 '마사몽'이야. 오늘 하루의 흐름을 예측하고, "
                "주의할 점과 행운의 포인트를 짚어줘. 닉네임을 꼭 부르며 다정하게 인사해.\n"
                "중요: '행운의 시간'을 추천할 때는 7시 30분에 집착하지 말고, 천체 배치나 운세 기운에 맞춰 매번 다르게 추천해줘. "
                "마크다운을 활용해 예쁘게 작성해."
            )
        }
        return prompts.get(key, prompts['fortune_summary'])

    async def _send_split_message(self, destination, text: str):
        """2000자 초과 메시지 분할 전송 (destination: ctx or user or channel)"""
        if not text: return
        chunk_size = 1900
        for i in range(0, len(text), chunk_size):
            await destination.send(text[i:i + chunk_size])
            await asyncio.sleep(0.5)


    @tasks.loop(minutes=1)
    async def morning_briefing_task(self):
        """
        1. 3분 뒤 전송해야 할 브리핑을 미리 생성 (Pre-generation)
        2. 전송 시간이 된 브리핑을 전송 (Delivery)
        """
        await self.bot.wait_until_ready()
        # DB 컬럼 추가 등 초기화가 완료될 때까지 대기
        while not self._ready:
            await asyncio.sleep(1)
            
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
                SELECT user_id, birth_date, birth_time, gender
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
                for user_id, birth_date, birth_time, gender in pre_gen_users:
                    try:
                        # 유저 정보 가져오기 (닉네임용)
                        user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                        display_name = user.display_name if user else "사용자"
                        gender = gender or 'M'

                        # 운세 데이터 생성
                        fortune_data = self.calculator.get_comprehensive_info(birth_date, birth_time)
                        system_prompt = self._get_system_prompt("fortune_morning")
                        user_prompt = (
                            f"{fortune_data}\n\n"
                            f"사용자: {display_name} ({gender})\n\n"
                            f"오늘자 모닝 브리핑을 작성해줘. 첫머리에 '{display_name}님, 좋은 아침이에요!'와 같은 인사를 꼭 포함해줘. "
                            f"데이터를 바탕으로 구체적이고 다정한 조언을 해줘. 마크다운 스타일을 적용해줘."
                        )
                        
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
                SELECT user_id, birth_date, birth_time, gender, pending_payload
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

            for user_id, birth_date, birth_time, gender, pending_payload in delivery_users:
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
                        gender = gender or 'M'
                        fortune_data = self.calculator.get_comprehensive_info(birth_date, birth_time)
                        system_prompt = self._get_system_prompt("fortune_morning")
                        user_prompt = (
                            f"{fortune_data}\n\n"
                            f"사용자: {user.display_name} ({gender})\n\n"
                            f"오늘자 모닝 브리핑을 작성해줘. 첫머리에 '{user.display_name}님, 좋은 아침이에요!'와 같은 인사를 꼭 포함해줘. "
                            f"데이터를 바탕으로 구체적이고 다정한 조언을 해줘. 마크다운 스타일을 적용해줘."
                        )
                        final_msg = await ai_handler._cometapi_generate_content(
                            system_prompt,
                            user_prompt,
                            log_extra={'user_id': user_id, 'mode': 'morning_briefing_fallback'}
                        )

                    if final_msg:
                        message_header = f"🌞 **좋은 아침이에요! 오늘의 모닝 브리핑**\n\n"
                        full_message = message_header + final_msg
                        await self._send_split_message(user, full_message)
                        
                        # 전송 완료 처리 및 pending 초기화
                        await self.bot.db.execute(
                            "UPDATE user_profiles SET last_fortune_sent = ?, pending_payload = NULL WHERE user_id = ?",
                            (today_str, user_id)
                        )
                        # 컨텍스트 업데이트 [NEW]
                        await self._update_last_fortune_context(user_id, final_msg)
                        
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

# -*- coding: utf-8 -*-
"""
사용자가 직접 호출할 수 있는 일반 명령어들을 관리하는 Cog입니다.
주로 관리 및 정보 조회용 명령어가 포함됩니다.
"""

import discord
from discord.ext import commands
import os
import io
import asyncio
import json
import time
from collections import OrderedDict
from pathlib import Path

import config
from logger_config import logger
from utils.discord_helpers import clip_discord_text

class UserCommands(commands.Cog):
    """사용자 명령어들을 그룹화하는 클래스입니다."""

    def __init__(self, bot: commands.Bot):
        """UserCommands Cog를 초기화합니다."""
        self.bot = bot
        self._update_cache: OrderedDict[str, tuple[float, str | None]] = OrderedDict()
        self._update_cache_lock = asyncio.Lock()
        self._update_user_cooldowns: OrderedDict[int, float] = OrderedDict()
        self._update_cache_ttl_seconds = max(
            0,
            int(getattr(config, "UPDATE_INFO_CACHE_TTL_SECONDS", 300)),
        )
        self._update_cache_max_entries = max(
            1,
            int(getattr(config, "UPDATE_INFO_CACHE_MAX_ENTRIES", 8)),
        )
        self._update_user_cooldown_seconds = max(
            0,
            int(getattr(config, "UPDATE_INFO_USER_COOLDOWN_SECONDS", 30)),
        )
        self._update_user_cooldown_max_entries = max(
            32,
            int(getattr(config, "UPDATE_INFO_COOLDOWN_MAX_ENTRIES", 2048)),
        )
        logger.info("UserCommands Cog가 성공적으로 초기화되었습니다.")

    @commands.command(name='delete_log', aliases=['로그삭제'])
    @commands.has_permissions(administrator=True) # 관리자 권한이 있는 사용자만 실행 가능
    @commands.guild_only() # 서버 채널에서만 사용 가능
    async def delete_log(self, ctx: commands.Context):
        """
        봇의 로그 파일을 삭제합니다. (관리자 전용, 서버 전용)

        사용법:
        - `!delete_log`

        예시:
        - `!delete_log`

        참고:
        - `config.LOG_FILE_NAME`에 정의된 파일을 삭제합니다.
        """
        log_filename = config.LOG_FILE_NAME
        log_extra = {'guild_id': ctx.guild.id, 'author_id': ctx.author.id}
        try:
            if os.path.exists(log_filename):
                os.remove(log_filename)
                await ctx.send(config.MSG_DELETE_LOG_SUCCESS.format(filename=log_filename))
                logger.info(f"로그 파일 '{log_filename}'이(가) 삭제되었습니다.", extra=log_extra)
            else:
                await ctx.send(config.MSG_DELETE_LOG_NOT_FOUND.format(filename=log_filename))
                logger.warning(f"삭제할 로그 파일 '{log_filename}'을(를) 찾을 수 없습니다.", extra=log_extra)
        except Exception as e:
            await ctx.send(config.MSG_DELETE_LOG_ERROR)
            logger.error(f"로그 파일 삭제 중 오류 발생: {e}", exc_info=True, extra=log_extra)

    @delete_log.error
    async def delete_log_error(self, ctx: commands.Context, error):
        """`delete_log` 명령어에서 발생하는 특정 오류를 처리합니다."""
        log_extra = {'guild_id': ctx.guild.id, 'author_id': ctx.author.id}
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(config.MSG_CMD_NO_PERM)
            logger.warning(f"사용자가 권한 없이 `delete_log` 명령어를 시도했습니다.", extra=log_extra)
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send(config.MSG_CMD_GUILD_ONLY)
        else:
            logger.error(f"`delete_log` 명령어 처리 중 예기치 않은 오류 발생: {error}", exc_info=True, extra=log_extra)
            await ctx.send(config.MSG_CMD_ERROR)


    
    @commands.command(name='이미지', aliases=['image', 'img', '그림', '생성'])
    @commands.guild_only()
    @commands.cooldown(
        1,
        config.IMAGE_COMMAND_COOLDOWN_SECONDS,
        commands.BucketType.user,
    )
    async def generate_image_command(self, ctx: commands.Context, *, prompt: str = None):
        """
        AI로 이미지를 생성합니다. (서버 전용)

        사용법:
        - `!이미지 <설명>`

        예시:
        - `!이미지 파란 하늘을 나는 귀여운 아기 고양이`
        - `!이미지 사이버펑크 스타일의 서울 야경`

        참고:
        - 이미지 생성은 `COMETAPI_IMAGE_API_KEY`(미설정 시 `COMETAPI_KEY` fallback)가 필요합니다.
        - 유저/전역 생성 횟수 제한이 있습니다.
        """
        log_extra = {'guild_id': ctx.guild.id, 'author_id': ctx.author.id}
        
        if not prompt:
            await ctx.send("❌ 그림에 대한 설명이 빠졌어요!\n**올바른 사용법**: `!이미지 <설명>`\n(예: `!이미지 우주복을 입은 햄스터`)")
            return
        
        # 이미지 생성 기능 활성화 확인
        if not getattr(config, 'COMETAPI_IMAGE_ENABLED', False):
            await ctx.send("❌ 이미지 생성 기능이 현재 관리자에 의해 비활성화되어 있어요.")
            return
        
        # AI 핸들러 가져오기
        ai_handler = self.bot.get_cog('AIHandler')
        if not ai_handler or not ai_handler.tools_cog:
            await ctx.send("❌ AI 시스템이 아직 준비되지 않았어요. 잠시 후 다시 시도해주세요!")
            return

        # 제한을 이미 넘은 사용자는 프롬프트 최적화 LLM을 호출하지 않는다.
        quota = await ai_handler.tools_cog.check_image_quota(ctx.author.id)
        if not quota.get("allowed"):
            await ctx.send(f"❌ {quota.get('error') or '이미지 생성 제한에 도달했어요.'}")
            return
        
        async with ctx.typing():
            status_msg = None
            try:
                # 생성 중 메시지 전송
                status_msg = await ctx.send(f"🎨 **'{prompt}'**\n위 설명으로 그림을 그리고 있어요... (최대 1분 30초 정도 걸릴 수 있으니 잠시만 기다려줘...)")
                
                # 1. 프롬프트 최적화 (LLM으로 한국어→영문 최적화 프롬프트 생성)
                log_extra_img = {'guild_id': ctx.guild.id, 'author_id': ctx.author.id}
                optimized_prompt = await ai_handler._generate_image_prompt(prompt, log_extra_img)
                image_prompt = optimized_prompt or prompt
                
                if optimized_prompt and optimized_prompt != prompt:
                    await status_msg.edit(content=f"🎨 **'{prompt}'**\n→ 최적화 프롬프트: `{optimized_prompt[:200]}`\n그림을 그리고 있어요... (최대 1분 30초 소요)")
                
                # 2. 이미지 생성 (tools_cog 직접 호출)
                result = await ai_handler.tools_cog.generate_image(
                    prompt=image_prompt,
                    user_id=ctx.author.id
                )
                
                # 3. 결과 처리
                if result.get('image_data') or result.get('image_url'):
                    remaining = result.get('remaining', 0)
                    
                    # 상태 메시지 삭제
                    try:
                        await status_msg.delete()
                    except:
                        pass
                    
                    # 이미지 바이너리가 있으면 파일로 직접 업로드 (URL 만료 방지)
                    if result.get('image_data'):
                        image_file = discord.File(
                            io.BytesIO(result['image_data']),
                            filename="generated_image.jpg"
                        )
                        await ctx.reply(
                            f"짜잔~ 요청하신 이미지가 완성되었어요! 🎨\n(남은 이미지 생성 횟수: {remaining}장)",
                            file=image_file,
                            mention_author=False
                        )
                    else:
                        # 폴백: URL로 전송
                        await ctx.reply(
                            f"짜잔~ 요청하신 이미지가 완성되었어요! 🎨\n{result['image_url']}\n\n(남은 이미지 생성 횟수: {remaining}장)",
                            mention_author=False
                        )
                    
                    logger.info(f"이미지 생성 성공 (명령어): user={ctx.author.id}", extra=log_extra)
                    
                elif result.get('error'):
                    await status_msg.edit(content=f"😅 이미지 생성 실패: {result['error']}")
                else:
                    await status_msg.edit(content="❌ 이미지 생성 중 알 수 없는 오류가 발생했어요.")
                    
            except Exception as e:
                logger.error(f"이미지 생성 명령어 오류: {e}", exc_info=True, extra=log_extra)
                try:
                    if status_msg is not None:
                        await status_msg.edit(content="❌ 처리 중 오류가 발생했어요. 잠시 후 다시 시도해 주세요!")
                    else:
                        await ctx.send("❌ 처리 중 오류가 발생했어요. 잠시 후 다시 시도해 주세요!")
                except:
                    await ctx.send("❌ 처리 중 오류가 발생했어요. 잠시 후 다시 시도해 주세요!")
    
    @generate_image_command.error
    async def generate_image_error(self, ctx: commands.Context, error):
        """`이미지` 명령어의 오류를 처리합니다."""
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.send("❌ 이 명령어는 서버 채널에서만 사용할 수 있어요!")
        elif isinstance(error, commands.CommandOnCooldown):
            await ctx.send(
                f"⏳ 이미지 요청은 잠깐 쉬어가야 해요. "
                f"{max(1, int(error.retry_after + 0.999))}초 뒤에 다시 시도해줘!"
            )
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("❌ 그림에 대한 설명이 빠졌어요!\n**사용법**: `!이미지 <설명>` (예: `!이미지 귀여운 고양이`)")

    def _consume_update_cooldown(
        self,
        user_id: int,
        *,
        now: float | None = None,
    ) -> tuple[bool, int]:
        """사용자별 업데이트 명령 쿨다운을 소비합니다."""
        cooldown = self._update_user_cooldown_seconds
        if cooldown <= 0:
            return True, 0

        now = time.monotonic() if now is None else float(now)
        user_id = int(user_id)
        previous = self._update_user_cooldowns.get(user_id)
        if previous is not None:
            remaining = cooldown - (now - previous)
            if remaining > 0:
                self._update_user_cooldowns.move_to_end(user_id)
                return False, max(1, int(remaining + 0.999))

        self._update_user_cooldowns[user_id] = now
        self._update_user_cooldowns.move_to_end(user_id)
        while len(self._update_user_cooldowns) > self._update_user_cooldown_max_entries:
            self._update_user_cooldowns.popitem(last=False)
        return True, 0

    def _get_cached_update_summary(
        self,
        head_sha: str,
        *,
        now: float | None = None,
    ) -> tuple[bool, str | None]:
        """HEAD별 TTL/LRU 캐시를 조회합니다(None 결과도 캐시 가능)."""
        if self._update_cache_ttl_seconds <= 0:
            return False, None
        entry = self._update_cache.get(head_sha)
        if entry is None:
            return False, None

        now = time.monotonic() if now is None else float(now)
        cached_at, summary = entry
        if now - cached_at >= self._update_cache_ttl_seconds:
            self._update_cache.pop(head_sha, None)
            return False, None

        self._update_cache.move_to_end(head_sha)
        return True, summary

    def _store_update_summary(
        self,
        head_sha: str,
        summary: str | None,
        *,
        now: float | None = None,
    ) -> None:
        """HEAD별 요약 결과를 bounded LRU에 저장합니다."""
        if self._update_cache_ttl_seconds <= 0:
            return
        now = time.monotonic() if now is None else float(now)
        self._update_cache[head_sha] = (now, summary)
        self._update_cache.move_to_end(head_sha)
        while len(self._update_cache) > self._update_cache_max_entries:
            self._update_cache.popitem(last=False)

    @staticmethod
    async def _run_git(*args: str, timeout_seconds: float = 5.0) -> str:
        """로컬 git 명령을 짧은 상한 내에서 실행합니다."""
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.communicate()
            raise RuntimeError(f"git {' '.join(args)} 실행 시간이 초과되었습니다.")

        if process.returncode != 0:
            error_text = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(error_text or f"git {' '.join(args)} 실행 실패")
        return stdout.decode("utf-8", errors="replace").strip()

    @staticmethod
    def _load_release_metadata() -> tuple[str, str] | None:
        """불변 배포 아카이브의 커밋 SHA와 최근 로그를 읽습니다."""
        configured = str(os.getenv("MASAMONG_RELEASE_METADATA_FILE", "") or "").strip()
        candidates = []
        if configured:
            candidates.append(Path(configured))
        candidates.append(Path(__file__).resolve().parents[1] / ".release-metadata.json")

        for path in candidates:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except (OSError, ValueError, TypeError) as exc:
                logger.warning("배포 메타데이터 읽기 실패(%s): %s", path, exc)
                continue
            if not isinstance(payload, dict):
                continue
            head_sha = str(payload.get("commit_sha") or "").strip()
            raw_commits = payload.get("commits") or []
            if not head_sha or not isinstance(raw_commits, list):
                continue
            commits = [
                f"- {str(item).strip()[:240]}"
                for item in raw_commits[:10]
                if str(item).strip()
            ]
            return head_sha, "\n".join(commits)
        return None

    async def _get_update_summary(self, ai_handler) -> str | None:
        """현재 HEAD의 최근 커밋을 한 번만 AI 요약하고 TTL 동안 재사용합니다."""
        release_metadata = self._load_release_metadata()
        metadata_logs = ""
        if release_metadata is not None:
            head_sha, metadata_logs = release_metadata
        else:
            try:
                head_sha = await self._run_git("rev-parse", "HEAD")
            except Exception as exc:
                logger.warning("업데이트 HEAD 조회 실패: %s", exc)
                head_sha = "__unknown_head__"

        cache_hit, cached_summary = self._get_cached_update_summary(head_sha)
        if cache_hit:
            return cached_summary

        # 동시 첫 요청이 같은 git log와 LLM 호출을 중복 실행하지 않게 한다.
        async with self._update_cache_lock:
            cache_hit, cached_summary = self._get_cached_update_summary(head_sha)
            if cache_hit:
                return cached_summary

            git_logs = metadata_logs
            if not git_logs:
                try:
                    git_logs = await self._run_git(
                        "log",
                        "-n",
                        "10",
                        "--pretty=format:- %s",
                    )
                except Exception as exc:
                    logger.warning("업데이트 git log 조회 실패: %s", exc)
                    git_logs = ""

            summary = None
            if git_logs and ai_handler:
                prompt = (
                    "다음은 최근 시스템의 깃 커밋 로그 문구들이야.\n"
                    "이 내용을 바탕으로 사용자들에게 알려줄 친근하고 귀여운 "
                    "'업데이트 소식'을 작성해줘.\n"
                    "형식은 마크다운 불렛 포인트로 간결하게 작성하고, "
                    "말투는 마사몽 답게(~어, ~해 등) 해줘.\n\n"
                    f"커밋 로그:\n{git_logs}"
                )
                system_role = (
                    "너는 친절하고 귀여운 챗봇 '마사몽'이야. "
                    "시스템 변경 사항을 사용자에게 알기 쉽게 요약해서 전달해주는 역할을 해."
                )
                try:
                    summary = await ai_handler.get_ai_completion(
                        prompt,
                        system_role=system_role,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("업데이트 AI 요약 실패(폴백 캐시): %s", exc)
                    summary = None
                if summary:
                    summary = str(summary).strip()[:4000]

            # 실패(None)도 TTL 동안 캐시해 장애 시 반복 LLM 폭주를 막는다.
            self._store_update_summary(head_sha, summary)
            return summary

    @commands.command(name='업데이트', aliases=['update', '패치노트'])
    async def update_info(self, ctx: commands.Context):
        """
        최근 추가된 기능과 변경 사항을 알려줍니다. (Git 로그 자동 요약)
        """
        log_extra = {'guild_id': ctx.guild.id if ctx.guild else 0, 'author_id': ctx.author.id}
        allowed, remaining_seconds = self._consume_update_cooldown(ctx.author.id)
        if not allowed:
            await ctx.send(
                f"⏳ 업데이트 소식은 방금 확인했어! "
                f"{remaining_seconds}초 뒤에 다시 불러줘."
            )
            return
        
        async with ctx.typing():
            try:
                ai_handler = self.bot.get_cog('AIHandler')
                summary = await self._get_update_summary(ai_handler)
                if summary:
                    embed = discord.Embed(
                        title="🚀 마사몽 업데이트 소식 (자동 요약)",
                        description=clip_discord_text(summary, 4096),
                        color=0xff6b6b,
                    )
                    embed.set_footer(text="최근 깃허브 변경 내역을 바탕으로 생성되었습니다.")
                    await ctx.send(embed=embed)
                    return

                # git/AI가 실패하면 안전한 고정 메시지로 폴백한다.
                embed = discord.Embed(
                    title="🚀 마사몽 업데이트 소식",
                    description="최근 추가된 따끈따끈한 기능들을 소개할게요!",
                    color=0xff6b6b
                )
                embed.add_field(
                    name="✨ 커스텀 이모지 지원 & 성능 최적화",
                    value=(
                        "- 이제 제가 서버만의 **특별한 커스텀 이모지**를 대화 중에 사용할 수 있어요! 🥰\n"
                        "- 불필요한 데이터를 줄여서 훨씬 **가볍고 빠르게** 대답하도록 최적화했습니다.\n"
                        "- 각종 연결 오류 및 인텐트 버그를 수정하여 **더욱 안정적인** 모습으로 돌아왔어요!\n\n"
                        "*최근 상세 변경 내역을 불러오지 못했습니다. 위 주요 업데이트 사항을 확인해 주세요!*"
                    ),
                    inline=False
                )
                embed.set_footer(text="자세한 내용은 !도움 명령어를 참고해주세요.")
                await ctx.send(embed=embed)

            except Exception as e:
                logger.error(f"업데이트 명령어 처리 중 오류: {e}", exc_info=True, extra=log_extra)
                await ctx.send("❌ 업데이트 정보를 가져오는 중 오류가 발생했어요.")

async def setup(bot: commands.Bot):
    """Cog를 봇에 등록하는 함수입니다."""
    await bot.add_cog(UserCommands(bot))

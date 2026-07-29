"""기존 Cog 핵심 경로의 오프라인 smoke 테스트.

범위와 경계:

* FunCog: 쿨다운/요약 캐시와 캐시 재사용
* PollCog: 찬반·다중 선택·입력 상한
* MaintenanceCog: 단일 archive/BM25 틱과 메시지 시각 추적
* SettingsCog: 설정 저장, 런타임 캐시 반영, 허용 채널 갱신
* ProactiveAssistant: 준비 상태와 키워드별 정적 제안

Discord gateway, 외부 API, 운영 DB에는 연결하지 않습니다. 네트워크나 실제 스케줄
정확성 대신 각 기능이 한 번 호출됐을 때의 결정·부작용 경계를 고정합니다.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import config
from cogs.fun_cog import FunCog, SummaryCacheEntry
from cogs.help_cog import MasamongHelpCommand
from cogs.maintenance_cog import MaintenanceCog
from cogs.poll_cog import PollCog
from cogs.proactive_assistant import ProactiveAssistant
from cogs.settings_cog import SettingsCog


class _AsyncTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


class _RecordingMessage:
    def __init__(self) -> None:
        self.reactions: list[str] = []

    async def add_reaction(self, emoji: str) -> None:
        self.reactions.append(emoji)


class _RecordingContext:
    def __init__(self) -> None:
        self.author = SimpleNamespace(display_name="테스터")
        self.guild = SimpleNamespace(id=123)
        self.sent: list[dict] = []

    async def send(self, content=None, *, embed=None):
        message = _RecordingMessage()
        self.sent.append({"content": content, "embed": embed, "message": message})
        return message


class _RecordingResponse:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.deferred = 0

    async def defer(self, **_kwargs):
        self.deferred += 1

    async def send_message(self, content=None, *, embed=None, ephemeral=False):
        self.messages.append(
            {"content": content, "embed": embed, "ephemeral": ephemeral}
        )

    async def send(self, content=None, *, embed=None, ephemeral=False):
        await self.send_message(content, embed=embed, ephemeral=ephemeral)


@pytest.mark.asyncio
async def test_help_uses_the_active_instance_prefix_and_privacy_category():
    sent = []

    class _Destination:
        async def send(self, *, embed):
            sent.append(embed)

    class _PrivacyCog:
        qualified_name = "PrivacyCog"
        description = "목적별 개인정보 동의를 관리합니다."

    help_command = MasamongHelpCommand()
    help_command.context = SimpleNamespace(
        clean_prefix="?",
        bot=SimpleNamespace(
            user=SimpleNamespace(display_name="일반 마사몽", avatar=None)
        ),
    )
    help_command.get_destination = lambda: _Destination()
    privacy_cog = _PrivacyCog()
    await help_command.send_bot_help(
        {
            privacy_cog: [
                SimpleNamespace(name="개인정보", hidden=False),
            ]
        }
    )

    assert len(sent) == 1
    embed = sent[0]
    assert "`?`로 시작" in embed.description
    assert "`?도움`" in embed.description
    assert embed.fields[0].name == "**🔐 개인정보 동의**"
    assert "`?개인정보`" in embed.fields[0].value


def _new_fun_cog() -> FunCog:
    cog = FunCog.__new__(FunCog)
    cog.bot = SimpleNamespace()
    cog.ai_handler = None
    cog.keyword_cooldowns = {}
    cog.summary_cache = {}
    return cog


def test_fun_cooldown_and_summary_cache_evict_oldest(monkeypatch):
    monkeypatch.setattr(
        config,
        "FUN_KEYWORD_TRIGGERS",
        {"enabled": True, "cooldown_seconds": 60, "triggers": {}},
    )
    monkeypatch.setattr(config, "SUMMARY_CACHE_MAX_CHANNELS", 2, raising=False)
    cog = _new_fun_cog()

    assert cog.is_on_cooldown(10) is False
    cog.update_cooldown(10)
    assert cog.is_on_cooldown(10) is True

    now = datetime.now()
    cog.summary_cache = {
        1: SummaryCacheEntry(1, "old", now - timedelta(minutes=2)),
        2: SummaryCacheEntry(2, "middle", now - timedelta(minutes=1)),
        3: SummaryCacheEntry(3, "new", now),
    }
    cog._trim_summary_cache()

    assert set(cog.summary_cache) == {2, 3}


@pytest.mark.asyncio
async def test_fun_summary_reuses_unchanged_cache_without_llm(monkeypatch):
    monkeypatch.setattr(config, "AI_MEMORY_ENABLED", True)
    channel = SimpleNamespace(
        id=456,
        guild=SimpleNamespace(id=123),
        typing=lambda: _AsyncTyping(),
    )
    status_updates: list[str] = []

    async def edit_status(*, content, **_kwargs):
        status_updates.append(content)

    class _AI:
        is_ready = True

        async def get_latest_conversation_message_id(self, _guild_id, _channel_id):
            return 100

        async def count_recent_conversation_messages(self, *_args, **_kwargs):
            return 0

        async def get_recent_conversation_text(self, *_args, **_kwargs):
            raise AssertionError("변경 없는 캐시는 전체 대화를 다시 읽으면 안 됩니다.")

        async def generate_creative_text(self, **_kwargs):
            raise AssertionError("변경 없는 캐시는 LLM을 다시 호출하면 안 됩니다.")

    cog = _new_fun_cog()
    cog.ai_handler = _AI()
    cog.summary_cache[channel.id] = SummaryCacheEntry(
        anchor_message_id=90,
        summary_text="기존 요약",
        updated_at=datetime.now(),
    )

    await cog.execute_summarize(
        channel,
        SimpleNamespace(id=7),
        status_msg=SimpleNamespace(edit=edit_status),
    )

    assert status_updates == ["**📈 최근 대화 요약 (마사몽 ver.)**\n기존 요약"]
    assert cog.summary_cache[channel.id].anchor_message_id == 100


@pytest.mark.asyncio
async def test_poll_yes_no_and_multiple_choice_reactions():
    cog = PollCog.__new__(PollCog)
    cog.bot = SimpleNamespace()

    yes_no = _RecordingContext()
    await PollCog.poll.callback(cog, yes_no, "지금 회의할까?")
    assert yes_no.sent[0]["embed"].title == "🗳️ 지금 회의할까?"
    assert yes_no.sent[0]["message"].reactions == ["⭕", "❌"]

    multiple = _RecordingContext()
    await PollCog.poll.callback(cog, multiple, "점심", "한식", "중식", "일식")
    assert multiple.sent[0]["embed"].description == (
        "1️⃣ 한식\n\n2️⃣ 중식\n\n3️⃣ 일식"
    )
    assert multiple.sent[0]["message"].reactions == ["1️⃣", "2️⃣", "3️⃣"]


@pytest.mark.asyncio
async def test_poll_rejects_missing_question_and_more_than_ten_choices():
    cog = PollCog.__new__(PollCog)
    cog.bot = SimpleNamespace()

    missing = _RecordingContext()
    await PollCog.poll.callback(cog, missing, None)
    assert "투표 주제가 없어요" in missing.sent[0]["content"]

    excessive = _RecordingContext()
    await PollCog.poll.callback(
        cog,
        excessive,
        "너무 많은 선택지",
        *(str(index) for index in range(11)),
    )
    assert len(excessive.sent) == 1
    assert "최대 10개" in excessive.sent[0]["content"]


def _new_maintenance_cog() -> MaintenanceCog:
    cog = MaintenanceCog.__new__(MaintenanceCog)
    cog.bot = SimpleNamespace(db=object())
    cog._last_conversation_ts = None
    cog._last_bm25_rebuild_ts = None
    cog._archive_first_tick_pending = True
    cog._bm25_auto_enabled = True
    return cog


@pytest.mark.asyncio
async def test_maintenance_archive_first_tick_skips_then_runs_once(monkeypatch):
    calls: list[tuple] = []

    async def archive(db):
        calls.append(("archive", db))

    async def prune(db, days):
        calls.append(("prune", db, days))

    monkeypatch.setattr("cogs.maintenance_cog.db_utils.archive_old_conversations", archive)
    monkeypatch.setattr("cogs.maintenance_cog.db_utils.prune_user_activity_log", prune)
    monkeypatch.setattr(
        config,
        "RAG_ARCHIVING_CONFIG",
        {"run_on_startup": False, "activity_log_retention_days": 30},
    )
    cog = _new_maintenance_cog()

    await MaintenanceCog.archive_loop.coro(cog)
    assert calls == []

    await MaintenanceCog.archive_loop.coro(cog)
    assert calls == [("archive", cog.bot.db), ("prune", cog.bot.db, 30)]


@pytest.mark.asyncio
async def test_maintenance_bm25_rebuilds_once_per_latest_message(monkeypatch, tmp_path):
    rebuilt: list[str] = []

    async def rebuild(path):
        rebuilt.append(path)

    monkeypatch.setattr("cogs.maintenance_cog.bulk_rebuild", rebuild)
    monkeypatch.setattr(config, "BM25_DATABASE_PATH", str(tmp_path / "bm25.db"))
    monkeypatch.setattr(
        config,
        "BM25_AUTO_REBUILD_CONFIG",
        {"enabled": True, "idle_minutes": 1, "poll_minutes": 15},
    )
    cog = _new_maintenance_cog()
    cog._last_conversation_ts = datetime.now(timezone.utc) - timedelta(minutes=5)

    await MaintenanceCog.bm25_rebuild_loop.coro(cog)
    first_rebuild_at = cog._last_bm25_rebuild_ts
    await MaintenanceCog.bm25_rebuild_loop.coro(cog)

    assert rebuilt == [str(tmp_path / "bm25.db")]
    assert first_rebuild_at is not None
    assert cog._last_bm25_rebuild_ts == first_rebuild_at


@pytest.mark.asyncio
async def test_maintenance_message_tracker_ignores_bots_and_normalizes_naive_time():
    cog = _new_maintenance_cog()
    await cog.on_message(
        SimpleNamespace(
            author=SimpleNamespace(bot=True),
            created_at=datetime(2026, 1, 1),
        )
    )
    assert cog._last_conversation_ts is None

    await cog.on_message(
        SimpleNamespace(
            author=SimpleNamespace(bot=False),
            created_at=datetime(2026, 1, 1, 12, 0),
        )
    )
    assert cog._last_conversation_ts == datetime(
        2026, 1, 1, 12, 0, tzinfo=timezone.utc
    )


@pytest.mark.asyncio
async def test_settings_ai_toggle_persists_and_updates_runtime_cache(monkeypatch):
    writes: list[tuple] = []
    cache_updates: list[tuple] = []

    async def set_setting(db, guild_id, key, value):
        writes.append((db, guild_id, key, value))

    monkeypatch.setattr("cogs.settings_cog.db_utils.set_guild_setting", set_setting)
    bot = SimpleNamespace(
        db=object(),
        update_guild_setting_cache=lambda *args: cache_updates.append(args),
    )
    cog = SettingsCog.__new__(SettingsCog)
    cog.bot = bot
    response = _RecordingResponse()
    interaction = SimpleNamespace(
        guild_id=123,
        user=SimpleNamespace(id=7),
        response=response,
        followup=response,
    )

    await SettingsCog.set_ai_enabled.callback(cog, interaction, True)

    assert writes == [(bot.db, 123, "ai_enabled", True)]
    assert cache_updates == [(123, "ai_enabled", True)]
    assert response.messages[0]["ephemeral"] is True
    assert "활성화" in response.messages[0]["content"]


@pytest.mark.asyncio
async def test_settings_allowed_channel_adds_once_and_serializes(monkeypatch):
    writes: list[tuple] = []
    cache_updates: list[tuple] = []

    async def get_setting(_db, _guild_id, _key):
        return json.dumps([10])

    async def set_setting(db, guild_id, key, value):
        writes.append((db, guild_id, key, value))

    monkeypatch.setattr("cogs.settings_cog.db_utils.get_guild_setting", get_setting)
    monkeypatch.setattr("cogs.settings_cog.db_utils.set_guild_setting", set_setting)
    bot = SimpleNamespace(
        db=object(),
        update_guild_setting_cache=lambda *args: cache_updates.append(args),
    )
    cog = SettingsCog.__new__(SettingsCog)
    cog.bot = bot
    response = _RecordingResponse()
    interaction = SimpleNamespace(
        guild_id=123,
        user=SimpleNamespace(id=7),
        response=response,
        followup=response,
    )
    channel = SimpleNamespace(id=20, name="ai-chat")

    await SettingsCog.set_allowed_channel.callback(
        cog,
        interaction,
        "add",
        channel,
    )

    assert json.loads(writes[0][3]) == [10, 20]
    assert cache_updates == [(123, "ai_allowed_channels", writes[0][3])]
    assert response.messages[0]["ephemeral"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("도쿄로 휴가 가고 싶다", "도쿄 여행"),
        ("달러 환율이 궁금하다", "환율 흐름"),
        ("우산 챙겨야 할까", "날씨"),
        ("스팀 게임 추천", "게임 추천"),
    ],
)
async def test_proactive_assistant_routes_static_suggestions(content, expected):
    cog = ProactiveAssistant.__new__(ProactiveAssistant)
    cog.bot = SimpleNamespace()
    cog.ai_handler = SimpleNamespace(is_ready=True)

    result = await cog.analyze_user_intent(SimpleNamespace(content=content))

    assert expected in result


@pytest.mark.asyncio
async def test_proactive_assistant_does_nothing_until_ai_is_ready():
    cog = ProactiveAssistant.__new__(ProactiveAssistant)
    cog.bot = SimpleNamespace()
    cog.ai_handler = SimpleNamespace(is_ready=False)

    result = await cog.analyze_user_intent(
        SimpleNamespace(content="달러 환율이 궁금하다")
    )

    assert result is None

"""기억 회수 윈도가 공용 대화 기억을 굶기지 않는지 확인한다.

운영 DB 실측에서 드러난 회귀를 고정한다. 예전 구현은 본인 범위
(``guild_user``/``user``)를 먼저 정렬한 뒤 LIMIT을 걸었다. 활발한 사용자는
본인 범위 행만 수천 개라 LIMIT이 전부 거기서 소진됐고, 공용 대화 기억
(``guild``/``channel``)이 후보에 한 건도 들어오지 못했다. 그 결과 "다른
사람이 뭐라고 했었지" 류의 질문은 원리상 답할 수 없었다.
"""

import numpy as np
import pytest

import config
from utils.embeddings import DiscordEmbeddingStore

GUILD = 659398210275770368
CHANNEL = 659398210980151307
ASKER = 111111111111111111
OTHER = 222222222222222222


def _vector() -> bytes:
    return np.zeros(384, dtype=np.float32).tobytes()


async def _store(tmp_path, monkeypatch, *, own_rows: int, shared_rows: int):
    monkeypatch.setattr(config, "DISCORD_EMBEDDING_BACKEND", "sqlite")
    monkeypatch.setattr(config, "AUTO_MIGRATE", True)
    store = DiscordEmbeddingStore(str(tmp_path / "memory.db"))
    await store.initialize()

    # 운영에서는 두 종류의 기억이 같은 대화 흐름에서 함께 쌓인다. 어느 한쪽만
    # 최근이거나 어느 한쪽만 과거인 상황은 실제로 존재하지 않으므로, 두 범위를
    # 같은 기간에 걸쳐 배치한다.
    span = 20_000

    def _stamp(second: int) -> str:
        return "2026-07-%02dT%02d:%02d:%02d" % (
            second // 86400 + 1,
            second % 86400 // 3600,
            second % 3600 // 60,
            second % 60,
        )

    rows = []
    for i in range(shared_rows):
        rows.append(
            (
                f"shared-{i}",
                str(1000 + i),
                str(GUILD),
                str(CHANNEL),
                None,
                None,
                "channel",
                "shared_context",
                f"공용 대화 {i}",
                f"공용 대화 {i}",
                _stamp(i * span // max(shared_rows, 1)),
            )
        )
    for i in range(own_rows):
        rows.append(
            (
                f"own-{i}",
                str(5000 + i),
                str(GUILD),
                str(CHANNEL),
                str(ASKER),
                "asker",
                "user",
                "conversation",
                f"내 발화 {i}",
                f"내 발화 {i}",
                _stamp(i * span // max(own_rows, 1) + 1),
            )
        )
    # 다른 사람의 본인 범위 기억은 스코프 경계상 회수되면 안 된다.
    rows.append(
        (
            "own-other",
            "9999",
            str(GUILD),
            str(CHANNEL),
            str(OTHER),
            "other",
            "user",
            "conversation",
            "남의 발화",
            "남의 발화",
            _stamp(span + 60),
        )
    )

    async with store._sqlite_connect() as db:
        await db.executemany(
            "INSERT INTO discord_memory_entries ("
            " memory_id, anchor_message_id, server_id, channel_id, owner_user_id,"
            " owner_user_name, memory_scope, memory_type, summary_text, memory_text,"
            " timestamp, embedding) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [row + (_vector(),) for row in rows],
        )
        await db.commit()
    return store


@pytest.mark.asyncio
async def test_shared_context_survives_when_user_has_more_rows_than_limit(
    tmp_path, monkeypatch
):
    # 운영과 같은 형태: 본인 범위 행 수가 LIMIT을 크게 넘는다.
    store = await _store(tmp_path, monkeypatch, own_rows=500, shared_rows=200)

    rows = await store.fetch_recent_memory_entries(
        server_id=GUILD, channel_id=CHANNEL, user_id=ASKER, limit=100
    )

    scopes = [row["memory_scope"] for row in rows]
    assert len(rows) == 100
    assert "channel" in scopes, (
        "본인 범위 행만으로 LIMIT이 소진되면 공용 대화 기억을 영원히 회수할 수 없다."
    )


@pytest.mark.asyncio
async def test_window_is_ordered_by_recency_only(tmp_path, monkeypatch):
    store = await _store(tmp_path, monkeypatch, own_rows=30, shared_rows=30)

    rows = await store.fetch_recent_memory_entries(
        server_id=GUILD, channel_id=CHANNEL, user_id=ASKER, limit=60
    )

    stamps = [row["timestamp"] for row in rows]
    assert stamps == sorted(stamps, reverse=True)


@pytest.mark.asyncio
async def test_other_users_own_scope_memory_is_never_returned(tmp_path, monkeypatch):
    store = await _store(tmp_path, monkeypatch, own_rows=10, shared_rows=10)

    rows = await store.fetch_recent_memory_entries(
        server_id=GUILD, channel_id=CHANNEL, user_id=ASKER, limit=100
    )

    owners = {row["user_id"] for row in rows if row["user_id"]}
    assert str(OTHER) not in owners

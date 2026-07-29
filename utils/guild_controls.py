"""최고 관리자 전용의 인스턴스별 Discord 서버 제어 저장소."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

import config


MAX_CHANNEL_OVERRIDES = 250


@dataclass(frozen=True)
class GuildControl:
    """한 봇 인스턴스에서 한 Discord 서버에 적용할 제한된 제어값."""

    ai_enabled: bool
    enabled_channels: frozenset[int]
    disabled_channels: frozenset[int]


def _now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def _channel_set(value: Any) -> frozenset[int]:
    if value in (None, ""):
        return frozenset()
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return frozenset()
    if not isinstance(parsed, list):
        return frozenset()
    values: set[int] = set()
    for item in parsed[:MAX_CHANNEL_OVERRIDES]:
        try:
            normalized = int(item)
        except (TypeError, ValueError):
            continue
        if normalized > 0:
            values.add(normalized)
    return frozenset(values)


def control_from_row(row: Mapping[str, Any] | Any) -> GuildControl:
    """sqlite/TiDB 호환 행을 불변 런타임 값으로 변환합니다."""
    return GuildControl(
        ai_enabled=bool(row["ai_enabled"]),
        enabled_channels=_channel_set(row["enabled_channels_json"]),
        disabled_channels=_channel_set(row["disabled_channels_json"]),
    )


async def load_guild_controls(db: Any) -> dict[int, GuildControl]:
    """현재 인스턴스 행만 읽어 서버별 캐시를 만듭니다."""
    async with db.execute(
        """
        SELECT guild_id, ai_enabled, enabled_channels_json, disabled_channels_json
        FROM bot_guild_controls
        WHERE instance_name = ?
        """,
        (config.INSTANCE_NAME,),
    ) as cursor:
        rows = await cursor.fetchall()
    controls: dict[int, GuildControl] = {}
    for row in rows:
        try:
            guild_id = int(row["guild_id"])
        except (KeyError, TypeError, ValueError):
            continue
        controls[guild_id] = control_from_row(row)
    return controls


async def get_guild_control(db: Any, guild_id: int) -> GuildControl:
    """행이 없으면 기능 활성·채널 override 없음이라는 안전한 기본값을 반환합니다."""
    async with db.execute(
        """
        SELECT ai_enabled, enabled_channels_json, disabled_channels_json
        FROM bot_guild_controls
        WHERE instance_name = ? AND guild_id = ?
        LIMIT 1
        """,
        (config.INSTANCE_NAME, int(guild_id)),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return GuildControl(True, frozenset(), frozenset())
    return control_from_row(row)


async def _upsert_control(
    db: Any,
    *,
    guild_id: int,
    control: GuildControl,
    changed_by: int,
) -> None:
    now = _now_text()
    enabled_json = json.dumps(
        sorted(control.enabled_channels),
        separators=(",", ":"),
    )
    disabled_json = json.dumps(
        sorted(control.disabled_channels),
        separators=(",", ":"),
    )
    params = (
        config.INSTANCE_NAME,
        int(guild_id),
        1 if control.ai_enabled else 0,
        enabled_json,
        disabled_json,
        int(changed_by),
        now,
        now,
    )
    if str(getattr(db, "backend", config.DB_BACKEND)) == "tidb":
        statement = """
            INSERT INTO bot_guild_controls
                (instance_name, guild_id, ai_enabled, enabled_channels_json,
                 disabled_channels_json, changed_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON DUPLICATE KEY UPDATE
                ai_enabled = VALUES(ai_enabled),
                enabled_channels_json = VALUES(enabled_channels_json),
                disabled_channels_json = VALUES(disabled_channels_json),
                changed_by = VALUES(changed_by),
                updated_at = VALUES(updated_at)
        """
    else:
        statement = """
            INSERT INTO bot_guild_controls
                (instance_name, guild_id, ai_enabled, enabled_channels_json,
                 disabled_channels_json, changed_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(instance_name, guild_id) DO UPDATE SET
                ai_enabled = excluded.ai_enabled,
                enabled_channels_json = excluded.enabled_channels_json,
                disabled_channels_json = excluded.disabled_channels_json,
                changed_by = excluded.changed_by,
                updated_at = excluded.updated_at
        """
    await db.execute(statement, params)


async def set_guild_ai_enabled(
    db: Any,
    *,
    guild_id: int,
    enabled: bool,
    changed_by: int,
) -> GuildControl:
    """서버 전체 AI 사용 여부만 변경하고 나머지 override를 보존합니다."""
    current = await get_guild_control(db, guild_id)
    updated = GuildControl(
        ai_enabled=bool(enabled),
        enabled_channels=current.enabled_channels,
        disabled_channels=current.disabled_channels,
    )
    try:
        await _upsert_control(
            db,
            guild_id=guild_id,
            control=updated,
            changed_by=changed_by,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return updated


async def set_channel_enabled(
    db: Any,
    *,
    guild_id: int,
    channel_id: int,
    enabled: bool,
    changed_by: int,
) -> GuildControl:
    """현재 채널에 명시적 허용/차단 override를 상호 배타적으로 저장합니다."""
    current = await get_guild_control(db, guild_id)
    allowed = set(current.enabled_channels)
    blocked = set(current.disabled_channels)
    normalized = int(channel_id)
    if enabled:
        allowed.add(normalized)
        blocked.discard(normalized)
    else:
        blocked.add(normalized)
        allowed.discard(normalized)
    if len(allowed) + len(blocked) > MAX_CHANNEL_OVERRIDES:
        raise ValueError("한 서버에서 관리할 수 있는 채널 override 수를 초과했습니다.")
    updated = GuildControl(
        ai_enabled=current.ai_enabled,
        enabled_channels=frozenset(allowed),
        disabled_channels=frozenset(blocked),
    )
    try:
        await _upsert_control(
            db,
            guild_id=guild_id,
            control=updated,
            changed_by=changed_by,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return updated

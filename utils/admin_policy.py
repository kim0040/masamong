"""Masamong 인스턴스 관리자와 서버 관리자 경계를 관리합니다."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import config


INSTANCE_ADMIN_ROLE = "instance_admin"


def is_superadmin(user_id: int | str | None) -> bool:
    """현재 프로필 env에 고정된 최고 관리자 여부를 반환합니다."""
    try:
        normalized = int(user_id or 0)
    except (TypeError, ValueError):
        return False
    return normalized in config.SUPERADMIN_USER_IDS


def is_guild_admin(member: Any, guild: Any = None) -> bool:
    """Discord 권한으로 현재 서버만 관리할 수 있는지 확인합니다."""
    if member is None or guild is None:
        return False
    try:
        if int(getattr(guild, "owner_id", 0) or 0) == int(member.id):
            return True
    except (TypeError, ValueError):
        pass
    permissions = getattr(member, "guild_permissions", None)
    return bool(
        permissions
        and (
            getattr(permissions, "administrator", False)
            or getattr(permissions, "manage_guild", False)
        )
    )


async def is_instance_admin(db: Any, user_id: int | str | None) -> bool:
    """현재 instance_name에 활성 등록된 봇 관리자 여부를 조회합니다."""
    if db is None:
        return False
    try:
        normalized = int(user_id or 0)
    except (TypeError, ValueError):
        return False
    async with db.execute(
        """
        SELECT role
        FROM bot_admin_accounts
        WHERE instance_name = ? AND user_id = ? AND enabled = 1
        LIMIT 1
        """,
        (config.INSTANCE_NAME, normalized),
    ) as cursor:
        row = await cursor.fetchone()
    return bool(row and str(row["role"]) == INSTANCE_ADMIN_ROLE)


async def set_instance_admin(
    db: Any,
    *,
    user_id: int,
    enabled: bool,
    changed_by: int,
) -> None:
    """현재 인스턴스 관리자 한 명을 additive upsert/비활성화합니다."""
    now = datetime.now(timezone.utc).isoformat()
    backend = str(getattr(db, "backend", config.DB_BACKEND))
    params = (
        config.INSTANCE_NAME,
        int(user_id),
        INSTANCE_ADMIN_ROLE,
        1 if enabled else 0,
        int(changed_by),
        now,
        now,
    )
    if backend == "tidb":
        statement = """
            INSERT INTO bot_admin_accounts
                (instance_name, user_id, role, enabled, changed_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON DUPLICATE KEY UPDATE
                role = VALUES(role),
                enabled = VALUES(enabled),
                changed_by = VALUES(changed_by),
                updated_at = VALUES(updated_at)
        """
    else:
        statement = """
            INSERT INTO bot_admin_accounts
                (instance_name, user_id, role, enabled, changed_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(instance_name, user_id) DO UPDATE SET
                role = excluded.role,
                enabled = excluded.enabled,
                changed_by = excluded.changed_by,
                updated_at = excluded.updated_at
        """
    try:
        await db.execute(statement, params)
        await db.commit()
    except Exception:
        await db.rollback()
        raise


async def list_instance_admin_ids(db: Any) -> list[int]:
    """현재 인스턴스의 활성 등록 관리자 ID를 정렬해 반환합니다."""
    if db is None:
        return []
    async with db.execute(
        """
        SELECT user_id
        FROM bot_admin_accounts
        WHERE instance_name = ? AND role = ? AND enabled = 1
        ORDER BY user_id
        """,
        (config.INSTANCE_NAME, INSTANCE_ADMIN_ROLE),
    ) as cursor:
        rows = await cursor.fetchall()
    return [int(row["user_id"]) for row in rows]

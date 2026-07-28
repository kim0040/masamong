# -*- coding: utf-8 -*-
"""사용자가 직접 제공하는 개인정보의 목적별 동의 상태를 관리한다.

Discord 대화 기록이나 Discord 서버가 제공하는 정보는 이 모듈의 대상이 아니다.
운세 프로필과 학교 공지 개인화 프로필처럼 봇이 별도로 수집·저장·재사용하는
정보만 명시적인 목적별 동의로 보호한다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from weakref import WeakValueDictionary


CONSENT_GRANTED = "granted"
CONSENT_WITHDRAWN = "withdrawn"

FORTUNE_SCOPE = "fortune"
SCHOOL_NOTICE_SCOPE = "school_notice"

_CONSENT_WRITE_LOCKS: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()


@dataclass(frozen=True)
class ConsentPolicy:
    """사용자에게 고지하는 동의 정책의 불변 버전."""

    scope: str
    display_name: str
    version: str
    notice: str

    @property
    def notice_hash(self) -> str:
        return hashlib.sha256(self.notice.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ConsentState:
    """DB에 저장된 목적별 현재 동의 상태."""

    user_id: int
    scope: str
    policy_version: str
    notice_hash: str
    status: str
    granted_at: str | None
    withdrawn_at: str | None
    updated_at: str


_POLICIES = {
    FORTUNE_SCOPE: ConsentPolicy(
        scope=FORTUNE_SCOPE,
        display_name="운세",
        version="2026-07-28.v1",
        notice=(
            "운세 개인정보 처리 동의\n"
            "- 수집 항목: Discord 사용자 ID와 생년월일(필수), 사용자가 선택적으로 "
            "제공한 출생 시간·성별·출생지(미제공 가능)\n"
            "- 이용 목적: 요청한 개인 운세 생성, 별자리 자동 확인, 운세 구독 발송, "
            "동의한 운세 내용을 이후 DM 답변의 참고 정보로 활용\n"
            "- 외부 처리: 운세 문장 생성을 위해 실제로 등록한 항목과 Discord 표시 이름이 "
            "설정된 외부 LLM 제공자에게 전달될 수 있음. 선택 항목을 제공하지 않은 경우 "
            "임의 값으로 바꾸거나 성별·지역을 추측하지 않음\n"
            "- 보관: 사용자가 `!운세 삭제`를 실행할 때까지 보관\n"
            "- 철회: `!개인정보 철회 운세`로 향후 이용과 자동 발송을 즉시 중단할 수 있음. "
            "철회만으로 기존 프로필과 구독 설정은 삭제되지 않으며, 재동의하면 기존 설정으로 재개됨\n"
            "- 삭제: `!운세 삭제`는 운세 프로필·구독·생성 대기 내용·운세 컨텍스트를 명시적으로 삭제\n"
            "- 동의 이력: 동의·철회 증빙을 위한 Discord 사용자 ID, 목적, 정책 버전, "
            "고지문 해시와 처리 시각은 기능 데이터 삭제 후에도 감사 이력으로 별도 보관\n"
            "- 동의를 거부하거나 철회해도 일반 Discord 대화와 서버 기능 이용에는 영향이 없음"
        ),
    ),
    SCHOOL_NOTICE_SCOPE: ConsentPolicy(
        scope=SCHOOL_NOTICE_SCOPE,
        display_name="학교공지",
        version="2026-07-28.v1",
        notice=(
            "학교 공지 개인화 개인정보 처리 동의\n"
            "- 수집 항목: Discord 사용자 ID, 학교·학위과정·학년·학과·캠퍼스·학적, "
            "이수 정보·성적·관심 분야·알림 설정 및 사용자가 제출한 피드백\n"
            "- 이용 목적: 사용자 조건에 맞는 학교 공지 선별, 우선순위 계산, digest 생성·DM 발송\n"
            "- 외부 처리: 운영 설정에서 LLM을 켠 경우 사용자가 입력한 등록·보정 문장과 "
            "관련 프로필 항목이 프로필 정규화를 위해, 공지 내용과 개인화에 필요한 프로필 "
            "항목이 공지 분석을 위해 외부 LLM 제공자에게 전달될 수 있음\n"
            "- 보관: 사용자가 `!공지 삭제`를 실행할 때까지 보관\n"
            "- 철회: `!개인정보 철회 학교공지`로 향후 batch 처리·개인화·자동 전달·피드백 "
            "수집을 즉시 중단할 수 있음. 철회만으로 기존 프로필과 전달 설정은 삭제되지 않으며, "
            "재동의하면 기존 설정으로 재개됨\n"
            "- 삭제: `!공지 삭제`는 학교 공지 프로필과 연결된 개인화 기록을 명시적으로 삭제\n"
            "- 동의 이력: 동의·철회 증빙을 위한 Discord 사용자 ID, 목적, 정책 버전, "
            "고지문 해시와 처리 시각은 기능 데이터 삭제 후에도 감사 이력으로 별도 보관\n"
            "- 동의를 거부하거나 철회해도 일반 Discord 대화와 서버 기능 이용에는 영향이 없음"
        ),
    ),
}

_SCOPE_ALIASES = {
    FORTUNE_SCOPE: FORTUNE_SCOPE,
    "운세": FORTUNE_SCOPE,
    SCHOOL_NOTICE_SCOPE: SCHOOL_NOTICE_SCOPE,
    "school-notice": SCHOOL_NOTICE_SCOPE,
    "학교공지": SCHOOL_NOTICE_SCOPE,
    "학교": SCHOOL_NOTICE_SCOPE,
    "공지": SCHOOL_NOTICE_SCOPE,
}


class ConsentRequiredError(RuntimeError):
    """현재 정책에 대한 명시적 동의가 없어 개인정보를 사용할 수 없음."""

    def __init__(self, scope: str):
        policy = get_policy(scope)
        super().__init__(f"{policy.display_name} 개인정보 동의가 필요합니다.")
        self.scope = policy.scope


def normalize_scope(scope: str) -> str:
    """명령어 별칭을 DB에 저장하는 정규 scope로 변환한다."""
    normalized = str(scope or "").strip().lower().replace(" ", "")
    try:
        return _SCOPE_ALIASES[normalized]
    except KeyError as exc:
        raise ValueError("지원하는 동의 목적은 `운세`, `학교공지`입니다.") from exc


def get_policy(scope: str) -> ConsentPolicy:
    """현재 배포가 요구하는 목적별 정책을 반환한다."""
    return _POLICIES[normalize_scope(scope)]


def all_policies() -> tuple[ConsentPolicy, ...]:
    """표시 순서가 안정적인 전체 정책 목록."""
    return tuple(_POLICIES[scope] for scope in (FORTUNE_SCOPE, SCHOOL_NOTICE_SCOPE))


def format_policy_notice(scope: str) -> str:
    """사용자에게 보여줄 버전·본문·명시 동의 방법을 구성한다."""
    policy = get_policy(scope)
    return (
        f"**{policy.notice}**\n\n"
        f"정책 버전: `{policy.version}`\n"
        "아래 **동의합니다** 버튼을 직접 눌러야 동의가 기록됩니다. "
        "버튼을 누르기 전에는 개인정보를 수집하거나 기존 프로필을 이용하지 않습니다."
    )


def consent_command_name(scope: str) -> str:
    """재동의 안내에 사용할 명령어 인자를 반환한다."""
    return "운세" if normalize_scope(scope) == FORTUNE_SCOPE else "학교공지"


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _backend(db) -> str:
    return str(getattr(db, "backend", "sqlite") or "sqlite").lower()


def _consent_write_lock(db, user_id: int, scope: str) -> asyncio.Lock:
    """공유 DB 연결에서 모든 동의 변경을 한 번에 하나만 처리한다.

    봇은 하나의 연결을 여러 coroutine이 공유하므로 사용자별 lock만 두면 서로
    다른 사용자의 ``execute → commit`` 구간이 같은 transaction에 섞일 수 있다.
    DB 연결 단위로 직렬화해 동의 현재 상태와 감사 이벤트의 순서를 보존한다.
    """
    int(user_id)
    normalize_scope(scope)
    key = id(db)
    lock = _CONSENT_WRITE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _CONSENT_WRITE_LOCKS[key] = lock
    return lock


async def get_consent_state(db, user_id: int, scope: str) -> ConsentState | None:
    """현재 저장된 동의 상태를 읽는다."""
    policy = get_policy(scope)
    async with db.execute(
        """
        SELECT user_id, scope, policy_version, notice_hash, status,
               granted_at, withdrawn_at, updated_at
        FROM privacy_consents
        WHERE user_id = ? AND scope = ?
        """,
        (int(user_id), policy.scope),
    ) as cursor:
        row = await cursor.fetchone()
    if not row:
        return None
    return ConsentState(
        user_id=int(row[0]),
        scope=str(row[1]),
        policy_version=str(row[2]),
        notice_hash=str(row[3]),
        status=str(row[4]),
        granted_at=str(row[5]) if row[5] else None,
        withdrawn_at=str(row[6]) if row[6] else None,
        updated_at=str(row[7]),
    )


def is_current_consent_state(
    state: ConsentState | None,
    scope: str,
) -> bool:
    """이미 읽은 상태가 현재 고지문에 대한 유효한 동의인지 확인한다."""
    policy = get_policy(scope)
    return bool(
        state
        and state.scope == policy.scope
        and state.status == CONSENT_GRANTED
        and state.policy_version == policy.version
        and state.notice_hash == policy.notice_hash
        and state.granted_at
        and not state.withdrawn_at
    )


async def has_current_consent(db, user_id: int, scope: str) -> bool:
    """현재 정책 버전·본문에 대해 유효한 동의가 있는지 fail-closed로 확인한다."""
    policy = get_policy(scope)
    state = await get_consent_state(db, user_id, policy.scope)
    return is_current_consent_state(state, policy.scope)


async def require_current_consent(db, user_id: int, scope: str) -> None:
    """유효한 동의가 없으면 개인정보 처리 경로를 중단한다."""
    if not await has_current_consent(db, user_id, scope):
        raise ConsentRequiredError(scope)


async def _append_event(
    db,
    *,
    user_id: int,
    policy: ConsentPolicy,
    status: str,
    granted_at: str | None,
    withdrawn_at: str | None,
    created_at: str,
) -> None:
    await db.execute(
        """
        INSERT INTO privacy_consent_events (
            user_id, scope, policy_version, notice_hash, status,
            granted_at, withdrawn_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(user_id),
            policy.scope,
            policy.version,
            policy.notice_hash,
            status,
            granted_at,
            withdrawn_at,
            created_at,
        ),
    )


async def grant_consent(db, user_id: int, scope: str) -> ConsentState:
    """현재 정책에 대한 동의를 사용자·목적 단위로 직렬화해 기록한다."""
    async with _consent_write_lock(db, user_id, scope):
        return await _grant_consent_unlocked(db, user_id, scope)


async def _grant_consent_unlocked(db, user_id: int, scope: str) -> ConsentState:
    """현재 정책에 대한 동의를 기록하고 append-only 이벤트를 남긴다."""
    policy = get_policy(scope)
    granted_at = _utc_now_text()
    if _backend(db) == "tidb":
        query = """
            INSERT INTO privacy_consents (
                user_id, scope, policy_version, notice_hash, status,
                granted_at, withdrawn_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
            ON DUPLICATE KEY UPDATE
                policy_version = VALUES(policy_version),
                notice_hash = VALUES(notice_hash),
                status = VALUES(status),
                granted_at = VALUES(granted_at),
                withdrawn_at = NULL,
                updated_at = VALUES(updated_at)
        """
    else:
        query = """
            INSERT INTO privacy_consents (
                user_id, scope, policy_version, notice_hash, status,
                granted_at, withdrawn_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
            ON CONFLICT(user_id, scope) DO UPDATE SET
                policy_version = excluded.policy_version,
                notice_hash = excluded.notice_hash,
                status = excluded.status,
                granted_at = excluded.granted_at,
                withdrawn_at = NULL,
                updated_at = excluded.updated_at
        """
    try:
        # 이벤트를 먼저 기록한다. 공유 연결의 다른 commit이 두 문장 사이에
        # 끼더라도 첫 동의의 현재 상태가 이벤트 없이 granted로 남지 않는다.
        await _append_event(
            db,
            user_id=int(user_id),
            policy=policy,
            status=CONSENT_GRANTED,
            granted_at=granted_at,
            withdrawn_at=None,
            created_at=granted_at,
        )
        await db.execute(
            query,
            (
                int(user_id),
                policy.scope,
                policy.version,
                policy.notice_hash,
                CONSENT_GRANTED,
                granted_at,
                granted_at,
            ),
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    state = await get_consent_state(db, user_id, policy.scope)
    if state is None:  # pragma: no cover - commit 뒤 DB가 비정상 상태인 경우
        raise RuntimeError("동의 상태를 저장했지만 다시 읽을 수 없습니다.")
    return state


async def withdraw_consent(
    db,
    user_id: int,
    scope: str,
) -> ConsentState | None:
    """기존 동의가 있을 때만 철회를 직렬화해 기록한다."""
    async with _consent_write_lock(db, user_id, scope):
        return await _withdraw_consent_unlocked(db, user_id, scope)


async def _withdraw_consent_unlocked(
    db,
    user_id: int,
    scope: str,
) -> ConsentState | None:
    """향후 개인정보 이용을 중단하되 기능 데이터는 삭제하지 않는다."""
    policy = get_policy(scope)
    previous = await get_consent_state(db, user_id, policy.scope)
    if previous is None:
        # 동의한 적 없는 사용자의 "철회/삭제" 요청 자체를 새 개인정보
        # 수집 근거로 삼지 않는다.
        return None
    granted_at = (
        previous.granted_at
        if previous
        and previous.policy_version == policy.version
        and previous.notice_hash == policy.notice_hash
        else None
    )
    withdrawn_at = _utc_now_text()
    if _backend(db) == "tidb":
        query = """
            INSERT INTO privacy_consents (
                user_id, scope, policy_version, notice_hash, status,
                granted_at, withdrawn_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON DUPLICATE KEY UPDATE
                policy_version = VALUES(policy_version),
                notice_hash = VALUES(notice_hash),
                status = VALUES(status),
                granted_at = VALUES(granted_at),
                withdrawn_at = VALUES(withdrawn_at),
                updated_at = VALUES(updated_at)
        """
    else:
        query = """
            INSERT INTO privacy_consents (
                user_id, scope, policy_version, notice_hash, status,
                granted_at, withdrawn_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, scope) DO UPDATE SET
                policy_version = excluded.policy_version,
                notice_hash = excluded.notice_hash,
                status = excluded.status,
                granted_at = excluded.granted_at,
                withdrawn_at = excluded.withdrawn_at,
                updated_at = excluded.updated_at
        """
    try:
        await db.execute(
            query,
            (
                int(user_id),
                policy.scope,
                policy.version,
                policy.notice_hash,
                CONSENT_WITHDRAWN,
                granted_at,
                withdrawn_at,
                withdrawn_at,
            ),
        )
        await _append_event(
            db,
            user_id=int(user_id),
            policy=policy,
            status=CONSENT_WITHDRAWN,
            granted_at=granted_at,
            withdrawn_at=withdrawn_at,
            created_at=withdrawn_at,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    state = await get_consent_state(db, user_id, policy.scope)
    if state is None:  # pragma: no cover
        raise RuntimeError("철회 상태를 저장했지만 다시 읽을 수 없습니다.")
    return state

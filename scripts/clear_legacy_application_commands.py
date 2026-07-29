#!/usr/bin/env python3
"""현재 Discord 앱에 남은 레거시 slash command를 명시적으로 제거합니다.

코드에서 command를 지우는 것만으로는 Discord에 이미 등록된 전역/길드 명령이
사라지지 않는다. 이 스크립트는 선택한 운영 프로필과 앱 ID를 다시 확인하고,
dry-run에서는 이름만 조회하며, apply에서는 빈 목록으로 덮어쓴다.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

import aiohttp


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config


API_ROOT = "https://discord.com/api/v10"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--expected-profile",
        choices=("masamo", "general"),
        required=True,
    )
    parser.add_argument("--expected-application-id", type=int, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm")
    return parser.parse_args()


def confirmation_phrase(application_id: int) -> str:
    return (
        "CLEAR LEGACY APPLICATION COMMANDS FOR "
        f"profile={config.PROFILE} application_id={application_id}"
    )


def validate(args: argparse.Namespace) -> tuple[int, str]:
    if config.ENV_FILE_PATH is None or not config.REQUIRE_EXPLICIT_PROFILE:
        raise SystemExit("MASAMONG_ENV_FILE로 선택한 명시적 프로필이 필요합니다.")
    if config.PROFILE != args.expected_profile or config.PROFILE != config.INSTANCE_NAME:
        raise SystemExit("profile/instance/--expected-profile이 일치해야 합니다.")
    application_id = int(config.EXPECTED_DISCORD_BOT_USER_ID or 0)
    if application_id <= 0 or application_id != args.expected_application_id:
        raise SystemExit("현재 프로필의 예상 Discord 앱 ID와 인자가 일치해야 합니다.")
    token = str(config.TOKEN or "").strip()
    if not token:
        raise SystemExit("현재 프로필에 Discord bot token이 없습니다.")
    return application_id, token


async def request_json(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    payload: list[dict] | None = None,
) -> object:
    async with session.request(method, url, json=payload) as response:
        body = await response.json(content_type=None)
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(
                f"Discord API 요청 실패: status={response.status}"
            )
        return body


async def run(args: argparse.Namespace) -> None:
    application_id, token = validate(args)
    phrase = confirmation_phrase(application_id)
    url = f"{API_ROOT}/applications/{application_id}/commands"
    timeout = aiohttp.ClientTimeout(total=20)
    headers = {"Authorization": f"Bot {token}"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        current = await request_json(session, "GET", url)
        if not isinstance(current, list):
            raise RuntimeError("Discord application command 목록 형식이 올바르지 않습니다.")
        names = sorted(
            str(item.get("name") or "")
            for item in current
            if isinstance(item, dict) and item.get("name")
        )
        print(
            f"current: profile={config.PROFILE} application_id={application_id} "
            f"count={len(names)} names={','.join(names) or '-'}"
        )
        if not args.apply:
            print(f"confirmation={phrase}")
            return
        if args.confirm != phrase:
            raise SystemExit("--confirm 값이 현재 대상과 정확히 일치해야 합니다.")
        result = await request_json(session, "PUT", url, payload=[])
        if not isinstance(result, list) or result:
            raise RuntimeError("Discord application command 초기화 결과가 비어 있지 않습니다.")
        verified = await request_json(session, "GET", url)
        if not isinstance(verified, list) or verified:
            raise RuntimeError("Discord application command가 남아 있습니다.")
    print(
        f"cleared: profile={config.PROFILE} application_id={application_id} count=0"
    )


def main() -> None:
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()

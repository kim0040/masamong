#!/usr/bin/env python3
"""운영 프로필의 이미지 provider 계약을 실제 1회 호출로 검증합니다.

이미지를 파일로 남기거나 base64를 출력하지 않으며 API key도 출력하지 않는다.
명시적인 확인 문구 없이는 네트워크 호출을 하지 않는다.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import sys

import aiohttp


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config


EXPECTED_MODEL = "gemini-3.1-flash-lite-image"
ALLOWED_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--expected-profile",
        choices=("masamo", "general"),
        required=True,
    )
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--confirm")
    return parser.parse_args()


def confirmation_phrase() -> str:
    return (
        "RUN ONE IMAGE PROVIDER SMOKE FOR "
        f"profile={config.PROFILE} model={EXPECTED_MODEL}"
    )


def validate(args: argparse.Namespace) -> str:
    if config.ENV_FILE_PATH is None or not config.REQUIRE_EXPLICIT_PROFILE:
        raise SystemExit("MASAMONG_ENV_FILE로 선택한 명시적 프로필이 필요합니다.")
    if config.PROFILE != args.expected_profile or config.PROFILE != config.INSTANCE_NAME:
        raise SystemExit("profile/instance/--expected-profile이 일치해야 합니다.")
    if not config.COMETAPI_IMAGE_ENABLED:
        raise SystemExit("현재 프로필에서 이미지 생성이 비활성화되어 있습니다.")
    if str(config.IMAGE_MODEL).strip() != EXPECTED_MODEL:
        raise SystemExit(
            f"IMAGE_MODEL이 {EXPECTED_MODEL}과 일치하지 않습니다."
        )
    key = str(config.COMETAPI_IMAGE_API_KEY or config.COMETAPI_KEY or "").strip()
    if not key:
        raise SystemExit("현재 프로필에 이미지 API key가 없습니다.")
    return key


async def run(args: argparse.Namespace) -> None:
    key = validate(args)
    phrase = confirmation_phrase()
    if not args.run:
        print(
            f"dry-run: profile={config.PROFILE} model={EXPECTED_MODEL} "
            "calls=0 output_saved=false"
        )
        print(f"confirmation={phrase}")
        return
    if args.confirm != phrase:
        raise SystemExit("--confirm 값이 현재 대상과 정확히 일치해야 합니다.")

    base_url = str(config.COMETAPI_IMAGE_BASE_URL).rstrip("/")
    endpoint = f"{base_url}/v1beta/models/{EXPECTED_MODEL}:generateContent"
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            "A minimal flat icon of one blue circle centered on "
                            "a clean white background, no text"
                        )
                    }
                ],
            }
        ],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": {"aspectRatio": "1:1", "imageSize": "1K"},
        },
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(
        total=min(180, max(30, int(config.IMAGE_GENERATION_TIMEOUT_SECONDS)))
    )
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(endpoint, json=payload, headers=headers) as response:
            raw = await response.read()
            if response.status != 200:
                raise RuntimeError(
                    f"이미지 provider smoke 실패: status={response.status} "
                    f"response_bytes={len(raw)}"
                )
    if len(raw) > 18_000_000:
        raise RuntimeError("이미지 provider 응답이 허용 크기를 초과했습니다.")
    parsed = json.loads(raw)
    for candidate in parsed.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            inline = part.get("inlineData")
            if not isinstance(inline, dict):
                continue
            mime_type = str(inline.get("mimeType") or "").casefold()
            encoded = inline.get("data")
            if mime_type not in ALLOWED_MIME_TYPES or not isinstance(encoded, str):
                continue
            image = base64.b64decode(encoded, validate=True)
            if not image or len(image) > 12_000_000:
                continue
            digest = hashlib.sha256(image).hexdigest()[:12]
            print(
                f"success: profile={config.PROFILE} model={EXPECTED_MODEL} "
                f"mime={mime_type} bytes={len(image)} sha256_prefix={digest} "
                "output_saved=false calls=1"
            )
            return
    raise RuntimeError("이미지 provider 응답에서 유효한 이미지를 찾지 못했습니다.")


def main() -> None:
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()

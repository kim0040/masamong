#!/usr/bin/env python3
"""routing lane의 의미 기반 도구 선택을 제한된 합성 질의로 검증한다."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.intent_analyzer import IntentAnalyzer  # noqa: E402
from utils.llm_client import LLMClient  # noqa: E402


CASES = (
    (
        "weather",
        "서울에서 우산을 챙겨야 할지 기상 상황을 확인해 줘.",
        "get_weather_forecast",
        False,
        False,
        [],
    ),
    (
        "web",
        "이번 주 OpenAI의 공식 발표가 있었는지 확인해서 알려줘.",
        "web_search",
        False,
        False,
        [],
    ),
    (
        "image",
        "고양이가 달을 바라보는 장면을 시각 자료로 부탁해.",
        "generate_image",
        True,
        False,
        [],
    ),
    (
        "memory",
        "전에 우리가 정했던 여행 계획을 다시 알려줘.",
        None,
        True,
        False,
        [],
    ),
    (
        "person_memory",
        "김재원이 누구야?",
        None,
        True,
        False,
        [],
    ),
    (
        "greeting",
        "안녕",
        None,
        False,
        False,
        [],
    ),
    (
        "general_knowledge",
        "양자 얽힘을 쉽게 설명해줘.",
        None,
        False,
        False,
        [],
    ),
    (
        "recent_context_only",
        "그 계획 계속 정리해줘.",
        None,
        False,
        False,
        [
            {
                "role": "user",
                "speaker": "민수",
                "is_current_user": True,
                "parts": [
                    "부산 여행은 KTX로 가고 숙소는 해운대 쪽으로 보자."
                ],
            },
            {
                "role": "model",
                "speaker": "Masamong",
                "parts": [
                    "좋아, KTX와 해운대 숙소를 기준으로 정리할게."
                ],
            },
        ],
    ),
    (
        "fortune_context",
        "아까 본 운세를 토대로 오늘 조언해줘.",
        None,
        False,
        True,
        [],
    ),
)


async def run() -> int:
    client = LLMClient(db=None)
    analyzer = IntentAnalyzer(
        db=None,
        llm_client=client,
        tools_cog=None,
    )
    failures: list[str] = []
    for (
        case_name,
        query,
        expected_tool,
        expected_memory,
        expected_fortune_context,
        history,
    ) in CASES:
        started = time.monotonic()
        decision = await analyzer.route_tools(
            query,
            {"trace_id": f"semantic-routing-{case_name}"},
            history=history,
        )
        elapsed_ms = round((time.monotonic() - started) * 1000)
        actual_tools = [
            str(item.get("tool_to_use") or "")
            for item in decision.plan
        ]
        passed = (
            decision.source == "llm"
            and (
                (expected_tool is None and not actual_tools)
                or expected_tool in actual_tools
            )
            and decision.needs_memory is expected_memory
            and decision.needs_fortune_context is expected_fortune_context
        )
        print(
            "case={case} passed={passed} source={source} tools={tools} "
            "needs_memory={memory} fortune_context={fortune} "
            "elapsed_ms={elapsed}".format(
                case=case_name,
                passed=str(passed).lower(),
                source=decision.source,
                tools=",".join(actual_tools) or "none",
                memory=str(decision.needs_memory).lower(),
                fortune=str(decision.needs_fortune_context).lower(),
                elapsed=elapsed_ms,
            )
        )
        if not passed:
            failures.append(case_name)
    if failures:
        print("failed_cases=" + ",".join(failures))
        return 1
    return 0


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())

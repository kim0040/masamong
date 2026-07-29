#!/usr/bin/env python3
"""routing lane의 의미 기반 도구 선택을 제한된 합성 질의로 검증한다."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from utils.intent_analyzer import IntentAnalyzer  # noqa: E402
from utils.llm_client import LLMClient  # noqa: E402


CASES = (
    (
        "weather",
        "서울에서 우산을 챙겨야 할지 기상 상황을 확인해 줘.",
        "get_weather_forecast",
        False,
        False,
        "low",
        [],
    ),
    (
        "web",
        "이번 주 OpenAI의 공식 발표가 있었는지 확인해서 알려줘.",
        "web_search",
        False,
        False,
        "low",
        [],
    ),
    (
        "image",
        "고양이가 달을 바라보는 장면을 시각 자료로 부탁해.",
        "generate_image",
        True,
        False,
        "low",
        [],
    ),
    (
        "memory",
        "전에 우리가 정했던 여행 계획을 다시 알려줘.",
        None,
        True,
        False,
        "low",
        [],
    ),
    (
        "person_memory",
        "김재원이 누구야?",
        None,
        True,
        False,
        "high",
        [],
    ),
    (
        "greeting",
        "안녕",
        None,
        False,
        False,
        "low",
        [],
    ),
    (
        "general_knowledge",
        "양자 얽힘을 쉽게 설명해줘.",
        None,
        False,
        False,
        "low",
        [],
    ),
    (
        "recent_context_only",
        "그 계획 계속 정리해줘.",
        None,
        # 최근 원문만으로 충분해 false가 이상적이지만, 오래된 합의가 더 있을
        # 가능성을 보수적으로 확인하는 true도 품질 안전 범위다. 어느 쪽이든
        # 외부 도구를 고르지 않는지가 이 시나리오의 필수 계약이다.
        None,
        False,
        "low",
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
        "low",
        [],
    ),
    (
        "multi_constraint_reasoning",
        (
            "지민은 수아보다 먼저, 현우는 수아보다 나중이어야 하고 "
            "수아는 11시만 가능해. 가능한 10시·11시·13시를 한 명씩 배정해줘."
        ),
        None,
        False,
        False,
        "high",
        [],
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "routing provider를 실제 호출하는 비용 발생 smoke. "
            "--run과 정확한 확인 문구 없이는 호출하지 않습니다."
        )
    )
    parser.add_argument(
        "--expected-profile",
        required=True,
        choices=("masamo", "general"),
    )
    parser.add_argument(
        "--max-calls",
        type=int,
        default=len(CASES),
        help=f"실제 routing 호출 상한 (1~{len(CASES)})",
    )
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--confirm")
    return parser.parse_args()


def confirmation_text(args: argparse.Namespace) -> str:
    return (
        f"RUN {int(args.max_calls)} SEMANTIC ROUTING CALLS FOR "
        f"profile={args.expected_profile} "
        f"model={config.LLM_ROUTING_PRIMARY_MODEL}"
    )


def validate_execution(args: argparse.Namespace) -> bool:
    if config.PROFILE != args.expected_profile:
        raise SystemExit(
            f"현재 profile={config.PROFILE!r}이 "
            f"--expected-profile={args.expected_profile!r}와 다릅니다."
        )
    if not 1 <= int(args.max_calls) <= len(CASES):
        raise SystemExit(f"--max-calls는 1~{len(CASES)}여야 합니다.")
    expected = confirmation_text(args)
    if not args.run:
        print(
            "DRY-RUN: provider를 호출하지 않았습니다. 실행하려면 "
            f"--run --confirm {expected!r}"
        )
        return False
    if args.confirm != expected:
        raise SystemExit("--confirm 값이 현재 profile/model/호출 상한과 일치하지 않습니다.")
    return True


async def run(args: argparse.Namespace) -> int:
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
        expected_reasoning,
        history,
    ) in CASES[: int(args.max_calls)]:
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
            and (
                expected_memory is None
                or decision.needs_memory is expected_memory
            )
            and decision.needs_fortune_context is expected_fortune_context
            and decision.reasoning_level == expected_reasoning
        )
        print(
            "case={case} passed={passed} source={source} tools={tools} "
            "needs_memory={memory} fortune_context={fortune} "
            "reasoning={reasoning} elapsed_ms={elapsed}".format(
                case=case_name,
                passed=str(passed).lower(),
                source=decision.source,
                tools=",".join(actual_tools) or "none",
                memory=str(decision.needs_memory).lower(),
                fortune=str(decision.needs_fortune_context).lower(),
                reasoning=decision.reasoning_level or "fixed",
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
    args = parse_args()
    if not validate_execution(args):
        return 0
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())

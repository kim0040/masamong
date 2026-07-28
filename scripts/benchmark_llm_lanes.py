#!/usr/bin/env python3
"""운영/후보 LLM 레인 조합을 합성 데이터로 제한 비교한다.

비밀 키, Discord 원문, DB 데이터는 출력하거나 저장하지 않는다. 호출 수는
고정된 합성 case 수 × 반복 수 × 모델 수로 제한된다.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import re
import statistics
import sys
import time
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from utils.intent_analyzer import IntentAnalyzer  # noqa: E402
from utils.llm_client import LLMClient  # noqa: E402


@dataclass(frozen=True)
class RoutingCase:
    name: str
    query: str
    expected_tool: str | None
    expected_memory: bool
    history: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class MainCase:
    name: str
    system_prompt: str
    user_prompt: str
    validator: Callable[[str], tuple[int, list[str]]]


ROUTING_CASES = (
    RoutingCase(
        "weather_indirect",
        "서울에서 우산을 챙겨야 할지 기상 상황을 확인해 줘.",
        "get_weather_forecast",
        False,
    ),
    RoutingCase(
        "web_current",
        "이번 주 OpenAI의 공식 발표가 있었는지 확인해서 알려줘.",
        "web_search",
        False,
    ),
    RoutingCase(
        "image_semantic",
        "고양이가 달을 바라보는 장면을 시각 자료로 부탁해.",
        "generate_image",
        False,
    ),
    RoutingCase(
        "long_memory",
        "전에 우리가 정했던 여행 계획을 다시 알려줘.",
        None,
        True,
    ),
    RoutingCase(
        "no_tool_opinion",
        "검색이라는 개념에 대한 네 생각만 말해줘.",
        None,
        False,
    ),
    RoutingCase(
        "weather_followup",
        "그럼 내일은?",
        "get_weather_forecast",
        False,
        (
            {
                "role": "user",
                "speaker": "사용자",
                "is_current_user": True,
                "parts": ["이번 주말 제주도 날씨를 알려줘."],
            },
            {
                "role": "model",
                "speaker": "Masamong",
                "parts": ["제주도 주말 날씨를 확인해볼게."],
            },
        ),
    ),
)


def _memory_fidelity_validator(text: str) -> tuple[int, list[str]]:
    normalized = text.lower()
    checks = {
        "date": "8월 3일" in text or "8/3" in text,
        "time": "14:00" in text or "오후 2시" in text,
        "place": "부산역" in text,
        "negation": "자가용" in text and ("않" in text or "안 " in text),
        "unresolved": "ktx" in normalized and "미정" in text,
        "discord_format": "|---" not in text and "<table" not in normalized,
        "concise": 0 < len(text) <= 1_200,
    }
    score = round(sum(checks.values()) / len(checks) * 100)
    return score, [name for name, passed in checks.items() if not passed]


def _weather_validator(text: str) -> tuple[int, list[str]]:
    normalized = text.lower()
    checks = {
        "rain": "70" in text,
        "umbrella": "우산" in text,
        "no_fake_failure": "조회 실패" not in text,
        "discord_format": "|---" not in text and "<table" not in normalized,
        "concise": 0 < len(text) <= 1_200,
    }
    score = round(sum(checks.values()) / len(checks) * 100)
    return score, [name for name, passed in checks.items() if not passed]


def _failure_honesty_validator(text: str) -> tuple[int, list[str]]:
    normalized = text.lower()
    checks = {
        "states_failure": any(
            token in text
            for token in ("확인하지 못", "조회하지 못", "불러오지 못", "실패")
        ),
        "offers_retry": "다시" in text or "잠시 후" in text,
        "no_invented_temperature": re.search(r"\d+(?:\.\d+)?\s*도", text) is None,
        "discord_format": "|---" not in text and "<table" not in normalized,
        "concise": 0 < len(text) <= 1_200,
    }
    score = round(sum(checks.values()) / len(checks) * 100)
    return score, [name for name, passed in checks.items() if not passed]


MAIN_CASES = (
    MainCase(
        "memory_fidelity",
        (
            "Discord 봇의 최종 답변을 한국어로 작성한다. 제공된 내용만 근거로 "
            "결정 사항과 미정 사항을 분리한다. 표와 HTML은 쓰지 않는다."
        ),
        (
            "[이전 대화 압축본]\n민수는 부산 이동에 자가용을 쓰지 않기로 했다. "
            "KTX 예약은 아직 미정이다.\n"
            "[최근 대화]\n민수: 회의는 8월 3일 오후 2시 부산역이다.\n"
            "[현재 질문]\n확정된 내용과 아직 정하지 않은 내용을 짧게 알려줘."
        ),
        _memory_fidelity_validator,
    ),
    MainCase(
        "weather_grounding",
        (
            "Discord 봇의 최종 답변을 한국어로 작성한다. 도구 결과를 최우선 "
            "사실로 사용하고 없는 수치를 만들지 않는다. 표와 HTML은 쓰지 않는다."
        ),
        (
            "[도구 실행 결과]\n서울 내일 최저 24도, 최고 31도, 강수확률 70%.\n"
            "[현재 질문]\n내일 우산을 챙겨야 할지 핵심만 알려줘."
        ),
        _weather_validator,
    ),
    MainCase(
        "tool_failure_honesty",
        (
            "Discord 봇의 최종 답변을 한국어로 작성한다. 도구가 실패하면 실패를 "
            "정직하게 알리고 수치나 날씨를 추측하지 않는다. 표와 HTML은 쓰지 않는다."
        ),
        (
            "[도구 실행 결과]\n기상청 API 시간 초과로 조회 실패.\n"
            "[현재 질문]\n지금 서울 기온이 몇 도야?"
        ),
        _failure_honesty_validator,
    ),
)


class _BenchmarkRouterClient:
    def __init__(
        self,
        llm_client: LLMClient,
        target: dict[str, str],
    ):
        self._llm_client = llm_client
        self._target = target
        self.use_cometapi = True

    async def fast_generate_text(
        self,
        prompt: str,
        _model: str | None,
        log_extra: dict,
        *,
        trace_key: str = "benchmark",
        max_tokens: int | None = None,
    ) -> str | None:
        _ = trace_key
        return await self._llm_client.call_routing_lane_target(
            self._target,
            prompt=prompt,
            log_extra=log_extra,
            max_tokens=max_tokens,
        )


def _candidate_target(
    base_target: dict[str, str],
    *,
    model: str,
    reasoning_effort: str,
    name: str,
) -> dict[str, str]:
    target = deepcopy(base_target)
    target["name"] = name
    target["model"] = model
    target["reasoning_effort"] = reasoning_effort
    return target


async def _run_routing_case(
    client: LLMClient,
    target: dict[str, str],
    case: RoutingCase,
    repeat: int,
) -> dict[str, Any]:
    analyzer = IntentAnalyzer(
        db=None,
        llm_client=_BenchmarkRouterClient(client, target),
        tools_cog=None,
    )
    started = time.monotonic()
    decision = await analyzer.route_tools(
        case.query,
        {
            "trace_id": (
                f"lane-benchmark-routing-{target['model']}-{case.name}-{repeat}"
            )
        },
        history=list(case.history),
    )
    elapsed_ms = round((time.monotonic() - started) * 1_000)
    tools = [
        str(item.get("tool_to_use") or "")
        for item in decision.plan
    ]
    passed = (
        decision.source == "llm"
        and (
            (case.expected_tool is None and not tools)
            or case.expected_tool in tools
        )
        and decision.needs_memory is case.expected_memory
    )
    return {
        "lane": "routing",
        "model": target["model"],
        "reasoning_effort": target.get("reasoning_effort") or "",
        "case": case.name,
        "repeat": repeat,
        "passed": passed,
        "elapsed_ms": elapsed_ms,
        "source": decision.source,
        "tools": tools,
        "needs_memory": decision.needs_memory,
    }


async def _run_main_case(
    client: LLMClient,
    target: dict[str, str],
    case: MainCase,
    repeat: int,
) -> dict[str, Any]:
    started = time.monotonic()
    response = await client.call_main_lane_target(
        target,
        system_prompt=case.system_prompt,
        user_prompt=case.user_prompt,
        log_extra={
            "trace_id": (
                f"lane-benchmark-main-{target['model']}-{case.name}-{repeat}"
            )
        },
        max_tokens=512,
    )
    elapsed_ms = round((time.monotonic() - started) * 1_000)
    text = str(response or "").strip()
    score, missing = case.validator(text)
    return {
        "lane": "main",
        "model": target["model"],
        "reasoning_effort": target.get("reasoning_effort") or "",
        "case": case.name,
        "repeat": repeat,
        "passed": score == 100,
        "quality_score": score,
        "missing_checks": missing,
        "elapsed_ms": elapsed_ms,
        "response_chars": len(text),
        "preview": " ".join(text.split())[:240],
    }


def _summaries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["lane"], row["model"])].append(row)

    summaries: list[dict[str, Any]] = []
    for (lane, model), items in grouped.items():
        elapsed = [int(item["elapsed_ms"]) for item in items]
        summary: dict[str, Any] = {
            "lane": lane,
            "model": model,
            "cases": len(items),
            "pass_rate": round(
                sum(bool(item["passed"]) for item in items) / len(items),
                3,
            ),
            "median_ms": round(statistics.median(elapsed)),
            "min_ms": min(elapsed),
            "max_ms": max(elapsed),
        }
        if lane == "main":
            summary["mean_quality_score"] = round(
                statistics.mean(
                    int(item["quality_score"]) for item in items
                ),
                1,
            )
        summaries.append(summary)
    return sorted(summaries, key=lambda item: (item["lane"], item["model"]))


async def run(args: argparse.Namespace) -> int:
    client = LLMClient(db=None)
    routing_targets = client.get_lane_targets("routing")
    main_targets = client.get_lane_targets("main")
    if not routing_targets or not main_targets:
        print(
            json.dumps(
                {"error": "routing 또는 main 레인 설정이 없습니다."},
                ensure_ascii=False,
            )
        )
        return 2

    current_routing = routing_targets[0]
    current_main = main_targets[0]
    candidate_routing = _candidate_target(
        current_routing,
        model=args.candidate_routing_model,
        reasoning_effort=args.reasoning_effort,
        name="routing.candidate",
    )
    candidate_main = _candidate_target(
        current_main,
        model=args.candidate_main_model,
        reasoning_effort=args.reasoning_effort,
        name="main.candidate",
    )
    route_pairs = (current_routing, candidate_routing)
    main_pairs = (current_main, candidate_main)
    planned_calls = (
        (
            0
            if args.skip_routing
            else len(ROUTING_CASES) * args.routing_repeats * len(route_pairs)
        )
        + (
            0
            if args.skip_main
            else len(MAIN_CASES) * args.main_repeats * len(main_pairs)
        )
    )
    if planned_calls > args.max_calls:
        print(
            json.dumps(
                {
                    "error": "planned_calls가 max_calls를 초과합니다.",
                    "planned_calls": planned_calls,
                    "max_calls": args.max_calls,
                },
                ensure_ascii=False,
            )
        )
        return 2

    print(
        json.dumps(
            {
                "event": "benchmark_start",
                "planned_calls": planned_calls,
                "current": {
                    "routing": current_routing["model"],
                    "main": current_main["model"],
                },
                "candidate": {
                    "routing": candidate_routing["model"],
                    "main": candidate_main["model"],
                    "reasoning_effort": args.reasoning_effort,
                },
            },
            ensure_ascii=False,
        )
    )

    rows: list[dict[str, Any]] = []
    if not args.skip_routing:
        for repeat in range(1, args.routing_repeats + 1):
            for case in ROUTING_CASES:
                for target in route_pairs:
                    call_started = time.monotonic()
                    try:
                        row = await _run_routing_case(
                            client,
                            target,
                            case,
                            repeat,
                        )
                    except Exception as exc:
                        row = {
                            "lane": "routing",
                            "model": target["model"],
                            "case": case.name,
                            "repeat": repeat,
                            "passed": False,
                            "elapsed_ms": round(
                                (time.monotonic() - call_started) * 1_000
                            ),
                            "error_type": type(exc).__name__,
                        }
                    rows.append(row)
                    print(json.dumps(row, ensure_ascii=False))

    if not args.skip_main:
        for repeat in range(1, args.main_repeats + 1):
            for case in MAIN_CASES:
                for target in main_pairs:
                    call_started = time.monotonic()
                    try:
                        row = await _run_main_case(
                            client,
                            target,
                            case,
                            repeat,
                        )
                    except Exception as exc:
                        row = {
                            "lane": "main",
                            "model": target["model"],
                            "case": case.name,
                            "repeat": repeat,
                            "passed": False,
                            "quality_score": 0,
                            "elapsed_ms": round(
                                (time.monotonic() - call_started) * 1_000
                            ),
                            "error_type": type(exc).__name__,
                        }
                    rows.append(row)
                    print(json.dumps(row, ensure_ascii=False))

    summaries = _summaries(rows)
    print(
        json.dumps(
            {
                "event": "benchmark_summary",
                "summaries": summaries,
            },
            ensure_ascii=False,
        )
    )
    return 0 if all(item["pass_rate"] == 1.0 for item in summaries) else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--candidate-routing-model",
        default="deepseek-v4-flash",
    )
    parser.add_argument(
        "--candidate-main-model",
        default="deepseek-v4-pro",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=("low", "medium", "high"),
        default="low",
    )
    parser.add_argument("--routing-repeats", type=int, default=2)
    parser.add_argument("--main-repeats", type=int, default=1)
    parser.add_argument("--skip-routing", action="store_true")
    parser.add_argument("--skip-main", action="store_true")
    parser.add_argument("--max-calls", type=int, default=32)
    args = parser.parse_args()
    args.routing_repeats = max(1, min(5, args.routing_repeats))
    args.main_repeats = max(1, min(3, args.main_repeats))
    args.max_calls = max(1, min(64, args.max_calls))
    return args


def main() -> int:
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

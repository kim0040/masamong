from scripts.benchmark_llm_lanes import (
    _candidate_target,
    _failure_honesty_validator,
    _memory_fidelity_validator,
    _parallel_reasoning_validator,
    _weather_validator,
)


def test_failure_honesty_accepts_clear_lookup_failure_synonyms():
    score, missing = _failure_honesty_validator(
        "기상청 API가 시간 초과되어 기온을 가져오지 못했습니다. "
        "정확한 수치는 추측하지 않고 잠시 후 다시 조회할게요."
    )

    assert score == 100
    assert missing == []


def test_memory_fidelity_accepts_equivalent_negation_and_unresolved_phrasing():
    score, missing = _memory_fidelity_validator(
        "확정된 내용은 부산 이동 시 자가용 미사용, 회의는 8월 3일 오후 2시 "
        "부산역입니다. 아직 정하지 않은 내용은 KTX 예약 여부입니다."
    )

    assert score == 100
    assert missing == []


def test_weather_accepts_contextual_umbrella_recommendation():
    score, missing = _weather_validator(
        "네, 챙기세요. 내일 서울은 강수확률 70%입니다."
    )

    assert score == 100
    assert missing == []


def test_parallel_reasoning_allows_explicit_rejection_of_naive_answer():
    score, missing = _parallel_reasoning_validator(
        "정답은 5시간입니다. 동시에 말리므로 40시간이 아니라 5시간이 걸립니다."
    )

    assert score == 100
    assert missing == []


def test_candidate_target_can_override_inherited_provider_only():
    target = _candidate_target(
        {
            "name": "routing.primary",
            "model": "openai/gpt-5.6-luna",
            "provider_only": "openai",
        },
        model="z-ai/glm-5.3-flash",
        reasoning_effort="low",
        name="routing.candidate",
        provider_only="morph",
    )

    assert target == {
        "name": "routing.candidate",
        "model": "z-ai/glm-5.3-flash",
        "provider_only": "morph",
        "reasoning_effort": "low",
    }

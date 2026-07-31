# -*- coding: utf-8 -*-
"""Discord 대화 로그를 구조화 메모리 유닛으로 정제하는 헬퍼."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable


_WHITESPACE_RE = re.compile(r"\s+")
_NOISE_ONLY_RE = re.compile(r"^[ㅋㅎㅠㅜ!?.,~…\-\s]+$")
_TOKEN_RE = re.compile(r"[A-Za-z0-9가-힣][A-Za-z0-9가-힣:/._+\-]{1,}")

_STOPWORDS = {
    "그리고",
    "그러면",
    "그래서",
    "근데",
    "그냥",
    "나는",
    "너는",
    "우리는",
    "저는",
    "진짜",
    "그거",
    "이거",
    "저거",
    "오늘",
    "어제",
    "내일",
    "지금",
    "이제",
    "다음",
    "관련",
    "정도",
    "이번",
    "저번",
    "하나",
    "둘",
    "셋",
    "있다",
    "없다",
    "하면",
    "해도",
    "해서",
    "하는",
    "했다",
    "합니다",
    "했다가",
    "ㅋㅋ",
    "ㅎㅎ",
}

@dataclass(frozen=True)
class StructuredMemoryUnit:
    memory_id: str
    anchor_message_id: int
    owner_user_id: int | None
    owner_user_name: str
    memory_scope: str
    memory_type: str
    summary_text: str
    memory_text: str
    raw_context: str
    source_message_ids: list[int]
    speaker_names: list[str]
    keywords: list[str]
    timestamp_iso: str


def normalize_message_content(text: str) -> str:
    """메시지 저장용 기본 정제."""
    if not text:
        return ""
    normalized = text.replace("\u200b", " ").replace("\r", "\n")
    lines = []
    for raw_line in normalized.split("\n"):
        line = _WHITESPACE_RE.sub(" ", raw_line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines).strip()


def build_storage_text(
    content: str,
    *,
    attachment_count: int = 0,
    embed_count: int = 0,
    sticker_count: int = 0,
) -> str:
    """원본 로그용 메시지 본문을 구성한다."""
    base = normalize_message_content(content)
    extras: list[str] = []
    if attachment_count:
        extras.append(f"[첨부 {attachment_count}개]")
    if embed_count:
        extras.append(f"[임베드 {embed_count}개]")
    if sticker_count:
        extras.append(f"[스티커 {sticker_count}개]")
    if base and extras:
        return f"{base}\n" + " ".join(extras)
    if base:
        return base
    return " ".join(extras).strip()


def is_meaningful_text(text: str, *, min_chars: int = 2) -> bool:
    """노이즈(ㅋㅋ, 이모티콘 등)를 제외하고 유의미한 텍스트인지 판별합니다."""
    cleaned = normalize_message_content(text)
    if not cleaned:
        return False
    if _NOISE_ONLY_RE.fullmatch(cleaned):
        return False
    compact = re.sub(r"[^A-Za-z0-9가-힣]", "", cleaned)
    if len(compact) >= min_chars:
        return True
    return bool(_TOKEN_RE.search(cleaned))


def extract_keywords(text: str, *, limit: int = 8) -> list[str]:
    """텍스트에서 불용어를 제외한 핵심 키워드를 최대 limit개 추출합니다."""
    seen: set[str] = set()
    keywords: list[str] = []
    for token in _TOKEN_RE.findall(text or ""):
        cleaned_token = token.strip().strip(":")
        norm = cleaned_token.lower()
        if len(norm) < 2 or norm in _STOPWORDS:
            continue
        if norm in seen:
            continue
        seen.add(norm)
        keywords.append(cleaned_token)
        if len(keywords) >= limit:
            break
    return keywords


_EXPLICIT_PREFERENCE_PATTERN = re.compile(
    r"(?:"
    r"(?:나는|난|저는|전|내가|제가|내\s*취향(?:은|이)?|제\s*취향(?:은|이)?)"
    r".{0,80}?(?:좋아|싫어|선호|즐겨)"
    r"|"
    r"(?:좋아해|좋아함|좋아한다|싫어해|싫어함|싫어한다|선호해|선호함|즐겨\s*(?:먹|보|하))"
    r")",
    re.IGNORECASE,
)
_THIRD_PERSON_PREFERENCE_PATTERN = re.compile(
    r"(?:너|넌|네가|니가|쟤|걔|그 사람|누가).{0,50}"
    r"(?:좋아|싫어|선호|취향|즐겨)"
    r"|(?:좋아|싫어|선호)(?:하네|하냐|하니|하나|한다며|하는구나)",
    re.IGNORECASE,
)
_EXPLICIT_PLAN_PATTERN = re.compile(
    r"(?:"
    r"(?:내일|모레|다음\s*주|이번\s*주|주말|"
    r"\d{1,2}\s*(?:월|일|시|분)|월요일|화요일|수요일|목요일|금요일|토요일|일요일)"
    r".{0,80}?(?:약속|일정|예정|예약|만나|가야|해야|할게|하기로|준비)"
    r"|"
    r"(?:나는|난|저는|전|내가|제가).{0,80}?"
    r"(?:할게|하기로|가야|해야|예정|예약|준비)"
    r")",
    re.IGNORECASE,
)
_ASSISTANT_COMMITMENT_PATTERN = re.compile(
    r"(?:"
    r"(?:내\s*선택|내\s*취향|나라면|나는|난|내가).{0,100}?"
    r"(?:고르|선택|좋아|싫어|선호|편이야|쪽이야)"
    r"|"
    r"(?:고르라면|둘\s*중(?:에)?).{0,100}?(?:고르|선택)"
    r")",
    re.IGNORECASE,
)


def classify_memory_type(text: str, *, speaker_count: int, owner_specific: bool) -> str:
    """대화 내용을 지속성이 있는 메모리 유형으로 보수적으로 분류합니다.

    단순한 ``좋아``(동의), ``좋아하네``(상대방 평가), ``해야``(일반 조언)까지
    개인 선호·계획으로 저장하면 검색 시 엉뚱한 개인화가 발생한다. 자기 진술과
    구체적인 미래 표지가 있는 경우만 preference/plan으로 올립니다.
    """
    lowered = (text or "").lower()
    preference_match = bool(_EXPLICIT_PREFERENCE_PATTERN.search(lowered))
    if preference_match and not (
        owner_specific and _THIRD_PERSON_PREFERENCE_PATTERN.search(lowered)
    ):
        return "preference"
    if _EXPLICIT_PLAN_PATTERN.search(lowered):
        return "plan"
    if any(keyword in lowered for keyword in ("출근", "퇴근", "학교", "회사", "직장", "시험", "면접")):
        return "profile" if owner_specific else "event"
    if speaker_count > 1 and not owner_specific:
        return "shared_context"
    return "conversation"


def is_assistant_commitment(text: str) -> bool:
    """봇이 자신의 선택·취향을 명시적으로 고정한 답변인지 판별합니다."""
    return bool(_ASSISTANT_COMMITMENT_PATTERN.search(text or ""))


def truncate_text(text: str, limit: int) -> str:
    """텍스트를 limit 글자로 자르고 말줄임표를 붙입니다."""
    cleaned = normalize_message_content(text)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)].rstrip() + "…"


def _compact_for_containment(text: str) -> str:
    """포함 관계 비교용으로 화자 표지·공백·말줄임표를 제거합니다.

    봇 응답은 요약에 ``이전 답변`` 출처 표지를, 원문에는 짧은 화자 표지를
    붙여 저장한다. 표지는 다르지만 본문이 같을 때 두 번 이어 붙이지 않도록
    비교할 때만 한 줄짜리 선두 표지를 제거한다. 실제 저장 열은 바꾸지 않는다.
    """
    normalized = str(text or "").strip()
    normalized = re.sub(
        r"^[^:\n]{1,64}의\s*이전\s*답변\s*:\s*",
        "",
        normalized,
        count=1,
    )
    normalized = re.sub(
        r"^[^:\n]{1,64}\s*:\s*",
        "",
        normalized,
        count=1,
    )
    return _WHITESPACE_RE.sub("", normalized).rstrip("…")


def compose_memory_text(
    summary_text: str,
    raw_context: str,
    *,
    limit: int,
    keywords: Iterable[str] = (),
    speaker_names: Iterable[str] = (),
    memory_type: str = "conversation",
    timestamp_iso: str = "",
) -> str:
    """E5 passage 임베딩용 독립 문서를 간결하게 구성한다.

    합성 질의 실측에서 유형·참여자·날짜·키워드 라벨을 본문에 반복하면
    E5의 관련 문서 분리 여유가 낮아졌다. 이 값들은 이미 별도 DB 열에
    보존하므로 벡터 입력에는 독립 요약과 화자 포함 원문만 넣는다. 라벨도
    제거해 의미 신호를 희석하지 않되, 요약 누락에 대비해 원문 근거는 남긴다.

    한 사람의 짧은 발화로 만든 유닛은 요약과 원문이 같은 문장이다. 둘을
    그대로 이으면 같은 내용이 벡터 본문과 프롬프트에 두 번 들어가, 흔한
    표현의 가중치만 올라가고 기억 블록 예산도 절반이 낭비된다. 운영 기억
    실측에서 개인 범위 유닛의 62%가 이 경우였으므로, 한쪽이 다른 쪽에
    포함되면 더 많은 정보를 담은 쪽 하나만 남긴다.
    """
    # 호출 계약과 metadata 저장 코드를 단순하게 유지하기 위한 인자다.
    # 임베딩 본문에는 넣지 않고 StructuredMemoryUnit의 별도 열에 보존한다.
    _ = keywords, speaker_names, memory_type, timestamp_iso
    summary = normalize_message_content(summary_text)
    context = normalize_message_content(raw_context)
    if summary and context:
        compact_summary = _compact_for_containment(summary)
        compact_context = _compact_for_containment(context)
        if compact_summary and compact_context:
            if compact_summary in compact_context:
                return truncate_text(context, limit)
            if compact_context in compact_summary:
                return truncate_text(summary, limit)
        return truncate_text(f"{summary}\n{context}", limit)
    return truncate_text(summary or context, limit)


def merge_payload_to_turns(payload: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """동일 화자의 연속 메시지를 하나의 turn으로 병합합니다."""
    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for item in payload:
        content = normalize_message_content(str(item.get("content") or ""))
        if not is_meaningful_text(content):
            continue

        message_id = item.get("message_id")
        try:
            message_id_int = int(message_id)
        except (TypeError, ValueError):
            continue

        user_id = item.get("user_id")
        try:
            user_id_int = int(user_id)
        except (TypeError, ValueError):
            user_id_int = None

        user_name = str(item.get("user_name") or "Unknown").strip() or "Unknown"
        created_at = str(item.get("created_at") or "")
        is_bot = bool(item.get("is_bot"))

        if (
            current
            and current["user_id"] == user_id_int
            and current["user_name"] == user_name
            and current["is_bot"] == is_bot
        ):
            current["contents"].append(content)
            current["message_ids"].append(message_id_int)
            current["end_at"] = created_at
            continue

        current = {
            "user_id": user_id_int,
            "user_name": user_name,
            "is_bot": is_bot,
            "contents": [content],
            "message_ids": [message_id_int],
            "start_at": created_at,
            "end_at": created_at,
        }
        turns.append(current)

    return turns


def _build_context_lines(turns: list[dict[str, Any]], *, max_turns: int = 8, max_line_chars: int = 180) -> list[str]:
    """턴 데이터를 `화자명: 내용` 형식의 요약 라인으로 변환합니다."""
    lines: list[str] = []
    for turn in turns[:max_turns]:
        merged = " ".join(turn["contents"])
        lines.append(f"{turn['user_name']}: {truncate_text(merged, max_line_chars)}")
    return lines


def build_assistant_memory_unit(
    payload: Iterable[dict[str, Any]],
    *,
    channel_id: int,
    memory_scope: str,
    max_summary_chars: int = 500,
    max_context_chars: int = 1_600,
) -> StructuredMemoryUnit | None:
    """전송 완료된 봇 응답을 추가 LLM 호출 없이 검색 가능한 한 유닛으로 만듭니다.

    Discord의 분할 메시지는 하나의 답변이므로 연속 청크를 합쳐 한 번만
    임베딩합니다. 일반 답변도 ``assistant_response``로 보존하고, 명시적으로
    자신의 선택·취향을 말한 답변은 ``assistant_commitment``로 표시합니다.
    """
    bot_turns = [
        turn
        for turn in merge_payload_to_turns(payload)
        if bool(turn.get("is_bot"))
    ]
    if not bot_turns:
        return None

    message_ids = [
        message_id
        for turn in bot_turns
        for message_id in turn["message_ids"]
    ]
    if not message_ids:
        return None

    merged = "\n".join(
        " ".join(str(content) for content in turn["contents"])
        for turn in bot_turns
    )
    if not is_meaningful_text(merged, min_chars=8):
        return None

    user_name = str(bot_turns[0].get("user_name") or "마사몽")
    memory_type = (
        "assistant_commitment"
        if is_assistant_commitment(merged)
        else "assistant_response"
    )
    raw_context = truncate_text(
        f"{user_name}: {merged}",
        max_context_chars,
    )
    summary_text = truncate_text(
        f"{user_name}의 이전 답변: {merged}",
        max_summary_chars,
    )
    timestamp_iso = str(bot_turns[-1].get("end_at") or "")
    return StructuredMemoryUnit(
        memory_id=(
            f"assistant:{channel_id}:{message_ids[0]}:{message_ids[-1]}"
        ),
        anchor_message_id=message_ids[-1],
        owner_user_id=None,
        owner_user_name=user_name,
        memory_scope=memory_scope,
        memory_type=memory_type,
        summary_text=summary_text,
        memory_text=compose_memory_text(
            summary_text,
            raw_context,
            limit=max(max_summary_chars, max_context_chars),
            keywords=extract_keywords(merged),
            speaker_names=[user_name],
            memory_type=memory_type,
            timestamp_iso=timestamp_iso,
        ),
        raw_context=raw_context,
        source_message_ids=message_ids,
        speaker_names=[user_name],
        keywords=extract_keywords(merged),
        timestamp_iso=timestamp_iso,
    )


def build_structured_memory_units(
    payload: Iterable[dict[str, Any]],
    *,
    channel_id: int,
    max_summary_chars: int = 320,
    max_context_chars: int = 1200,
    user_turn_min_chars: int = 12,
    shared_scope: str = "channel",
    user_scope: str = "user",
) -> list[StructuredMemoryUnit]:
    """대화 페이로드를 채널 공유 메모리와 사용자별 메모리 유닛으로 변환합니다."""
    turns = merge_payload_to_turns(payload)
    if not turns:
        return []

    speaker_names = list(dict.fromkeys(str(turn["user_name"]) for turn in turns))
    all_message_ids = [mid for turn in turns for mid in turn["message_ids"]]
    anchor_message_id = all_message_ids[-1]
    timestamp_iso = str(turns[-1]["end_at"] or "")
    context_lines = _build_context_lines(turns)
    raw_context = truncate_text("\n".join(context_lines), max_context_chars)
    full_text = "\n".join(context_lines)
    speaker_name_tokens = {name.strip().lower() for name in speaker_names if name.strip()}
    keywords = [token for token in extract_keywords(full_text) if token.lower() not in speaker_name_tokens]
    participants = ", ".join(speaker_names[:4])
    shared_body = " / ".join(context_lines)
    shared_summary = (
        f"{participants}의 대화: "
        f"{truncate_text(shared_body, max_summary_chars)}"
    )
    shared_memory_type = classify_memory_type(
        full_text,
        speaker_count=len(speaker_names),
        owner_specific=False,
    )
    shared_memory_text = compose_memory_text(
        shared_summary,
        raw_context,
        limit=max(max_context_chars, max_summary_chars),
        keywords=keywords,
        speaker_names=speaker_names,
        memory_type=shared_memory_type,
        timestamp_iso=timestamp_iso,
    )

    units: list[StructuredMemoryUnit] = [
        StructuredMemoryUnit(
            memory_id=f"channel:{channel_id}:{all_message_ids[0]}:{anchor_message_id}",
            anchor_message_id=anchor_message_id,
            owner_user_id=None,
            owner_user_name="Shared Memory",
            memory_scope=shared_scope,
            memory_type=shared_memory_type,
            summary_text=truncate_text(shared_summary, max_summary_chars),
            memory_text=shared_memory_text,
            raw_context=raw_context,
            source_message_ids=all_message_ids,
            speaker_names=speaker_names,
            keywords=keywords,
            timestamp_iso=timestamp_iso,
        )
    ]

    grouped_turns: dict[tuple[int | None, str, bool], dict[str, Any]] = {}
    for turn in turns:
        key = (turn["user_id"], turn["user_name"], bool(turn["is_bot"]))
        bucket = grouped_turns.setdefault(
            key,
            {
                "user_id": turn["user_id"],
                "user_name": turn["user_name"],
                "is_bot": bool(turn["is_bot"]),
                "contents": [],
                "message_ids": [],
                "end_at": turn["end_at"],
            },
        )
        bucket["contents"].extend(turn["contents"])
        bucket["message_ids"].extend(turn["message_ids"])
        bucket["end_at"] = turn["end_at"]

    for grouped in grouped_turns.values():
        merged = " ".join(grouped["contents"])
        compact = re.sub(r"[^A-Za-z0-9가-힣]", "", merged)
        if len(compact) < user_turn_min_chars:
            continue
        if grouped["is_bot"] and not is_assistant_commitment(merged):
            # 일반 봇 답변은 공유 대화 유닛에만 남기고, 사용자 개인 기억처럼
            # 별도 복제하지 않는다. 명시적인 자기 선택만 assistant 전용
            # 기억으로 만들어 이후 답변의 일관성을 보강한다.
            continue
        owner_keywords = [
            token
            for token in extract_keywords(merged)
            if token.lower() != str(grouped["user_name"]).strip().lower()
        ]
        owner_summary = (
            f"{grouped['user_name']}: "
            f"{truncate_text(merged, max_summary_chars)}"
        )
        owner_raw_context = truncate_text(f"{grouped['user_name']}: {merged}", max_context_chars)
        owner_memory_type = (
            "assistant_commitment"
            if grouped["is_bot"]
            else classify_memory_type(
                merged,
                speaker_count=1,
                owner_specific=True,
            )
        )
        owner_scope = shared_scope if grouped["is_bot"] else user_scope
        memory_prefix = "assistant" if grouped["is_bot"] else "user"
        units.append(
            StructuredMemoryUnit(
                memory_id=(
                    f"{memory_prefix}:{grouped['user_id'] or 0}:"
                    f"{grouped['message_ids'][0]}:{grouped['message_ids'][-1]}"
                ),
                anchor_message_id=grouped["message_ids"][-1],
                owner_user_id=(
                    None if grouped["is_bot"] else grouped["user_id"]
                ),
                owner_user_name=grouped["user_name"],
                memory_scope=owner_scope,
                memory_type=owner_memory_type,
                summary_text=truncate_text(owner_summary, max_summary_chars),
                memory_text=compose_memory_text(
                    owner_summary,
                    owner_raw_context,
                    limit=max(max_context_chars, max_summary_chars),
                    keywords=owner_keywords,
                    speaker_names=[grouped["user_name"]],
                    memory_type=owner_memory_type,
                    timestamp_iso=str(grouped["end_at"] or ""),
                ),
                raw_context=owner_raw_context,
                source_message_ids=list(grouped["message_ids"]),
                speaker_names=[grouped["user_name"]],
                keywords=owner_keywords,
                timestamp_iso=str(grouped["end_at"] or ""),
            )
        )

    return units

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


def classify_memory_type(text: str, *, speaker_count: int, owner_specific: bool) -> str:
    """대화 내용을 분석하여 메모리 유형(preference/plan/profile/event/shared_context/conversation)을 분류합니다."""
    lowered = (text or "").lower()
    if any(keyword in lowered for keyword in ("좋아", "싫어", "선호", "취향", "자주", "즐겨")):
        return "preference"
    if any(keyword in lowered for keyword in ("약속", "일정", "해야", "할게", "해야지", "준비", "예정")):
        return "plan"
    if any(keyword in lowered for keyword in ("출근", "퇴근", "학교", "회사", "직장", "시험", "면접")):
        return "profile" if owner_specific else "event"
    if speaker_count > 1 and not owner_specific:
        return "shared_context"
    return "conversation"


def truncate_text(text: str, limit: int) -> str:
    """텍스트를 limit 글자로 자르고 말줄임표를 붙입니다."""
    cleaned = normalize_message_content(text)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)].rstrip() + "…"


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
    """
    # 호출 계약과 metadata 저장 코드를 단순하게 유지하기 위한 인자다.
    # 임베딩 본문에는 넣지 않고 StructuredMemoryUnit의 별도 열에 보존한다.
    _ = keywords, speaker_names, memory_type, timestamp_iso
    summary = normalize_message_content(summary_text)
    context = normalize_message_content(raw_context)
    if summary and context:
        return truncate_text(f"{summary}\n{context}", limit)
    return truncate_text(summary or context, limit)


def merge_payload_to_turns(payload: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """동일 화자의 연속 메시지를 하나의 turn으로 병합합니다."""
    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for item in payload:
        if bool(item.get("is_bot")):
            continue
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

        if current and current["user_id"] == user_id_int and current["user_name"] == user_name:
            current["contents"].append(content)
            current["message_ids"].append(message_id_int)
            current["end_at"] = created_at
            continue

        current = {
            "user_id": user_id_int,
            "user_name": user_name,
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


def build_structured_memory_units(
    payload: Iterable[dict[str, Any]],
    *,
    channel_id: int,
    max_summary_chars: int = 320,
    max_context_chars: int = 1200,
    user_turn_min_chars: int = 12,
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
            memory_scope="channel",
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

    grouped_turns: dict[tuple[int | None, str], dict[str, Any]] = {}
    for turn in turns:
        key = (turn["user_id"], turn["user_name"])
        bucket = grouped_turns.setdefault(
            key,
            {
                "user_id": turn["user_id"],
                "user_name": turn["user_name"],
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
        owner_memory_type = classify_memory_type(
            merged,
            speaker_count=1,
            owner_specific=True,
        )
        units.append(
            StructuredMemoryUnit(
                memory_id=(
                    f"user:{grouped['user_id'] or 0}:{grouped['message_ids'][0]}:{grouped['message_ids'][-1]}"
                ),
                anchor_message_id=grouped["message_ids"][-1],
                owner_user_id=grouped["user_id"],
                owner_user_name=grouped["user_name"],
                memory_scope="user",
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

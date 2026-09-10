# -*- coding: utf-8 -*-
"""시세 도구용 질의 해석. 기업 별칭 표가 아니라 ISO 통화·명시 티커만 다룹니다."""

from __future__ import annotations

import re

_TICKER_RE = re.compile(r"^[A-Z0-9^][A-Z0-9.^=:-]{0,24}$")
_FX_SYMBOL_RE = re.compile(r"^([A-Z]{3})([A-Z]{3})=X$")
_ISO_PAIR_RE = re.compile(r"\b([A-Z]{3})\s*/\s*([A-Z]{3})\b")

_CURRENCY_ALIASES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("usd", "달러", "미국달러", "미국 달러", "미국돈", "미국 돈"), "USD"),
    (("jpy", "엔화", "일본엔", "일본 엔", "엔"), "JPY"),
    (("eur", "유로", "유로화"), "EUR"),
    (("gbp", "파운드", "영국파운드", "sterling"), "GBP"),
    (("cny", "위안", "위안화", "위엔", "인민폐"), "CNY"),
    (("aud", "호주달러", "호주 달러"), "AUD"),
    (("cad", "캐나다달러", "캐나다 달러"), "CAD"),
    (("chf", "프랑", "스위스프랑"), "CHF"),
    (("hkd", "홍콩달러", "홍콩 달러"), "HKD"),
    (("sgd", "싱가포르달러", "싱가포르 달러"), "SGD"),
    (("krw", "원화", "한화", "한국돈", "한국 돈"), "KRW"),
)

_FX_INTENT = (
    "환율", "환전", "환산", "fx", "exchange rate", "얼마", "몇원", "몇 원",
)


KR_MARKET_UNSUPPORTED = (
    "국내 상장 종목과 코스피·코스닥 지수는 조회하지 않아요. "
    "미국 상장 종목, 암호화폐, 환율로 물어봐 주세요."
)


def looks_like_ticker(value: str) -> bool:
    text = str(value or "").strip().upper()
    return bool(text) and bool(_TICKER_RE.fullmatch(text))


def is_kr_listing(symbol: str) -> bool:
    text = str(symbol or "").strip().upper()
    return text.endswith(".KS") or text.endswith(".KQ")


def kr_market_unsupported_result() -> dict:
    return {
        "status": "error",
        "error": KR_MARKET_UNSUPPORTED,
        "failure_kind": "unsupported_market",
        "provider_failure": False,
    }


def detect_fx_pair(query: str) -> tuple[str, str] | None:
    """질의에서 (base, quote) ISO 통화쌍을 고릅니다.

    한국 사용자 기본은 외화 1단위당 원화입니다. 달러-엔처럼 외화끼리면
    나온 순서대로 base/quote를 씁니다.
    """
    text = str(query or "").strip()
    if not text:
        return None
    folded = text.casefold()

    from_symbol = _FX_SYMBOL_RE.fullmatch(text.replace(" ", "").upper())
    if from_symbol:
        return from_symbol.group(1), from_symbol.group(2)

    iso_pair = _ISO_PAIR_RE.search(text.upper())
    if iso_pair:
        return iso_pair.group(1), iso_pair.group(2)

    found: list[str] = []
    for aliases, code in _CURRENCY_ALIASES:
        for alias in aliases:
            if alias == "엔":
                if not re.search(r"(?<![가-힣])엔(?![가-힣])", text):
                    continue
            elif alias.isascii():
                if alias not in folded:
                    continue
            elif alias not in text:
                continue
            if code not in found:
                found.append(code)
            break

    if "원" in text and "KRW" not in found and any(
        marker in text for marker in ("환율", "환전", "환산", "몇원", "몇 원")
    ):
        found.append("KRW")

    fx_intent = any(marker in text or marker in folded for marker in _FX_INTENT)
    if not found:
        return None
    if not fx_intent and set(found) <= {"KRW"}:
        return None
    if len(found) == 1:
        if found[0] == "KRW":
            return None
        if fx_intent or found[0] != "KRW":
            return found[0], "KRW"
        return None
    if "KRW" in found:
        foreign = next(code for code in found if code != "KRW")
        return foreign, "KRW"
    return found[0], found[1]


def detect_fx_symbol(query: str) -> str | None:
    pair = detect_fx_pair(query)
    if not pair:
        return None
    return f"{pair[0]}{pair[1]}=X"

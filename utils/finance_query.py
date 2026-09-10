# -*- coding: utf-8 -*-
"""시세 도구용 질의 해석. 기업 별칭 표가 아니라 ISO 통화·명시 티커만 다룹니다."""

from __future__ import annotations

import re

_TICKER_RE = re.compile(r"^[A-Z0-9^][A-Z0-9.^=:-]{0,24}$")
_FX_SYMBOL_RE = re.compile(r"^([A-Z]{3})([A-Z]{3})=X$")
_ISO_PAIR_RE = re.compile(r"\b([A-Z]{3})\s*/\s*([A-Z]{3})\b")
_ISO_4217 = frozenset({
    "AED", "AFN", "ALL", "AMD", "ANG", "AOA", "ARS", "AUD", "AWG", "AZN",
    "BAM", "BBD", "BDT", "BGN", "BHD", "BIF", "BMD", "BND", "BOB", "BRL",
    "BSD", "BTN", "BWP", "BYN", "BZD", "CAD", "CDF", "CHF", "CLP", "CNY",
    "COP", "CRC", "CUP", "CVE", "CZK", "DJF", "DKK", "DOP", "DZD", "EGP",
    "ERN", "ETB", "EUR", "FJD", "FKP", "GBP", "GEL", "GHS", "GIP", "GMD",
    "GNF", "GTQ", "GYD", "HKD", "HNL", "HRK", "HTG", "HUF", "IDR", "ILS",
    "INR", "IQD", "IRR", "ISK", "JMD", "JOD", "JPY", "KES", "KGS", "KHR",
    "KMF", "KRW", "KWD", "KYD", "KZT", "LAK", "LBP", "LKR", "LRD", "LSL",
    "LYD", "MAD", "MDL", "MGA", "MKD", "MMK", "MNT", "MOP", "MRU", "MUR",
    "MVR", "MWK", "MXN", "MYR", "MZN", "NAD", "NGN", "NIO", "NOK", "NPR",
    "NZD", "OMR", "PAB", "PEN", "PGK", "PHP", "PKR", "PLN", "PYG", "QAR",
    "RON", "RSD", "RUB", "RWF", "SAR", "SBD", "SCR", "SDG", "SEK", "SGD",
    "SHP", "SLE", "SOS", "SRD", "SSP", "STN", "SVC", "SYP", "SZL", "THB",
    "TJS", "TMT", "TND", "TOP", "TRY", "TTD", "TWD", "TZS", "UAH", "UGX",
    "USD", "UYU", "UZS", "VES", "VND", "VUV", "WST", "XAF", "XCD", "XDR",
    "XOF", "XPF", "YER", "ZAR", "ZMW",
})

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
    "알려", "시세", "해줘", "궁금",
)
_YEN_TOKEN_RE = re.compile(
    r"(?<![가-힣])(?:엔화|엔만|엔(?=[은는을를이가\s,.\?!]|$))"
)
_AMOUNT_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*(만|억)?\s*"
    r"(달러|엔화|엔|유로|파운드|위안|원|usd|jpy|eur|gbp|cny|krw)?",
    re.IGNORECASE,
)
_AMOUNT_CURRENCY = {
    "달러": "USD",
    "엔화": "JPY",
    "엔": "JPY",
    "유로": "EUR",
    "파운드": "GBP",
    "위안": "CNY",
    "원": "KRW",
    "usd": "USD",
    "jpy": "JPY",
    "eur": "EUR",
    "gbp": "GBP",
    "cny": "CNY",
    "krw": "KRW",
}
_CURRENCY_KO = {
    "USD": "달러",
    "JPY": "엔",
    "EUR": "유로",
    "GBP": "파운드",
    "CNY": "위안",
    "KRW": "원",
    "AUD": "호주달러",
    "CAD": "캐나다달러",
    "CHF": "프랑",
    "HKD": "홍콩달러",
    "SGD": "싱가포르달러",
    "THB": "바트",
}


KR_MARKET_UNSUPPORTED = (
    "국내 상장 종목과 코스피·코스닥 지수는 조회하지 않아요. "
    "미국 상장 종목, 암호화폐, 환율로 물어봐 주세요."
)


def looks_like_ticker(value: str) -> bool:
    """명시 티커만 통과시킵니다. 회사 영문명(NVIDIA, BITCOIN)은 검색에 맡깁니다."""
    text = str(value or "").strip().upper()
    if not text or not _TICKER_RE.fullmatch(text):
        return False
    if any(mark in text for mark in (":", ".", "^", "=")) or any(
        ch.isdigit() for ch in text
    ):
        return True
    return 1 <= len(text) <= 5


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
                if not _YEN_TOKEN_RE.search(text):
                    continue
            elif alias.isascii():
                if alias not in folded:
                    continue
            elif alias not in text:
                continue
            if code not in found:
                found.append(code)
            break

    for code in re.findall(r"\b([A-Z]{3})\b", text.upper()):
        if code in _ISO_4217 and code not in found:
            found.append(code)

    if "원" in text and "KRW" not in found and any(
        marker in text for marker in ("환율", "환전", "환산", "몇원", "몇 원")
    ):
        found.append("KRW")

    fx_intent = any(marker in text or marker in folded for marker in _FX_INTENT)
    if not found:
        if fx_intent and "환율" in text:
            return "USD", "KRW"
        return None
    if not fx_intent and set(found) <= {"KRW"}:
        return None
    if len(found) == 1:
        if found[0] == "KRW":
            return None
        return found[0], "KRW"
    if "KRW" in found:
        foreign = next(code for code in found if code != "KRW")
        return foreign, "KRW"
    return found[0], found[1]


def detect_fx_symbol(query: str) -> str | None:
    pair = detect_fx_pair(query)
    if not pair:
        return None
    return f"{pair[0]}{pair[1]}=X"


def needs_listed_name_refinement(query: str, hint_symbol: str | None = None) -> bool:
    """한글이 섞인 종목 질의는 검색 API 전에 LLM이 영문 상장명으로 정제한다.

    티커·환율 질의는 API가 이미 해석하므로 LLM을 끼우지 않는다. 회사 목록을
    코드에 쌓지 않는다.
    """
    text = str(query or "").strip()
    if not text:
        return False
    hint = str(hint_symbol or "").strip()
    if looks_like_ticker(text.upper()) and " " not in text:
        return False
    if hint and looks_like_ticker(hint.upper()):
        return False
    if detect_fx_pair(text):
        return False
    return bool(re.search(r"[가-힣]", text))


def split_quote_query(
    user_query: str | None = None,
    symbol: str | None = None,
    stock_name: str | None = None,
) -> tuple[str, str]:
    """시세 도구 인자를 (자연어, 명시 티커)로 나눕니다."""
    raw_symbol = str(symbol or "").strip()
    query_text = str(user_query or stock_name or raw_symbol or "").strip()
    direct = raw_symbol.upper()
    if direct and not looks_like_ticker(direct):
        if not query_text:
            query_text = raw_symbol
        direct = ""
    return query_text, direct


def looks_like_fx_quote(result: dict | None) -> bool:
    """성공한 시세 결과가 환율인지 판별합니다."""
    if not isinstance(result, dict) or result.get("status") != "success":
        return False
    if result.get("provider") == "exchangerate-api":
        return True
    pair = result.get("pair")
    if isinstance(pair, dict) and pair.get("base") and pair.get("quote"):
        return True
    symbol = str(result.get("symbol") or "").replace(" ", "").upper()
    return bool(_FX_SYMBOL_RE.fullmatch(symbol))


def _format_fx_amount(value: float) -> str:
    text = f"{float(value):,.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _query_conversion_lines(
    query: str,
    base: str,
    quote: str,
    rate: float,
) -> list[str]:
    """질문에 금액이 있으면 조회된 환율로만 환산합니다."""
    if rate == 0:
        return []
    lines: list[str] = []
    seen: set[str] = set()
    for match in _AMOUNT_RE.finditer(str(query or "")):
        raw_number = float(match.group(1).replace(",", ""))
        scale = match.group(2) or ""
        if scale == "만":
            raw_number *= 10_000
        elif scale == "억":
            raw_number *= 100_000_000
        word = (match.group(3) or "").casefold()
        source = _AMOUNT_CURRENCY.get(word) or _AMOUNT_CURRENCY.get(
            (match.group(3) or "").upper()
        )
        if source is None:
            if 1900 <= raw_number <= 2100:
                continue
            source = base
        if source == base:
            converted = raw_number * float(rate)
            target = quote
        elif source == quote:
            converted = raw_number / float(rate)
            target = base
        else:
            continue
        label = (
            f"{_format_fx_amount(raw_number)} {_CURRENCY_KO.get(source, source)} = "
            f"{_format_fx_amount(converted)} {_CURRENCY_KO.get(target, target)}"
        )
        if label in seen:
            continue
        seen.add(label)
        lines.append(label)
        if len(lines) >= 3:
            break
    return lines


def format_fx_user_reply(result: dict, query: str = "") -> str:
    """LLM 없이 조회된 환율만 사용자에게 보여 줍니다."""
    pair = result.get("pair") if isinstance(result.get("pair"), dict) else {}
    symbol = str(result.get("symbol") or "").replace(" ", "").upper()
    parsed = _FX_SYMBOL_RE.fullmatch(symbol)
    base = str(pair.get("base") or (parsed.group(1) if parsed else "")).upper()
    quote = str(
        pair.get("quote") or (parsed.group(2) if parsed else result.get("currency") or "")
    ).upper()
    rate = pair.get("rate")
    if not isinstance(rate, (int, float)):
        rate = result.get("price")
    if not base or not quote or not isinstance(rate, (int, float)):
        return ""
    inverse = pair.get("inverse")
    if not isinstance(inverse, (int, float)) and float(rate) != 0:
        inverse = 1.0 / float(rate)
    base_ko = _CURRENCY_KO.get(base, base)
    quote_ko = _CURRENCY_KO.get(quote, quote)
    lines = [
        f"{base_ko}-{quote_ko} 환율이에요. "
        f"1{base_ko} = {_format_fx_amount(float(rate))}{quote_ko}."
    ]
    if base == "JPY" and quote == "KRW":
        lines.append(
            f"한국에서 자주 쓰는 기준으로는 100엔 = "
            f"{float(rate) * 100:,.2f}원이에요."
        )
    elif isinstance(inverse, (int, float)):
        lines.append(
            f"거꾸로 보면 1{quote_ko} = {_format_fx_amount(float(inverse))}{base_ko}."
        )
    lines.extend(_query_conversion_lines(query, base, quote, float(rate)))
    checked = str(result.get("checked_at_kst") or "").strip()
    if checked:
        lines.append(f"조회 시각(KST): {checked}")
    return "\n".join(lines)


def format_quote_user_reply(result: dict, query: str = "") -> str:
    """성공한 시세 조회를 LLM 없이 렌더링합니다."""
    if looks_like_fx_quote(result):
        return format_fx_user_reply(result, query)
    if not isinstance(result, dict) or result.get("status") != "success":
        return ""
    price = result.get("price")
    if not isinstance(price, (int, float)):
        return ""
    name = result.get("name") or result.get("symbol") or "종목"
    currency = result.get("currency") or ""
    change = result.get("change_percent")
    change_text = (
        f", {float(change):+.2f}%"
        if isinstance(change, (int, float))
        else ""
    )
    lines = [f"{name} 현재가 {float(price):,.2f} {currency}{change_text}."]
    checked = str(result.get("checked_at_kst") or "").strip()
    if checked:
        lines.append(f"조회 시각(KST): {checked}")
    return "\n".join(lines)

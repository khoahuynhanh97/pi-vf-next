"""Deterministic text normalization utilities.

All functions are deterministic and testable. Ambiguous input returns the raw
evidence without fabricating a normalized value.
"""

from __future__ import annotations

import re

_CURRENCY_BY_SYMBOL = {"₫": "VND", "đ": "VND", "¥": "JPY"}
_CURRENCY_BY_CODE = {"VND": "VND", "USD": "USD", "JPY": "JPY", "KRW": "KRW", "EUR": "EUR", "THB": "THB", "CNY": "CNY"}


def normalize_whitespace(value) -> str:
    return " ".join(str(value or "").split()).strip()


def parse_currency(value: str) -> str | None:
    """Detect an explicit currency code or an unambiguous symbol."""
    text = normalize_whitespace(value)
    for code in _CURRENCY_BY_CODE:
        if re.search(rf"\b{code}\b", text, re.IGNORECASE):
            return code
    for symbol, code in _CURRENCY_BY_SYMBOL.items():
        if symbol in text:
            return code
    return None


def parse_price(value: str) -> tuple[int | float | None, str | None, str]:
    """Parse a price string into (amount, currency, raw).

    ``$`` is deliberately NOT mapped to a currency without context; treat it as
    ambiguous unless a code or an unambiguous symbol is present.
    """
    raw = normalize_whitespace(value)
    if not raw:
        return None, None, raw
    currency = parse_currency(raw)
    amount_text = re.sub(r"[^\d.,]", "", raw)
    if not amount_text:
        return None, currency, raw
    if currency == "VND":
        cleaned = amount_text.replace(",", "").replace(".", "")
        return _to_int(cleaned), "VND", raw
    if currency == "JPY":
        cleaned = amount_text.replace(",", "")
        return _to_int(cleaned), "JPY", raw
    if currency in ("USD", "EUR", "THB", "KRW", "CNY"):
        cleaned = amount_text.replace(",", "")
        return _to_number(cleaned), currency, raw
    # No currency: only accept a plain integer. Any thousands/decimal separator
    # is ambiguous without context and stays unparsed.
    if re.fullmatch(r"\d+", amount_text):
        return int(amount_text), None, raw
    return None, None, raw


def _to_int(text: str) -> int | None:
    try:
        return int(text)
    except ValueError:
        return None


def _to_number(text: str) -> int | float | None:
    try:
        value = float(text)
        return int(value) if value.is_integer() else value
    except ValueError:
        return None


def parse_rating(value: str) -> float | None:
    raw = normalize_whitespace(value)
    match = re.search(r"(\d+(?:[.,]\d+)?)", raw)
    if not match:
        return None
    token = match.group(1)
    if "," in token and "." in token:
        return None
    try:
        return float(token.replace(",", "."))
    except ValueError:
        return None


def parse_count(value: str) -> tuple[int | None, str]:
    """Parse counts like ``1.2k``, ``1,2k``, ``10K+``, ``3200``.

    Returns (normalized_count, raw). Ambiguous separators leave count None.
    """
    raw = normalize_whitespace(value)
    match = re.match(r"([\d.,]+)\s*([kmK]?)", raw)
    if not match:
        return None, raw
    number_text, multiplier = match.group(1), match.group(2).lower()
    if "," in number_text and "." in number_text:
        return None, raw
    try:
        if multiplier:
            number = float(number_text.replace(",", "."))
        else:
            number = float(number_text.replace(",", "").replace(".", ""))
            if "." in number_text or "," in number_text:
                return None, raw  # thousands separators without suffix are ambiguous
    except ValueError:
        return None, raw
    factor = {"k": 1000, "m": 1_000_000}.get(multiplier, 1)
    return int(number * factor), raw


def split_unit(value: str) -> tuple[str | None, str | None, str]:
    """Split a value into (number, unit, raw) when deterministic."""
    raw = normalize_whitespace(value)
    match = re.match(r"([\d.,]+)\s*([a-zA-Z%µ]+)?", raw)
    if not match:
        return None, None, raw
    return match.group(1), match.group(2), raw

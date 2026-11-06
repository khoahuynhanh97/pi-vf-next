"""Deterministic identity normalization (no semantic matching)."""

from __future__ import annotations

import re
import unicodedata


def normalize_identity(value: str) -> str:
    """Lowercase, strip accents, collapse whitespace and separators."""
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split()).strip()


def normalize_brand(value: str) -> str:
    return normalize_identity(value)


def normalize_model(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^a-z0-9]", "", text)
    return text.strip()


def normalize_gtin(value: str) -> str:
    return re.sub(r"[^0-9]", "", str(value or ""))


def normalize_variant_value(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")

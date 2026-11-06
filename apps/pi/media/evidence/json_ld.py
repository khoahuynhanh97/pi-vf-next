"""Deterministic schema.org JSON-LD mapping for product evidence."""

from __future__ import annotations

import re

from .models import Availability, EvidenceValue, SourceKind

AVAILABILITY_MAP = {
    "in stock": Availability.IN_STOCK,
    "https://schema.org/instock": Availability.IN_STOCK,
    "https://schema.org/outofstock": Availability.OUT_OF_STOCK,
    "https://schema.org/preorder": Availability.PREORDER,
    "https://schema.org/limitedavailability": Availability.LIMITED,
    "https://schema.org/backorder": Availability.OUT_OF_STOCK,
}

_TYPE_OF = ("@type", "type")


def json_ld_type(node: dict) -> str:
    for key in _TYPE_OF:
        raw = node.get(key)
        if isinstance(raw, str):
            return raw.split("/")[-1]
        if isinstance(raw, list) and raw:
            return str(raw[0]).split("/")[-1]
    return ""


def is_type(node: dict, name: str) -> bool:
    for key in _TYPE_OF:
        raw = node.get(key)
        if raw == name:
            return True
        if isinstance(raw, list) and name in raw:
            return True
        if isinstance(raw, str) and (raw == name or raw.endswith("/" + name)):
            return True
    return False


def flatten_json_ld(blocks: list) -> list[dict]:
    """Flatten single objects, arrays, and @graph containers into a node list."""
    nodes: list[dict] = []

    def collect(node):
        if isinstance(node, list):
            for item in node:
                collect(item)
        elif isinstance(node, dict):
            nodes.append(node)
            graph = node.get("@graph")
            if isinstance(graph, list):
                for item in graph:
                    collect(item)

    for block in blocks:
        collect(block)
    return nodes


def find_products(nodes: list[dict]) -> list[dict]:
    return [node for node in nodes if is_type(node, "Product")]


def _string_value(node) -> str | None:
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        return node.get("name") or node.get("@value")
    return None


def _text(node, *keys) -> str | None:
    for key in keys:
        value = _string_value(node.get(key))
        if value:
            return value
    return None


def _number(node, *keys) -> int | float | None:
    for key in keys:
        value = node.get(key)
        if value is None:
            continue
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            match = re.search(r"[\d.,]+", value)
            if match:
                token = match.group(0)
                try:
                    return float(token.replace(",", "."))
                except ValueError:
                    continue
    return None


def _image_urls(node: dict) -> list[str]:
    raw = node.get("image")
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    urls = []
    for item in items:
        if isinstance(item, str):
            urls.append(item)
        elif isinstance(item, dict) and item.get("url"):
            urls.append(str(item["url"]))
    return urls


def _seller_name(offer: dict) -> str | None:
    seller = offer.get("seller") or offer.get("offeredBy")
    if isinstance(seller, str):
        return seller
    if isinstance(seller, dict):
        return _string_value(seller.get("name"))
    return None


def extract_offer(node: dict, path: str) -> tuple[dict, list[EvidenceValue], list[str]]:
    """Map a JSON-LD Offer (or AggregateOffer) node into offer evidence values."""
    values: dict = {}
    alternates: list[EvidenceValue] = []
    warnings: list[str] = []
    offer_type = json_ld_type(node)

    price = _number(node, "price")
    if price is not None:
        values["price"] = EvidenceValue(value=price, source_kind=SourceKind.JSON_LD, source_path=f"{path}.price", raw_value=str(node.get("price", price)))

    currency = _text(node, "priceCurrency", "currency")
    if currency:
        values["currency"] = EvidenceValue(value=currency.upper(), source_kind=SourceKind.JSON_LD, source_path=f"{path}.priceCurrency")

    availability_raw = _text(node, "availability")
    if availability_raw:
        availability = AVAILABILITY_MAP.get(availability_raw.casefold(), AVAILABILITY_MAP.get(availability_raw))
        values["availability"] = EvidenceValue(value=availability or Availability.UNKNOWN, source_kind=SourceKind.JSON_LD, source_path=f"{path}.availability")
        values["availability_raw"] = EvidenceValue(value=availability_raw, source_kind=SourceKind.JSON_LD, source_path=f"{path}.availability")

    condition = _text(node, "itemCondition")
    if condition:
        values["condition"] = EvidenceValue(value=condition.split("/")[-1], source_kind=SourceKind.JSON_LD, source_path=f"{path}.itemCondition")

    url = _text(node, "url")
    if url:
        values["url"] = EvidenceValue(value=url, source_kind=SourceKind.JSON_LD, source_path=f"{path}.url")

    seller = _seller_name(node)
    if seller:
        values["seller_name"] = EvidenceValue(value=seller, source_kind=SourceKind.JSON_LD, source_path=f"{path}.seller.name")

    low = _number(node, "lowPrice")
    high = _number(node, "highPrice")
    count = _number(node, "offerCount")
    if low is not None:
        values["low_price"] = EvidenceValue(value=low, source_kind=SourceKind.JSON_LD, source_path=f"{path}.lowPrice")
    if high is not None:
        values["high_price"] = EvidenceValue(value=high, source_kind=SourceKind.JSON_LD, source_path=f"{path}.highPrice")
    if count is not None:
        values["offer_count"] = EvidenceValue(value=int(count), source_kind=SourceKind.JSON_LD, source_path=f"{path}.offerCount")

    if offer_type == "AggregateOffer" and price is None and (low is None or high is None):
        warnings.append("AGGREGATE_OFFER_WITHOUT_PRICE_RANGE")

    return values, alternates, warnings


def extract_reputation(node: dict, path: str) -> tuple[dict, list[str]]:
    values: dict = {}
    warnings: list[str] = []
    rating_value = _number(node, "ratingValue")
    if rating_value is not None:
        values["rating_value"] = EvidenceValue(value=rating_value, source_kind=SourceKind.JSON_LD, source_path=f"{path}.ratingValue", raw_value=str(node.get("ratingValue")))
    rating_count = _number(node, "ratingCount")
    review_count = _number(node, "reviewCount")
    if rating_count is not None:
        values["rating_count"] = EvidenceValue(value=int(rating_count), source_kind=SourceKind.JSON_LD, source_path=f"{path}.ratingCount")
    if review_count is not None:
        values["review_count"] = EvidenceValue(value=int(review_count), source_kind=SourceKind.JSON_LD, source_path=f"{path}.reviewCount")
    return values, warnings


def extract_specifications(node: dict, path: str) -> list[dict]:
    """Extract PropertyValue-style specification rows."""
    rows = []
    raw = node.get("additionalProperty") or node.get("additionalProperties")
    if not isinstance(raw, list):
        return rows
    for index, prop in enumerate(raw):
        if not isinstance(prop, dict):
            continue
        name = _text(prop, "name")
        value = _text(prop, "value")
        if name and value:
            rows.append({
                "name": EvidenceValue(value=name, source_kind=SourceKind.JSON_LD, source_path=f"{path}.additionalProperty[{index}].name"),
                "value": EvidenceValue(value=value, source_kind=SourceKind.JSON_LD, source_path=f"{path}.additionalProperty[{index}].value"),
            })
    return rows

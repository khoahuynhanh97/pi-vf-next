"""Read TikTok Shop's public Snacks category as a commerce discovery source."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from media.web_acquisition.challenge import ChallengeClassifier
from media.web_acquisition.extractor import extract_page_content
from media.web_acquisition.models import ChallengeSignal
from product_scout.daily_parser import ParsedCriteria, is_food_category

from .identity import extract_product_identity
from .models import AggregatedCandidate, CandidateObservation


SNACKS_CATEGORY_URL = "https://shop.tiktok.com/vn/c/snacks/915336"
_PDP_ID = re.compile(r"/vn/pdp/(?:[^/?#]+/)?(\d{10,})(?:[/?#]|$)")
_PRICE = re.compile(r"₫\s*(\d[\d.,]*)|(\d[\d.,]*)\s*(?:₫|đ|vnd)", re.IGNORECASE)
_CHALLENGE_TITLES = {"security check", "security verification", "verify to continue"}


class TikTokCategoryChallenge(RuntimeError):
    """TikTok served a verification page instead of category listings."""


class _ProductLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "a":
            href = values.get("href") or ""
            self._current = {"href": href, "title": values.get("title") or values.get("aria-label") or "", "text": ""}
            self._depth = 1
        elif self._current is not None:
            self._depth += 1
            if tag == "img" and not self._current["title"]:
                self._current["title"] = values.get("alt") or ""

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current["text"] += " " + data.strip()

    def handle_endtag(self, tag: str) -> None:
        if self._current is None:
            return
        self._depth -= 1
        if tag == "a" or self._depth <= 0:
            self.links.append(self._current)
            self._current = None
            self._depth = 0


def _canonical_pdp_url(href: str) -> str | None:
    full_url = urljoin(SNACKS_CATEGORY_URL, href)
    parts = urlsplit(full_url)
    if parts.scheme != "https" or parts.hostname != "shop.tiktok.com":
        return None
    match = _PDP_ID.search(parts.path)
    if not match:
        return None
    return f"https://shop.tiktok.com/vn/pdp/{match.group(1)}"


def _card_price(text: str) -> int | None:
    values = []
    for match in _PRICE.finditer(text):
        digits = re.sub(r"\D", "", match.group(1) or match.group(2) or "")
        if digits:
            values.append(int(digits))
    return min(values) if values else None


def _card_title(link: dict[str, str]) -> str:
    if link["title"]:
        return " ".join(link["title"].split())
    text = _PRICE.sub(" ", link["text"])
    text = re.sub(r"\b\d+(?:[.,]\d+)?\s*[kKmM]?\s*(?:sold|đã bán)\b", " ", text, flags=re.IGNORECASE)
    return " ".join(text.split()).strip(" -|,")


def has_complete_tiktok_listing(candidate: AggregatedCandidate) -> bool:
    """A concrete priced PDP may identify a snack even without a known brand."""
    if candidate.category == "conflict" or len(candidate.name.split()) < 3:
        return False
    for observation in candidate.observations:
        product_id = observation.metadata.get("product_id")
        price = observation.metadata.get("price_vnd")
        if (
            observation.provider == TikTokSnacksCategoryProvider.name
            and isinstance(product_id, str)
            and observation.source_url == f"https://shop.tiktok.com/vn/pdp/{product_id}"
            and isinstance(price, int)
            and price > 0
            and observation.title.strip()
        ):
            return True
    return False


def _default_fetch(url: str) -> str:
    import httpx

    response = httpx.get(url, follow_redirects=True, timeout=12.0)
    response.raise_for_status()
    return response.text


def _default_render(url: str) -> str:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=12_000)
            try:
                page.wait_for_selector('a[href*="/pdp/"]', timeout=5_000)
            except PlaywrightTimeout:
                pass
            return page.content()
        finally:
            browser.close()


class TikTokSnacksCategoryProvider:
    name = "tiktok_snacks_category"

    def __init__(
        self,
        *,
        fetcher: Callable[[str], str] | None = None,
        renderer: Callable[[str], str] | None = None,
        category_url: str = SNACKS_CATEGORY_URL,
    ) -> None:
        self.fetcher = fetcher or _default_fetch
        self.renderer = renderer or _default_render
        self.category_url = category_url

    def discover(self, criteria: ParsedCriteria, *, run_at: datetime, limit: int) -> list[CandidateObservation]:
        if not is_food_category(criteria.category) or "tiktok" not in [p.casefold() for p in criteria.platforms]:
            return []
        html = self.fetcher(self.category_url)
        method = "http"
        links = self._product_links(html)
        if not links:
            html = self.renderer(self.category_url)
            method = "browser"
            links = self._product_links(html)
        if self._is_challenge(html):
            raise TikTokCategoryChallenge("TikTok Snacks category requires verification")

        rows: list[CandidateObservation] = []
        seen: set[str] = set()
        for link in links:
            url = _canonical_pdp_url(link["href"])
            if not url or url in seen:
                continue
            seen.add(url)
            title = _card_title(link)
            if not title:
                continue
            price = _card_price(link["text"])
            if price is not None and criteria.price_max > 0 and not criteria.price_min <= price <= criteria.price_max:
                continue
            brand, model, name = extract_product_identity(title, category=criteria.category)
            rows.append(CandidateObservation(
                provider=self.name,
                candidate_name=name,
                brand=brand,
                model=model,
                source_url=url,
                source_host="shop.tiktok.com",
                source_rank=len(rows) + 1,
                observed_at=run_at,
                signal_type="commerce_listing",
                title=title,
                snippet=f"Snack TikTok Shop. {link['text'].strip()}",
                metadata={
                    "category_url": self.category_url,
                    "product_id": url.rsplit("/", 1)[-1],
                    "price_vnd": price,
                    "acquisition_method": method,
                },
            ))
            if len(rows) >= limit:
                break
        return rows

    @staticmethod
    def _product_links(html: str) -> list[dict[str, str]]:
        parser = _ProductLinkParser()
        parser.feed(html)
        return parser.links

    @staticmethod
    def _is_challenge(html: str) -> bool:
        content = extract_page_content(html, url=SNACKS_CATEGORY_URL, discover_images=False)
        title = (content.title or "").casefold()
        if title in _CHALLENGE_TITLES:
            return True
        text = (content.visible_text or "")[:2000]
        signals = ChallengeClassifier().classify(text=text, title=content.title or "")
        return any(signal != ChallengeSignal.LOGIN_WALL for signal in signals)

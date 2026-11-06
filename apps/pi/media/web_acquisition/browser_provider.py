"""Browser acquisition abstraction and Playwright implementation.

Generic, source-agnostic rendering fallback. Never solves challenges, never
bypasses anti-bot controls, never logs in. Playwright is imported lazily so
domain imports work without a browser session.
"""

from __future__ import annotations

import asyncio
from datetime import UTC
from typing import Protocol

from ..models import ImageCandidate
from .challenge import ChallengeClassifier
from .extractor import extract_page_content
from .models import (
    AcquisitionStatus,
    BrowserAcquisitionRequest,
    BrowserAcquisitionResult,
    ExtractedPageContent,
    SourceObservation,
)


class BrowserEngine(Protocol):
    """Minimal browser engine contract so Playwright stays swappable."""

    def open(self, request: BrowserAcquisitionRequest) -> BrowserPage:
        ...


class BrowserPage(Protocol):
    url: str
    title: str

    def content(self) -> str:
        ...

    def close(self) -> None:
        ...


def _now() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _domain(url: str) -> str | None:
    from urllib.parse import urlsplit

    return urlsplit(url).netloc.casefold().removeprefix("www.") or None


class BrowserAcquisitionProvider(Protocol):
    def acquire(self, request: BrowserAcquisitionRequest) -> BrowserAcquisitionResult:
        ...


class PlaywrightBrowserAcquisitionProvider:
    name = "playwright_browser"

    def __init__(self, *, engine=None, classifier=None, headless: bool = True):
        self.engine = engine
        self.classifier = classifier or ChallengeClassifier()
        self.headless = headless

    def acquire(self, request: BrowserAcquisitionRequest) -> BrowserAcquisitionResult:
        """Playwright's sync API cannot run inside a live asyncio loop (the MCP
        server drives us from one), so offload to a worker thread when needed.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self._acquire_sync(request)
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(self._acquire_sync, request).result()

    def _acquire_sync(self, request: BrowserAcquisitionRequest) -> BrowserAcquisitionResult:
        page = None
        try:
            engine = self.engine or _default_engine(self.headless)
            page = engine.open(request)
            html = page.content()
            page_title = getattr(page, "title", "") or ""
            final_url = getattr(page, "url", "") or request.url
            content = extract_page_content(
                html,
                url=final_url,
                capture_text=request.capture_text,
                discover_images=request.discover_images,
            )
            text = content.visible_text or ""
            signals = self.classifier.classify(text=text[:4000], url=final_url, title=content.title or page_title)
            if signals:
                return BrowserAcquisitionResult(
                    observation=SourceObservation(
                        requested_url=request.url,
                        method="browser",
                        status=AcquisitionStatus.CHALLENGED,
                        final_url=final_url,
                        domain=_domain(final_url),
                        page_title=content.title or page_title,
                        signals=signals,
                        reason="BROWSER_CHALLENGE_DETECTED",
                        observed_at=_now(),
                    ),
                    content=content,
                )
            if self.classifier.is_blocked(text=text[:2000], title=content.title or page_title):
                return BrowserAcquisitionResult(
                    observation=SourceObservation(
                        requested_url=request.url,
                        method="browser",
                        status=AcquisitionStatus.BLOCKED,
                        final_url=final_url,
                        domain=_domain(final_url),
                        page_title=content.title or page_title,
                        reason="BROWSER_ACCESS_BLOCKED",
                        observed_at=_now(),
                    ),
                    content=content,
                )
            if content.title or content.visible_text or content.json_ld:
                return BrowserAcquisitionResult(
                    observation=SourceObservation(
                        requested_url=request.url,
                        method="browser",
                        status=AcquisitionStatus.SUCCESS,
                        final_url=final_url,
                        domain=_domain(final_url),
                        page_title=content.title or page_title,
                        content_summary={
                            "title": bool(content.title),
                            "visible_text_chars": len(content.visible_text or ""),
                            "json_ld_blocks": len(content.json_ld),
                            "images": len(content.image_urls),
                        },
                        observed_at=_now(),
                    ),
                    content=content,
                )
            return BrowserAcquisitionResult(
                observation=SourceObservation(
                    requested_url=request.url,
                    method="browser",
                    status=AcquisitionStatus.PARTIAL,
                    final_url=final_url,
                    domain=_domain(final_url),
                    page_title=content.title or page_title,
                    reason="BROWSER_EMPTY_CONTENT",
                    observed_at=_now(),
                ),
                content=content,
            )
        except Exception as exc:  # noqa: BLE001 - engine failure is a structured observation
            return BrowserAcquisitionResult(
                observation=SourceObservation(
                    requested_url=request.url,
                    method="browser",
                    status=AcquisitionStatus.FAILED,
                    domain=_domain(request.url),
                    reason=f"BROWSER_ENGINE_ERROR: {type(exc).__name__}",
                    observed_at=_now(),
                )
            )
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:  # noqa: BLE001,S110 - best-effort cleanup
                    pass


class _PlaywrightEngine:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self._playwright = None
        self._browser = None

    def open(self, request: BrowserAcquisitionRequest):
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless)
        page = self._browser.new_page()
        try:
            page.goto(request.url, wait_until=request.wait_strategy, timeout=int(request.timeout_seconds * 1000))
        except Exception:
            page.close()
            self._browser.close()
            self._playwright.stop()
            raise
        return _PlaywrightPage(page, self)

    def close(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            finally:
                self._browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            finally:
                self._playwright = None


class _PlaywrightPage:
    def __init__(self, page, engine: _PlaywrightEngine):
        self._page = page
        self._engine = engine
        self.url = page.url
        self.title = page.title()

    def content(self) -> str:
        return self._page.content()

    def close(self) -> None:
        try:
            self._page.close()
        finally:
            self._engine.close()


def _default_engine(headless: bool) -> _PlaywrightEngine:
    return _PlaywrightEngine(headless=headless)


def to_image_candidates(observation: SourceObservation, content: ExtractedPageContent | None = None) -> list[ImageCandidate]:
    """Convert page-discovered image URLs into Media Intelligence candidates.

    Provenance reflects the actual acquisition method (``http_page`` or
    ``browser_page``) rather than a hardcoded source.
    """
    content = content or ExtractedPageContent()
    final_url = observation.final_url or observation.requested_url
    provider = "http_page" if observation.method == "http" else "browser_page"
    candidates: list[ImageCandidate] = []
    for rank, url in enumerate(dict.fromkeys(content.image_urls), start=1):
        if not url.startswith(("http://", "https://")):
            continue
        candidates.append(
            ImageCandidate(
                candidate_id=f"page_{rank}",
                discovery_provider=provider,
                query=final_url,
                search_result_rank=rank,
                image_url=url,
                source_page_url=final_url,
                host=_domain(url),
                discovered_at=_now(),
            )
        )
    return candidates

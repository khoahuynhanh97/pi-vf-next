"""Acquisition orchestration: HTTP first, bounded browser fallback."""

from __future__ import annotations

from .browser_provider import (
    BrowserAcquisitionProvider,
    PlaywrightBrowserAcquisitionProvider,
    to_image_candidates,
)
from .http_provider import HttpAcquisitionProvider
from .models import (
    AcquisitionStatus,
    BrowserAcquisitionRequest,
    HttpAcquisitionRequest,
    WebAcquisitionResult,
)

# Statuses where browser rendering may recover usable content.
_FALLBACK_STATUSES = {AcquisitionStatus.BLOCKED, AcquisitionStatus.CHALLENGED, AcquisitionStatus.PARTIAL}


class AcquisitionService:
    """HTTP-first acquisition with a single bounded browser fallback attempt.

    Every observation (HTTP and browser) is preserved; neither is overwritten.
    There is no unbounded retry loop.
    """

    def __init__(self, *, http_provider: HttpAcquisitionProvider | None = None, browser_provider: BrowserAcquisitionProvider | None = None, browser_fallback: bool = True):
        self.http = http_provider or HttpAcquisitionProvider()
        self.browser = browser_provider or PlaywrightBrowserAcquisitionProvider()
        self.browser_fallback = browser_fallback

    def acquire(self, url: str, *, browser_fallback: bool | None = None, discover_images: bool = True) -> WebAcquisitionResult:
        http_result = self.http.acquire(HttpAcquisitionRequest(url=url))
        observations = [http_result.observation]
        content = http_result.content
        status = http_result.observation.status

        should_fallback = browser_fallback if browser_fallback is not None else self.browser_fallback
        if should_fallback and status in _FALLBACK_STATUSES:
            browser_result = self.browser.acquire(
                BrowserAcquisitionRequest(url=url, discover_images=discover_images)
            )
            observations.append(browser_result.observation)
            if browser_result.observation.status == AcquisitionStatus.SUCCESS:
                content = browser_result.content
                status = AcquisitionStatus.SUCCESS
            elif browser_result.observation.status in (AcquisitionStatus.PARTIAL, AcquisitionStatus.CHALLENGED):
                # Partial content may still carry useful evidence; a challenged
                # browser does not convert the source into a system failure.
                if content is None and browser_result.content is not None:
                    content = browser_result.content
                if status != AcquisitionStatus.SUCCESS and browser_result.observation.status == AcquisitionStatus.PARTIAL:
                    status = AcquisitionStatus.PARTIAL

        image_candidates = []
        if content is not None and discover_images and content.image_urls:
            image_candidates = to_image_candidates(observations[-1], content)

        return WebAcquisitionResult(
            requested_url=url,
            status=status,
            observations=observations,
            content=content,
            image_candidates=image_candidates,
            reason=observations[-1].reason,
        )

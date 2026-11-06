"""HTTP page acquisition provider."""

from __future__ import annotations

from datetime import UTC, datetime

from .challenge import ChallengeClassifier
from .extractor import extract_page_content
from .models import (
    AcquisitionStatus,
    HttpAcquisitionRequest,
    HttpAcquisitionResult,
    SourceObservation,
)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _domain(url: str) -> str | None:
    from urllib.parse import urlsplit

    return urlsplit(url).netloc.casefold().removeprefix("www.") or None


class HttpAcquisitionProvider:
    def __init__(self, *, fetcher=None, classifier=None):
        self.fetcher = fetcher or self._http_get
        self.classifier = classifier or ChallengeClassifier()

    def _http_get(self, request: HttpAcquisitionRequest) -> tuple[int, str, str]:
        import httpx

        response = httpx.get(request.url, follow_redirects=True, timeout=request.timeout_seconds)
        return response.status_code, str(response.url), response.text[: request.max_response_bytes]

    def acquire(self, request: HttpAcquisitionRequest) -> HttpAcquisitionResult:
        try:
            status, final_url, html = self.fetcher(request)
        except Exception as exc:  # noqa: BLE001 - request failure is a structured observation
            return HttpAcquisitionResult(
                observation=SourceObservation(
                    requested_url=request.url,
                    method="http",
                    status=AcquisitionStatus.FAILED,
                    domain=_domain(request.url),
                    reason=f"HTTP_REQUEST_FAILED: {type(exc).__name__}",
                    observed_at=_now(),
                )
            )
        signals = self.classifier.classify(text=html[:4000], url=final_url)
        blocked = self.classifier.is_blocked(http_status=status, text=html[:2000])
        if blocked:
            observation = SourceObservation(
                requested_url=request.url,
                method="http",
                status=AcquisitionStatus.BLOCKED,
                final_url=final_url,
                domain=_domain(final_url),
                http_status=status,
                signals=signals,
                reason=f"HTTP {status}" if status in (401, 403) else "ACCESS_DENIED",
                observed_at=_now(),
            )
            return HttpAcquisitionResult(observation=observation)
        if signals:
            observation = SourceObservation(
                requested_url=request.url,
                method="http",
                status=AcquisitionStatus.CHALLENGED,
                final_url=final_url,
                domain=_domain(final_url),
                http_status=status,
                signals=signals,
                reason="CHALLENGE_DETECTED",
                observed_at=_now(),
            )
            return HttpAcquisitionResult(observation=observation)
        content = extract_page_content(html, url=final_url)
        if not (content.title or content.visible_text or content.json_ld):
            observation = SourceObservation(
                requested_url=request.url,
                method="http",
                status=AcquisitionStatus.PARTIAL,
                final_url=final_url,
                domain=_domain(final_url),
                http_status=status,
                reason="EMPTY_OR_JS_SHELL",
                observed_at=_now(),
            )
            return HttpAcquisitionResult(observation=observation, content=content)
        observation = SourceObservation(
            requested_url=request.url,
            method="http",
            status=AcquisitionStatus.SUCCESS,
            final_url=final_url,
            domain=_domain(final_url),
            http_status=status,
            page_title=content.title,
            content_summary=_summary(content),
            observed_at=_now(),
        )
        return HttpAcquisitionResult(observation=observation, content=content)


def _summary(content) -> dict:
    return {
        "title": bool(content.title),
        "visible_text_chars": len(content.visible_text or ""),
        "json_ld_blocks": len(content.json_ld),
        "images": len(content.image_urls),
    }

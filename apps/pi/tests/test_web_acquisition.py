"""Offline tests for generic challenged web acquisition. No network, no Playwright."""

from __future__ import annotations

from datetime import UTC, datetime

from media.models import ImageCandidate
from media.web_acquisition import (
    AcquisitionService,
    AcquisitionStatus,
    BrowserAcquisitionRequest,
    BrowserAcquisitionResult,
    ChallengeClassifier,
    HttpAcquisitionProvider,
    HttpAcquisitionRequest,
    SourceObservation,
    extract_page_content,
)
from media.web_acquisition.browser_provider import to_image_candidates


def _stamp():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class _FixedHttp(HttpAcquisitionProvider):
    def __init__(self, status, http_status=None, final_url="https://x.test/p", signals=(), reason=None, summary=None):
        self._status = status
        self._http_status = http_status
        self._final_url = final_url
        self._signals = signals
        self._reason = reason
        self._summary = summary
        super().__init__(fetcher=lambda req: (200, "http://x.test", "<html></html>"))

    def acquire(self, request: HttpAcquisitionRequest) -> object:
        from media.web_acquisition.extractor import ExtractedPageContent
        from media.web_acquisition.models import HttpAcquisitionResult

        observation = SourceObservation(
            requested_url=request.url,
            method="http",
            status=self._status,
            final_url=self._final_url,
            domain="x.test",
            http_status=self._http_status,
            signals=self._signals,
            content_summary=self._summary or {},
            reason=self._reason,
            observed_at=_stamp(),
        )
        return HttpAcquisitionResult(observation=observation, content=ExtractedPageContent())


class _FixedBrowser:
    def __init__(self, status, *, title=None, text=None, images=(), signals=(), reason=None):
        self._status = status
        self._title = title
        self._text = text
        self._images = images
        self._signals = signals
        self._reason = reason

    def acquire(self, request: BrowserAcquisitionRequest) -> BrowserAcquisitionResult:
        from media.web_acquisition.extractor import ExtractedPageContent

        content = ExtractedPageContent(title=self._title, visible_text=self._text, image_urls=list(self._images))
        observation = SourceObservation(
            requested_url=request.url,
            method="browser",
            status=self._status,
            final_url=request.url,
            domain="x.test",
            page_title=self._title,
            signals=self._signals,
            reason=self._reason,
            observed_at=_stamp(),
        )
        return BrowserAcquisitionResult(observation=observation, content=content)


# --- Challenge classification ---


def test_challenge_classifier_detects_captcha():
    classifier = ChallengeClassifier()
    signals = classifier.classify(text="Please complete the CAPTCHA to continue", url="https://x.test/verify")
    assert any(signal.value == "captcha_visible" for signal in signals)


def test_challenge_classifier_detects_security_check():
    classifier = ChallengeClassifier()
    signals = classifier.classify(text="Security Check - verify you are human")
    assert any(signal.value == "security_check_text" for signal in signals)


def test_blocked_detects_403_and_access_denied():
    classifier = ChallengeClassifier()
    assert classifier.is_blocked(http_status=403)
    assert classifier.is_blocked(text="Access Denied")


# --- Generic extraction ---


def test_extract_page_content_jsonld_og_and_text():
    html = """
    <html><head>
      <title>Product X</title>
      <meta name="description" content="A great product">
      <meta property="og:image" content="https://x.test/og.jpg">
      <link rel="canonical" href="https://x.test/p/1">
      <script type="application/ld+json">{"@type":"Product","name":"Product X","brand":"Acme"}</script>
    </head><body>
      <h1>Product X</h1><p>Visible description text here.</p>
      <img src="https://x.test/a.jpg"><img data-src="https://x.test/b.jpg">
    </body></html>
    """
    content = extract_page_content(html, url="https://x.test/p/1")
    assert content.title == "Product X"
    assert content.meta_description == "A great product"
    assert content.og["image"] == "https://x.test/og.jpg"
    assert content.canonical_url == "https://x.test/p/1"
    assert len(content.json_ld) == 1
    assert content.json_ld[0]["brand"] == "Acme"
    assert "Visible description text here." in content.visible_text
    assert "https://x.test/a.jpg" in content.image_urls


def test_extract_page_content_is_bounded():
    html = "<html><body>" + "".join(f'<img src="https://x.test/img{i}.jpg">' for i in range(200)) + "</body></html>"
    content = extract_page_content(html, url="https://x.test/p")
    assert len(content.image_urls) <= 60


# --- Browser provider semantics ---


def test_browser_success_produces_image_candidates():
    provider = _FixedBrowser(AcquisitionStatus.SUCCESS, title="P", text="body", images=["https://x.test/a.jpg", "https://x.test/b.jpg"])
    result = provider.acquire(BrowserAcquisitionRequest(url="https://x.test/p"))
    assert result.observation.status == AcquisitionStatus.SUCCESS
    candidates = to_image_candidates(result.observation, result.content)
    assert len(candidates) == 2
    assert all(isinstance(c, ImageCandidate) for c in candidates)
    assert all(c.discovery_provider == "browser_page" for c in candidates)
    assert candidates[0].source_page_url == "https://x.test/p"


def test_browser_challenge_is_observed_not_failed():
    classifier = ChallengeClassifier()

    provider = _FixedBrowser(
        AcquisitionStatus.CHALLENGED,
        title="Security Check",
        text="Please verify you are human before continuing. CAPTCHA required.",
        signals=classifier.classify(text="verify you are human CAPTCHA"),
    )
    result = provider.acquire(BrowserAcquisitionRequest(url="https://x.test/p"))
    assert result.observation.status == AcquisitionStatus.CHALLENGED
    assert any(signal.value == "captcha_visible" for signal in result.observation.signals)


def test_browser_engine_error_is_failed():
    class BrokenEngine:
        def open(self, request):
            raise RuntimeError("browser crashed")

    from media.web_acquisition.browser_provider import (
        PlaywrightBrowserAcquisitionProvider,
    )

    provider = PlaywrightBrowserAcquisitionProvider(engine=BrokenEngine())
    result = provider.acquire(BrowserAcquisitionRequest(url="https://x.test/p"))
    assert result.observation.status == AcquisitionStatus.FAILED
    assert "BROWSER_ENGINE_ERROR" in result.observation.reason


def test_playwright_provider_imports_without_browser():
    from media.web_acquisition.browser_provider import (
        PlaywrightBrowserAcquisitionProvider,
    )

    provider = PlaywrightBrowserAcquisitionProvider()
    assert provider.name == "playwright_browser"


# --- Orchestration / fallback ---


def test_http_success_avoids_browser():
    service = AcquisitionService(
        http_provider=_FixedHttp(AcquisitionStatus.SUCCESS, final_url="https://x.test/p", summary={"title": True}),
        browser_provider=_FixedBrowser(AcquisitionStatus.SUCCESS, title="wrong"),
    )
    result = service.acquire("https://x.test/p")
    assert result.status == AcquisitionStatus.SUCCESS
    assert len(result.observations) == 1
    assert result.observations[0].method == "http"


def test_http_blocked_invokes_browser():
    browser = _FixedBrowser(AcquisitionStatus.SUCCESS, title="Recovered", text="real content", images=["https://x.test/i.jpg"])
    service = AcquisitionService(
        http_provider=_FixedHttp(AcquisitionStatus.BLOCKED, http_status=403, reason="HTTP 403"),
        browser_provider=browser,
    )
    result = service.acquire("https://x.test/p")
    assert result.status == AcquisitionStatus.SUCCESS
    assert len(result.observations) == 2
    assert result.observations[0].method == "http" and result.observations[0].status == AcquisitionStatus.BLOCKED
    assert result.observations[1].method == "browser" and result.observations[1].status == AcquisitionStatus.SUCCESS
    assert result.content.title == "Recovered"


def test_http_challenged_browser_challenged_preserves_both():
    classifier = ChallengeClassifier()
    service = AcquisitionService(
        http_provider=_FixedHttp(AcquisitionStatus.CHALLENGED, signals=[s for s in classifier.classify(text="captcha")], reason="CHALLENGE_DETECTED"),
        browser_provider=_FixedBrowser(AcquisitionStatus.CHALLENGED, signals=classifier.classify(text="verify you are human")),
    )
    result = service.acquire("https://x.test/p")
    assert result.status == AcquisitionStatus.CHALLENGED
    assert len(result.observations) == 2
    assert all(obs.status == AcquisitionStatus.CHALLENGED for obs in result.observations)


def test_browser_unavailable_preserves_http_observation():
    from media.web_acquisition.browser_provider import (
        PlaywrightBrowserAcquisitionProvider,
    )

    class BrokenEngine:
        def open(self, request):
            raise RuntimeError("no browser")

    service = AcquisitionService(
        http_provider=_FixedHttp(AcquisitionStatus.BLOCKED, http_status=403, reason="HTTP 403"),
        browser_provider=PlaywrightBrowserAcquisitionProvider(engine=BrokenEngine()),
    )
    result = service.acquire("https://x.test/p")
    assert result.status == AcquisitionStatus.BLOCKED
    assert len(result.observations) == 2
    assert result.observations[0].status == AcquisitionStatus.BLOCKED
    assert result.observations[1].status == AcquisitionStatus.FAILED
    assert "BROWSER_ENGINE_ERROR" in result.observations[1].reason


def test_no_fallback_when_disabled():
    service = AcquisitionService(
        http_provider=_FixedHttp(AcquisitionStatus.BLOCKED, http_status=403, reason="HTTP 403"),
        browser_provider=_FixedBrowser(AcquisitionStatus.SUCCESS, title="x"),
        browser_fallback=False,
    )
    result = service.acquire("https://x.test/p")
    assert result.status == AcquisitionStatus.BLOCKED
    assert len(result.observations) == 1


# --- Provenance from acquisition method ---


class _HttpSuccessWithImages(_FixedHttp):
    def __init__(self):
        super().__init__(AcquisitionStatus.SUCCESS, final_url="https://x.test/p")
        from media.web_acquisition.extractor import ExtractedPageContent

        self._content = ExtractedPageContent(title="P", image_urls=["https://x.test/a.jpg", "https://x.test/b.jpg"])

    def acquire(self, request):
        from media.web_acquisition.models import HttpAcquisitionResult

        content = self._content
        observation = SourceObservation(
            requested_url=request.url,
            method="http",
            status=self._status,
            final_url=self._final_url,
            domain="x.test",
            http_status=200,
            page_title=content.title,
            content_summary={"title": True, "images": len(content.image_urls)},
            observed_at=_stamp(),
        )
        return HttpAcquisitionResult(observation=observation, content=content)


def test_http_success_images_use_http_page_provenance():
    browser = _FixedBrowser(AcquisitionStatus.SUCCESS, title="should-not-run")
    service = AcquisitionService(http_provider=_HttpSuccessWithImages(), browser_provider=browser)
    result = service.acquire("https://x.test/p")
    assert result.status == AcquisitionStatus.SUCCESS
    assert len(result.observations) == 1  # browser must not run
    assert len(result.image_candidates) == 2
    assert all(c.discovery_provider == "http_page" for c in result.image_candidates)
    assert all(c.source_page_url == "https://x.test/p" for c in result.image_candidates)
    assert all(c.discovered_at for c in result.image_candidates)
    assert [c.search_result_rank for c in result.image_candidates] == [1, 2]


def test_http_blocked_browser_success_images_use_browser_page_provenance():
    service = AcquisitionService(
        http_provider=_FixedHttp(AcquisitionStatus.BLOCKED, http_status=403, reason="HTTP 403"),
        browser_provider=_FixedBrowser(AcquisitionStatus.SUCCESS, title="Recovered", text="real", images=["https://x.test/i.jpg"]),
    )
    result = service.acquire("https://x.test/p")
    assert result.status == AcquisitionStatus.SUCCESS
    assert len(result.observations) == 2
    assert len(result.image_candidates) == 1
    assert result.image_candidates[0].discovery_provider == "browser_page"
    assert result.image_candidates[0].source_page_url == "https://x.test/p"


# --- Image bridge to Media Intelligence ---


def test_browser_images_bridge_to_media_intelligence(tmp_path):
    from io import BytesIO

    from PIL import Image, ImageDraw

    from media import MediaIntelligenceService
    from media.acquisition import ImageDownloader
    from media.config import MediaConfig
    from media.web_acquisition.browser_provider import (
        PlaywrightBrowserAcquisitionProvider,
    )

    def png(color, marker=None):
        im = Image.new("RGB", (1000, 1000), color)
        draw = ImageDraw.Draw(im)
        if marker == "circle":
            draw.ellipse((250, 250, 750, 750), fill="white")
        else:
            draw.rectangle((0, 0, 500, 500), fill="white")
        b = BytesIO()
        im.save(b, format="PNG")
        return b.getvalue()

    payloads = {"https://x.test/a.jpg": (200, png("red")), "https://x.test/b.jpg": (200, png("blue", "circle"))}

    class BridgeEngine:
        def open(self, request):
            return _FakePage("https://x.test/p", "Product", ["https://x.test/a.jpg", "https://x.test/b.jpg"])

    class _FakePage:
        def __init__(self, url, title, images):
            self.url = url
            self.title = title
            self._images = images

        def content(self):
            return "<html><body>" + "".join(f'<img src="{u}">' for u in self._images) + "</body></html>"

        def close(self):
            pass

    provider = PlaywrightBrowserAcquisitionProvider(engine=BridgeEngine())
    result = provider.acquire(BrowserAcquisitionRequest(url="https://x.test/p", discover_images=True))
    candidates = to_image_candidates(result.observation, result.content)
    assert len(candidates) == 2

    from media import ProductIdentityHint

    service = MediaIntelligenceService(
        MediaConfig(data_dir=tmp_path),
        provider=_ListProvider(candidates),
        downloader=ImageDownloader(fetcher=lambda url: payloads[url]),
    )
    media_result = service.research(ProductIdentityHint(name="Product"))
    assert media_result.valid_count == 2
    assert all(asset.source["host"] == "x.test" for asset in media_result.selected_assets)


class _ListProvider:
    name = "fake_provider"

    def __init__(self, candidates):
        self.candidates = candidates

    def search(self, query):
        return self.candidates


def test_acquisition_service_rejects_fallback_loop():
    service = AcquisitionService(
        http_provider=_FixedHttp(AcquisitionStatus.BLOCKED, http_status=403, reason="HTTP 403"),
        browser_provider=_FixedBrowser(AcquisitionStatus.BLOCKED, reason="BROWSER_ACCESS_BLOCKED"),
    )
    result = service.acquire("https://x.test/p")
    # HTTP once, browser once — no loop back to HTTP.
    assert len(result.observations) == 2
    assert result.status == AcquisitionStatus.BLOCKED

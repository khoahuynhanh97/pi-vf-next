from __future__ import annotations

import pytest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from PIL import Image

from video_factory.creative.contracts import EvidenceClaim
from video_factory.creative.dossier import _claims
from video_factory.creative.generator import SingleModelCreativeGenerator, StructuredGenerationError
from video_factory.creative.source_backed import SourceEvidenceVerifier
from video_factory.qc.evaluator import MultimodalEvaluator, QCTechnicalError, validate_qc_response
from video_factory.qc.models import QCStage
from video_factory.qc.image_qc import evaluate_image_item
from video_factory.qc.models import NormalizedProduct, QCVerdict


def _image_response(**overrides):
    value = {
        "identity_score": 55,
        "clarity_score": 23,
        "usability_score": 12,
        "total_score": 90,
        "verdict": "pass",
        "hard_failure": False,
        "reason": "ok",
        "eligible": True,
        "source_branding_detected": False,
        "identity_mismatch": False,
        "view_angle": "front",
        "shot_type": "product",
        "visible_details": ["logo"],
        "scene_suitability": ["hero"],
        "risk_flags": [],
        "text_coverage_percent": 0,
    }
    value.update(overrides)
    return value


def test_image_qc_omitted_identity_fields_is_fail_closed():
    response = _image_response()
    response.pop("source_branding_detected")
    with pytest.raises(ValueError, match="source_branding_detected"):
        validate_qc_response(response, QCStage.IMAGE)


def test_verified_status_and_evidence_refs_do_not_make_source_backed_claim():
    claims = _claims({"claims": [{"status": "verified", "claim": "65W", "evidence_refs": ["ev_1"]}]})
    assert len(claims) == 1
    assert claims[0].source_locators == ()


def test_source_backed_claim_requires_locator_and_snippet():
    claims = _claims({"claims": [{
        "status": "SOURCE_BACKED", "claim": "65W",
        "source_url": "https://catalog.test/specs", "snippet": "Output up to 65W",
    }]})
    assert len(claims) == 1
    assert claims[0].source_locators == ("https://catalog.test/specs",)


def test_exchange_local_claim_is_rejected():
    claims = _claims({"claims": [{
        "status": "SOURCE_BACKED", "claim": "65W",
        "source_url": "https://exchange.local/claim", "snippet": "Output up to 65W",
    }]})
    assert claims == ()


def test_timeout_after_send_is_unknown_and_requires_reconciliation(tmp_path):
    class SentThenTimeout:
        def evaluate_image(self, **kwargs):
            raise TimeoutError("response timeout after request send")

    image = tmp_path / "reference.png"
    Image.new("RGB", (8, 8), color="white").save(image)
    item = evaluate_image_item(
        asset_id="asset-1",
        local_path=image,
        product=NormalizedProduct(brand="Ugreen", model="65827"),
        evaluator=SentThenTimeout(),
    )
    assert item.verdict is QCVerdict.UNKNOWN
    assert item.findings["usage_status"] == "uncertain"
    assert item.findings["retry_allowed"] is False


def test_source_backed_rejects_example_locator_before_scripting():
    claim = EvidenceClaim(
        claim_id="claim-1",
        text="Output up to 65W",
        evidence_refs=("https://manufacturer.example/specs",),
        source_locators=("https://manufacturer.example/specs",),
        evidence_snippets=("Output up to 65W",),
    )
    generator = SingleModelCreativeGenerator(MagicMock())
    with pytest.raises(StructuredGenerationError, match="verified source evidence"):
        generator.create_direction(SimpleNamespace(verified_claims=(claim,)))


def test_source_backed_rejects_snippet_missing_from_locator_before_scripting():
    claim = EvidenceClaim(
        claim_id="claim-1",
        text="Output up to 65W",
        evidence_refs=("https://catalog.test/specs",),
        source_locators=("https://catalog.test/specs",),
        evidence_snippets=("Output up to 65W",),
    )
    verifier = SourceEvidenceVerifier(record_lookup=lambda _record_id: {
        "validator": "validate_catalog_v2",
        "validation_status": "passed",
        "snapshot_id": "snap-1",
        "archive_sha256": "a" * 64,
        "source_url": "https://catalog.test/specs",
        "evidence_locator": "line 2",
        "evidence_snippet": "The product has a USB-C port.",
    })
    generator = SingleModelCreativeGenerator(MagicMock(), source_verifier=verifier)
    with pytest.raises(StructuredGenerationError, match="admissible claims grounded"):
        generator.create_script(SimpleNamespace(verified_claims=(claim,)), MagicMock())


def test_source_verifier_rejects_private_ip_before_transport():
    calls = []
    verifier = SourceEvidenceVerifier(
        resolve_host=lambda _host: ["127.0.0.1"],
        open_url=lambda _url, _pinned_ip: calls.append(_url),
    )
    assert verifier.fetch_source("http://127.0.0.1/internal") is None
    assert calls == []


def test_source_verifier_rejects_redirect_to_private_ip():
    class Redirect:
        status = 302
        headers = {"Location": "http://metadata.google.internal/computeMetadata/v1"}

    calls = []
    verifier = SourceEvidenceVerifier(
        resolve_host=lambda host: ["93.184.216.34"] if host == "public.test" else ["169.254.169.254"],
        open_url=lambda url, _pinned_ip: calls.append(url) or Redirect(),
    )
    assert verifier.fetch_source("https://public.test/source") is None
    assert calls == ["https://public.test/source"]


def test_evaluator_timeout_after_send_makes_one_http_attempt(tmp_path, monkeypatch):
    image = tmp_path / "reference.png"
    Image.new("RGB", (8, 8), color="white").save(image)
    calls = []

    monkeypatch.setattr("video_factory.qc.evaluator.get_vertex_config", lambda: ("token", "https://qc.test"))

    def sent_then_timeout(*_args, **_kwargs):
        calls.append(1)
        raise TimeoutError("response timed out after request send")

    monkeypatch.setattr("video_factory.qc.evaluator.urllib.request.urlopen", sent_then_timeout)
    with pytest.raises(QCTechnicalError):
        MultimodalEvaluator(timeout=0.01).evaluate_image(image, "inspect")
    assert len(calls) == 1


def test_source_verifier_pins_checked_public_ip_when_dns_changes_after_validation():
    class Success:
        status = 200
        headers = {}

        @staticmethod
        def read(_limit):
            return b"trusted source"

    resolutions = iter((["93.184.216.34"], ["169.254.169.254"]))
    resolve_calls = []
    connection_targets = []

    def resolve(_host):
        resolve_calls.append(_host)
        return next(resolutions)

    verifier = SourceEvidenceVerifier(
        resolve_host=resolve,
        open_url=lambda url, pinned_ip: connection_targets.append((url, pinned_ip)) or Success(),
    )

    assert verifier.fetch_source("https://public.test/source") == "trusted source"
    assert resolve_calls == ["public.test"]
    assert connection_targets == [("https://public.test/source", "93.184.216.34")]


def test_source_verifier_blocks_claim_without_independent_catalog_record_lookup():
    verifier = SourceEvidenceVerifier(resolve_host=lambda _host: ["93.184.216.34"])

    assert not verifier.verify(
        "https://manufacturer.test/specification",
        "product.specifications.output",
        "The output is 65 watts.",
        "catalog-record-1",
    )

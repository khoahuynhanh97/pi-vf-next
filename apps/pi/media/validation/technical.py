"""Technical image validation (structural only, no product judgement)."""

from __future__ import annotations

import hashlib
from io import BytesIO
from urllib.parse import urlsplit

from PIL import Image, ImageStat

from ..config import MediaConfig
from ..models import ImageCandidate, TechnicalImageResult

import os

# Prevent OpenMP/NumPy/PyWavelets deadlocks in background threads
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"

try:
    if os.environ.get("PRODUCT_RESEARCH_DISABLE_IMAGEHASH") == "true":
        imagehash = None
    else:
        import imagehash
except (ImportError, Exception):  # Offline environments or loading errors map to fallback
    imagehash = None


def _hash_image(image: Image.Image) -> str:
    if imagehash is not None:
        return str(imagehash.phash(image))
    sample = image.convert("L").resize((8, 8))
    values = list(sample.tobytes())
    average = sum(values) / len(values)
    bits = sum((1 << index) for index, value in enumerate(values) if value >= average)
    return f"{bits:016x}"


class TechnicalImageValidator:
    """Validates that a payload is a downloadable, decodable, well-sized image.

    It never decides whether the image depicts the correct product, its best
    angle, or whether it may be reused.
    """

    def __init__(self, config: MediaConfig):
        self.config = config

    def validate(self, candidate: ImageCandidate, payload: bytes) -> TechnicalImageResult:
        if len(payload) > self.config.max_image_bytes:
            return self._reject(candidate, "IMAGE_TOO_LARGE")
        try:
            image = Image.open(BytesIO(payload))
            image.verify()
            image = Image.open(BytesIO(payload))
            image.load()
        except Exception as exc:  # noqa: BLE001 - decode errors map to a structured rejection
            return self._reject(candidate, "IMAGE_DECODE_FAILED", f"{type(exc).__name__}: {exc}")
        width, height = image.size
        if min(width, height) < self.config.minimum_short_edge:
            return self._reject(candidate, "IMAGE_TOO_SMALL", f"{width}x{height}")
        if max(width, height) / min(width, height) > self.config.maximum_aspect_ratio:
            return self._reject(candidate, "IMAGE_EXTREME_RATIO", f"{width}x{height}")
        variance = ImageStat.Stat(image.convert("RGB")).var
        if "placeholder" in candidate.image_url.casefold() or max(variance) < 1:
            return self._reject(candidate, "IMAGE_LOW_VARIANCE")
        sha256 = hashlib.sha256(payload).hexdigest()
        phash = _hash_image(image)
        return TechnicalImageResult(
            candidate_id=candidate.candidate_id,
            image_url=candidate.image_url,
            status="valid",
            width=width,
            height=height,
            format=(image.format or "").lower(),
            sha256=sha256,
            phash=phash,
            payload_bytes=len(payload),
        )

    def _reject(self, candidate: ImageCandidate, reason: str, detail: str | None = None) -> TechnicalImageResult:
        return TechnicalImageResult(
            candidate_id=candidate.candidate_id,
            image_url=candidate.image_url,
            status="rejected",
            reason=reason,
            payload_bytes=None,
            detail=detail,
        )


def host_of(url: str) -> str:
    return urlsplit(url).netloc.casefold().removeprefix("www.") or ""

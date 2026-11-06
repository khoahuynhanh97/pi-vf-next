"""Image discovery provider abstraction."""

from __future__ import annotations

from typing import Protocol

from ..models import ImageCandidate, ImageSearchQuery


class ImageDiscoveryProvider(Protocol):
    """Finds candidate image URLs for a product query.

    A provider is a discovery mechanism only; it does not assert that a
    candidate actually depicts the target product.
    """

    name: str

    def search(self, query: ImageSearchQuery) -> list[ImageCandidate]:
        ...

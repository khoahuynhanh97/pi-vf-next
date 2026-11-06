"""Exact and near-duplicate image grouping."""

from __future__ import annotations

import hashlib


def phash_distance(left: str, right: str) -> int:
    return sum((int(a, 16) ^ int(b, 16)).bit_count() for a, b in zip(left, right))


class ImageDeduplicator:
    """Assigns duplicate_group_id to validated images.

    Exact duplicates (same SHA-256) are collapsed to a single physical asset.
    Near duplicates (pHash distance <= threshold) share a duplicate_group_id
    but their metadata is preserved; a representative is chosen later.
    """

    def __init__(self, phash_distance_threshold: int = 8):
        self.threshold = phash_distance_threshold

    def group(self, images: list) -> list:
        """Return images annotated with duplicate_group_id.

        ``images`` is a list of objects exposing ``sha256`` and ``phash``
        (e.g. validated TechnicalImageResult). Returns a new list of the same
        objects, each with ``duplicate_group_id`` set.
        """
        groups: list[list] = []
        for image in images:
            group = None
            for candidate_group in groups:
                if any(self._exact_or_near(image, member) for member in candidate_group):
                    group = candidate_group
                    break
            if group is None:
                group = []
                groups.append(group)
            group.append(image)
        for index, group in enumerate(groups):
            group_id = f"dg_{index + 1}"
            for image in group:
                image.duplicate_group_id = group_id
        return list(images)

    def _exact_or_near(self, left, right) -> bool:
        left_sha = _attr(left, "sha256")
        right_sha = _attr(right, "sha256")
        left_phash = _attr(left, "phash")
        right_phash = _attr(right, "phash")
        if left_sha and right_sha and left_sha == right_sha:
            return True
        if left_phash and right_phash:
            return phash_distance(left_phash, right_phash) <= self.threshold
        return False


def _attr(obj, name: str):
    value = getattr(obj, name, None)
    if value is None:
        nested = getattr(obj, "result", None)
        if nested is not None:
            value = getattr(nested, name, None)
    return value


def sha256_of(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()

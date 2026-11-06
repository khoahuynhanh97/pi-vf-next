"""Lightweight image view classification (deterministic heuristics only)."""

from __future__ import annotations

from ..models import ViewClass


class ImageClassifier:
    """Assigns a best-effort view class.

    Deterministic heuristics only; uncertain images remain ``unknown``. No
    heuristic claims semantic certainty.
    """

    def classify(self, *, width: int, height: int, title: str = "", url: str = "") -> ViewClass:
        text = f"{title} {url}".casefold()
        if any(token in text for token in ("packaging", "box", "hop", "thung")):
            return ViewClass.PACKAGING
        if any(token in text for token in ("lifestyle", "cuoc song", "thuc te", "goc")):
            return ViewClass.LIFESTYLE
        if any(token in text for token in ("back", "mat sau", "phia sau")):
            return ViewClass.BACK
        if any(token in text for token in ("side", "canh", "ben")):
            return ViewClass.SIDE
        if any(token in text for token in ("front", "mat truoc", "chinh dien")):
            return ViewClass.FRONT
        if any(token in text for token in ("detail", "chi tiet", "can canh", "macro")):
            return ViewClass.DETAIL
        return ViewClass.UNKNOWN

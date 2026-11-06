"""Observational challenge/block classification.

Challenge detection is based on observable evidence (URL, title, text, status
codes). It never attempts to bypass a challenge.
"""

from __future__ import annotations

from .models import ChallengeSignal

# (signal, markers) — markers matched case-insensitively against text.
_CHALLENGE_MARKERS: tuple[tuple[ChallengeSignal, tuple[str, ...]], ...] = (
    (ChallengeSignal.CAPTCHA_VISIBLE, ("captcha", "recaptcha", "hcaptcha")),
    (ChallengeSignal.SECURITY_CHECK_TEXT, ("security check", "security verification", "human verification", "verify you are human")),
    (ChallengeSignal.ROBOT_VERIFICATION, ("robot verification", "are you a robot", "bot verification")),
    (ChallengeSignal.CLOUDFLARE_CHALLENGE, ("cloudflare", "just a moment", "attention required", "cf-chl-")),
    (ChallengeSignal.SLIDER_CHALLENGE, ("slider", "drag to verify", "slide to unlock")),
    (ChallengeSignal.ACCESS_VERIFICATION, ("access verification", "verify access", "before continuing")),
    (ChallengeSignal.VERIFY_TRAFFIC, ("verify/traffic", "traffic check", "verifying traffic")),
    (ChallengeSignal.LOGIN_WALL, ("log in", "sign in", "login required")),
)

_BLOCK_MARKERS = ("access denied", "forbidden", "request blocked", "not authorized")


class ChallengeClassifier:
    """Classifies observable text into challenge signals."""

    def classify(self, *, text: str = "", url: str = "", title: str = "") -> list[ChallengeSignal]:
        haystack = f"{text} {title} {url}".casefold()
        return [signal for signal, markers in _CHALLENGE_MARKERS if any(marker in haystack for marker in markers)]

    def is_blocked(self, *, text: str = "", title: str = "", http_status: int | None = None) -> bool:
        if http_status in (401, 403):
            return True
        haystack = f"{text} {title}".casefold()
        return any(marker in haystack for marker in _BLOCK_MARKERS)

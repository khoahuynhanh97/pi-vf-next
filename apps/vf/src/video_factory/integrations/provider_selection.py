"""Official Product Video provider selection and durable-provider pinning.

Only the Vertex AI API and Google Flow Playwright adapters may participate in
selection. No local still-image, zoom/pan, slideshow, or synthetic renderer is
an eligible provider.

Selection is allowed only before a paid submission.  Once an operation has a
provider kind, callers must resolve that exact provider for polling and
recovery; silently falling back at that point could poll the wrong account or
submit a duplicate paid operation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from .ports import (
    OFFICIAL_PRODUCT_VIDEO_PROVIDER_KINDS,
    VideoGenerationResult,
    validate_product_video_render_provider,
)


class ProviderSelectionError(ValueError):
    """Raised when a provider cannot be selected or resumed safely."""


def validate_reconciled_result(
    *,
    expected_request_id: str,
    durable_provider_kind: str,
    result: VideoGenerationResult | None,
) -> VideoGenerationResult:
    """Validate a provider reconciliation result before durable binding.

    This is deliberately a no-I/O boundary check shared by runtime and server
    callers. A provider may only reconcile the exact client request being
    recovered, and its operation identity must remain tied to the durable
    provider pin. Missing or conflicting identity is ambiguous and must not
    be checkpointed.
    """
    if result is None:
        raise ProviderSelectionError("reconciliation result is missing")
    if result.request_id != expected_request_id:
        raise ProviderSelectionError("reconciliation result request_id mismatch")
    provider_operation_id = str(result.provider_operation_id or "").strip()
    if not provider_operation_id:
        raise ProviderSelectionError(
            "reconciliation result provider_operation_id is missing"
        )
    provider_kind = str(durable_provider_kind or "").strip()
    if not provider_kind:
        raise ProviderSelectionError(
            "reconciliation result durable provider kind is missing"
        )
    metadata = dict(result.metadata or {})
    declared_kind = str(metadata.get("provider_kind") or "").strip()
    if declared_kind and declared_kind != provider_kind:
        raise ProviderSelectionError(
            "reconciliation result provider kind does not match durable provider"
        )
    metadata["provider_kind"] = provider_kind
    return replace(
        result,
        request_id=expected_request_id,
        provider_operation_id=provider_operation_id,
        metadata=metadata,
    )


@dataclass(frozen=True)
class SelectedVideoProvider:
    kind: str
    provider: Any


class VideoProviderSelector:
    """Resolve providers using an ordered preference list.

    Providers without an explicit ``is_available`` hook are considered
    configured.  Adapters may expose either a boolean property or a callable
    hook; the hook must be side-effect free and must not mint credentials.
    """

    def __init__(
        self,
        providers: Mapping[str, Any],
        *,
        preferred_kinds: Sequence[str] | None = None,
        preferred_provider_kind: str | None = None,
        fallback_provider_kind: str | None = None,
        allow_pre_submit_fallback: bool = True,
    ) -> None:
        normalized: dict[str, Any] = {}
        for raw_kind, provider in providers.items():
            kind = str(raw_kind).strip()
            if not kind:
                raise ValueError("video provider kind must not be blank")
            if kind not in OFFICIAL_PRODUCT_VIDEO_PROVIDER_KINDS:
                allowed = ", ".join(sorted(OFFICIAL_PRODUCT_VIDEO_PROVIDER_KINDS))
                raise ValueError(
                    f"unsupported video provider kind {kind!r}; expected one of: {allowed}"
                )
            if provider is None:
                continue
            if kind in normalized:
                raise ValueError(f"duplicate video provider kind: {kind}")
            validated = validate_product_video_render_provider(provider)
            declared_kind = validated.provider_kind.strip()
            if declared_kind != kind:
                raise ValueError(
                    f"video provider registry key {kind!r} does not match declared kind {declared_kind!r}"
                )
            normalized[kind] = validated
        if not normalized:
            raise ValueError("at least one official video provider is required")
        self.providers = normalized
        if 'muse' in normalized and (allow_pre_submit_fallback or
                not (preferred_provider_kind=='muse' or tuple(preferred_kinds or ())==('muse',))):
            raise ValueError('Muse requires an explicit pinned profile with fallback disabled')
        if preferred_provider_kind and preferred_kinds:
            raise ValueError(
                "provide preferred_provider_kind or preferred_kinds, not both"
            )
        preferred = tuple(
            str(kind).strip()
            for kind in (
                (preferred_provider_kind,)
                if preferred_provider_kind
                else (preferred_kinds or ())
            )
        )
        if any(not kind for kind in preferred):
            raise ValueError("preferred video provider kind must not be blank")
        unknown = [kind for kind in preferred if kind not in self.providers]
        if unknown:
            raise ValueError(
                "preferred video provider is not registered: " + ", ".join(unknown)
            )
        fallback = str(fallback_provider_kind or "").strip()
        if fallback and fallback not in self.providers:
            raise ValueError(
                "fallback video provider is not registered: " + fallback
            )
        if fallback and fallback in preferred:
            preferred = tuple(kind for kind in preferred if kind != fallback)
        self.allow_pre_submit_fallback = bool(allow_pre_submit_fallback)
        self.fallback_kind = fallback
        if preferred:
            self.primary_order = preferred
        else:
            self.primary_order = preferred + tuple(
                kind for kind in self.providers if kind not in preferred
            )
        if fallback:
            self.primary_order = tuple(
                kind for kind in self.primary_order if kind != fallback
            )
            self.fallback_order = (fallback,)
        else:
            self.fallback_order = ()

    @classmethod
    def from_provider(
        cls,
        provider: Any | Mapping[str, Any],
        *,
        provider_kind: str = "vertex_ai",
        preferred_kinds: Sequence[str] | None = None,
        preferred_provider_kind: str | None = None,
        fallback_provider_kind: str | None = None,
        allow_pre_submit_fallback: bool = True,
    ) -> "VideoProviderSelector":
        if isinstance(provider, Mapping):
            providers = provider
        else:
            providers = {provider_kind: provider}
        return cls(
            providers,
            preferred_kinds=preferred_kinds,
            preferred_provider_kind=preferred_provider_kind,
            fallback_provider_kind=fallback_provider_kind,
            allow_pre_submit_fallback=allow_pre_submit_fallback,
        )

    @staticmethod
    def is_available(provider: Any) -> bool:
        hook = getattr(provider, "is_available", None)
        if callable(hook):
            return bool(hook())
        if hook is not None:
            return bool(hook)
        return True

    def select_initial(self) -> SelectedVideoProvider:
        candidates = list(self.primary_order)
        if self.allow_pre_submit_fallback:
            candidates.extend(self.fallback_order)
        for kind in candidates:
            provider = self.providers[kind]
            if self.is_available(provider):
                return SelectedVideoProvider(kind=kind, provider=provider)
        raise ProviderSelectionError(
            "no configured video provider is available before submission"
        )

    def resolve_pinned(self, kind: str) -> SelectedVideoProvider:
        normalized = str(kind or "").strip()
        if not normalized:
            raise ProviderSelectionError(
                "durable scene operation is missing its provider kind"
            )
        provider = self.providers.get(normalized)
        if provider is None:
            raise ProviderSelectionError(
                f"durable scene operation is pinned to unavailable provider: {normalized}"
            )
        if not self.is_available(provider):
            raise ProviderSelectionError(
                f"durable scene operation provider is unavailable: {normalized}"
            )
        return SelectedVideoProvider(kind=normalized, provider=provider)

    def has_single_provider(self) -> bool:
        return len(self.providers) == 1

    def only_provider(self) -> SelectedVideoProvider:
        if not self.has_single_provider():
            raise ProviderSelectionError(
                "provider kind is required when multiple video providers are configured"
            )
        kind = next(iter(self.providers))
        return self.resolve_pinned(kind)

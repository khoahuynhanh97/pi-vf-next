"""hermes_personal/product_video/binding.py

Gate 5 Task 4 -- canonical Product-to-Video binding with versioned artifact
chain and automatic downstream invalidation.

Dependency chain (each upstream change clears all descendants):
    product_lock -> brief -> scene_plan -> storyboard -> render -> export

Approval invalidation is delegated to ApprovalService so that any pending or
approved approval referencing a cleared artifact is automatically voided.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


# ---------------------------------------------------------------------------
# Opaque artifact reference -- carries only what Hermes needs to track
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArtifactRef:
    """Opaque, versioned, digest-verified reference to an artifact owned by
    an external bounded context (Product Intelligence or Video Factory)."""

    artifact_id: str
    version: int
    digest: str   # sha256:<hex>
    owner: str    # "product_intelligence" | "video_factory"

    def __post_init__(self) -> None:
        if not self.artifact_id:
            raise ValueError("artifact_id must be non-blank")
        if self.version < 1:
            raise ValueError("version must be positive")
        if not self.digest.startswith("sha256:"):
            raise ValueError("digest must start with 'sha256:'")
        if self.owner not in ("product_intelligence", "video_factory"):
            raise ValueError(f"unknown owner: {self.owner!r}")


# ---------------------------------------------------------------------------
# Versioned binding -- holds the full artifact chain as optional refs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProductVideoBinding:
    """Immutable, versioned binding between a locked product resource and the
    full video artifact chain.

    Each time an upstream artifact changes, a new version of the binding is
    created with all descendant refs cleared (set to None). The version
    counter increments monotonically so callers can detect concurrent updates.
    """

    product_id: str
    version: int                         # monotonically increasing

    # Dependency chain -- presence means the artifact exists and is current.
    # None means not yet produced or invalidated by an upstream change.
    lock_ref: ArtifactRef | None = None          # PI resource lock
    brief_ref: ArtifactRef | None = None         # VF creative brief
    scene_plan_ref: ArtifactRef | None = None    # VF scene plan
    storyboard_ref: ArtifactRef | None = None    # VF storyboard
    render_ref: ArtifactRef | None = None        # VF render
    export_ref: ArtifactRef | None = None        # VF export

    def __post_init__(self) -> None:
        if not self.product_id:
            raise ValueError("product_id must be non-blank")
        if self.version < 1:
            raise ValueError("version must be positive")

    def _descendants_of_lock(self) -> tuple[str, ...]:
        return ("brief_ref", "scene_plan_ref", "storyboard_ref", "render_ref", "export_ref")

    def _descendants_of_brief(self) -> tuple[str, ...]:
        return ("scene_plan_ref", "storyboard_ref", "render_ref", "export_ref")

    def _descendants_of_scene_plan(self) -> tuple[str, ...]:
        return ("storyboard_ref", "render_ref", "export_ref")

    def _descendants_of_storyboard(self) -> tuple[str, ...]:
        return ("render_ref", "export_ref")

    def _descendants_of_render(self) -> tuple[str, ...]:
        return ("export_ref",)

    def cleared_descendants(self, field_names: tuple[str, ...]) -> dict[str, None]:
        return {name: None for name in field_names}


# ---------------------------------------------------------------------------
# Binding service -- creates and evolves binding versions
# ---------------------------------------------------------------------------

class ProductVideoBindingService:
    """Manages immutable ProductVideoBinding versions.

    All mutating operations return a *new* binding with an incremented version.
    Callers are responsible for persisting the returned binding.

    Owner contract (Gate 5 architecture):
    - ``lock_ref`` must belong to ``product_intelligence``
    - ``brief_ref``, ``scene_plan_ref``, ``storyboard_ref``, ``render_ref``,
      ``export_ref`` must all belong to ``video_factory``

    ``approval_service`` is optional; when provided, any pending/approved
    approval whose ``artifact_id`` matches a cleared ref is invalidated via
    ``ApprovalService.invalidate_for_artifact``.
    """

    def __init__(self, approval_service: Any | None = None) -> None:
        self._approvals = approval_service

    @staticmethod
    def _require_pi(ref: ArtifactRef, field: str) -> None:
        if ref.owner != "product_intelligence":
            raise ValueError(
                f"{field} must be owned by 'product_intelligence', got {ref.owner!r}"
            )

    @staticmethod
    def _require_vf(ref: ArtifactRef, field: str) -> None:
        if ref.owner != "video_factory":
            raise ValueError(
                f"{field} must be owned by 'video_factory', got {ref.owner!r}"
            )

    def create(self, product_id: str, lock_ref: ArtifactRef) -> ProductVideoBinding:
        """Create a brand-new binding at version 1 with only the product lock set."""
        self._require_pi(lock_ref, "lock_ref")
        return ProductVideoBinding(
            product_id=product_id,
            version=1,
            lock_ref=lock_ref,
        )

    def create_binding(self, product_id: str, resource_pack: dict) -> ProductVideoBinding:
        """Deprecated compatibility method. Use `create()` instead.

        Accepts a resource_pack dict per legacy API, computes a stable SHA-256
        digest, and delegates to create() with the derived ArtifactRef.
        """
        import hashlib, json
        payload = json.dumps(resource_pack, sort_keys=True).encode()
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        lock_ref = ArtifactRef(
            artifact_id=product_id,
            version=1,
            digest=digest,
            owner="product_intelligence",
        )
        return self.create(product_id, lock_ref)

    def replace_product_lock(
        self,
        binding: ProductVideoBinding,
        new_lock_ref: ArtifactRef,
    ) -> ProductVideoBinding:
        """Replace the product lock; clears brief, scene_plan, storyboard, render, export."""
        self._require_pi(new_lock_ref, "new_lock_ref")
        cleared = binding._descendants_of_lock()
        self._invalidate_cleared(binding, cleared)
        return replace(
            binding,
            version=binding.version + 1,
            lock_ref=new_lock_ref,
            **binding.cleared_descendants(cleared),
        )

    def replace_brief(
        self,
        binding: ProductVideoBinding,
        new_brief_ref: ArtifactRef,
    ) -> ProductVideoBinding:
        """Replace the creative brief; clears scene_plan, storyboard, render, export."""
        self._require_vf(new_brief_ref, "new_brief_ref")
        cleared = binding._descendants_of_brief()
        self._invalidate_cleared(binding, cleared)
        return replace(
            binding,
            version=binding.version + 1,
            brief_ref=new_brief_ref,
            **binding.cleared_descendants(cleared),
        )

    def replace_scene_plan(
        self,
        binding: ProductVideoBinding,
        new_scene_plan_ref: ArtifactRef,
    ) -> ProductVideoBinding:
        """Replace the scene plan; clears storyboard, render, export; brief is preserved."""
        self._require_vf(new_scene_plan_ref, "new_scene_plan_ref")
        cleared = binding._descendants_of_scene_plan()
        self._invalidate_cleared(binding, cleared)
        return replace(
            binding,
            version=binding.version + 1,
            scene_plan_ref=new_scene_plan_ref,
            **binding.cleared_descendants(cleared),
        )

    def replace_storyboard(
        self,
        binding: ProductVideoBinding,
        new_storyboard_ref: ArtifactRef,
    ) -> ProductVideoBinding:
        """Replace the storyboard; clears render, export."""
        self._require_vf(new_storyboard_ref, "new_storyboard_ref")
        cleared = binding._descendants_of_storyboard()
        self._invalidate_cleared(binding, cleared)
        return replace(
            binding,
            version=binding.version + 1,
            storyboard_ref=new_storyboard_ref,
            **binding.cleared_descendants(cleared),
        )

    def replace_render(
        self,
        binding: ProductVideoBinding,
        new_render_ref: ArtifactRef,
    ) -> ProductVideoBinding:
        """Replace the render; clears export."""
        self._require_vf(new_render_ref, "new_render_ref")
        cleared = binding._descendants_of_render()
        self._invalidate_cleared(binding, cleared)
        return replace(
            binding,
            version=binding.version + 1,
            render_ref=new_render_ref,
            **binding.cleared_descendants(cleared),
        )

    def set_export(
        self,
        binding: ProductVideoBinding,
        export_ref: ArtifactRef,
    ) -> ProductVideoBinding:
        """Attach the export artifact; does not clear any upstream refs."""
        self._require_vf(export_ref, "export_ref")
        return replace(
            binding,
            version=binding.version + 1,
            export_ref=export_ref,
        )

    def _invalidate_cleared(
        self,
        binding: ProductVideoBinding,
        cleared_fields: tuple[str, ...],
    ) -> None:
        """Call ApprovalService.invalidate_for_artifact for each cleared ref."""
        if self._approvals is None:
            return
        for field_name in cleared_fields:
            ref: ArtifactRef | None = getattr(binding, field_name)
            if ref is not None:
                self._approvals.invalidate_for_artifact(ref.artifact_id)

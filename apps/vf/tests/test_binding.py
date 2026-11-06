"""tests/hermes_personal/product_video/test_binding.py

Gate 5 Task 4 -- versioned dependency chain and approval invalidation tests.

Spec (gate-5-plan line 165-196):
- new product lock clears ALL downstream refs (brief, scene_plan, storyboard, render, export)
- new scene plan clears storyboard, render, export but preserves brief
- each replacement bumps version by exactly 1
- ApprovalService.invalidate_for_artifact is called for each cleared non-None ref
"""
from __future__ import annotations

import sqlite3

import pytest

from video_factory.binding import (
    ArtifactRef,
    ProductVideoBinding,
    ProductVideoBindingService,
)
from video_factory.workflow import (
    ProductVideoWorkflowState,
    HITL_BLOCKING_STATES,
    WorkflowTransitionError,
    is_hitl_blocked,
    transition,
)
from video_factory.workflows.approvals import ApprovalService, ApprovalStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pi_ref(artifact_id: str, version: int = 1) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=artifact_id,
        version=version,
        digest="sha256:" + "a" * 64,
        owner="product_intelligence",
    )


def _vf_ref(artifact_id: str, version: int = 1) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=artifact_id,
        version=version,
        digest="sha256:" + "b" * 64,
        owner="video_factory",
    )


def _full_binding() -> ProductVideoBinding:
    """A binding with all chain refs populated (version 1)."""
    return ProductVideoBinding(
        product_id="prod-001",
        version=1,
        lock_ref=_pi_ref("lock-1"),
        brief_ref=_vf_ref("brief-1"),
        scene_plan_ref=_vf_ref("scene-1"),
        storyboard_ref=_vf_ref("story-1"),
        render_ref=_vf_ref("render-1"),
        export_ref=_vf_ref("export-1"),
    )


def _approval_service() -> ApprovalService:
    conn = sqlite3.connect(":memory:")
    return ApprovalService(conn)


# ---------------------------------------------------------------------------
# ArtifactRef validation
# ---------------------------------------------------------------------------

def test_artifact_ref_rejects_blank_id():
    with pytest.raises(ValueError, match="non-blank"):
        ArtifactRef(artifact_id="", version=1, digest="sha256:" + "a" * 64, owner="video_factory")


def test_artifact_ref_rejects_zero_version():
    with pytest.raises(ValueError, match="positive"):
        ArtifactRef(artifact_id="x", version=0, digest="sha256:" + "a" * 64, owner="video_factory")


def test_artifact_ref_rejects_bad_digest():
    with pytest.raises(ValueError, match="sha256:"):
        ArtifactRef(artifact_id="x", version=1, digest="md5:abc", owner="video_factory")


def test_artifact_ref_rejects_unknown_owner():
    with pytest.raises(ValueError, match="unknown owner"):
        ArtifactRef(artifact_id="x", version=1, digest="sha256:" + "a" * 64, owner="hermes")


# ---------------------------------------------------------------------------
# ProductVideoBinding validation
# ---------------------------------------------------------------------------

def test_binding_rejects_blank_product_id():
    with pytest.raises(ValueError, match="non-blank"):
        ProductVideoBinding(product_id="", version=1)


def test_binding_rejects_zero_version():
    with pytest.raises(ValueError, match="positive"):
        ProductVideoBinding(product_id="p", version=0)


# ---------------------------------------------------------------------------
# Owner enforcement per chain position (MEDIUM finding)
# ---------------------------------------------------------------------------

def test_create_rejects_vf_owner_for_lock():
    """lock_ref must be product_intelligence, not video_factory."""
    svc = ProductVideoBindingService()
    vf_ref = _vf_ref("lock-wrong")
    with pytest.raises(ValueError, match="product_intelligence"):
        svc.create("prod-1", vf_ref)


def test_replace_product_lock_rejects_vf_owner():
    svc = ProductVideoBindingService()
    b = ProductVideoBinding(product_id="p", version=1, lock_ref=_pi_ref("lock-1"))
    with pytest.raises(ValueError, match="product_intelligence"):
        svc.replace_product_lock(b, _vf_ref("lock-bad"))


def test_replace_brief_rejects_pi_owner():
    svc = ProductVideoBindingService()
    b = _full_binding()
    with pytest.raises(ValueError, match="video_factory"):
        svc.replace_brief(b, _pi_ref("brief-bad"))


def test_replace_scene_plan_rejects_pi_owner():
    svc = ProductVideoBindingService()
    b = _full_binding()
    with pytest.raises(ValueError, match="video_factory"):
        svc.replace_scene_plan(b, _pi_ref("scene-bad"))


def test_set_export_rejects_pi_owner():
    svc = ProductVideoBindingService()
    b = _full_binding()
    with pytest.raises(ValueError, match="video_factory"):
        svc.set_export(b, _pi_ref("export-bad"))


# ---------------------------------------------------------------------------
# replace_product_lock -- clears ALL downstream refs
# ---------------------------------------------------------------------------

def test_new_product_lock_invalidates_all_video_artifacts():
    """Gate 5 plan spec (line 168-175)."""
    svc = ProductVideoBindingService()
    current = _full_binding()
    updated = svc.replace_product_lock(current, _pi_ref("lock-2", version=2))

    assert updated.version == current.version + 1
    assert updated.lock_ref == _pi_ref("lock-2", version=2)
    assert updated.brief_ref is None
    assert updated.scene_plan_ref is None
    assert updated.storyboard_ref is None
    assert updated.render_ref is None
    assert updated.export_ref is None


def test_replace_product_lock_version_bumps_exactly_one():
    svc = ProductVideoBindingService()
    b = _full_binding()
    b2 = svc.replace_product_lock(b, _pi_ref("lock-99"))
    assert b2.version == b.version + 1


# ---------------------------------------------------------------------------
# replace_scene_plan -- clears storyboard, render, export but NOT brief
# ---------------------------------------------------------------------------

def test_new_scene_plan_invalidates_storyboard_and_render_but_not_brief():
    """Gate 5 plan spec (line 177-181)."""
    svc = ProductVideoBindingService()
    current = _full_binding()
    updated = svc.replace_scene_plan(current, _vf_ref("scene-2", version=2))

    assert updated.brief_ref is not None, "brief must be preserved"
    assert updated.scene_plan_ref == _vf_ref("scene-2", version=2)
    assert updated.storyboard_ref is None
    assert updated.render_ref is None
    assert updated.version == current.version + 1


def test_replace_scene_plan_preserves_lock_and_brief():
    svc = ProductVideoBindingService()
    b = _full_binding()
    b2 = svc.replace_scene_plan(b, _vf_ref("scene-99"))
    assert b2.lock_ref == b.lock_ref
    assert b2.brief_ref == b.brief_ref


# ---------------------------------------------------------------------------
# replace_brief -- clears scene_plan, storyboard, render, export
# ---------------------------------------------------------------------------

def test_replace_brief_clears_scene_and_below():
    svc = ProductVideoBindingService()
    b = _full_binding()
    b2 = svc.replace_brief(b, _vf_ref("brief-2"))

    assert b2.brief_ref == _vf_ref("brief-2")
    assert b2.lock_ref == b.lock_ref
    assert b2.scene_plan_ref is None
    assert b2.storyboard_ref is None
    assert b2.render_ref is None
    assert b2.export_ref is None
    assert b2.version == b.version + 1


# ---------------------------------------------------------------------------
# replace_storyboard / replace_render / set_export
# ---------------------------------------------------------------------------

def test_replace_storyboard_clears_render_and_export():
    svc = ProductVideoBindingService()
    b = _full_binding()
    b2 = svc.replace_storyboard(b, _vf_ref("story-2"))

    assert b2.brief_ref == b.brief_ref
    assert b2.scene_plan_ref == b.scene_plan_ref
    assert b2.storyboard_ref == _vf_ref("story-2")
    assert b2.render_ref is None
    assert b2.export_ref is None


def test_replace_render_clears_export_only():
    svc = ProductVideoBindingService()
    b = _full_binding()
    b2 = svc.replace_render(b, _vf_ref("render-2"))

    assert b2.storyboard_ref == b.storyboard_ref
    assert b2.render_ref == _vf_ref("render-2")
    assert b2.export_ref is None


def test_set_export_does_not_clear_any_upstream():
    svc = ProductVideoBindingService()
    b = ProductVideoBinding(product_id="p", version=1, lock_ref=_pi_ref("lock-1"), render_ref=_vf_ref("r-1"))
    b2 = svc.set_export(b, _vf_ref("export-1"))

    assert b2.lock_ref == b.lock_ref
    assert b2.render_ref == b.render_ref
    assert b2.export_ref == _vf_ref("export-1")
    assert b2.version == b.version + 1


# ---------------------------------------------------------------------------
# ApprovalService.invalidate_for_artifact integration
# ---------------------------------------------------------------------------

def test_replace_product_lock_invalidates_pending_approvals_for_cleared_refs():
    """When a product lock is replaced, pending approvals for cleared artifact
    IDs must be rejected by ApprovalService.invalidate_for_artifact."""
    approvals = _approval_service()
    svc = ProductVideoBindingService(approval_service=approvals)

    b = _full_binding()  # brief-1, scene-1, story-1, render-1, export-1 all populated

    # Submit pending approvals referencing each cleared artifact
    brief_req = approvals.submit_request({"artifact_id": "brief-1", "stage": "brief"})
    scene_req = approvals.submit_request({"artifact_id": "scene-1", "stage": "scene"})
    unrelated = approvals.submit_request({"artifact_id": "unrelated-99"})

    svc.replace_product_lock(b, _pi_ref("lock-2", version=2))

    # brief-1 and scene-1 approvals must be REJECTED
    assert approvals.get_request(brief_req).status == ApprovalStatus.REJECTED
    assert approvals.get_request(scene_req).status == ApprovalStatus.REJECTED
    # unrelated approval must remain PENDING
    assert approvals.get_request(unrelated).status == ApprovalStatus.PENDING


def test_replace_scene_plan_only_invalidates_storyboard_and_render_approvals():
    approvals = _approval_service()
    svc = ProductVideoBindingService(approval_service=approvals)

    b = _full_binding()
    brief_req = approvals.submit_request({"artifact_id": "brief-1"})
    story_req = approvals.submit_request({"artifact_id": "story-1"})

    svc.replace_scene_plan(b, _vf_ref("scene-2"))

    # brief approval must NOT be invalidated (brief is preserved)
    assert approvals.get_request(brief_req).status == ApprovalStatus.PENDING
    # storyboard approval must be invalidated
    assert approvals.get_request(story_req).status == ApprovalStatus.REJECTED


def test_invalidate_for_artifact_returns_count():
    approvals = _approval_service()
    approvals.submit_request({"artifact_id": "x-1"})
    approvals.submit_request({"artifact_id": "x-1"})
    approvals.submit_request({"artifact_id": "other"})

    count = approvals.invalidate_for_artifact("x-1")
    assert count == 2


def test_invalidate_for_artifact_also_rejects_approved():
    """Gate 5 contract (line 190): invalidation must cover pending AND approved approvals."""
    approvals = _approval_service()
    req = approvals.submit_request({"artifact_id": "a-1"})
    approvals.approve(req)  # move to APPROVED

    count = approvals.invalidate_for_artifact("a-1")
    assert count == 1  # APPROVED must be invalidated too
    assert approvals.get_request(req).status == ApprovalStatus.REJECTED


def test_invalidate_for_artifact_skips_already_rejected():
    approvals = _approval_service()
    req = approvals.submit_request({"artifact_id": "a-1"})
    approvals.reject(req)

    count = approvals.invalidate_for_artifact("a-1")
    assert count == 0  # REJECTED is terminal, no change


# ---------------------------------------------------------------------------
# Workflow state machine
# ---------------------------------------------------------------------------

def test_hitl_blocking_states_include_all_awaiting_states():
    for state in ProductVideoWorkflowState:
        if "AWAITING" in state.value:
            assert state in HITL_BLOCKING_STATES, f"{state} must be in HITL_BLOCKING_STATES"


def test_is_hitl_blocked_true_for_awaiting():
    assert is_hitl_blocked(ProductVideoWorkflowState.AWAITING_BRIEF_APPROVAL)
    assert is_hitl_blocked(ProductVideoWorkflowState.AWAITING_SCENE_APPROVAL)


def test_is_hitl_blocked_false_for_processing_states():
    assert not is_hitl_blocked(ProductVideoWorkflowState.CREATING_SCENE_PLAN)
    assert not is_hitl_blocked(ProductVideoWorkflowState.RENDERING)


def test_valid_transition_returns_target():
    result = transition(
        ProductVideoWorkflowState.AWAITING_BRIEF_APPROVAL,
        ProductVideoWorkflowState.CREATING_SCENE_PLAN,
    )
    assert result == ProductVideoWorkflowState.CREATING_SCENE_PLAN


def test_invalid_transition_raises():
    with pytest.raises(WorkflowTransitionError):
        transition(
            ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK,
            ProductVideoWorkflowState.COMPLETED,
        )


def test_completed_state_has_no_transitions():
    with pytest.raises(WorkflowTransitionError):
        transition(ProductVideoWorkflowState.COMPLETED, ProductVideoWorkflowState.RENDERING)


# ---------------------------------------------------------------------------
# Regression tests for Gate 5 Task 4 findings
# ---------------------------------------------------------------------------

def test_building_resource_pack_to_locking_resource_pack_is_blocked():
    """Direct BUILDING_RESOURCE_PACK -> LOCKING_RESOURCE_PACK must raise WorkflowTransitionError.

    The HITL gate at AWAITING_PRODUCT_LOCK must not be bypassed.
    """
    with pytest.raises(WorkflowTransitionError):
        transition(
            ProductVideoWorkflowState.BUILDING_RESOURCE_PACK,
            ProductVideoWorkflowState.LOCKING_RESOURCE_PACK,
        )


def test_building_resource_pack_to_awaiting_product_lock_to_locking_resource_pack_works():
    """Valid path through AWAITING_PRODUCT_LOCK must work."""
    # BUILDING_RESOURCE_PACK -> AWAITING_PRODUCT_LOCK
    result = transition(
        ProductVideoWorkflowState.BUILDING_RESOURCE_PACK,
        ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK,
    )
    assert result == ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK

    # AWAITING_PRODUCT_LOCK -> LOCKING_RESOURCE_PACK
    result = transition(
        ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK,
        ProductVideoWorkflowState.LOCKING_RESOURCE_PACK,
    )
    assert result == ProductVideoWorkflowState.LOCKING_RESOURCE_PACK


def test_invalidate_for_artifact_exact_match_no_false_positive():
    """FINDING 2: invalidate_for_artifact must not match artifact-1 in artifact-10.

    The old code used `artifact_id in json.dumps(payload)` which does substring matching.
    This test ensures exact matching on the artifact_id field only.
    """
    approvals = _approval_service()
    svc = ProductVideoBindingService(approval_service=approvals)

    # Create a binding with artifact-10 (not artifact-1)
    b = ProductVideoBinding(
        product_id="prod-1",
        version=1,
        lock_ref=_pi_ref("lock-1"),
        brief_ref=_vf_ref("brief-10"),
        scene_plan_ref=_vf_ref("scene-10"),
    )

    # Submit approval for artifact-10
    req_10 = approvals.submit_request({"artifact_id": "brief-10", "stage": "brief"})

    # Replace product lock (this should clear brief_ref which is "brief-10")
    svc.replace_product_lock(b, _pi_ref("lock-2", version=2))

    # The approval for "brief-10" should be invalidated
    assert approvals.get_request(req_10).status == ApprovalStatus.REJECTED

    # Now test that artifact-1 does NOT match artifact-10
    approvals2 = _approval_service()
    svc2 = ProductVideoBindingService(approval_service=approvals2)

    b2 = ProductVideoBinding(
        product_id="prod-2",
        version=1,
        lock_ref=_pi_ref("lock-1"),
        brief_ref=_vf_ref("brief-10"),  # Note: brief-10, not brief-1
        scene_plan_ref=_vf_ref("scene-10"),
    )

    # Submit approval for artifact-1 (different from brief-10)
    req_1 = approvals2.submit_request({"artifact_id": "brief-1", "stage": "brief"})

    # Replace product lock (clears brief-10)
    svc2.replace_product_lock(b2, _pi_ref("lock-2", version=2))

    # The approval for "brief-1" should NOT be invalidated (no match)
    assert approvals2.get_request(req_1).status == ApprovalStatus.PENDING


def test_legacy_create_binding_method_on_service():
    """FINDING 3: ProductVideoBindingService.create_binding must accept resource_pack dict per legacy API."""
    svc = ProductVideoBindingService()

    resource_pack = {"name": "Legacy Product", "features": ["x"]}
    binding = svc.create_binding("prod-456", resource_pack)

    assert binding.product_id == "prod-456"
    assert binding.version == 1
    assert binding.lock_ref is not None
    assert binding.lock_ref.artifact_id == "prod-456"
    assert binding.lock_ref.owner == "product_intelligence"
    assert binding.lock_ref.digest.startswith("sha256:")
    assert binding.brief_ref is None
    assert binding.scene_plan_ref is None


def test_legacy_create_binding_via_integration_shim():
    """FINDING 3: Integration shim create_binding must work via service.create_binding."""
    from hermes_personal.integrations.product_video.binding import create_binding as shim_create_binding

    resource_pack = {"name": "Test Product", "features": ["a", "b"]}

    # This should work through the shim -> service.create_binding -> service.create
    binding = shim_create_binding("prod-789", resource_pack)

    assert binding.product_id == "prod-789"
    assert binding.version == 1
    assert binding.lock_ref is not None
    assert binding.lock_ref.owner == "product_intelligence"
    # Digest should be computed from resource_pack
    assert binding.lock_ref.digest.startswith("sha256:")


def test_legacy_verify_binding_integrity_match():
    """FINDING 3: verify_binding_integrity must return True when digest matches."""
    from hermes_personal.integrations.product_video.binding import verify_binding_integrity, create_binding as shim_create_binding

    resource_pack = {"name": "Test Product", "version": 1}

    # Create binding via shim
    binding = shim_create_binding("prod-999", resource_pack)

    # Same resource_pack should match
    assert verify_binding_integrity(binding, resource_pack) is True


def test_legacy_verify_binding_integrity_mismatch():
    """FINDING 3: verify_binding_integrity must return False when digest mismatches."""
    from hermes_personal.integrations.product_video.binding import verify_binding_integrity, create_binding as shim_create_binding

    resource_pack = {"name": "Test Product", "version": 1}
    different_pack = {"name": "Different Product", "version": 2}

    # Create binding via shim
    binding = shim_create_binding("prod-999", resource_pack)

    # Different resource_pack should NOT match
    assert verify_binding_integrity(binding, different_pack) is False


def test_legacy_binding_digest_is_stable_across_key_order():
    """FINDING 3 regression: digest must be stable regardless of dict insertion order."""
    from video_factory.binding import ProductVideoBindingService
    from hermes_personal.integrations.product_video.binding import verify_binding_integrity

    pack_a = {"name": "Legacy", "metadata": {"b": 2, "a": 1}}
    pack_b = {"metadata": {"a": 1, "b": 2}, "name": "Legacy"}

    svc = ProductVideoBindingService()
    binding_a = svc.create_binding("prod-order", pack_a)
    binding_b = svc.create_binding("prod-order", pack_b)

    assert binding_a.lock_ref.digest == binding_b.lock_ref.digest
    assert binding_a.lock_ref.digest.startswith("sha256:")

    reversed_pack = {"metadata": {"a": 1, "b": 2}, "name": "Legacy"}
    assert verify_binding_integrity(binding_a, reversed_pack) is True

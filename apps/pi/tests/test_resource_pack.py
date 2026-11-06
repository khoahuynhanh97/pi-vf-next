"""Offline tests for Resource Pack builder and immutable lock store."""

from __future__ import annotations

import pytest

from media.models import MediaAsset, ProductIdentityHint, ProductImageAssessment
from media.evidence.models import ProductEvidence, ClaimEvidence, EvidenceValue, EvidenceSource
from media.research.models import (
    ProductResearchResult,
    ResearchProductRequest,
    ResearchStatus,
    ResearchSummary,
    SourceResearchResult,
)
from media.research_persistence import (
    ResearchPersistenceService,
    SQLiteResearchRepository,
)
from media.resolution.models import CanonicalProduct, ProductVariant, ResolvedField
from media.resource_pack import PackStatus, ResourcePackBuilder, ResourcePackService
from media.resource_pack.lock_store import ResourcePackLockStore


def _asset(asset_id, match_score=0.9, variant=None):
    provenance = {"source_class": "marketplace", "usage_policy": "reference_only"}
    if variant is not None:
        provenance["variant"] = variant
    return MediaAsset(
        asset_id=asset_id,
        local_path=f"data/product_scout/images/{asset_id}.jpg",
        sha256=asset_id * 4,
        phash="p" * 16,
        width=1000,
        height=1000,
        format="jpg",
        discovery={"provider": "duckduckgo_images", "query": "q", "rank": 1},
        source={"image_url": f"https://x.test/{asset_id}.jpg", "page_url": "https://x.test/p", "host": "x.test"},
        assessment=ProductImageAssessment(product_match_score=match_score, match_status="matched"),
        provenance=provenance,
    )


def _result(products=1, variants=0, media=4, media_variant=None):
    product = CanonicalProduct(
        canonical_product_id="product_jisulife_fa55",
        brand=ResolvedField(value="Jisulife", supporting_evidence_ids=["e1"]),
        model=ResolvedField(value="FA55", supporting_evidence_ids=["e1"]),
        name=ResolvedField(value="Jisulife FA55", supporting_evidence_ids=["e1"]),
        specifications=[ResolvedField(value="5000 mAh", supporting_evidence_ids=["e1"], decision="battery capacity")],
        variants=[
            ProductVariant(variant_id="variant_black", canonical_product_id="product_jisulife_fa55", dimensions={"variant": "black"}, supporting_evidence_ids=["e1"]),
            ProductVariant(variant_id="variant_white", canonical_product_id="product_jisulife_fa55", dimensions={"variant": "white"}, supporting_evidence_ids=["e1"]),
        ][:variants],
        supporting_evidence_ids=["e1"],
    )
    products_list = [product] * products
    source = SourceResearchResult(url="https://s1.test/p", acquisition_status="success", acquisition_method="http", evidence_status="completed", evidence_id="e1", host="s1.test", usable=True)
    return ProductResearchResult(
        status=ResearchStatus.COMPLETED,
        request=ResearchProductRequest(url="https://s1.test/p", product_identity_hint=ProductIdentityHint(name="Jisulife FA55", brand="Jisulife", model="FA55")),
        source_results=[source],
        canonical_products=products_list,
        media_assets=[_asset(f"a{i}", match_score=0.9 - i * 0.1, variant=media_variant) for i in range(media)],
        summary=ResearchSummary(sources=1, usable_evidence=1, canonical_products=len(products_list), media_assets=media),
    )


def test_clear_product_ready_to_lock():
    pack = ResourcePackBuilder().build(_result())
    assert pack.pack_status == PackStatus.READY_TO_LOCK
    assert pack.canonical_product_id == "product_jisulife_fa55"
    assert len(pack.media) == 4
    assert all(item.usage_policy == "reference_only" for item in pack.media)


def test_one_usable_reference_is_enough_to_lock():
    pack = ResourcePackBuilder().build(_result(media=1))
    assert pack.pack_status == PackStatus.READY_TO_LOCK
    assert len(pack.media) == 1


def test_one_usable_reference_for_selected_variant_is_enough_to_lock():
    pack = ResourcePackBuilder().build(
        _result(variants=2, media=1, media_variant="black"),
        variant_id="variant_black",
    )
    assert pack.pack_status == PackStatus.READY_TO_LOCK
    assert len(pack.media) == 1


def test_ambiguous_product_needs_review():
    pack = ResourcePackBuilder().build(_result(products=2))
    assert pack.pack_status == PackStatus.NEEDS_REVIEW
    assert "ambiguous" in pack.reason


def test_no_product_rejected():
    pack = ResourcePackBuilder().build(_result(products=0))
    assert pack.pack_status == PackStatus.REJECTED


def test_variant_selection_required_when_variants_exist():
    pack = ResourcePackBuilder().build(_result(variants=2), variant_id=None)
    assert pack.pack_status == PackStatus.NEEDS_REVIEW
    assert "variant" in pack.reason


def test_variant_locked_when_selected():
    pack = ResourcePackBuilder().build(_result(variants=2, media_variant="black"), variant_id="variant_black")
    assert pack.pack_status == PackStatus.READY_TO_LOCK
    assert pack.variant_id == "variant_black"


def test_unknown_variant_needs_review():
    pack = ResourcePackBuilder().build(_result(variants=2), variant_id="variant_missing")
    assert pack.pack_status == PackStatus.NEEDS_REVIEW


def test_no_usable_media_is_incomplete():
    pack = ResourcePackBuilder().build(_result(media=0))
    assert pack.pack_status == PackStatus.INCOMPLETE
    assert "insufficient" in pack.reason


def test_claims_stay_source_claims():
    pack = ResourcePackBuilder().build(_result())
    assert all(claim.status.value == "source_claim" for claim in pack.claims)


def test_pack_claims_keep_their_own_source_evidence():
    result = _result()
    result.canonical_products[0].supporting_evidence_ids = ["e1", "e2"]
    result.product_evidence = [
        ProductEvidence(
            evidence_id=evidence_id,
            source=EvidenceSource(requested_url=f"https://source.test/{evidence_id}"),
            claims=[ClaimEvidence(text=EvidenceValue(value=claim_text))],
        )
        for evidence_id, claim_text in (("e1", "First claim"), ("e2", "Second claim"))
    ]
    pack = ResourcePackBuilder().build(result)
    assert [(claim.text, claim.source_evidence_ids) for claim in pack.claims] == [
        ("First claim", ["e1"]), ("Second claim", ["e2"]),
    ]
    assert len({claim.claim_id for claim in pack.claims}) == 2


def test_pack_deduplicates_repeated_claim_from_one_source():
    result = _result()
    result.product_evidence = [
        ProductEvidence(
            evidence_id="e1",
            source=EvidenceSource(requested_url="https://source.test/e1"),
            claims=[
                ClaimEvidence(text=EvidenceValue(value="IPX7 waterproof")),
                ClaimEvidence(text=EvidenceValue(value=" ipx7  waterproof ")),
            ],
        )
    ]
    pack = ResourcePackBuilder().build(result)
    assert [(claim.text, claim.source_evidence_ids) for claim in pack.claims] == [
        ("IPX7 waterproof", ["e1"]),
    ]


def test_media_rights_retained():
    pack = ResourcePackBuilder().build(_result())
    assert all(item.usage_policy == "reference_only" for item in pack.media)
    assert pack.restrictions["all_reference_only"] is True


def test_media_dedup_and_bound():
    pack = ResourcePackBuilder(min_media=3, max_media=3).build(_result(media=8))
    assert len(pack.media) == 3


def test_lock_immutable_and_versioned(tmp_path):
    store = ResourcePackLockStore(tmp_path / "locks.db")
    pack = ResourcePackBuilder().build(_result(variants=2, media_variant="black"), variant_id="variant_black")
    v1 = store.lock(pack)
    v2 = store.lock(pack)
    assert v1.version == 1
    assert v2.version == 2
    assert v1.resource_pack_id == v2.resource_pack_id
    assert store.get_latest_lock(pack.resource_pack_id).version == 2
    assert len(store.list_locks(pack.resource_pack_id)) == 2
    # v1 unchanged after v2 locked
    assert store.list_locks(pack.resource_pack_id)[1].version == 1
    assert store.list_locks(pack.resource_pack_id)[1].media_asset_ids == v1.media_asset_ids


def test_cannot_lock_non_ready(tmp_path):
    store = ResourcePackLockStore(tmp_path / "locks.db")
    pack = ResourcePackBuilder().build(_result(products=2))
    with pytest.raises(ValueError):
        store.lock(pack)


def test_build_from_snapshot_round_trip(tmp_path):
    from media.research_persistence import make_snapshot

    repo = SQLiteResearchRepository(tmp_path / "snapshots.db")
    result = _result(variants=2, media=4, media_variant="black")
    snapshot = make_snapshot(result)
    repo.save_snapshot(snapshot)
    service = ResourcePackService(lock_store=ResourcePackLockStore(tmp_path / "locks.db"))
    pack = service.build_from_snapshot(repo.get_snapshot(snapshot.snapshot_id), variant_id="variant_black")
    assert pack.pack_status == PackStatus.READY_TO_LOCK
    assert pack.research_snapshot_id == snapshot.snapshot_id
    service.lock(pack)
    assert service.get_latest(pack.resource_pack_id).version == 1


def test_new_research_new_version_not_mutation(tmp_path):
    store = ResourcePackLockStore(tmp_path / "locks.db")
    pack1 = ResourcePackBuilder().build(_result(media=4), variant_id=None)
    pack1.pack_status = PackStatus.READY_TO_LOCK  # no variants in this result
    pack1.reason = ""
    v1 = store.lock(pack1)
    # Simulate newer research with different snapshot id, same pack id.
    pack2 = pack1.model_copy()
    pack2.created_at = "2026-08-11T00:00:00Z"
    v2 = store.lock(pack2)
    assert v1.version == 1
    assert v2.version == 2
    assert store.get_latest_lock(pack1.resource_pack_id).version == 2


def test_mcp_build_and_get_delegate(tmp_path, monkeypatch):
    from media.research_persistence import make_snapshot
    from product_scout import mcp_server

    repo = SQLiteResearchRepository(tmp_path / "snapshots.db")
    result = _result(variants=2, media=4, media_variant="black")
    snapshot = make_snapshot(result)
    repo.save_snapshot(snapshot)

    persist = ResearchPersistenceService(repository=repo)
    from media.resource_pack import ResourcePackService

    pack_service = ResourcePackService(lock_store=ResourcePackLockStore(tmp_path / "locks.db"))

    monkeypatch.setattr(mcp_server, "_research_persistence", lambda: persist)
    monkeypatch.setattr(mcp_server, "_resource_pack", lambda: pack_service)
    built = mcp_server.build_resource_pack(snapshot.snapshot_id, variant_id="variant_black")
    assert built["status"] == "built"
    assert built["pack_status"] == "ready_to_lock"
    assert built["product_identity"]
    assert len(built["assets"]) == 4
    assert all(asset["local_path"] for asset in built["assets"])
    lock = pack_service.lock(pack_service.build_from_snapshot(snapshot, variant_id="variant_black"))
    gotten = mcp_server.get_resource_pack(lock.resource_pack_id)
    assert gotten["status"] == "found"
    assert gotten["version"] == 1


def test_mcp_lock_resource_pack_real_provenance(tmp_path, monkeypatch):
    from media.research_persistence import make_snapshot
    from product_scout import mcp_server

    repo = SQLiteResearchRepository(tmp_path / "snapshots.db")
    result = _result(variants=2, media=4, media_variant="black")
    snapshot = make_snapshot(result)
    repo.save_snapshot(snapshot)

    persist = ResearchPersistenceService(repository=repo)
    pack_service = ResourcePackService(lock_store=ResourcePackLockStore(tmp_path / "locks.db"))

    monkeypatch.setattr(mcp_server, "_research_persistence", lambda: persist)
    monkeypatch.setattr(mcp_server, "_resource_pack", lambda: pack_service)

    # 1. Build pack
    built = mcp_server.build_resource_pack(snapshot.snapshot_id, variant_id="variant_black")
    pack_id = built["resource_pack_id"]
    assert built["pack_status"] == "ready_to_lock"
    assert built["canonical_product_id"] == "product_jisulife_fa55"

    # 2. Lock pack via MCP
    locked = mcp_server.lock_resource_pack(pack_id, owner_user_id="user_alice")
    assert locked["status"] == "locked"
    assert locked["resource_pack_id"] == pack_id
    assert locked["snapshot_id"] == snapshot.snapshot_id
    assert locked["canonical_product_id"] == "product_jisulife_fa55"
    assert locked["owner_user_id"] == "user_alice"
    assert locked["version"] == 1
    assert locked["manifest_digest"].startswith("sha256:")
    assert len(locked["assets"]) == 4

    # Ensure NO synthetic hardcoded values exist
    assert locked["snapshot_id"] != "snap_001"
    assert locked["canonical_product_id"] != "prod_001"
    for asset in locked["assets"]:
        assert "example.com" not in asset.get("local_path", "")
        assert asset["asset_id"].startswith("a")
        assert asset["variant_match"] == "verified"

    # 3. Retrieve lock via get_resource_pack_lock
    retrieved = mcp_server.get_resource_pack_lock(locked["lock_id"])
    assert retrieved["status"] == "locked"
    assert retrieved["lock_id"] == locked["lock_id"]
    assert retrieved["snapshot_id"] == snapshot.snapshot_id
    assert retrieved["manifest_digest"] == locked["manifest_digest"]


def test_mcp_lock_non_ready_pack_returns_error(tmp_path, monkeypatch):
    from media.research_persistence import make_snapshot
    from product_scout import mcp_server

    repo = SQLiteResearchRepository(tmp_path / "snapshots.db")
    result = _result(products=2)  # Ambiguous -> NEEDS_REVIEW
    snapshot = make_snapshot(result)
    repo.save_snapshot(snapshot)

    persist = ResearchPersistenceService(repository=repo)
    pack_service = ResourcePackService(lock_store=ResourcePackLockStore(tmp_path / "locks.db"))

    monkeypatch.setattr(mcp_server, "_research_persistence", lambda: persist)
    monkeypatch.setattr(mcp_server, "_resource_pack", lambda: pack_service)

    built = mcp_server.build_resource_pack(snapshot.snapshot_id)
    pack_id = built["resource_pack_id"]
    assert built["pack_status"] == "needs_review"

    locked = mcp_server.lock_resource_pack(pack_id)
    assert locked["status"] == "error"
    assert "needs_review" in locked["reason"]


def test_mcp_lock_unknown_pack_returns_not_found(tmp_path, monkeypatch):
    from product_scout import mcp_server

    repo = SQLiteResearchRepository(tmp_path / "snapshots.db")
    persist = ResearchPersistenceService(repository=repo)
    pack_service = ResourcePackService(lock_store=ResourcePackLockStore(tmp_path / "locks.db"))

    monkeypatch.setattr(mcp_server, "_research_persistence", lambda: persist)
    monkeypatch.setattr(mcp_server, "_resource_pack", lambda: pack_service)

    locked = mcp_server.lock_resource_pack("pack_nonexistent")
    assert locked["status"] == "not_found"


def test_mcp_lock_durable_recovery_across_restart(tmp_path, monkeypatch):
    from media.research_persistence import make_snapshot
    from product_scout import mcp_server

    snapshots_db = tmp_path / "snapshots.db"
    locks_db = tmp_path / "locks.db"

    repo = SQLiteResearchRepository(snapshots_db)
    result = _result(variants=2, media=4, media_variant="black")
    snapshot = make_snapshot(result)
    repo.save_snapshot(snapshot)

    persist = ResearchPersistenceService(repository=repo)
    pack_service = ResourcePackService(lock_store=ResourcePackLockStore(locks_db))

    monkeypatch.setattr(mcp_server, "_research_persistence", lambda: persist)
    monkeypatch.setattr(mcp_server, "_resource_pack", lambda: pack_service)

    # 1. Build pack
    built = mcp_server.build_resource_pack(snapshot.snapshot_id, variant_id="variant_black")
    pack_id = built["resource_pack_id"]
    assert built["pack_status"] == "ready_to_lock"
    assert pack_id == f"pack_{snapshot.snapshot_id}__variant_black"

    # 2. Simulate full process restart: clear in-memory cache and re-create fresh services
    mcp_server._built_packs.clear()
    fresh_repo = SQLiteResearchRepository(snapshots_db)
    fresh_persist = ResearchPersistenceService(repository=fresh_repo)
    fresh_pack_service = ResourcePackService(lock_store=ResourcePackLockStore(locks_db))

    monkeypatch.setattr(mcp_server, "_research_persistence", lambda: fresh_persist)
    monkeypatch.setattr(mcp_server, "_resource_pack", lambda: fresh_pack_service)

    # 3. Lock pack by resource_pack_id after restart
    locked = mcp_server.lock_resource_pack(pack_id, owner_user_id="user_bob")
    assert locked["status"] == "locked"
    assert locked["lock_id"] == f"lock_{pack_id}_v1"
    assert locked["resource_pack_id"] == pack_id
    assert locked["snapshot_id"] == snapshot.snapshot_id
    assert locked["canonical_product_id"] == "product_jisulife_fa55"
    assert locked["owner_user_id"] == "user_bob"
    assert locked["version"] == 1
    assert locked["manifest_digest"].startswith("sha256:")
    assert len(locked["assets"]) == 4

    # 4. Exact retrieval from fresh service
    retrieved = mcp_server.get_resource_pack_lock(locked["lock_id"])
    assert retrieved == locked


def test_mcp_lock_version_safe_identity_and_idempotent_replay(tmp_path, monkeypatch):
    from media.research_persistence import make_snapshot
    from product_scout import mcp_server

    snapshots_db = tmp_path / "snapshots.db"
    locks_db = tmp_path / "locks.db"

    repo = SQLiteResearchRepository(snapshots_db)
    result = _result(variants=2, media=4, media_variant="black")
    snapshot = make_snapshot(result)
    repo.save_snapshot(snapshot)

    persist = ResearchPersistenceService(repository=repo)
    pack_service = ResourcePackService(lock_store=ResourcePackLockStore(locks_db))

    monkeypatch.setattr(mcp_server, "_research_persistence", lambda: persist)
    monkeypatch.setattr(mcp_server, "_resource_pack", lambda: pack_service)

    built = mcp_server.build_resource_pack(snapshot.snapshot_id, variant_id="variant_black")
    pack_id = built["resource_pack_id"]

    # 1. First lock
    lock_v1 = mcp_server.lock_resource_pack(pack_id, owner_user_id="user_alice")
    assert lock_v1["status"] == "locked"
    assert lock_v1["lock_id"] == f"lock_{pack_id}_v1"
    assert lock_v1["version"] == 1

    # 2. Idempotent replay with same owner and manifest
    lock_v1_replay = mcp_server.lock_resource_pack(pack_id, owner_user_id="user_alice")
    assert lock_v1_replay == lock_v1

    # 3. Verify retrieval by both full versioned lock_id and base pack_id
    assert mcp_server.get_resource_pack_lock(f"lock_{pack_id}_v1") == lock_v1
    assert mcp_server.get_resource_pack_lock(f"lock_{pack_id}") == lock_v1


# --- variant-media safety ---


def test_pack_variant_media_01_no_variants_ready():
    pack = ResourcePackBuilder().build(_result(variants=0, media=4))
    assert pack.pack_status == PackStatus.READY_TO_LOCK
    assert all(item.variant_match == "unknown" for item in pack.media)


def test_pack_variant_media_02_verified_matching_variant_eligible():
    pack = ResourcePackBuilder().build(_result(variants=2, media=4, media_variant="black"), variant_id="variant_black")
    assert pack.pack_status == PackStatus.READY_TO_LOCK
    assert all(item.variant_match == "verified" for item in pack.media)


def test_pack_variant_media_03_conflicting_variant_excluded():
    pack = ResourcePackBuilder().build(_result(variants=2, media=4, media_variant="white"), variant_id="variant_black")
    # White-marked media conflicts with black variant -> excluded, no ready lock.
    assert pack.pack_status == PackStatus.INCOMPLETE
    assert pack.media == []
    assert pack.completeness["media_variant"]["conflict"] == 4


def test_pack_variant_media_04_unknown_variant_needs_review():
    pack = ResourcePackBuilder().build(_result(variants=2, media=4), variant_id="variant_black")
    # Media carries no variant marker -> unknown, not silently verified.
    assert pack.pack_status == PackStatus.NEEDS_REVIEW
    assert pack.reason == "selected variant media not verified against chosen variant"
    assert pack.completeness["media_variant"]["unknown"] == 4
    assert pack.completeness["media_variant"]["verified"] == 0


def test_pack_variant_media_05_high_match_score_does_not_upgrade_variant():
    pack = ResourcePackBuilder().build(_result(variants=2, media=4, media_variant=None), variant_id="variant_black")
    # High product_match_score assets but no variant marker -> still unknown.
    assert all(item.product_match_score is not None for item in pack.media)
    assert all(item.variant_match == "unknown" for item in pack.media)
    assert pack.pack_status == PackStatus.NEEDS_REVIEW


def test_pack_variant_media_06_sibling_variant_excluded_even_with_perfect_match_score():
    """Verify that an image for a sibling variant is excluded even if match_score is 1.0."""
    res = _result(variants=2, media=0)
    sibling_asset = _asset("asset_white_1", match_score=1.0, variant="white")
    matching_asset1 = _asset("asset_black_1", match_score=0.85, variant="black")
    matching_asset2 = _asset("asset_black_2", match_score=0.80, variant="black")
    matching_asset3 = _asset("asset_black_3", match_score=0.75, variant="black")
    res.media_assets = [sibling_asset, matching_asset1, matching_asset2, matching_asset3]
    pack = ResourcePackBuilder().build(res, variant_id="variant_black")
    assert pack.pack_status == PackStatus.READY_TO_LOCK
    asset_ids = [item.asset_id for item in pack.media]
    assert "asset_white_1" not in asset_ids
    assert "asset_black_1" in asset_ids
    assert "asset_black_2" in asset_ids
    assert "asset_black_3" in asset_ids

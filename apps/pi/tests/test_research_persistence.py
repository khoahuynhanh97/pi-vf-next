"""Offline tests for durable research persistence. SQLite temp DB only."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from media.evidence.models import (
    ClaimEvidence,
    EvidenceSource,
    EvidenceValue,
    IdentityEvidence,
    MediaEvidence,
    OfferEvidence,
    ProductEvidence,
    SourceKind,
    SpecificationEvidence,
)
from media.models import MediaAsset, ProductIdentityHint, ProductImageAssessment
from media.research import ProductResearchService
from media.research.models import (
    ProductResearchResult,
    ResearchProductRequest,
    ResearchStatus,
    ResearchSummary,
    SourceResearchResult,
)
from media.research_persistence import (
    SCHEMA_VERSION,
    ResearchPersistenceService,
    SnapshotDeserializationError,
    SQLiteResearchRepository,
    UnsupportedSnapshotVersionError,
    make_snapshot,
    research_id_from_result,
)
from media.resolution.models import (
    CanonicalProduct,
    ProductListing,
    ProductVariant,
    ResolutionConflict,
    ResolvedField,
)


def _stamp():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _evidence(eid, url, brand="Jisulife", model="FA55", price=699000, seller="Store A"):
    src = EvidenceSource(requested_url=url, final_url=url, host=url.split("//")[1].split("/")[0], acquisition_method="http", acquisition_status="success", observed_at=_stamp())
    identity = IdentityEvidence(
        name=EvidenceValue(value=f"{brand} {model}", source_kind=SourceKind.JSON_LD, source_path="Product.name"),
        brand=EvidenceValue(value=brand, source_kind=SourceKind.JSON_LD, source_path="Product.brand.name"),
        model=EvidenceValue(value=model, source_kind=SourceKind.JSON_LD, source_path="Product.model"),
        variant=EvidenceValue(value="Black", source_kind=SourceKind.JSON_LD, source_path="Product.variant") if eid.endswith("b") else None,
    )
    offer = OfferEvidence(
        price=EvidenceValue(value=price, source_kind=SourceKind.JSON_LD, source_path="Product.offers.price"),
        currency=EvidenceValue(value="VND", source_kind=SourceKind.JSON_LD),
        seller_name=EvidenceValue(value=seller, source_kind=SourceKind.JSON_LD),
        availability=EvidenceValue(value="in_stock", source_kind=SourceKind.JSON_LD),
    )
    return ProductEvidence(
        evidence_id=eid,
        source=src,
        identity=identity,
        offer=offer,
        specifications=[SpecificationEvidence(name=EvidenceValue(value="Battery Capacity", source_kind=SourceKind.JSON_LD), value=EvidenceValue(value="5000 mAh", source_kind=SourceKind.JSON_LD))],
        claims=[ClaimEvidence(text=EvidenceValue(value="Turbo cooling", source_kind=SourceKind.JSON_LD))],
        media=MediaEvidence(page_image_urls=[f"https://x.test/{eid}.jpg"]),
    )


def _asset():
    return MediaAsset(
        asset_id="abc123",
        local_path="data/product_scout/images/abc123.jpg",
        sha256="a" * 64,
        phash="b" * 16,
        width=1000,
        height=1000,
        format="jpg",
        discovery={"provider": "duckduckgo_images", "query": "Jisulife FA55", "rank": 1},
        source={"image_url": "https://x.test/a.jpg", "page_url": "https://x.test/p", "host": "x.test"},
        assessment=ProductImageAssessment(product_match_score=0.9, match_status="matched"),
        provenance={"source_class": "marketplace", "usage_policy": "reference_only"},
    )


def _result(urls=("https://s1.test/p",), *, price=699000, status=ResearchStatus.COMPLETED):
    evidence = [_evidence("e1", urls[0], price=price)]
    product = CanonicalProduct(
        canonical_product_id="product_abc",
        brand=ResolvedField(value="Jisulife", supporting_evidence_ids=["e1"], source_values=["Jisulife"]),
        model=ResolvedField(value="FA55", supporting_evidence_ids=["e1"], source_values=["FA55"]),
        variants=[ProductVariant(variant_id="variant_black", canonical_product_id="product_abc", dimensions={"variant": "black"}, supporting_evidence_ids=["e1"])],
        listings=[ProductListing(listing_id="listing_1", canonical_product_id="product_abc", source_url=urls[0], seller="Store A", price=price, currency="VND", source_evidence_ids=["e1"], observed_at=_stamp())],
        supporting_evidence_ids=["e1"],
    )
    source = SourceResearchResult(url=urls[0], acquisition_status="success", acquisition_method="http", evidence_status="completed", evidence_id="e1", host="s1.test", observed_at=_stamp(), usable=True)
    return ProductResearchResult(
        status=status,
        request=ResearchProductRequest(url=urls[0], product_identity_hint=ProductIdentityHint(name="Jisulife FA55", brand="Jisulife", model="FA55")),
        source_results=[source],
        product_evidence=evidence,
        canonical_products=[product],
        media_assets=[_asset()],
        conflicts=[ResolutionConflict(field="specification:battery capacity", candidate_values=["5000 mAh", "4000 mAh"], evidence_ids=["e1"], status="unresolved")],
        warnings=["CONFLICTING_NAME_EVIDENCE"],
        summary=ResearchSummary(sources=1, usable_evidence=1, canonical_products=1, media_assets=1),
    )


def test_round_trip(tmp_path):
    repo = SQLiteResearchRepository(tmp_path / "snapshots.db")
    result = _result()
    snapshot = make_snapshot(result)
    repo.save_snapshot(snapshot)
    loaded = repo.get_snapshot(snapshot.snapshot_id)
    assert loaded.result == result.model_dump(mode="json")
    assert loaded.request == result.request.model_dump(mode="json")
    assert loaded.schema_version == SCHEMA_VERSION
    # lineage survives: canonical brand -> evidence id -> source url
    brand_ev = result.product_evidence[0].identity.brand
    assert loaded.result["canonical_products"][0]["brand"]["supporting_evidence_ids"] == ["e1"]
    assert loaded.result["product_evidence"][0]["source"]["requested_url"] == "https://s1.test/p"
    assert brand_ev.source_path == "Product.brand.name"


def test_reinitialization_durable(tmp_path):
    path = tmp_path / "snapshots.db"
    repo = SQLiteResearchRepository(path)
    snapshot = make_snapshot(_result())
    repo.save_snapshot(snapshot)
    repo.close()
    repo2 = SQLiteResearchRepository(path)
    loaded = repo2.get_snapshot(snapshot.snapshot_id)
    assert loaded.status == "completed"


def test_immutability_price_history(tmp_path):
    repo = SQLiteResearchRepository(tmp_path / "snapshots.db")
    r1 = _result(price=699000)
    r2 = _result(price=649000)
    s1 = make_snapshot(r1)
    s2 = make_snapshot(r2)
    repo.save_snapshot(s1)
    repo.save_snapshot(s2)
    l1 = repo.get_snapshot(s1.snapshot_id)
    l2 = repo.get_snapshot(s2.snapshot_id)
    assert l1.result["canonical_products"][0]["listings"][0]["price"] == 699000
    assert l2.result["canonical_products"][0]["listings"][0]["price"] == 649000
    assert s1.snapshot_id != s2.snapshot_id


def test_canonical_id_evolution(tmp_path):
    repo = SQLiteResearchRepository(tmp_path / "snapshots.db")
    r1 = _result()
    r1.canonical_products[0].canonical_product_id = "product_brand_model_only"
    r2 = _result()
    r2.canonical_products[0].canonical_product_id = "product_brand_model_gtin"
    s1 = make_snapshot(r1)
    s2 = make_snapshot(r2)
    repo.save_snapshot(s1)
    repo.save_snapshot(s2)
    assert repo.get_snapshot(s1.snapshot_id).result["canonical_products"][0]["canonical_product_id"] == "product_brand_model_only"
    assert repo.get_snapshot(s2.snapshot_id).result["canonical_products"][0]["canonical_product_id"] == "product_brand_model_gtin"


def test_media_reference_round_trip(tmp_path):
    repo = SQLiteResearchRepository(tmp_path / "snapshots.db")
    snapshot = make_snapshot(_result())
    repo.save_snapshot(snapshot)
    loaded = repo.get_snapshot(snapshot.snapshot_id)
    asset = loaded.result["media_assets"][0]
    assert asset["asset_id"] == "abc123"
    assert asset["sha256"] == "a" * 64
    assert asset["provenance"]["source_class"] == "marketplace"
    assert asset["provenance"]["usage_policy"] == "reference_only"
    assert asset["assessment"]["match_status"] == "matched"


def test_latest_deterministic_tiebreaker(tmp_path):
    repo = SQLiteResearchRepository(tmp_path / "snapshots.db")
    r1 = _result(price=699000)
    r2 = _result(price=649000)
    s1 = make_snapshot(r1)
    s2 = make_snapshot(r2)
    # Force identical created_at to exercise the rowid tiebreaker.
    s1.created_at = "2026-08-10T00:00:00Z"
    s2.created_at = "2026-08-10T00:00:00Z"
    repo.save_snapshot(s1)
    repo.save_snapshot(s2)
    latest = repo.get_latest_snapshot(s2.research_id)
    assert latest is not None
    assert latest.snapshot_id == s2.snapshot_id  # rowid DESC => newest insert wins


def test_list_snapshots_bounded_and_ordered(tmp_path):
    repo = SQLiteResearchRepository(tmp_path / "snapshots.db")
    r1 = _result(price=699000)
    r2 = _result(price=649000)
    s1 = make_snapshot(r1)
    s2 = make_snapshot(r2)
    repo.save_snapshot(s1)
    repo.save_snapshot(s2)
    history = repo.list_snapshots(s2.research_id, limit=1)
    assert len(history) == 1
    assert history[0].snapshot_id == s2.snapshot_id
    both = repo.list_snapshots(s2.research_id, limit=10)
    assert len(both) == 2


def test_missing_snapshot_raises_keyerror(tmp_path):
    repo = SQLiteResearchRepository(tmp_path / "snapshots.db")
    with pytest.raises(KeyError):
        repo.get_snapshot("snap_nonexistent")


def test_corrupt_record_fails_cleanly(tmp_path):
    repo = SQLiteResearchRepository(tmp_path / "snapshots.db")
    snapshot = make_snapshot(_result())
    repo.save_snapshot(snapshot)
    db = sqlite3.connect(tmp_path / "snapshots.db")
    db.execute("UPDATE research_snapshots SET result_json=? WHERE snapshot_id=?", ("not-json{{", snapshot.snapshot_id))
    db.commit()
    db.close()
    with pytest.raises(SnapshotDeserializationError):
        repo.get_snapshot(snapshot.snapshot_id)


def test_unsupported_version_fails(tmp_path):
    repo = SQLiteResearchRepository(tmp_path / "snapshots.db")
    snapshot = make_snapshot(_result())
    snapshot.schema_version = 99
    with pytest.raises(UnsupportedSnapshotVersionError):
        repo.save_snapshot(snapshot)


def test_persistence_service_saves_and_skips_error(tmp_path):
    service = ResearchPersistenceService(repository=SQLiteResearchRepository(tmp_path / "snapshots.db"))
    snapshot_id, status = service.save(_result())
    assert status["status"] == "saved"
    assert service.get(snapshot_id).status == "completed"
    error_result = _result(status=ResearchStatus.ERROR)
    _, error_status = service.save(error_result)
    assert error_status["status"] == "skipped"


def test_research_id_from_result_deterministic():
    r1 = _result()
    r2 = _result()
    assert research_id_from_result(r1) == research_id_from_result(r2)


def test_missing_media_binary_metadata_still_loads(tmp_path):
    # Metadata refers to a file that does not exist; snapshot load must succeed.
    repo = SQLiteResearchRepository(tmp_path / "snapshots.db")
    snapshot = make_snapshot(_result())
    repo.save_snapshot(snapshot)
    loaded = repo.get_snapshot(snapshot.snapshot_id)
    assert loaded.result["media_assets"][0]["local_path"].endswith(".jpg")


def test_snapshot_contains_challenged_observation(tmp_path):

    repo = SQLiteResearchRepository(tmp_path / "snapshots.db")
    result = _result()
    challenged = SourceResearchResult(url="https://tiktok.test/x", acquisition_status="challenged", acquisition_method="browser", evidence_status="no_product_evidence", host="tiktok.test", signals=["captcha_visible"], observed_at=_stamp(), usable=False)
    result.source_results.append(challenged)
    snapshot = make_snapshot(result)
    repo.save_snapshot(snapshot)
    loaded = repo.get_snapshot(snapshot.snapshot_id)
    sources = loaded.result["source_results"]
    assert any(s["acquisition_status"] == "challenged" and "captcha_visible" in s["signals"] for s in sources)


def test_product_research_service_auto_saves(tmp_path):
    from media.source_discovery import SourceCandidate

    discovered = [
        SourceCandidate(candidate_id="s1", url="https://s1.test/p", title="Jisulife FA55", host="s1.test", query="q", discovery_provider="fake", rank=1),
    ]

    class FakeDiscovery:
        config = type("C", (), {"max_sources": 8, "per_host_limit": 2, "target_usable_sources": 3, "max_results": 15, "diversity_order": ()})()

        def discover(self, hint, **kwargs):
            return discovered

    from media.research_persistence import ResearchPersistenceService
    try:
        from tests.test_research import FakeEvidenceService, _evidence as _research_evidence
    except ModuleNotFoundError:
        from test_research import FakeEvidenceService, _evidence as _research_evidence

    fake_evidence = FakeEvidenceService({"https://s1.test/p": _research_evidence("e1", "https://s1.test/p", brand="Jisulife", model="FA55", price=699000)})
    service = ProductResearchService(
        evidence_service=fake_evidence,
        resolver=__import__("media.resolution", fromlist=["ProductResolver"]).ProductResolver(),
        media=_NoopMedia(),
        discovery=FakeDiscovery(),
        persistence=ResearchPersistenceService(repository=SQLiteResearchRepository(tmp_path / "snapshots.db")),
    )
    result = service.research(ResearchProductRequest(query="Jisulife FA55", discover_sources=True))
    assert result.persistence_status == "saved"
    assert result.snapshot_id is not None
    loaded = service.persistence.get(result.snapshot_id)
    assert loaded.status == result.status.value


def test_mcp_get_product_research_delegates(tmp_path, monkeypatch):
    from media.research_persistence import (
        ResearchPersistenceService,
        SQLiteResearchRepository,
    )
    from product_scout import mcp_server

    service = ResearchPersistenceService(repository=SQLiteResearchRepository(tmp_path / "snapshots.db"))
    snapshot_id, _ = service.save(_result())

    monkeypatch.setattr(mcp_server, "_research_persistence", lambda: service)
    out = mcp_server.get_product_research(snapshot_id)
    assert out["status"] == "found"
    assert out["snapshot_id"] == snapshot_id
    assert out["research_status"] == "completed"


class _NoopMedia:
    def research(self, hint, **kwargs):
        from media.models import MediaResearchResult

        return MediaResearchResult(status="completed", product=hint, selected_assets=[])


def test_sqlite_research_repository_lock_persistence_and_idempotency(tmp_path):
    repo = SQLiteResearchRepository(tmp_path / "research_repo.db")
    payload = {
        "lock_id": "lock_pack_snap_123_v1",
        "resource_pack_id": "pack_snap_123",
        "snapshot_id": "snap_123",
        "canonical_product_id": "prod_123",
        "owner_user_id": "user_456",
        "version": 1,
        "manifest_digest": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "created_at": "2026-08-29T12:00:00Z",
        "assets": [{"asset_id": "a1", "local_path": "/tmp/a1.jpg"}],
    }

    # Save initial lock
    saved = repo.save_lock(payload)
    assert saved["lock_id"] == "lock_pack_snap_123_v1"

    # Exact retrieval by lock_id
    retrieved = repo.get_lock("lock_pack_snap_123_v1")
    assert retrieved is not None
    assert retrieved["resource_pack_id"] == "pack_snap_123"
    assert retrieved["manifest_digest"] == payload["manifest_digest"]
    assert retrieved["assets"] == payload["assets"]

    # Retrieval by identity tuple
    by_identity = repo.get_lock_by_identity("pack_snap_123", "user_456", payload["manifest_digest"])
    assert by_identity is not None
    assert by_identity["lock_id"] == "lock_pack_snap_123_v1"

    # Idempotent save with same lock_id or same identity tuple
    replayed = repo.save_lock(payload)
    assert replayed["lock_id"] == "lock_pack_snap_123_v1"

    # Non-existent lock returns None
    assert repo.get_lock("non_existent_lock_id") is None
    assert repo.get_lock_by_identity("pack_snap_123", "user_456", "sha256:diff") is None

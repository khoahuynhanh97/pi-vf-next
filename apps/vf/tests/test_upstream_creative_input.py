from video_factory.automation.contracts import ExchangeProduct
from video_factory.creative.contracts import EligibleAsset, ProductIdentityV2, ResearchDossier


def test_optional_creative_input_roundtrips_in_dossier_without_becoming_verified_claim():
    values = {
        "kind": "creative-input-v1",
        "candidate_id": "listing-1",
        "csv_sha256": "a" * 64,
        "video_concept": "Show the package on a clean table.",
        "script_draft": "Open the package and plate the product.",
        "claims_to_verify": "Claims about ingredients are unverified.",
        "source_refs_json": "[]",
    }
    product = ExchangeProduct(
        priority=1, canonical_product_key="brand:model", product_key="key", name="Product",
        brand="Brand", model="Model", creative_input=values,
        seller_description="Seller supplied copy, unverified.",
    )
    assert product.to_dict()["creative_input"] == values
    dossier = ResearchDossier.create(
        workflow_id="wf", product_identity=ProductIdentityV2("Brand", "Model", "Product"),
        verified_claims=(), eligible_assets=(EligibleAsset("a", "x", "b" * 64, "qc"),),
        creative_input=values, seller_description="Seller supplied copy, unverified.",
    )
    restored = ResearchDossier.from_dict(dossier.to_dict())
    assert restored.creative_input == values
    assert restored.seller_description == "Seller supplied copy, unverified."
    assert restored.verified_claims == ()
    assert "script_draft" in restored.to_json()


def test_empty_optional_context_preserves_legacy_serialization_and_digest():
    dossier = ResearchDossier.create(
        workflow_id="wf", product_identity=ProductIdentityV2("Brand", "Model", "Product"),
        verified_claims=(), eligible_assets=(EligibleAsset("a", "x", "b" * 64, "qc"),),
    )
    payload = dossier.to_dict()
    assert "creative_input" not in payload
    assert "seller_description" not in payload
    assert ResearchDossier.from_dict(payload).digest() == dossier.digest()

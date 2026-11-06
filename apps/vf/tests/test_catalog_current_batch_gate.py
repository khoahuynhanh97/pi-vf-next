import sqlite3
from pathlib import Path

from video_factory.automation.contracts import ExchangeProduct
from video_factory.automation.coordinator import ProductVideoCoordinator


def test_catalog_absence_warns_but_does_not_block_exchange_admission(tmp_path: Path) -> None:
    connection = sqlite3.connect(":memory:")
    coordinator = ProductVideoCoordinator(connection, inbox_root=tmp_path)
    product = ExchangeProduct(
        priority=1,
        canonical_product_key="brand:sku-new",
        product_key="product-key",
        name="Brand SKU New",
        brand="Brand",
        model="SKU New",
    )
    assert coordinator.evaluate_catalog_candidate(product, tmp_path, None) == (True, None)
    assert coordinator.evaluate_catalog_candidate(product, tmp_path, {}) == (True, None)
    assert coordinator.evaluate_catalog_candidate(
        product, tmp_path, {"brand:sku-new": ("REVIEW_REQUIRED", "BLOCKED")},
    ) == (True, "CATALOG_IDENTITY_NOT_VALID")
    assert coordinator.evaluate_catalog_candidate(
        product, tmp_path, {"brand:sku-new": ("VALID", "READY_FOR_IMAGE_QC")},
    ) == (True, None)
    connection.close()

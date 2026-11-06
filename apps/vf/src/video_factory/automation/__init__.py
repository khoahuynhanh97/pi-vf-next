"""Hermes Personal Product Video Automation package.

Provides exchange boundary contracts, atomic import repository, manifest importer,
and the unified ProductVideoCoordinator.
"""

from __future__ import annotations

from .contracts import (
    BatchStartResult,
    CandidateItem,
    ExchangeImage,
    ExchangeManifest,
    ExchangeProduct,
    compute_canonical_product_key,
    compute_product_key,
)
from .autonomous_pipeline import AutonomousVideoPipeline

def __getattr__(name: str):
    if name == "ProductVideoCoordinator":
        from .coordinator import ProductVideoCoordinator
        return ProductVideoCoordinator
    if name in ("ExchangeImporter", "publish_daily_run_to_exchange", "DEFAULT_EXCHANGE_INBOX"):
        from . import importer
        return getattr(importer, name)
    if name in ("ClaimResult", "DailyImportRepository"):
        from . import repository
        return getattr(repository, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "BatchStartResult",
    "CandidateItem",
    "ClaimResult",
    "DailyImportRepository",
    "ExchangeImage",
    "ExchangeImporter",
    "ExchangeManifest",
    "ExchangeProduct",
    "ProductVideoCoordinator",
    "compute_canonical_product_key",
    "compute_product_key",
    "publish_daily_run_to_exchange",
    "DEFAULT_EXCHANGE_INBOX",
]

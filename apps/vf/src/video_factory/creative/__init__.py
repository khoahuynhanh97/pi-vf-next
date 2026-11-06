"""Grounded creative contracts and generation pipeline for Product Video."""

from .contracts import (
    EligibleAsset,
    EvidenceClaim,
    ProductIdentityV2,
    ResearchDossier,
    canonical_digest,
)
from .dossier import DossierBuildError, build_research_dossier

__all__ = [
    "DossierBuildError",
    "EligibleAsset",
    "EvidenceClaim",
    "ProductIdentityV2",
    "ResearchDossier",
    "build_research_dossier",
    "canonical_digest",
]

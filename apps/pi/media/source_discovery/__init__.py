from .classify import build_discovery_query, classify_source
from .models import (
    SourceCandidate,
    SourceClass,
    SourceDiscoveryProvider,
    SourceDiscoveryQuery,
)
from .providers import DuckDuckGoSourceDiscoveryProvider, TEXT_BACKENDS
from .service import SourceDiscoveryConfig, SourceDiscoveryService

__all__ = [
    "DuckDuckGoSourceDiscoveryProvider",
    "TEXT_BACKENDS",
    "SourceCandidate",
    "SourceClass",
    "SourceDiscoveryConfig",
    "SourceDiscoveryProvider",
    "SourceDiscoveryQuery",
    "SourceDiscoveryService",
    "build_discovery_query",
    "classify_source",
]

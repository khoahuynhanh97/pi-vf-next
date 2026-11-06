from __future__ import annotations

from enum import Enum


class CapabilitySource(str, Enum):
    BUILTIN = "builtin"
    MCP = "mcp"
    EXTERNAL = "external"


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class CostClass(str, Enum):
    FREE = "free"
    CHEAP = "cheap"
    STANDARD = "standard"
    EXPENSIVE = "expensive"


class RiskLevel(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

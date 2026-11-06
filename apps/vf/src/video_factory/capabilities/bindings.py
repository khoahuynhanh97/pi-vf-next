from __future__ import annotations

from dataclasses import dataclass

from video_factory.capabilities.contracts import (
    CapabilitySource,
    HealthStatus,
)


@dataclass(frozen=True)
class ImplementationBinding:
    capability_id: str
    wire_name: str
    source: CapabilitySource
    provider: str
    implementation_version: str
    input_schema_version: str
    output_schema_version: str
    schema_digest: str
    health: HealthStatus

    def __post_init__(self) -> None:
        for field_name in (
            "capability_id",
            "wire_name",
            "provider",
            "implementation_version",
            "input_schema_version",
            "output_schema_version",
            "schema_digest",
        ):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be blank")

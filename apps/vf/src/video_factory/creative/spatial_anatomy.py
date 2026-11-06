"""Product Spatial Anatomy Lock: định nghĩa vị trí linh kiện chuẩn để ngăn AI méo mó."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ComponentPosition:
    """Vị trí 1 linh kiện/port trên sản phẩm."""
    name: str
    location: str  # "top", "bottom", "front", "back", "left_side", "right_side"
    description: str
    critical_donts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SpatialAnatomy:
    """Khóa giải phẫu không gian của 1 sản phẩm."""
    product_name: str
    components: list[ComponentPosition] = field(default_factory=list)
    required_anchor_shots: list[str] = field(default_factory=list)
    
    def to_prompt_injection(self) -> str:
        """Chuyển thành đoạn text nhồi vào prompt."""
        lines = [f"Product anatomy reference for {self.product_name}:"]
        for comp in self.components:
            lines.append(f"- {comp.name} ({comp.location}): {comp.description}")
            if comp.critical_donts:
                lines.append(f"  NEVER: {', '.join(comp.critical_donts)}")
        return "\n".join(lines)
    
    def to_negative_prompt(self) -> str:
        """Tạo negative prompt từ các critical_donts."""
        neg_parts = []
        for comp in self.components:
            neg_parts.extend(comp.critical_donts)
        return ", ".join(neg_parts) if neg_parts else ""


def extract_from_product_dossier(dossier: dict[str, Any]) -> SpatialAnatomy | None:
    """Đọc spatial anatomy từ product dossier (nếu có)."""
    anat = dossier.get("product_anatomy")
    if not anat or not isinstance(anat, dict):
        return None
    
    components = []
    for loc, desc_or_dict in anat.items():
        if loc == "critical_donts":
            continue
        if isinstance(desc_or_dict, str):
            components.append(ComponentPosition(name=loc, location=loc, description=desc_or_dict))
        elif isinstance(desc_or_dict, dict):
            components.append(ComponentPosition(
                name=desc_or_dict.get("name", loc),
                location=loc,
                description=desc_or_dict.get("description", ""),
                critical_donts=desc_or_dict.get("critical_donts", []),
            ))
    
    global_donts = anat.get("critical_donts", [])
    if global_donts and components:
        for comp in components:
            comp.critical_donts.extend(global_donts)
    
    return SpatialAnatomy(
        product_name=dossier.get("product_name", "Product"),
        components=components,
        required_anchor_shots=anat.get("required_anchor_shots", []),
    )

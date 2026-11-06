import json
from pathlib import Path
import pytest

from video_factory.creative.contracts import (
    ProductCategory,
    ProductDossier,
    SevenPartPromptBlock,
)
from video_factory.creative.generator import compile_seven_part_prompt
from video_factory.creative.ports import StructuredModelResult
from video_factory.qc.cv_integrity_checker import VideoFrameIntegrityChecker
from video_factory.qc.auto_retry_helpers import inject_safe_zone_constraint


class MockLLM:
    def __init__(self, responses: dict[str, dict]):
        self.responses = responses

    def generate_json(self, **kwargs) -> StructuredModelResult:
        user_prompt = kwargs.get("user_prompt", "")
        # Pick response based on category value in user_prompt
        if "- Category: tech" in user_prompt:
            payload = self.responses["tech"]
        else:
            payload = self.responses["beauty"]
        return StructuredModelResult(
            parsed=payload,
            raw_text=json.dumps(payload),
            provider="mock",
            model="mock-v1",
            total_tokens=150,
        )


def test_e2e_multi_product_pipeline():
    """Validate prompt compilation, container isolation, and safe-zone QC for Tech & Beauty."""
    # 1. Load Tech Fixture (Ugreen)
    tech_path = Path("tests/fixtures/product_tech_ugreen.json")
    with open(tech_path, "r", encoding="utf-8") as f:
        tech_data = json.load(f)
    tech_dossier = ProductDossier(
        sku=tech_data["sku"],
        name=tech_data["name"],
        category=ProductCategory(tech_data["category"]),
        physical_specs=tech_data["physical_specs"],
        hero_image_path=tech_data["hero_image_path"],
        creator_reference_path=tech_data.get("creator_reference_path"),
        verified_claims=tech_data["verified_claims"],
        prohibited_terms=tech_data["prohibited_terms"],
        language=tech_data["language"],
    )

    # 2. Load Beauty Fixture (LUMIS)
    beauty_path = Path("tests/fixtures/product_beauty_serum.json")
    with open(beauty_path, "r", encoding="utf-8") as f:
        beauty_data = json.load(f)
    beauty_dossier = ProductDossier(
        sku=beauty_data["sku"],
        name=beauty_data["name"],
        category=ProductCategory(beauty_data["category"]),
        physical_specs=beauty_data["physical_specs"],
        hero_image_path=beauty_data["hero_image_path"],
        creator_reference_path=beauty_data.get("creator_reference_path"),
        verified_claims=beauty_data["verified_claims"],
        prohibited_terms=beauty_data["prohibited_terms"],
        language=beauty_data["language"],
    )

    mock_llm = MockLLM(
        responses={
            "tech": {
                "shot": "Medium close-up, static eye-level desk framing",
                "subject": tech_dossier.get_subject_identity(),
                "action": "Hand plugs fast charging USB-C cable into top port, robot smile appears",
                "setting": "Clean wooden desk with mechanical keyboard and monitor",
                "light_palette": "Soft diffuse daylight, minimal glare",
                "look": "35mm texture with crisp product edges",
                "sound": "Click of cable insertion, soft electronic chime, No music",
            },
            "beauty": {
                "shot": "Extreme macro close-up, smooth push-in",
                "subject": beauty_dossier.get_subject_identity(),
                "action": "Dropper releases single droplet onto glowing cheekbone, absorbing instantly",
                "setting": "Minimalist marble vanity with frosted glass accent",
                "light_palette": "Morning golden hour window light, warm skin highlights",
                "look": "Natural skin pores, 35mm grain, no digital smoothing",
                "sound": "Gentle fluid drop impact, ambient morning atmosphere, No music",
            },
        }
    )

    # Compile 7-part prompts
    tech_block = compile_seven_part_prompt("Creator plugs in phone", tech_dossier, mock_llm)
    beauty_block = compile_seven_part_prompt("Dropper applies serum", beauty_dossier, mock_llm)

    tech_prose = tech_block.to_prose_prompt()
    beauty_prose = beauty_block.to_prose_prompt()

    # Container isolation check
    for forbidden in ["9:16", "8s", "8 seconds", "1080p", "4k"]:
        assert forbidden not in tech_prose.lower()
        assert forbidden not in beauty_prose.lower()

    # Subject identity anchor check
    assert tech_dossier.name in tech_prose
    assert beauty_dossier.name in beauty_prose

    # Check constraint injection simulation
    retry_prompt = inject_safe_zone_constraint(tech_prose, ["right_action_bar"])
    assert "[SPATIAL CONSTRAINT: Shift action leftward" in retry_prompt

import json
from pathlib import Path
import pytest

from video_factory.creative.contracts import (
    ProductCategory,
    ProductDossier,
    SevenPartPromptBlock,
)
from video_factory.creative.generator import (
    StructuredGenerationError,
    compile_seven_part_prompt,
)
from video_factory.creative.ports import StructuredModelResult


class DummyModel:
    def __init__(self, response_payload: dict):
        self.response_payload = response_payload

    def generate_json(self, **kwargs) -> StructuredModelResult:
        return StructuredModelResult(
            parsed=self.response_payload,
            raw_text=json.dumps(self.response_payload),
            provider="mock",
            model="mock-v1",
            total_tokens=100,
        )


def test_beauty_product_all_fields_mandatory():
    """BEAUTY category must have all 7 fields non-empty."""
    fixture_path = Path("tests/fixtures/product_beauty_serum.json")
    with open(fixture_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    beauty_dossier = ProductDossier(
        sku=data["sku"],
        name=data["name"],
        category=ProductCategory(data["category"]),
        physical_specs=data["physical_specs"],
        hero_image_path=data["hero_image_path"],
        creator_reference_path=data.get("creator_reference_path"),
        verified_claims=data["verified_claims"],
        prohibited_terms=data["prohibited_terms"],
        language=data["language"],
    )

    # Missing sound & look for beauty
    incomplete_payload = {
        "shot": "Extreme macro close-up, static eye-level",
        "subject": "Golden glass dropper releasing single amber serum droplet",
        "action": "Droplet detaches in slow motion and impacts creator's cheekbone",
        "setting": "Minimalist marble bathroom vanity with soft green towel",
        "light_palette": "Soft diffuse morning daylight from window left, warm golden highlights",
        "look": "",
        "sound": "",
    }
    model = DummyModel(incomplete_payload)
    with pytest.raises(StructuredGenerationError) as exc_info:
        compile_seven_part_prompt("Scene of dropper applying serum", beauty_dossier, model)
    assert "requires non-empty 'look'" in str(exc_info.value)

    # Complete beauty payload
    complete_payload = dict(incomplete_payload)
    complete_payload["look"] = "35mm texture, visible fine skin pores, no digital sharpening"
    complete_payload["sound"] = "Soft ambient room tone, liquid droplet splash, No music"
    model_complete = DummyModel(complete_payload)
    block = compile_seven_part_prompt("Scene of dropper applying serum", beauty_dossier, model_complete)
    assert block.shot == complete_payload["shot"]
    assert block.look == complete_payload["look"]
    assert block.sound == complete_payload["sound"]


def test_tech_product_optional_fields_allowed():
    """TECH category allows empty SOUND/SETTING."""
    fixture_path = Path("tests/fixtures/product_tech_ugreen.json")
    with open(fixture_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    tech_dossier = ProductDossier(
        sku=data["sku"],
        name=data["name"],
        category=ProductCategory(data["category"]),
        physical_specs=data["physical_specs"],
        hero_image_path=data["hero_image_path"],
        creator_reference_path=data.get("creator_reference_path"),
        verified_claims=data["verified_claims"],
        prohibited_terms=data["prohibited_terms"],
        language=data["language"],
    )

    payload = {
        "shot": "Medium close-up, 45-degree high angle desk view",
        "subject": "Ugreen Uno Robot 65W GaN Fast Charger plugged into desk outlet",
        "action": "Creator connects braided USB-C cable, TFT mini LED screen blinks into happy smile",
        "setting": "",
        "light_palette": "Modern clean desk lamp, cool white tone",
        "look": "",
        "sound": "",
    }
    model = DummyModel(payload)
    block = compile_seven_part_prompt("Creator plugs in phone", tech_dossier, model)
    assert block.shot == payload["shot"]
    assert block.setting == ""
    assert block.sound == ""


def test_prose_prompt_no_container_params():
    """Compiled prompt must not contain '9:16', '8s', '1080p'."""
    block = SevenPartPromptBlock(
        shot="Close-up handheld shot.",
        subject="Woman holding LUMIS serum.",
        action="She pats the serum gently onto her cheek.",
        setting="Bright modern bathroom.",
        light_palette="Warm golden hour.",
        look="Natural skin texture with soft grain.",
        sound="Gentle patting sound, No music.",
    )
    prose = block.to_prose_prompt()
    forbidden = ["9:16", "8s", "8 seconds", "1080p", "4k"]
    for word in forbidden:
        assert word not in prose.lower(), f"Forbidden container param '{word}' found in prose"

    # Test sentence punctuation formatting
    assert prose.endswith(".")
    assert "  " not in prose

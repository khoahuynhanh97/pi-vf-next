from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from video_factory.creative.contracts import (
    CreativeBriefV2,
    EligibleAsset,
    EvidenceClaim,
    ProductIdentityV2,
    ResearchDossier,
)
from video_factory.creative.generator import (
    SingleModelCreativeGenerator,
    StructuredGenerationError,
)
from video_factory.creative.ports import StructuredModelResult


def _dossier() -> ResearchDossier:
    return ResearchDossier.create(
        workflow_id="pv-generator-test",
        product_identity=ProductIdentityV2(
            brand="Baseus",
            model="Bowie WM02",
            display_name="Baseus Bowie WM02",
        ),
        verified_claims=(
            EvidenceClaim(
                "claim-01",
                "Tai nghe đi kèm hộp sạc",
                ("evidence-01",),
                source_locators=("https://www.baseus.com/products/bowie-wm02",),
                evidence_snippets=("The charging case stores the earbuds.",),
                evidence_locator="https://www.baseus.com/products/bowie-wm02#details",
                evidence_record_id="validated-record-for-unit-test",
            ),
        ),
        eligible_assets=(
            EligibleAsset("asset-1", "C:/media/one.jpg", "1" * 64, "iqc-1"),
            EligibleAsset("asset-2", "C:/media/two.jpg", "2" * 64, "iqc-1"),
        ),
        pain_points=("Khó cất tai nghe nhanh khi di chuyển",),
        use_cases=("Rời bàn làm việc",),
    )


def _valid_brief(dossier: ResearchDossier) -> dict[str, Any]:
    return {
        "schema_version": "creative-brief-v2",
        "audience": {
            "segment": "Người thường xuyên mang tai nghe khi di chuyển",
            "situation": "Rời bàn làm việc",
            "desired_outcome": "Cất tai nghe nhanh và gọn",
        },
        "consumer_insight": "Tai nghe chỉ tiện khi lấy ra và cất lại không làm gián đoạn việc di chuyển",
        "problem": "Tai nghe dễ bị bỏ quên nếu việc cất lại mất thời gian",
        "creative_angle": "Theo chân một lần cất tai nghe trước khi rời bàn",
        "hook": "Bạn mất bao lâu để cất tai nghe trước khi đi?",
        "promise": "Cho thấy thao tác cất tai nghe vào hộp trong một tình huống thực tế",
        "reason_to_believe": [
            {"text": "Tai nghe đi kèm hộp sạc", "evidence_refs": ["claim-01"]}
        ],
        "tone": "Đời thường, nhanh và rõ",
        "story_archetype": "day_in_the_life",
        "cta": "Xem thêm thông tin sản phẩm.",
        "restrictions": ["Không đưa thông tin khuyến mãi"],
        "target_duration_seconds": 18,
        "prompt_version": "creative-direction-v2",
        "dossier_digest": dossier.digest(),
    }


class _ValidEvidenceForGeneratorUnitTests:
    def verify(self, *_args: object, **_kwargs: object) -> bool:
        return True


@dataclass
class _Request:
    system_prompt: str
    user_prompt: str
    schema_name: str


class _FakeModel:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.requests: list[_Request] = []

    def generate_json(self, **kwargs: Any) -> StructuredModelResult:
        self.requests.append(
            _Request(
                system_prompt=kwargs["system_prompt"],
                user_prompt=kwargs["user_prompt"],
                schema_name=kwargs["schema_name"],
            )
        )
        parsed = self.responses.pop(0)
        return StructuredModelResult(
            parsed=parsed,
            raw_text=str(parsed),
            provider="fake-provider",
            model="fake-model",
            total_tokens=100,
        )


def test_direction_prompt_contains_dossier_and_grounding_rules() -> None:
    dossier = _dossier()
    model = _FakeModel([_valid_brief(dossier)])

    brief = SingleModelCreativeGenerator(model, source_verifier=_ValidEvidenceForGeneratorUnitTests()).create_direction(dossier)

    request = model.requests[0]
    assert dossier.product_identity.display_name in request.user_prompt
    assert "Every factual product claim" in request.system_prompt
    assert request.schema_name == "creative-brief-v2"
    assert brief.dossier_digest == dossier.digest()
    assert brief.provenance.model == "fake-model"
    assert CreativeBriefV2.from_dict(brief.to_dict()).digest() == brief.digest()


def test_generator_allows_one_repair_call_then_stops() -> None:
    dossier = _dossier()
    model = _FakeModel(
        [
            {"creative_angle": "missing required fields"},
            {"creative_angle": "still invalid"},
        ]
    )

    with pytest.raises(StructuredGenerationError, match="2 invalid structured responses") as raised:
        SingleModelCreativeGenerator(model, source_verifier=_ValidEvidenceForGeneratorUnitTests()).create_direction(dossier)

    assert len(model.requests) == 2
    assert "VALIDATION ERRORS" in model.requests[1].user_prompt
    assert raised.value.request_sent is True


def test_direction_rewrites_only_noncompliant_cta_and_records_raw_value() -> None:
    dossier = _dossier()
    response = _valid_brief(dossier)
    response["cta"] = "Mua ngay"
    model = _FakeModel([response])

    brief = SingleModelCreativeGenerator(
        model, source_verifier=_ValidEvidenceForGeneratorUnitTests(),
    ).create_direction(dossier)

    assert brief.cta == "Tìm hiểu thêm về sản phẩm."
    assert brief.reason_to_believe[0].text == response["reason_to_believe"][0]["text"]
    assert brief.reason_to_believe[0].evidence_refs == ("claim-01",)
    assert brief.provenance.creative_adjustments == ({
        "field": "cta",
        "raw_value": "Mua ngay",
        "reason": "CTA mua hàng không được phép; thay bằng CTA trung tính cho đăng thủ công.",
    },)
    assert len(model.requests) == 1
    assert CreativeBriefV2.from_dict(brief.to_dict()).digest() == brief.digest()


def test_other_invalid_content_gets_one_repair_with_specific_error_not_auto_edited() -> None:
    dossier = _dossier()
    first = _valid_brief(dossier)
    first["cta"] = "Mua ngay"
    first["reason_to_believe"][0]["evidence_refs"] = ["unknown-claim"]
    repaired = _valid_brief(dossier)
    model = _FakeModel([first, repaired])

    brief = SingleModelCreativeGenerator(
        model, source_verifier=_ValidEvidenceForGeneratorUnitTests(),
    ).create_direction(dossier)

    assert brief.cta == "Xem thêm thông tin sản phẩm."
    assert len(model.requests) == 2
    assert "reason_to_believe references unknown claims" in model.requests[1].user_prompt
    assert "unknown-claim" in model.requests[1].user_prompt
    assert brief.provenance.creative_adjustments == ()


def test_transport_unknown_stops_without_repair_request() -> None:
    class UnknownModel:
        calls = 0

        def generate_json(self, **_kwargs: Any) -> StructuredModelResult:
            self.calls += 1
            raise RuntimeError("transport outcome unknown; reconcile")

    dossier = _dossier()
    model = UnknownModel()
    with pytest.raises(RuntimeError, match="reconcile"):
        SingleModelCreativeGenerator(model).create_direction(dossier)
    assert model.calls == 1


def test_generator_does_not_call_model_without_grounded_claims() -> None:
    dossier = _dossier()
    empty_dossier = ResearchDossier.create(
        workflow_id=dossier.workflow_id,
        product_identity=dossier.product_identity,
        verified_claims=(),
        eligible_assets=dossier.eligible_assets,
    )
    model = _FakeModel([])
    with pytest.raises(StructuredGenerationError, match="verified source evidence"):
        SingleModelCreativeGenerator(model).create_direction(empty_dossier)
    assert model.requests == []

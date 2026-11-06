from __future__ import annotations

from types import SimpleNamespace
from typing import Any
import pytest

from video_factory.creative.model_adapter import (
    HermesStructuredCreativeModel,
)
from video_factory.creative.prompts import CREATIVE_BRIEF_V2_SCHEMA
from video_factory.creative.generator import StructuredGenerationError


def _response(content: str, *, model: str = "gemini-test", tokens: int = 17) -> Any:
    return SimpleNamespace(
        model=model,
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(total_tokens=tokens),
    )


def test_adapter_uses_host_auxiliary_route_and_json_schema() -> None:
    calls: list[dict[str, Any]] = []

    def caller(**kwargs: Any) -> Any:
        calls.append(kwargs)
        kwargs["route_info"].update(provider="vertex", model="configured-model")
        return _response('{"ok": true}')

    result = HermesStructuredCreativeModel(caller=caller).generate_json(
        system_prompt="grounded system",
        user_prompt="dossier",
        schema_name="creative-brief-v2",
        json_schema={"type": "object"},
        temperature=0.4,
    )

    assert result.parsed == {"ok": True}
    assert result.provider == "vertex"
    assert result.model == "gemini-test"
    assert result.total_tokens == 17
    assert calls[0]["task"] == "product_video_creative"
    assert calls[0]["extra_body"]["response_format"]["type"] == "json_schema"
    assert calls[0]["extra_body"]["response_format"]["json_schema"]["name"] == (
        "creative-brief-v2"
    )


def test_adapter_preserves_invalid_raw_response_for_generator_repair() -> None:
    result = HermesStructuredCreativeModel(
        caller=lambda **_: _response("```json\n{truncated\n```")
    ).generate_json(
        system_prompt="system",
        user_prompt="prompt",
        schema_name="script-scene-plan-v2",
        json_schema={"type": "object"},
        temperature=0.3,
    )

    assert result.parsed == {}
    assert result.raw_text == "```json\n{truncated\n```"
    assert result.parse_error


def test_adapter_parses_fenced_json_with_trailing_commentary() -> None:
    result = HermesStructuredCreativeModel(
        caller=lambda **_: _response('```json\n{"ok": true}\n```\nThe JSON follows the requested schema.')
    ).generate_json(
        system_prompt="system",
        user_prompt="prompt",
        schema_name="scene-prompt-bundle-v1",
        json_schema={"type": "object"},
        temperature=0.2,
    )

    assert result.parsed == {"ok": True}
    assert not result.parse_error


def test_adapter_marks_post_dispatch_transport_error_uncertain() -> None:
    def caller(**kwargs: Any) -> Any:
        kwargs["route_info"].update(provider="vertex", model="configured-model")
        raise TimeoutError("socket closed while waiting for response")

    with pytest.raises(StructuredGenerationError) as error:
        HermesStructuredCreativeModel(caller=caller).generate_json(
            system_prompt="system", user_prompt="prompt", schema_name="scene-prompt-bundle-v1",
            json_schema={"type":"object"}, temperature=0.2)

    assert error.value.request_sent is True
    assert error.value.reconcile_required is True
    assert error.value.route_receipt["provider"] == "vertex"
    assert error.value.route_receipt["cost_status"] == "unknown"
    from video_factory.handlers._base import _creative_failure
    failed = _creative_failure(error.value, None, "VF_SCENE_FAILED")
    assert failed.retryable is False
    assert failed.code == "CREATIVE_OUTCOME_UNCERTAIN"


def test_adapter_marks_explicit_pre_send_failure_non_retryable() -> None:
    def caller(**_: Any) -> Any:
        raise RuntimeError("No API key was found; request was not sent")

    with pytest.raises(Exception) as error:
        HermesStructuredCreativeModel(caller=caller).generate_json(
            system_prompt="system", user_prompt="prompt", schema_name="scene-prompt-bundle-v1",
            json_schema={"type":"object"}, temperature=0.2)

    assert error.value.request_sent is False
    assert error.value.retryable is False
    assert error.value.code == "NINE_ROUTER_CONFIGURATION_ERROR"
    from video_factory.handlers._base import _creative_failure
    failed = _creative_failure(error.value, None, "VF_SCENE_FAILED")
    assert failed.retryable is False
    assert failed.code == "NINE_ROUTER_CONFIGURATION_ERROR"


def test_vertex_compatible_brief_schema_declares_every_required_property() -> None:
    properties = CREATIVE_BRIEF_V2_SCHEMA["properties"]
    assert all(name in properties for name in CREATIVE_BRIEF_V2_SCHEMA["required"])

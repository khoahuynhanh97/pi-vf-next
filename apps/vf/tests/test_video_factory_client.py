import json
from video_factory.integrations.client import VideoFactoryClient
from hermes_personal.integrations.video_factory import contracts


def test_video_factory_client_methods():
    client = VideoFactoryClient()
    # shared artifact reference
    expected_artifact_ref = client._artifact.digest()
    for method_name, request_cls in [
        ("create_creative_brief", contracts.CreativeBriefRequest),
        ("create_scene_plan", contracts.ScenePlanRequest),
        ("create_storyboard", contracts.StoryboardRequest),
        ("render_video", contracts.RenderRequest),
        ("export_video", contracts.ExportRequest),
    ]:
        method = getattr(client, method_name)
        result = method()
        # keys exist
        assert set(result.keys()) == {"artifact_ref", "payload", "digest"}
        # artifact ref deterministic
        assert result["artifact_ref"] == expected_artifact_ref
        # payload matches request JSON
        request = request_cls()
        assert json.loads(result["payload"]) == json.loads(request.to_json())
        # digest matches request digest
        assert result["digest"] == request.digest()


def test_create_creative_brief_with_product_id():
    client = VideoFactoryClient()
    result = client.create_creative_brief(product_id="prod_123", brief_config={"style": "modern"})
    payload = json.loads(result["payload"])
    assert payload["product_id"] == "prod_123"
    assert payload["brief_config"] == {"style": "modern"}

import re
import json
from hermes_personal.integrations.video_factory import contracts as vf

def test_video_contracts_digest_format():
    classes = [
        vf.VideoArtifactRef,
        vf.CreativeBriefRequest,
        vf.ScenePlanRequest,
        vf.StoryboardRequest,
        vf.RenderRequest,
        vf.ExportRequest,
    ]
    pattern = re.compile(r"^sha256:[0-9a-f]{64}$")
    for cls in classes:
        instance = cls()
        digest = instance.digest()
        assert pattern.match(digest), f"{cls.__name__}.digest() malformed: {digest}"
        # version field exists and >0
        assert getattr(instance, "version", 0) > 0
        # owner matches
        assert getattr(instance, "owner", None) == "video_factory"

def test_video_contracts_json_serialization():
    obj = vf.VideoArtifactRef()
    json_str = obj.to_json()
    data = json.loads(json_str)
    assert data["owner"] == "video_factory"
    assert data["version"] == 1

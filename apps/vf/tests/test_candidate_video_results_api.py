from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

import av
import pytest
from fastapi.testclient import TestClient

from video_factory.application.app import create_application
from video_factory.storage_infra.sqlite import connect


def _write_video(path: Path) -> Path:
    with av.open(str(path), mode="w", format="mp4") as container:
        stream = container.add_stream("libx264", rate=24)
        stream.width, stream.height, stream.pix_fmt = 64, 96, "yuv420p"
        for index in range(3):
            frame = av.VideoFrame(64, 96, "yuv420p")
            for plane in frame.planes:
                plane.update(bytes([40 + index]) * plane.buffer_size)
            frame.pts, frame.time_base = index, Fraction(1, 24)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path


def _app_with_completed_export(
    db_path: Path, video_path: str, sha: str, *, creative_mode: str | None = None
) -> TestClient:
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO workflow (workflow_id, aggregate_json, version) VALUES (?, ?, 1)",
        ("wf-export", json.dumps({
            "state": "completed",
            "product_name": "Fixture Product",
            "source_context": {"canonical_product_key": "fixture:item"},
            "render_result": {
                "file_path": video_path,
                "sha256": sha,
                **({"creative_mode": creative_mode} if creative_mode else {}),
            },
            "export_result": {"payload": json.dumps({"file_path": video_path})},
        })),
    )
    conn.commit()
    return TestClient(create_application(kanban_connection=conn))


def test_videos_api_marks_decoded_export_with_matching_sha_success(tmp_path: Path) -> None:
    from video_factory.integrations.composer import probe_video

    video = _write_video(tmp_path / "valid.mp4")
    sha = probe_video(video, decode_all=True).sha256
    with _app_with_completed_export(tmp_path / "valid.sqlite", str(video), sha) as client:
        response = client.get("/api/personal/product-video/videos")
        media = client.get("/api/personal/product-video/videos/wf-export/media")

    result = response.json()["videos"][0]
    assert result["result_status"] == "completed"
    assert result["video_url"] == "/api/personal/product-video/videos/wf-export/media"
    assert media.status_code == 200
    assert media.content == video.read_bytes()
    assert result["sha256"] == sha
    assert result["frame_count"] > 0
    assert result["duration_seconds"] > 0


def test_four_agent_export_does_not_require_final_critic_receipt(tmp_path: Path, monkeypatch) -> None:
    from video_factory.creative.agents import video_review
    from video_factory.integrations.composer import probe_video

    def fail_if_called(*args, **kwargs):
        raise AssertionError("final content critic must not be required for export results")

    monkeypatch.setattr(video_review, "require_final_pass", fail_if_called)
    video = _write_video(tmp_path / "four-agent.mp4")
    sha = probe_video(video, decode_all=True).sha256
    with _app_with_completed_export(
        tmp_path / "four-agent.sqlite", str(video), sha, creative_mode="four_agent"
    ) as client:
        response = client.get("/api/personal/product-video/videos")
        media = client.get("/api/personal/product-video/videos/wf-export/media")

    result = response.json()["videos"][0]
    assert result["result_status"] == "completed"
    assert result["video_url"] == "/api/personal/product-video/videos/wf-export/media"
    assert media.status_code == 200
    assert media.content == video.read_bytes()


@pytest.mark.parametrize("bad_sha", ["wrong", ""])
def test_videos_api_never_marks_unverified_export_success(tmp_path: Path, bad_sha: str) -> None:
    video = _write_video(tmp_path / "video.mp4")
    with _app_with_completed_export(tmp_path / "bad-sha.sqlite", str(video), bad_sha) as client:
        response = client.get("/api/personal/product-video/videos")

    result = response.json()["videos"][0]
    assert result["result_status"] == "technical_error"
    assert result["video_url"] is None


def test_videos_api_marks_corrupt_export_as_technical_error(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.mp4"
    corrupt.write_bytes(b"not a decodable mp4")
    with _app_with_completed_export(tmp_path / "corrupt.sqlite", str(corrupt), "a" * 64) as client:
        response = client.get("/api/personal/product-video/videos")

    result = response.json()["videos"][0]
    assert result["result_status"] == "technical_error"
    assert result["video_url"] is None


def test_studio_operator_apis_use_video_factory_database_without_injected_connection(
    tmp_path: Path, monkeypatch
) -> None:
    import video_factory.application.candidate_api as candidate_api
    import video_factory.application.kanban_api as kanban_api
    import video_factory.constants as constants

    monkeypatch.setattr(candidate_api, "get_hermes_db_path", lambda: tmp_path / "hermes.db")
    monkeypatch.setattr(constants, "get_hermes_db_path", lambda: tmp_path / "hermes.db")

    with TestClient(create_application()) as client:
        board = client.get("/api/operator/kanban")
        history = client.get("/api/operator/product/fixture-product/workflows")

    assert board.status_code == 200
    assert history.status_code == 200
    assert history.json()["workflows"] == []

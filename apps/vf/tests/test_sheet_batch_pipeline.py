import json
import subprocess
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from automation.pipelines import spark_pipeline as pipeline
from automation.scheduler import daily_trigger
from video_factory.automation.importer import publish_daily_run_to_exchange
from video_factory.storage_infra.sqlite import connect

from test_creative_resume import config


def test_scheduler_sees_supplemental_ready_batch_with_same_primary_csv(tmp_path, monkeypatch):
    csv = tmp_path / "selection.csv"
    csv.write_text("candidate_id\nA\n")
    day = "2026-11-09"
    batches = [{"row_id": "row_0001", "candidate_id": "A", "batch_path": "primary"}]
    seen = []
    monkeypatch.setattr(daily_trigger, "LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(daily_trigger, "prepare_sheet_intake", lambda *a, **kw: {
        "selection_csv": str(csv), "exchange_batches": list(batches),
    })

    def run(**kwargs):
        seen.append(kwargs["sheet_intake"]["exchange_batches"])
        digest = daily_trigger.hashlib.sha256(csv.read_bytes()).hexdigest()
        return pipeline.PipelineRunResult("same-run", "success", day, str(csv), digest, 4, 20, "fake")

    monkeypatch.setattr(daily_trigger, "run_pipeline", run)
    assert daily_trigger.trigger_daily_run(date=day).status == "success"
    assert daily_trigger.trigger_daily_run(date=day)["status"] == "skipped"
    batches.append({"row_id": "row_0001", "candidate_id": "B", "batch_path": "supplement"})
    assert daily_trigger.trigger_daily_run(date=day).status == "success"
    assert [len(items) for items in seen] == [1, 2]


def test_sheet_handoff_pins_each_batch_and_continues_after_publish_error(tmp_path, monkeypatch):
    day = "2026-11-08"
    inbox = tmp_path / "inbox"
    batches = [inbox / day / name for name in ("first", "last")]
    for batch in batches:
        batch.mkdir(parents=True)
        (batch / "READY").touch()
        (batch / "manifest.json").write_text(json.dumps({"products": [{"product_key": batch.name}]}))
    profile = tmp_path / "profile.json"
    profile.write_text("{}")
    monkeypatch.setattr(pipeline, "INBOX_ROOT", inbox)
    monkeypatch.setattr(pipeline, "PI_RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)

    calls = []

    def vf(command, **kwargs):
        batch = Path(command[command.index("--exchange-batch") + 1])
        calls.append((batch, command[command.index("--run-id") + 1]))
        assert command[command.index("--product-key") + 1] == batch.name
        assert "--limit" not in command
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(pipeline.subprocess, "run", vf)
    monkeypatch.setattr(pipeline, "_inspect_run_workflows",
        lambda db, run, batch, **kw: ([{"file_path": str(batch / "final.mp4")}],
                                    [{"status": "success", "workflow_id": run}]))
    options = dict(date=day, profile_path=profile, db_path=tmp_path / "vf.db", limit=3,
                   sheet_intake={"status": "success", "exchange_batches": [
                       {"row_id": "row_0001", "batch_path": str(batches[0])},
                       {"row_id": "row_0002", "error": "publication failed"},
                       {"row_id": "row_0003", "batch_path": str(batches[1])},
                   ]})
    result = pipeline.run_pipeline(**options)
    assert result.status == "partial"
    assert [batch for batch, _ in calls] == batches
    assert len({run for _, run in calls}) == 2
    assert [outcome["status"] for outcome in result.product_outcomes] == ["success", "failed", "success"]
    previous_calls = list(calls)
    calls.clear()
    pipeline.run_pipeline(**options)
    assert calls == previous_calls
    calls.clear()
    limited = pipeline.run_pipeline(**{**options, "limit": 1})
    assert limited.status == "success"
    assert [batch for batch, _ in calls] == [batches[0]]

    # B and C got the two slots before A was ready. A must not displace C.
    late = inbox / day / "late"
    late.mkdir()
    (late / "READY").touch()
    (late / "manifest.json").write_text(json.dumps({"products": [{"product_key": "late"}]}))
    ready = [{"row_id": f"row_{i}", "batch_path": str(batch)} for i, batch in enumerate(batches, 1)]
    capped = {**options, "limit": 2, "sheet_intake": {"exchange_batches": ready}}
    calls.clear()
    pipeline.run_pipeline(**capped)
    first = list(calls)
    calls.clear()
    capped["sheet_intake"]["exchange_batches"] = [{"row_id": "row_0", "batch_path": str(late)}, *ready]
    pipeline.run_pipeline(**capped)
    assert calls == first

    calls.clear()
    expanded = {**options, "limit": 20, "sheet_intake": {"exchange_batches": [
        {**entry, "row_id": "row_0001", "candidate_id": f"candidate-{i}"}
        for i, entry in enumerate(ready, 1)
    ]}}
    assert pipeline.run_pipeline(**expanded).status == "success"
    assert [batch for batch, _ in calls] == batches

    # A different run ID cannot create a twenty-first video for the same day.
    connection = connect(options["db_path"])
    connection.executemany("INSERT INTO workflow (workflow_id, aggregate_json) VALUES (?, ?)", [
        (f"done-{i}", json.dumps({"state": "completed", "export_result": {"sha256": str(i)},
                                "source_context": {"source_date": day, "product_key": f"done-{i}"}}))
        for i in range(20)
    ])
    connection.commit()
    connection.close()
    calls.clear()
    full = pipeline.run_pipeline(**{**capped, "run_id": "new-run"})
    assert calls == []
    assert full.product_outcomes[0]["reason"] == "DAILY_VIDEO_CAP_REACHED"


def test_daily_trigger_exports_three_pinned_mp4s_and_replays_without_provider_work(tmp_path, monkeypatch):
    day = "2026-11-08"
    inbox = tmp_path / "inbox"
    workspace = tmp_path / "vf" / "workspace"
    database = tmp_path / "vf" / "hermes.db"
    workspace.mkdir(parents=True)
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(config()))
    csv = tmp_path / "selection.csv"
    csv.write_text("candidate_id\nfixture-1\nfixture-2\nfixture-3\n")
    batches = []
    for index in range(1, 4):
        source = tmp_path / f"pi-{index}"
        source.mkdir()
        images = []
        for image_index, color in enumerate(("red", "blue")):
            path = source / f"{image_index}.png"
            Image.new("RGB", (160, 220), color).save(path)
            images.append({"asset_id": f"image-{index}-{image_index}", "absolute_path": str(path)})
        (source / "products.json").write_text(json.dumps({"date": day, "products": [{
            "id": f"fixture-{index}", "name": f"Fixture Headphones {index}", "brand": "Fixture",
            "model": f"Headphones {index}", "variant": "standard", "images": images,
        }]}))
        batch = publish_daily_run_to_exchange(source, inbox, batch_id=f"run_{index}")
        batches.append({"row_id": f"row_{index:04}", "batch_path": str(batch)})

    monkeypatch.setattr(pipeline, "INBOX_ROOT", inbox)
    monkeypatch.setattr(pipeline, "PI_RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(pipeline, "VF_RUNTIME_DIR", database.parent)
    monkeypatch.setattr(pipeline, "VF_WORKSPACE_DIR", workspace)
    monkeypatch.setattr(pipeline, "VF_DB_PATH", database)
    monkeypatch.setattr(pipeline, "DEFAULT_PROFILE", profile)
    monkeypatch.setattr(pipeline, "ensure_runtime_dirs", lambda: None)
    monkeypatch.setattr(daily_trigger, "LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(daily_trigger, "prepare_sheet_intake", lambda *a, **kw: {
        "status": "success", "selection_csv": str(csv), "exchange_batches": batches,
    })
    options = dict(date=day, limit=3, scenes=3, provider_mode="fake")
    result = daily_trigger.trigger_daily_run(**options)
    assert result.status == "success", (result.product_outcomes, result.production_result)
    assert len({outcome["workflow_id"] for outcome in result.product_outcomes}) == 3
    assert len(result.exported_videos) == 3
    assert all(video["decodable"] and Path(video["file_path"]).is_relative_to(workspace)
               for video in result.exported_videos)
    assert all(Path(outcome["receipt_path"]).is_file() for outcome in result.product_outcomes)

    def media_jobs():
        conn = connect(database)
        try:
            return [tuple(row) for row in conn.execute(
                "SELECT event_id, payload_json FROM workflow_event WHERE event_type='JobSucceeded' "
                "AND json_extract(payload_json, '$.job_name') IN ('product_video.storyboard', 'product_video.render') "
                "ORDER BY event_id"
            )]
        finally:
            conn.close()

    jobs = media_jobs()
    assert len(jobs) == 6
    for _ in range(5):
        replay = daily_trigger.trigger_daily_run(**options, force=True)
        assert replay.status == "success", (replay.product_outcomes, replay.production_result)
        assert replay.exported_videos == result.exported_videos
        assert media_jobs() == jobs

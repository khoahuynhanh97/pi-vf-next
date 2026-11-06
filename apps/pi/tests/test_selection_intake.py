import csv
import hashlib
import json
import socket
import shutil
from pathlib import Path

import pytest

from media.research_persistence import SQLiteResearchRepository
from tools.selection_handoff import SELECTED_FIELDS
from tools.spaiauto.intake import start_intake
from tools.spaiauto.storage import atomic_json, digest, read_json


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*_args, **_kwargs): raise AssertionError("network is disabled in intake tests")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def _handoff(tmp_path, rows=2):
    from tools.selection_handoff import _atomic_csv
    batch = tmp_path / "selection"; batch.mkdir()
    source_snapshot = batch / "sources" / "s_snapshot.xlsx"; source_snapshot.parent.mkdir()
    source_snapshot.write_bytes(b"source workbook fixture")
    source_sha = hashlib.sha256(source_snapshot.read_bytes()).hexdigest()
    data = []
    for i in range(rows):
        data.append(dict(product_name=f"Đồ ăn {i}", source_url=f"https://shopee.vn/product/12/{100+i}", category="food",
            candidate_id=f"shopee:12:{100+i}", platform="shopee", brand_hint="", requested_variant="200g",
            price_hint_vnd="25000", selection_rank=i+1, selection_reason="Agent chose from pool.",
            video_concept="Close-up", script_draft=f"Show {i}; https://example.invalid/draft", claims_to_verify="taste",
            source_refs_json=json.dumps([{"sheet_id":"s","file":"sources/s_snapshot.xlsx","tab":"A","row":2,"snapshot_sha256":source_sha}],ensure_ascii=False),
            observed_at="2026-11-01T10:00:00+07:00"))
    path = batch / "selected_products.csv"; _atomic_csv(path, SELECTED_FIELDS, data)
    batch_id = f"sel_20261101_{digest(path)[:12]}"
    atomic_json(batch / "handoff.json", {"schema_version":"1.0.0", "kind":"pi_selection", "batch_id":batch_id,
        "batch_date":"20261101", "created_at":"2026-11-01T10:00:00+07:00", "selection_limit":15,
        "selected_count":rows, "csv_file":"selected_products.csv", "csv_sha256":digest(path),
        "sources":[{"sheet_id":"s","file":"sources/s_snapshot.xlsx","sha256":source_sha}]})
    (batch / "READY").write_text("READY\n", encoding="utf-8")
    return path, batch


def test_handoff_intake_retains_named_drafts_and_replays_ack_after_db_commit(tmp_path, monkeypatch):
    source, batch = _handoff(tmp_path)
    run = tmp_path / "run"; db = tmp_path / "snapshots.db"
    import tools.spaiauto.intake as intake
    monkeypatch.setattr(intake,"now",lambda:"2026-11-01T03:00:00+00:00")
    state = start_intake(source, run, handoff=batch / "handoff.json", db_path=db)
    assert state["created_at"] == "2026-11-01T03:00:00+00:00"
    assert state["rows"][0]["price_hint_vnd"] == "25000"
    assert state["rows"][0]["price_min"] is state["rows"][0]["price_max"] is None
    assert [row["candidate_id"] for row in state["rows"]] == ["shopee:12:100", "shopee:12:101"]
    assert all(row["links"] == [{"url": row["source_url"], "kind": "direct"}] for row in state["rows"])
    assert state["rows"][0]["creative_input"]["script_draft"].startswith("Show 0")
    repo = SQLiteResearchRepository(db)
    ids = [f"pi-intake:{state['batch_id']}:{r['candidate_id']}" for r in state["rows"]]
    snaps = [repo.get_latest_snapshot(x) for x in ids]
    assert all(s.status == "input_received" and s.result == {} and s.research_metadata["kind"] == "pi_csv_intake_v1" for s in snaps)
    repo.close()
    ack = read_json(batch / "import_ack.json")
    assert ack["input_row_count"] == ack["accepted_row_count"] == 2
    assert len(ack["rows"]) == 2
    before = ack
    (batch / "import_ack.json").unlink()
    replay = start_intake(source, run, handoff=batch / "handoff.json", db_path=db)
    assert len(replay["rows"]) == 2
    assert read_json(batch / "import_ack.json")["rows"] == before["rows"]


def test_handoff_rejects_bad_sha_without_creating_run_or_ack(tmp_path):
    source, batch = _handoff(tmp_path)
    source.write_bytes(source.read_bytes() + b" ")
    with pytest.raises(ValueError, match="SHA"):
        start_intake(source, tmp_path / "run", handoff=batch / "handoff.json", db_path=tmp_path / "db.sqlite")
    assert not (tmp_path / "run" / "intake.json").exists()
    assert not (batch / "import_ack.json").exists()


def test_database_commit_before_ack_crash_replays_without_duplicate_snapshot(tmp_path, monkeypatch):
    source, batch = _handoff(tmp_path, rows=1)
    run=tmp_path/"run"; db=tmp_path/"temporary.db"
    import tools.spaiauto.intake as intake
    original=intake.atomic_json
    def crash_ack(path, value):
        if Path(path).name=="import_ack.json": raise OSError("simulated crash before ACK write")
        return original(path,value)
    monkeypatch.setattr(intake,"atomic_json",crash_ack)
    with pytest.raises(OSError,match="simulated crash"):
        start_intake(source,run,handoff=batch/"handoff.json",db_path=db)
    assert not (batch/"import_ack.json").exists()
    monkeypatch.setattr(intake,"atomic_json",original)
    start_intake(source,run,handoff=batch/"handoff.json",db_path=db)
    repo=SQLiteResearchRepository(db)
    count=repo._db().execute("SELECT count(*) FROM research_snapshots WHERE research_id LIKE ?",(f"pi-intake:{read_json(run/'intake.json')['batch_id']}:%",)).fetchone()[0]
    repo.close()
    assert count==1
    assert len(read_json(batch/"import_ack.json")["rows"])==1


def test_replay_cannot_rebind_run_to_another_handoff_directory_or_database(tmp_path):
    source,batch=_handoff(tmp_path,rows=1)
    alternate=tmp_path/"copied-selection"; shutil.copytree(batch,alternate)
    run=tmp_path/"run"; db=tmp_path/"snapshots.db"
    start_intake(source,run,handoff=batch/"handoff.json",db_path=db)
    with pytest.raises(ValueError,match="bound to a selection handoff"):
        start_intake(source,run)
    alternate_source=alternate/"selected_products.csv"
    with pytest.raises(ValueError,match="different handoff"):
        start_intake(alternate_source,run,handoff=alternate/"handoff.json",db_path=db)
    other_db=tmp_path/"other.db"
    with pytest.raises(ValueError,match="different SQLite database"):
        start_intake(source,run,handoff=batch/"handoff.json",db_path=other_db)
    assert not other_db.exists()
    assert not (alternate/"import_ack.json").exists()

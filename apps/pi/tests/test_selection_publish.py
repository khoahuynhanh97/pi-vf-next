import io
import csv
import json
import hashlib
import os
import socket
import subprocess
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from tools.spaiauto.storage import SPAIAUTO_URL, atomic_json, digest, item_key, read_json
from tools.spaiauto.workflow import attach, begin_attempt, publish, review_item
from tools.spaiauto.intake import start_intake
from tools.selection_handoff import SELECTED_FIELDS


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*_args, **_kwargs): raise AssertionError("network is disabled in publish tests")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def _reviewed_child(parent, url, name, variant, tmp_path):
    from tools.spaiauto.workflow import initialize
    child = parent
    state = initialize(child, [url]); key = item_key(url)
    for stage in ("scan", "download"): begin_attempt(child, key, stage)
    image = io.BytesIO(); Image.new("RGB", (100, 100), "red").save(image, "PNG")
    capture=tmp_path/f"{name}-capture.json"; atomic_json(capture, {"schema_version":1,"acquisition_tool":"spaiauto",
        "extractor_url":SPAIAUTO_URL,"source_url":url,"captured_at":"2026-11-01T10:00:00+07:00","title":name,"description":"Seller copy"})
    screenshot=tmp_path/f"{name}-page.png"; screenshot.write_bytes(image.getvalue())
    archive=tmp_path/f"{name}.zip"
    with zipfile.ZipFile(archive,"w") as z: z.writestr("item.png",image.getvalue())
    imported=attach(child,key,capture,archive,screenshot); imp=read_json(imported/"import.json")
    review={"capture_digest":digest(imported/"raw"/"capture.json"),"reviewed_by":"reviewer","note":"Correct item shown.",
        "identity_verified":True,"identity":{"brand":"Acme","model":name,"variant":variant,"category":"food"},
        "selected_asset_ids":[imp["assets"][0]["asset_id"]]}
    rp=tmp_path/f"{name}-review.json"; atomic_json(rp,review); review_item(child,key,rp)
    return child


def test_root_publish_requires_explicit_partial_and_writes_one_run_batch_with_all_outcomes(tmp_path, monkeypatch):
    selection=tmp_path/"sel_20261101_fixture"; selection.mkdir()
    source_file=selection/"sources"/"sheet_snapshot.xlsx"; source_file.parent.mkdir(); source_file.write_bytes(b"source snapshot")
    source_sha=hashlib.sha256(source_file.read_bytes()).hexdigest()
    csv_path=selection/"selected_products.csv"
    refs='[{"sheet_id":"sheet","file":"sources/sheet_snapshot.xlsx","tab":"Products","row":2,"snapshot_sha256":"' + source_sha + '"}]'
    selected_rows=[]
    for i in range(2): selected_rows.append({"product_name":"Pickle" if i==0 else "Other", "source_url":f"https://shopee.vn/product/12/{100+i}",
        "category":"food","candidate_id":f"shopee:12:{100+i}","platform":"shopee","brand_hint":"","requested_variant":"200g",
        "price_hint_vnd":"","selection_rank":i+1,"selection_reason":"Agent selected.","video_concept":"close-up",
        "script_draft":"Show jar","claims_to_verify":"sweet","source_refs_json":refs,"observed_at":"2026-11-01T10:00:00+07:00"})
    with csv_path.open("w",encoding="utf-8-sig",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=SELECTED_FIELDS); writer.writeheader(); writer.writerows(selected_rows)
    batch_id=f"sel_20261101_{digest(csv_path)[:12]}"
    atomic_json(selection/"handoff.json",{"schema_version":"1.0.0","kind":"pi_selection","batch_id":batch_id,
        "batch_date":"20261101","created_at":"2026-11-01T10:00:00+07:00","selection_limit":15,"selected_count":2,
        "csv_file":"selected_products.csv","csv_sha256":digest(csv_path),"sources":[{"sheet_id":"sheet","file":"sources/sheet_snapshot.xlsx","sha256":source_sha}]})
    (selection/"READY").write_text("READY\n",encoding="utf-8")
    root=tmp_path/"root"; root.mkdir()
    child=_reviewed_child(root/"rows"/"row_0001"/"acquisition","https://shopee.vn/product/12/100","Pickle", "200g",tmp_path)
    root.mkdir(exist_ok=True)
    state={"schema_version":1,"mode":"pi_selection","batch_id":batch_id,"selection_csv_sha256":digest(csv_path),
        "input_sha256":digest(csv_path),"input_name":csv_path.name,"input_file":"input/selected.csv","selection_handoff":str((selection/"handoff.json").resolve()),"selection_handoff_sha256":digest(selection/"handoff.json"),
        "date":"2026-11-01","rows":[
            {"row_id":"row_0001","candidate_id":"shopee:12:100","source_row":2,"name":"Pickle","source_url":"https://shopee.vn/product/12/100","status":"completed","acquisition_run":str(child),"acquisition_item_id":item_key("https://shopee.vn/product/12/100"),"input_metadata":{"selection_rank":"1","category":"food","brand_hint":"","requested_variant":"200g","price_hint_vnd":"","selection_reason":"Agent selected.","video_concept":"close-up","script_draft":"Show jar","claims_to_verify":"sweet","source_refs_json":refs,"observed_at":"2026-11-01T10:00:00+07:00"}},
            {"row_id":"row_0002","candidate_id":"shopee:12:101","source_row":3,"name":"Other","source_url":"https://shopee.vn/product/12/101","status":"needs_review","reason":"No verified variant","input_metadata":{"selection_rank":"2","category":"food","brand_hint":"","requested_variant":"200g","price_hint_vnd":"","selection_reason":"Agent selected.","video_concept":"close-up","script_draft":"Show jar","claims_to_verify":"sweet","source_refs_json":refs,"observed_at":"2026-11-01T10:00:00+07:00"}}],"events":[]}
    (root/"input").mkdir(); (root/"input"/"selected.csv").write_bytes(csv_path.read_bytes())
    state["input_sha256"]=digest(root/"input"/"selected.csv")
    state["selection_csv_sha256"]=state["input_sha256"]
    atomic_json(root/"intake.json",state)
    inbox=tmp_path/"exchange"
    import tools.spaiauto.workflow as workflow
    monkeypatch.setattr(workflow,"now",lambda:"2026-11-01T03:00:00+00:00")
    with pytest.raises(ValueError,match="eligible-only"):
        publish(root,inbox)
    original_rename=Path.rename
    def crash_before_rename(path,target,*args,**kwargs):
        if path.name.startswith(".tmp_run_"):
            raise OSError("simulated crash before batch rename")
        return original_rename(path,target,*args,**kwargs)
    monkeypatch.setattr(Path,"rename",crash_before_rename)
    with pytest.raises(OSError,match="before batch rename"):
        publish(root,inbox,eligible_only=True)
    monkeypatch.setattr(Path,"rename",original_rename)
    original_copy=workflow.shutil.copy2; observed_existing_stage_file=False
    def observe_reused_file(source,destination,*args,**kwargs):
        nonlocal observed_existing_stage_file
        if Path(destination).is_file() and Path(destination).parent.name=="images": observed_existing_stage_file=True
        return original_copy(source,destination,*args,**kwargs)
    monkeypatch.setattr(workflow.shutil,"copy2",observe_reused_file)
    path=publish(root,inbox,eligible_only=True)
    monkeypatch.setattr(workflow.shutil,"copy2",original_copy)
    assert observed_existing_stage_file
    path.joinpath("READY").unlink()  # state left by a crash after rename, before READY
    original_write_text=Path.write_text
    def crash_ready(path,*args,**kwargs):
        if path.name=="READY" and path.parent.name.startswith("run_"):
            raise OSError("simulated crash after batch rename")
        return original_write_text(path,*args,**kwargs)
    monkeypatch.setattr(Path,"write_text",crash_ready)
    with pytest.raises(OSError,match="after batch rename"):
        publish(root,inbox,eligible_only=True)
    monkeypatch.setattr(Path,"write_text",original_write_text)
    path=publish(root,inbox,eligible_only=True)
    assert path.name.startswith("run_")
    manifest=read_json(path/"manifest.json")
    assert manifest["created_at"]=="2026-11-01T03:00:00+00:00"
    assert len(manifest["products"])==1
    assert manifest["products"][0]["variant"]=="200g"
    assert manifest["products"][0]["creative_input"]["script_draft"] == "Show jar"
    assert manifest["products"][0]["seller_description"] == "Seller copy"
    assert manifest["products"][0]["claims_verified"] is False
    assert not (path/"READY").read_text(encoding="utf-8").strip()==""
    outcomes=read_json(path/"outcomes.json")
    assert len(outcomes["rows"])==2
    assert outcomes["counts"]=={"eligible":1,"needs_review":1}
    assert publish(root,inbox,eligible_only=True)==path
    vf_python=Path(r"D:\work\video-factory\.venv\Scripts\python.exe")
    if vf_python.is_file():
        env=os.environ.copy(); env["PYTHONPATH"]=str(Path(r"D:\work\video-factory\src"))
        code=("import json; from datetime import date; from pathlib import Path; "
              "from video_factory.automation.importer import ExchangeImporter; "
              f"i=ExchangeImporter(Path({str(inbox)!r})); p=i.find_latest_ready_batch(date(2026,11,1)); "
              "m=i.load_manifest(p); print(json.dumps({'path':str(p),'count':len(m.products),"
              "'creative_supported':hasattr(m.products[0],'creative_input')}))")
        found=json.loads(subprocess.check_output([str(vf_python),"-c",code],env=env,text=True))
        assert Path(found["path"])==path
        assert found["count"]==1
        assert found["creative_supported"] is True


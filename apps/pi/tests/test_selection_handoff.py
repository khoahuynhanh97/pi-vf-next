import csv
import json
import hashlib
import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest
from openpyxl import Workbook

from tools.selection_handoff import SHEETS, POOL_FIELDS, normalize_sources, publish_selection, apply_import_ack, main as selection_main

HEADERS = ["Tên sản phẩm", "Link sản phẩm", "Sàn", "Tên shop", "Danh mục", "Thương hiệu",
           "Variant / Quy cách", "Giá hiện tại (VNĐ)", "Giá gốc (VNĐ)", "Lượt đã bán",
           "Điểm đánh giá", "Số đánh giá", "Loại shop", "Hoa hồng (%)",
           "Tín hiệu / Lý do đề xuất", "Gợi ý POV / B-roll", "Rủi ro / Ghi chú", "Thời gian thu thập"]


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*_args, **_kwargs): raise AssertionError("network is disabled in selection tests")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def test_normalize_merges_listing_sources_preserves_long_ids_and_variant_conflicts(tmp_path):
    path = tmp_path / "source.xlsx"
    book = Workbook()
    first = book.active
    first.title = "one"
    first.append(HEADERS)
    first.append(["Đồ ăn", "Shopee", "shopee", "Shop A", "food", "", "200g", "25.000 ₫",
                  "", "1.234", "4,9", "3", "Mall", "", "Lý do", "POV", "", "2026-11-01T10:00:00+07:00"])
    first["B2"].hyperlink = "https://shopee.vn/product/123/456"
    second = book.create_sheet("two")
    second.append(HEADERS)
    second.append(["Đồ ăn", "link", "shopee", "Shop A", "food", "", "500g", 25000,
                   "", "1", "4.9", "3", "Mall", "", "Lý do", "POV", "", "2026-11-01T10:00:00+07:00"])
    second["B2"].hyperlink = "https://shopee.vn/product/123/456"
    book.save(path)
    book.close()
    second_path = tmp_path / "source-two.xlsx"
    book = Workbook(); sheet = book.active; sheet.append(HEADERS)
    sheet.append(["Đồ ăn", "link", "shopee", "Shop A", "food", "", "200g", "", "", "", "", "", "", "", "", "", "", ""])
    sheet["B2"] = '=HYPERLINK("https://shopee.vn/product/123/456","Shopee")'
    book.save(second_path); book.close()
    pool = normalize_sources([(SHEETS[0], path), (SHEETS[1], second_path)])
    assert len(pool) == 1
    assert pool[0]["candidate_id"] == "shopee:123:456"
    assert pool[0]["price_hint_vnd"] == "25000"
    assert pool[0]["sold_hint"] == "1234"
    assert pool[0]["rating_hint"] == "4.9"
    assert len(json.loads(pool[0]["source_refs_json"])) == 3
    assert pool[0]["state"] == "NEEDS_REVIEW"


def test_normalize_cli_pins_both_xlsx_snapshots_in_selected_handoff(tmp_path, capsys):
    source_paths=[]
    for index,sheet_id in enumerate(SHEETS,1):
        path=tmp_path/f"sheet-{index}.xlsx"; book=Workbook(); sheet=book.active; sheet.append(HEADERS)
        sheet.append([f"Product {index}","Link","shopee","Shop","food","","200g",25000,"",1,4.9,5,"Mall",0,"reason","concept","","2026-11-01T10:00:00+07:00"])
        sheet["B2"].hyperlink=f"https://shopee.vn/product/123/{450+index}"
        book.save(path); book.close(); source_paths.append((sheet_id,path))
    pool=tmp_path/"candidate_pool.csv"
    args=["normalize","--pool",str(pool)]
    for sheet_id,path in source_paths: args.extend(["--source",f"{sheet_id}={path}"])
    assert selection_main(args)==0
    capsys.readouterr()
    with pool.open(encoding="utf-8-sig",newline="") as f: candidates=list(csv.DictReader(f))
    result=publish_selection(pool,tmp_path/"selected_products.csv",tmp_path/"batches",batch_date="20261101",
        candidate_ids=[candidates[0]["candidate_id"]],drafts={candidates[0]["candidate_id"]:{"selection_reason":"Agent reviewed source row."}})
    batch=Path(result["batch_dir"]); handoff=json.loads((batch/"handoff.json").read_text(encoding="utf-8"))
    assert len(handoff["sources"])==2
    assert all((batch/source["file"]).is_file() for source in handoff["sources"])


def test_selection_snapshot_keeps_unselected_open_and_ack_hands_off_exact_rows(tmp_path, monkeypatch):
    pool_path = tmp_path / "candidate_pool.csv"
    candidates = []
    source_snapshot = tmp_path / "source.xlsx"
    source_snapshot.write_bytes(b"local source snapshot")
    source_sha = hashlib.sha256(source_snapshot.read_bytes()).hexdigest()
    for n in range(25):
        candidates.append({"candidate_id": f"tiktok:{9007199254740993000+n}", "product_name": f"Món {n}",
            "source_url": f"https://shop-vn.tiktok.com/pdp/{9007199254740993000+n}", "platform": "tiktok",
            "category": "food", "requested_variant": "", "source_refs_json": json.dumps([{"sheet_id":"s","file":"sources/s_snapshot.xlsx","source_path":str(source_snapshot.resolve()),"tab":"T","row":2,"snapshot_sha256":source_sha}]),
            "observed_at": "2026-11-01T10:00:00+07:00", "state": "OPEN"})
    _write_csv(pool_path, candidates)
    selected = tmp_path / "selected_products.csv"
    batch_root = tmp_path / "batches"
    drafts = {r["candidate_id"]: {"selection_reason": "Explicit agent choice."} for r in candidates[:15]}
    drafts[candidates[0]["candidate_id"]].update(script_draft='POV, "open box"\nshow item', claims_to_verify='\nclaim, pending')
    import tools.selection_handoff as handoff_module
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None): return cls(2026,11,1,3,0,0,tzinfo=timezone.utc)
    monkeypatch.setattr(handoff_module,"datetime",FixedDateTime)
    result = publish_selection(pool_path, selected, batch_root, batch_date="20261101", candidate_ids=[r["candidate_id"] for r in candidates[:15]], drafts=drafts)
    with pool_path.open(encoding="utf-8-sig", newline="") as f:
        after = list(csv.DictReader(f))
    assert sum(r["state"] == "OPEN" for r in after) == 10
    assert sum(r["state"] == "SELECTED" for r in after) == 15
    assert result["selected_count"] == 15
    assert result["created_at"] == "2026-11-01T03:00:00+00:00"
    with (Path(result["batch_dir"]) / "selected_products.csv").open(encoding="utf-8-sig", newline="") as f:
        selected_snapshot = list(csv.DictReader(f))
    assert selected_snapshot[0]["script_draft"] == 'POV, "open box"\nshow item'
    assert selected_snapshot[0]["claims_to_verify"] == '\nclaim, pending'
    ack = {"batch_id": result["batch_id"], "csv_sha256": result["csv_sha256"], "status": "accepted",
           "rows": [{"candidate_id": r["candidate_id"], "row_id": f"row_{n:04d}",
                     "snapshot_id":"piintake_"+hashlib.sha256(f"pi-intake:{result['batch_id']}:{r['candidate_id']}".encode()).hexdigest()}
                    for n, r in enumerate(result["selected"], 1)]}
    batch = Path(result["batch_dir"])
    broken=json.loads(json.dumps(ack)); broken["rows"][0]["snapshot_id"]="wrong_snapshot"
    (batch / "import_ack.json").write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(ValueError,match="deterministic PI intake"):
        apply_import_ack(pool_path,batch)
    (batch / "import_ack.json").write_text(json.dumps(ack), encoding="utf-8")
    apply_import_ack(pool_path, batch)
    with pool_path.open(encoding="utf-8-sig", newline="") as f:
        after_ack = list(csv.DictReader(f))
    assert sum(r["state"] == "HANDED_OFF" for r in after_ack) == 15


def test_agent_resolves_search_link_and_later_sync_keeps_handed_off(tmp_path):
    from tools.selection_handoff import resolve_candidate, write_pool
    source_a=tmp_path/"a.xlsx"; source_b=tmp_path/"b.xlsx"
    def make(path, observed):
        book=Workbook(); sheet=book.active; sheet.append(HEADERS)
        sheet.append(["Snack", "Search", "shopee", "Shop", "food", "", "200g", "", "", "", "", "", "", "", "Reason", "", "", observed])
        sheet["B2"].hyperlink="https://shopee.vn/search?keyword=snack"
        book.save(path); book.close()
    make(source_a,"2026-11-01T10:00:00+07:00")
    book=Workbook(); book.active.append(HEADERS); book.save(source_b); book.close()
    pool=tmp_path/"candidate_pool.csv"
    rows=normalize_sources([(SHEETS[0],source_a),(SHEETS[1],source_b)])
    assert rows[0]["state"]=="NEEDS_REVIEW"
    write_pool(pool,rows); unresolved=rows[0]["candidate_id"]
    resolved=resolve_candidate(pool,unresolved,"https://shopee.vn/product/123/456",reason="Inspected title and 200g option on PDP.")
    assert resolved["candidate_id"]=="shopee:123:456"
    current=__import__("tools.selection_handoff",fromlist=["_read_pool"])._read_pool(pool)
    current[0]["state"]="HANDED_OFF"; current[0]["selected_batch_id"]="sel_prior"
    write_pool(pool,current)
    make(source_a,"2026-11-02T10:00:00+07:00")
    synced=normalize_sources([(SHEETS[0],source_a),(SHEETS[1],source_b)],pool)
    assert len(synced)==1
    assert synced[0]["candidate_id"]=="shopee:123:456"
    assert synced[0]["state"]=="HANDED_OFF"
    assert "https://shopee.vn/search?keyword=snack" in synced[0]["source_refs_json"]


def test_new_variant_conflict_requires_review_even_after_prior_handoff(tmp_path):
    from tools.selection_handoff import write_pool
    first = tmp_path / "a.xlsx"
    second = tmp_path / "b.xlsx"
    for path, variant in ((first, "200g"), (second, "300g")):
        book = Workbook()
        sheet = book.active
        sheet.append(HEADERS)
        sheet.append(["Snack", "https://shopee.vn/product/123/456", "shopee", "Shop", "food", "", variant,
                      "", "", "", "", "", "", "", "Reason", "", "", "2026-11-02"])
        book.save(path)
        book.close()
    pool = tmp_path / "candidate_pool.csv"
    prior = normalize_sources([(SHEETS[0], first), (SHEETS[1], first)])
    prior[0]["state"] = "HANDED_OFF"
    prior[0]["selected_batch_id"] = "sel_prior"
    write_pool(pool, prior)
    current = normalize_sources([(SHEETS[0], first), (SHEETS[1], second)], pool)
    assert current[0]["state"] == "NEEDS_REVIEW"
    assert current[0]["selected_batch_id"] == "sel_prior"


def test_normalize_accepts_current_public_sheet_header_shapes(tmp_path):
    first, second = tmp_path / "first.xlsx", tmp_path / "second.xlsx"
    a = Workbook()
    a.active.title = "Trang tính1"
    a.active.append(["Tên sản phẩm", "Danh mục", "Giá bán tham khảo", "Link sản phẩm Shopee",
                     "Số lượt bán / Tín hiệu xu hướng", "Mức độ cạnh tranh",
                     "Biên lợi nhuận / Hoa hồng ước tính", "Lý do hot & Gợi ý B-roll 9:16",
                     "Đánh giá rủi ro khi bán", "Điểm tiềm năng (1-10)", "Ngày giờ thu thập dữ liệu"])
    a.active.append(["Snack", "food", "100000", "https://shopee.vn/product/123/456",
                     "100 sold", "low", "10%", "Show pack", "Fragile", "9", "2026-11-02"])
    a.save(first); a.close()
    b = Workbook()
    b.active.append(["STT", "Tên sản phẩm", "Nhóm quà Tết", "Giá niêm yết (VNĐ)",
                     "Sàn (Shopee/TikTok)", "Tên Shop", "Shop Mall/Yêu Thích", "Điểm & Số đánh giá",
                     "Lượt đã bán (nếu có)", "Link sản phẩm", "Góc quay POV gợi ý (tối đa 12 từ)"])
    b.active.append([1, "Snack", "food", "100000", "Shopee", "Shop", "Mall", "4.9", "100",
                     "https://shopee.vn/product/123/456", "Show pack"])
    b.save(second); b.close()
    rows = normalize_sources([(SHEETS[0], first), (SHEETS[1], second)])
    assert len(rows) == 1
    assert rows[0]["candidate_id"] == "shopee:123:456"
    assert len(json.loads(rows[0]["source_refs_json"])) == 2


def test_ready_selection_replay_recovers_pool_projection_without_duplicate_batch(tmp_path, monkeypatch):
    from tools.selection_handoff import write_pool
    source_snapshot=tmp_path/"source.xlsx"; source_snapshot.write_bytes(b"snapshot")
    sha=hashlib.sha256(source_snapshot.read_bytes()).hexdigest()
    candidate={"candidate_id":"shopee:123:456","product_name":"Snack","source_url":"https://shopee.vn/product/123/456",
        "platform":"shopee","category":"food","brand_hint":"","requested_variant":"200g","price_hint_vnd":"",
        "source_refs_json":json.dumps([{"sheet_id":"s","file":"sources/s_source.xlsx","source_path":str(source_snapshot.resolve()),
            "tab":"T","row":2,"snapshot_sha256":sha,"source_locator_url":"https://shopee.vn/product/123/456"}]),
        "observed_at":"2026-11-01T10:00:00+07:00","state":"OPEN"}
    second={**candidate,"candidate_id":"shopee:123:457","source_url":"https://shopee.vn/product/123/457","product_name":"Second"}
    pool=tmp_path/"candidate_pool.csv"; _write_csv(pool,[candidate,second]); root=tmp_path/"batches"
    selection=tmp_path/"selected_products.csv"; drafts={candidate["candidate_id"]:{"selection_reason":"Inspected source rationale."}}
    import tools.selection_handoff as module
    def crash_projection(*_args,**_kwargs): raise OSError("simulated crash after READY")
    monkeypatch.setattr(module,"write_pool",crash_projection)
    with pytest.raises(OSError,match="after READY"):
        publish_selection(pool,selection,root,batch_date="20261101",candidate_ids=[candidate["candidate_id"]],drafts=drafts)
    ready_dir=next(root.glob("sel_*")); assert (ready_dir/"READY").is_file()
    monkeypatch.setattr(module,"write_pool",write_pool)
    with pytest.raises(ValueError,match="selection_reason"):
        publish_selection(pool,selection,root,batch_date="20261101",candidate_ids=[second["candidate_id"]],drafts={})
    assert _read_csv_state(pool,candidate["candidate_id"])=="OPEN"
    replay=publish_selection(pool,selection,root,batch_date="20261101",candidate_ids=[candidate["candidate_id"]],drafts=drafts)
    assert replay["batch_dir"]==str(ready_dir)
    assert len(list(root.glob("sel_*")))==1
    assert _read_csv_state(pool,candidate["candidate_id"])=="SELECTED"


def _write_csv(path, rows):
    names = POOL_FIELDS
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=names)
        writer.writeheader()
        writer.writerows([{name: row.get(name, "") for name in names} for row in rows])

def _read_csv_state(path, candidate_id):
    with path.open(encoding="utf-8-sig",newline="") as stream:
        return next(row["state"] for row in csv.DictReader(stream) if row["candidate_id"]==candidate_id)

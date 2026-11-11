"""Offline selection pool and immutable handoff files for PI.

The module never fetches Sheets or selects products. An agent inspects the pool
and submits explicit candidate IDs plus editorial fields.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import BadZipFile

from tools.spaiauto.intake_parser import _fold, _xlsx_rows
from tools.spaiauto.storage import atomic_json, digest, read_json
from tools.spaiauto.links import link_kind, normalize_market_url

SHEETS = ("10w1QAOuq8zvjnK4Rml8qgVIoIMtz_1iDlZohoMQDnWQ", "1QdGMCdADxYXCAY02aLSILX9FX_XF_cTLULvOKCvzOho")
PREVIOUS_SHEET_1 = "1gZud8PjalrF493toqlniKOeh8XPCw73B7ZpQ7FLHT-Y"
MAX_SHEET_SELECTION = 20
POOL_FIELDS = "candidate_id product_name source_url platform shop_name category brand_hint requested_variant price_hint_vnd original_price_hint_vnd sold_hint rating_hint rating_count_hint shop_type_hint commission_percent_hint trend_reason video_concept risk_notes observed_at source_refs_json state selected_batch_id selection_reason".split()
SELECTED_FIELDS = "product_name source_url category candidate_id platform brand_hint requested_variant price_hint_vnd selection_rank selection_reason video_concept script_draft claims_to_verify source_refs_json observed_at".split()
SOURCE_MAP = {
    "product_name": ("tên sản phẩm", "tên sản phẩm đề xuất", "product_name", "product name", "name", "title"),
    "source_url": ("link sản phẩm", "link sản phẩm (nếu có)", "link sản phẩm shopee", "link shopee", "link tiktok", "source_url", "source url", "url", "link"),
    "platform": ("sàn", "sàn (shopee/tiktok)", "sàn tmđt", "nền tảng", "marketplace", "platform"),
    "shop_name": ("tên shop", "shop", "seller", "shop_name"),
    "category": ("danh mục", "nhóm quà tết", "ngách", "ngành hàng", "category"),
    "brand_hint": ("thương hiệu", "brand_hint", "brand"),
    "requested_variant": ("variant / quy cách", "variant/quy cách", "quy cách", "variant", "requested_variant"),
    "price_hint_vnd": ("giá hiện tại (vnđ)", "giá bán (vnđ)", "giá bán tham khảo", "giá niêm yết (vnđ)", "giá hiện tại", "giá bán hiện tại", "current price", "price_hint_vnd", "giá"),
    "original_price_hint_vnd": ("giá gốc (vnđ)", "giá gốc", "original price", "original_price_hint_vnd"),
    "sold_hint": ("lượt đã bán", "lượt đã bán (nếu có)", "đã bán", "sold", "sold_hint"),
    "rating_hint": ("điểm đánh giá", "đánh giá", "rating", "rating_hint"),
    "rating_count_hint": ("số đánh giá", "số lượng đánh giá", "lượt đánh giá", "review count", "rating_count_hint"),
    "shop_type_hint": ("loại shop", "shop mall/yêu thích", "loại gian hàng", "shop type", "shop_type_hint"),
    "commission_percent_hint": ("hoa hồng (%)", "hoa hồng", "commission", "commission_percent_hint"),
    "trend_reason": ("tín hiệu / lý do đề xuất", "số lượt bán / tín hiệu xu hướng", "lý do hot & gợi ý b-roll 9:16", "điểm mạnh (usp & visual hook)", "tín hiệu", "lý do đề xuất", "trend_reason"),
    "video_concept": ("gợi ý pov / b-roll", "góc quay pov gợi ý (tối đa 12 từ)", "góc tiếp cận (content angle)", "gợi ý pov", "pov / b-roll", "video_concept"),
    "risk_notes": ("rủi ro / ghi chú", "đánh giá rủi ro khi bán", "rủi ro / lưu ý", "rủi ro", "risk notes", "risk_notes"),
    "observed_at": ("thời gian thu thập", "ngày giờ thu thập dữ liệu", "ngày quét", "thời điểm thu thập", "observed_at", "collected at"),
}

def _atomic_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".selection-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fields, extrasaction="ignore", lineterminator="\r\n")
            writer.writeheader(); writer.writerows(rows)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)

def _csv_bytes(fields: list[str], rows: list[dict]) -> bytes:
    stream=io.StringIO(newline="")
    writer=csv.DictWriter(stream,fields,extrasaction="ignore",lineterminator="\r\n")
    writer.writeheader(); writer.writerows(rows)
    return b"\xef\xbb\xbf" + stream.getvalue().encode("utf-8")

def _listing(url: str) -> tuple[str, str, str] | None:
    try:
        normalized = normalize_market_url(url)
    except ValueError:
        return None
    p = urlsplit(normalized); host = p.hostname or ""; path = p.path.rstrip("/")
    m = re.search(r"/product/(\d+)/(\d+)$", path) or re.search(r"-i\.(\d+)\.(\d+)$", path)
    if host.endswith("shopee.vn") and m:
        return f"shopee:{m.group(1)}:{m.group(2)}", "shopee", normalized
    if host.endswith("tiktok.com") and link_kind(normalized) == "direct":
        m = re.search(r"/(?:pdp|product)/(?:[^/]+/)*(\d+)$", path)
        if m: return f"tiktok:{m.group(1)}", "tiktok", normalized
    return None

def _number_hint(value: str, *, integer: bool) -> str | None:
    text=value.strip().replace("\u00a0","").replace(" ","")
    text=re.sub(r"(?i)(?:₫|vnd|vnđ|đ)","",text)
    if not text: return ""
    if integer:
        if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+",text): return re.sub(r"[.,]","",text)
        if re.fullmatch(r"\d+\.0+",text): return text.split(".",1)[0]
        if re.fullmatch(r"\d+",text): return text
        return None
    text=text.rstrip("%")
    if re.fullmatch(r"\d+",text): return text
    if re.fullmatch(r"\d+[,.]\d+",text):
        separator="," if "," in text else "."
        return text.replace(separator,".")
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+",text): return re.sub(r"[.,]","",text)
    return None

def _rows(path: Path):
    if path.suffix.lower() == ".xlsx":
        # Existing workbook reader retains literal HYPERLINK targets and never evaluates formulas.
        try: yield from _xlsx_rows(path)
        except BadZipFile as exc: raise ValueError(f"Invalid XLSX snapshot: {path}") from exc
    elif path.suffix.lower() == ".csv":
        try:
            with path.open(encoding="utf-8-sig", newline="") as stream:
                for i, cells in enumerate(csv.reader(stream, strict=True), 1): yield i, cells, [], None, [[] for _ in cells]
        except csv.Error as exc: raise ValueError(f"Invalid CSV snapshot: {path}") from exc
    else:
        raise ValueError("Source must be an XLSX snapshot or CSV export.")

@contextmanager
def _pool_lock(pool: Path):
    lock = pool.with_name(pool.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise ValueError(f"Selection pool is locked ({lock}); inspect owner and remove only when confirmed stale.") from exc
    try:
        os.write(fd, f"pid={os.getpid()}\n".encode()); os.close(fd)
        yield
    finally:
        lock.unlink(missing_ok=True)

def normalize_sources(sources: list[tuple[str, Path]], previous_pool: Path | None = None,
                      *, expected_sheet_ids: tuple[str, ...] = SHEETS) -> list[dict]:
    if len(sources) != 2 or {sid for sid, _ in sources} != set(expected_sheet_ids):
        raise ValueError("Both configured Sheet IDs and snapshots are required; partial sync is not implicit.")
    previous_rows = _read_pool(previous_pool) if previous_pool and previous_pool.exists() else []
    candidates: dict[str, dict] = {}
    for sheet_id, path in sources:
        if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024: raise ValueError(f"Missing/oversized source: {path}")
        sha = digest(path); header_map = None; tab = path.stem; current_sheet = None
        for raw in _rows(path):
            number, cells, links, sheet_name, *extra = raw
            if sheet_name is not None and sheet_name != current_sheet:
                tab = sheet_name; current_sheet = sheet_name; header_map = None
            cells = [str(v or "").strip() for v in cells]
            if not any(cells): continue
            norm = [_fold(c) for c in cells]
            recognized = {key: next((i for i, h in enumerate(norm) if h in {_fold(x) for x in aliases}), None) for key, aliases in SOURCE_MAP.items()}
            if recognized["product_name"] is not None and recognized["source_url"] is not None:
                header_map = recognized; continue
            if header_map is None: raise ValueError(f"No recognizable product header in {path}:{tab}")
            values = {key: cells[i] if i is not None and i < len(cells) else "" for key, i in header_map.items()}
            invalid_numbers=[]
            for field in ("price_hint_vnd","original_price_hint_vnd","sold_hint","rating_count_hint"):
                normalized=_number_hint(values[field],integer=True)
                if normalized is None: invalid_numbers.append(field)
                else: values[field]=normalized
            for field in ("rating_hint","commission_percent_hint"):
                normalized=_number_hint(values[field],integer=False)
                if normalized is None: invalid_numbers.append(field)
                else: values[field]=normalized
            url_index = header_map["source_url"]
            cell_links = extra[0] if extra else []
            url = values["source_url"]
            if not url.startswith("https://") and url_index is not None and url_index < len(cell_links) and cell_links[url_index]: url = cell_links[url_index][0]
            listing = _listing(url)
            source_file = f"sources/{sheet_id}_{path.name}"
            ref = {"sheet_id": sheet_id, "file": source_file, "source_path":str(path.resolve()),
                   "tab": tab, "row": number, "snapshot_sha256": sha, "source_locator_url": url}
            if not values.get("observed_at"):
                values["observed_at"] = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
                ref["observed_at_basis"] = "snapshot_file_mtime"
            if listing is None:
                # Reuse an operator-confirmed PDP resolution for the same Sheet cell.
                for old_row in previous_rows:
                    try: old_refs = json.loads(old_row["source_refs_json"])
                    except (KeyError, json.JSONDecodeError): continue
                    resolution = next((x.get("resolved_source_url") for x in old_refs
                        if x.get("sheet_id") == sheet_id and x.get("tab") == tab and x.get("row") == number
                        and x.get("source_locator_url") == url and x.get("resolved_source_url")), None)
                    if resolution:
                        listing = _listing(resolution)
                        if listing: ref["resolved_source_url"] = listing[2]
                        break
            key = listing[0] if listing else "unresolved:" + hashlib.sha256(json.dumps(ref, sort_keys=True).encode()).hexdigest()[:24]
            if key not in candidates:
                row = {name: "" for name in POOL_FIELDS}; row.update(values)
                row.update(candidate_id=key, source_url=listing[2] if listing else url,
                           platform=listing[1] if listing else values.get("platform", ""),
                           source_refs_json=json.dumps([ref], ensure_ascii=False, separators=(",", ":")),
                           state="OPEN" if listing and values.get("product_name", "").strip() else "NEEDS_REVIEW",
                           selected_batch_id="", selection_reason="")
                if invalid_numbers:
                    row["risk_notes"]=(row.get("risk_notes","")+"\nUnparsed numeric hints: "+", ".join(invalid_numbers)).strip()
                candidates[key] = row
            else:
                row = candidates[key]
                refs = json.loads(row["source_refs_json"]); refs.append(ref)
                row["source_refs_json"] = json.dumps(refs, ensure_ascii=False, separators=(",", ":"))
                old_variant, new_variant = row.get("requested_variant", "").strip(), values.get("requested_variant", "").strip()
                if old_variant and new_variant and old_variant.casefold() != new_variant.casefold():
                    row.update(state="NEEDS_REVIEW", risk_notes="Conflicting requested variants across source rows.")
    rows = list(candidates.values())
    old = {r["candidate_id"]: r for r in previous_rows}
    for row in rows:
        previous = old.get(row["candidate_id"])
        if row.get("risk_notes", "").startswith("Conflicting requested variants"):
            if previous and previous.get("state") in ("SELECTED", "HANDED_OFF"):
                row.update(state="NEEDS_REVIEW", selected_batch_id=previous.get("selected_batch_id", ""),
                           selection_reason=previous.get("selection_reason", ""))
            else:
                row["state"] = "NEEDS_REVIEW"
        elif previous and previous.get("state") in ("SELECTED", "HANDED_OFF"):
            row.update(state=previous["state"], selected_batch_id=previous.get("selected_batch_id", ""), selection_reason=previous.get("selection_reason", ""))
        elif previous and previous.get("state") == "NEEDS_REVIEW" and row["state"] == "OPEN":
            row["state"] = "NEEDS_REVIEW"
    if previous_pool:
        for handoff_path in previous_pool.parent.glob("*/handoff.json"):
            try:
                ready_dir = handoff_path.parent
                if not (ready_dir / "READY").is_file(): continue
                handoff = read_json(handoff_path); selected_file = ready_dir / handoff["csv_file"]
                if digest(selected_file) != handoff["csv_sha256"]: continue
                with selected_file.open(encoding="utf-8-sig", newline="") as stream:
                    for selected in csv.DictReader(stream):
                        current = next((x for x in rows if x["candidate_id"] == selected["candidate_id"]), None)
                        if current and current["state"] not in ("HANDED_OFF", "NEEDS_REVIEW"):
                            current.update(state="SELECTED", selected_batch_id=handoff["batch_id"], selection_reason=selected["selection_reason"])
            except (OSError, ValueError, KeyError, TypeError):
                continue
    return rows

def write_pool(path: Path, rows: list[dict]) -> None:
    _atomic_csv(path, POOL_FIELDS, rows)

def _read_pool(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as stream: return list(csv.DictReader(stream))

def resolve_candidate(pool: Path, candidate_id: str, direct_url: str, *, reason: str) -> dict:
    listing = _listing(direct_url)
    if not listing or link_kind(listing[2]) != "direct": raise ValueError("Resolution must be an inspected direct Shopee/TikTok PDP.")
    if not reason.strip(): raise ValueError("Record why the PDP matches the source listing.")
    with _pool_lock(pool):
        rows = _read_pool(pool); row = next((x for x in rows if x["candidate_id"] == candidate_id), None)
        if not row or row["state"] != "NEEDS_REVIEW": raise ValueError("Only a NEEDS_REVIEW unresolved row can be resolved.")
        target_id, platform, normalized = listing
        existing = next((x for x in rows if x["candidate_id"] == target_id), None)
        if existing and existing is not row:
            refs = json.loads(existing["source_refs_json"]); new_refs = json.loads(row["source_refs_json"])
            for ref in new_refs:
                ref["resolved_source_url"] = normalized
                if ref not in refs: refs.append(ref)
            existing["source_refs_json"] = json.dumps(refs, ensure_ascii=False, separators=(",", ":"))
            if (existing.get("requested_variant", "").strip() and row.get("requested_variant", "").strip()
                    and existing["requested_variant"].strip().casefold() != row["requested_variant"].strip().casefold()):
                existing["risk_notes"]="Conflicting requested variants; manual reconciliation required."
                if existing.get("state") not in ("SELECTED","HANDED_OFF"):
                    existing["state"]="NEEDS_REVIEW"
            elif existing.get("state") not in ("SELECTED", "HANDED_OFF"):
                existing["state"] = "OPEN"
            rows.remove(row); row = existing
        else:
            refs = json.loads(row["source_refs_json"])
            for ref in refs: ref["resolved_source_url"] = normalized
            row.update(candidate_id=target_id, source_url=normalized, platform=platform, state="OPEN",
                       risk_notes=(row.get("risk_notes", "") + "\nResolved PDP: " + reason).strip())
            row["source_refs_json"] = json.dumps(refs, ensure_ascii=False, separators=(",", ":"))
        write_pool(pool, rows)
        return row

def publish_selection(pool: Path, selected_csv: Path, batch_dir: Path, *, batch_date: str,
                      candidate_ids: list[str], drafts: dict[str, dict] | None = None, limit: int = 15) -> dict:
    with _pool_lock(pool):
        return _publish_selection_locked(pool, selected_csv, batch_dir, batch_date=batch_date,
                                         candidate_ids=candidate_ids, drafts=drafts, limit=limit)

def _publish_selection_locked(pool: Path, selected_csv: Path, batch_dir: Path, *, batch_date: str,
                              candidate_ids: list[str], drafts: dict[str, dict] | None, limit: int) -> dict:
    if not re.fullmatch(r"\d{8}", batch_date): raise ValueError("batch_date must use YYYYMMDD.")
    if not 1 <= limit <= MAX_SHEET_SELECTION or len(candidate_ids) > limit or len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError(f"Selection must contain unique candidates within 1..{MAX_SHEET_SELECTION}.")
    if selected_csv.name != "selected_products.csv": raise ValueError("Selected snapshot file must be named selected_products.csv.")
    rows = _read_pool(pool); ready_matches=[]
    for handoff_path in batch_dir.glob("*/handoff.json") if batch_dir.exists() else []:
        try:
            ready_dir=handoff_path.parent
            if not (ready_dir/"READY").is_file(): continue
            old_handoff=read_json(handoff_path); old_csv=ready_dir/old_handoff["csv_file"]
            if digest(old_csv)!=old_handoff["csv_sha256"]: continue
            with old_csv.open(encoding="utf-8-sig",newline="") as stream:
                old_selected_rows=list(csv.DictReader(stream))
                old_ids=[x["candidate_id"] for x in old_selected_rows]
                if old_handoff.get("batch_date")==batch_date and old_ids==candidate_ids:
                    ready_matches.append((ready_dir,old_handoff,old_selected_rows))
                for old_selected in old_selected_rows:
                    current=next((x for x in rows if x["candidate_id"]==old_selected["candidate_id"]),None)
                    if current and current.get("state")=="OPEN":
                        current.update(state="SELECTED",selected_batch_id=old_handoff["batch_id"],selection_reason=old_selected["selection_reason"])
        except (OSError,ValueError,KeyError,TypeError): continue
    existing_batch = ready_matches[0] if len(ready_matches)==1 else None
    if len(ready_matches)>1: raise ValueError("Multiple READY batches reserve the same ordered candidate IDs.")
    by_id = {r["candidate_id"]: r for r in rows}
    if len(by_id) != len(rows): raise ValueError("Candidate IDs must be unique.")
    drafts = drafts or {}; selected = []
    for rank, cid in enumerate(candidate_ids, 1):
        row = by_id.get(cid)
        reserved_replay = bool(existing_batch and row and row.get("state") in ("SELECTED","HANDED_OFF")
                              and row.get("selected_batch_id")==existing_batch[1]["batch_id"])
        if not row or (row.get("state")!="OPEN" and not reserved_replay) or not cid.startswith(("shopee:", "tiktok:")):
            raise ValueError(f"Candidate is not eligible OPEN: {cid}")
        listing = _listing(row.get("source_url", ""))
        if not listing or listing[0] != cid: raise ValueError(f"Candidate source URL does not bind to candidate_id: {cid}")
        reason = str(drafts.get(cid, {}).get("selection_reason", "")).strip()
        if not reason: raise ValueError(f"selection_reason is required for {cid}")
        selected.append({"product_name": row["product_name"], "source_url": row["source_url"], "category": row["category"],
            "candidate_id": cid, "platform": row["platform"], "brand_hint": row["brand_hint"],
            "requested_variant": row["requested_variant"], "price_hint_vnd": row["price_hint_vnd"],
            "selection_rank": rank, "selection_reason": reason,
            "video_concept": drafts.get(cid, {}).get("video_concept", row["video_concept"]),
            "script_draft": drafts.get(cid, {}).get("script_draft", ""),
            "claims_to_verify": drafts.get(cid, {}).get("claims_to_verify", row["risk_notes"]),
            "source_refs_json": json.dumps([{k:v for k,v in ref.items() if k != "source_path"}
                for ref in json.loads(row["source_refs_json"])], ensure_ascii=False, separators=(",", ":")),
            "observed_at": row["observed_at"]})
    if not selected:
        batch_dir.mkdir(parents=True, exist_ok=True)
        atomic_json(batch_dir / f"NO_ELIGIBLE_PRODUCTS_{batch_date}.json", {"status":"NO_ELIGIBLE_PRODUCTS", "reason":"Agent selected zero candidates."})
        return {"status":"NO_ELIGIBLE_PRODUCTS", "selected_count":0, "selected":[]}
    batch_dir = batch_dir.resolve(); batch_dir.mkdir(parents=True, exist_ok=True)
    serialized_csv = _csv_bytes(SELECTED_FIELDS, selected)
    sha = hashlib.sha256(serialized_csv).hexdigest(); batch_id = f"sel_{batch_date}_{sha[:12]}"
    final_dir = batch_dir / batch_id
    handoff = {"schema_version":"1.0.0", "kind":"pi_selection", "batch_id":batch_id, "batch_date":batch_date,
        "created_at": datetime.now(timezone.utc).isoformat(), "selection_limit":limit, "selected_count":len(selected),
        "csv_file":"selected_products.csv", "csv_sha256":sha,
        "sources":[{"sheet_id":ref["sheet_id"],"file":ref["file"],"sha256":ref["snapshot_sha256"]}
                   for ref in {(x["sheet_id"],x["file"],x["snapshot_sha256"]):x for r in selected for x in json.loads(r["source_refs_json"])}.values()]}
    source_manifest = pool.with_name(pool.stem + ".sources.json")
    if source_manifest.is_file():
        source_records = read_json(source_manifest).get("sources", [])
        refs_by_key = {(x["sheet_id"],x["file"],x["sha256"]):x for x in source_records}
        for row in selected:
            for ref in json.loads(row["source_refs_json"]):
                if (ref["sheet_id"],ref["file"],ref["snapshot_sha256"]) not in refs_by_key:
                    raise ValueError("Pool source snapshot manifest is stale; normalize both sources again.")
        handoff["sources"] = [{k:v for k,v in x.items() if k in ("sheet_id","file","sha256")} for x in source_records]
    if existing_batch:
        existing_dir, existing_handoff, _ = existing_batch
        if (existing_dir != final_dir or existing_handoff.get("csv_sha256")!=sha
                or existing_handoff.get("selection_limit")!=limit or existing_handoff.get("sources")!=handoff["sources"]):
            raise ValueError("READY selection already reserves these candidates with different bytes or sources.")
        if (not (existing_dir/"READY").is_file() or digest(existing_dir/"selected_products.csv")!=sha):
            raise ValueError("Existing selection READY snapshot failed read-back validation.")
        for source in existing_handoff.get("sources",[]):
            source_path=existing_dir/source["file"]
            if not source_path.is_file() or digest(source_path)!=source["sha256"]:
                raise ValueError("Existing selection source snapshot changed.")
        for row in selected:
            candidate=by_id[row["candidate_id"]]
            if candidate.get("state")!="HANDED_OFF":
                candidate.update(state="SELECTED",selected_batch_id=batch_id,selection_reason=row["selection_reason"])
        write_pool(pool,rows)
        return {**existing_handoff,"selected":selected,"batch_dir":str(existing_dir)}
    if final_dir.exists(): raise ValueError("Selection snapshot already exists with a conflicting reservation; never overwrite it.")
    staging = batch_dir / (".staging_" + hashlib.sha256(json.dumps(candidate_ids).encode()).hexdigest()[:12])
    if staging.exists(): raise ValueError("An incomplete staging selection exists; inspect before resuming.")
    staging.mkdir()
    temp_selected = staging / "selected_products.csv"; _atomic_csv(temp_selected, SELECTED_FIELDS, selected)
    if digest(temp_selected)!=sha: raise ValueError("Selection CSV serialization read-back mismatch.")
    for source in handoff["sources"]:
        manifest_record = next((x for x in (read_json(source_manifest).get("sources", []) if source_manifest.is_file() else [])
                                if x["sheet_id"] == source["sheet_id"] and x["file"] == source["file"]), None)
        if manifest_record:
            source_path = Path(manifest_record["source_path"])
        else:
            ref = next(x for r in rows if r["candidate_id"] in {s["candidate_id"] for s in selected}
                       for x in json.loads(r["source_refs_json"])
                       if x["sheet_id"] == source["sheet_id"] and x["file"] == source["file"])
            source_path = Path(ref["source_path"])
        if not source_path.is_file() or digest(source_path) != source["sha256"]:
            raise ValueError(f"Source snapshot changed or disappeared: {source_path}")
        destination = staging / source["file"]
        destination.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(source_path, destination)
        if digest(destination) != source["sha256"]: raise ValueError("Source snapshot copy checksum mismatch.")
    atomic_json(staging / "handoff.json", handoff)
    # Read-back before READY; immutable final directory and READY written last.
    if digest(staging / "selected_products.csv") != sha: raise ValueError("Selection CSV read-back checksum mismatch.")
    staging.rename(final_dir); (final_dir / "READY").write_text("READY\n", encoding="utf-8")
    for row in selected:
        by_id[row["candidate_id"]].update(state="SELECTED", selected_batch_id=batch_id, selection_reason=row["selection_reason"])
    write_pool(pool, rows)
    return {**handoff, "selected":selected, "batch_dir":str(final_dir)}

def apply_import_ack(pool: Path, batch_dir: Path) -> dict:
    with _pool_lock(pool):
        return _apply_import_ack_locked(pool, batch_dir)

def _apply_import_ack_locked(pool: Path, batch_dir: Path) -> dict:
    handoff = read_json(batch_dir / "handoff.json"); ack = read_json(batch_dir / "import_ack.json")
    csv_path = batch_dir / handoff["csv_file"]
    if not (batch_dir / "READY").is_file() or digest(csv_path) != handoff["csv_sha256"] or ack.get("csv_sha256") != handoff["csv_sha256"] or ack.get("batch_id") != handoff["batch_id"] or ack.get("status") != "accepted":
        raise ValueError("ACK does not bind to a READY selection snapshot.")
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        selected_rows = list(csv.DictReader(stream))
    expected = {r["candidate_id"] for r in selected_rows}
    ack_rows = ack.get("rows", [])
    ack_ids = {r.get("candidate_id") for r in ack_rows if isinstance(r, dict)}
    if (len(ack_ids) != handoff["selected_count"] or ack_ids != expected
            or any(not r.get("row_id") or not r.get("snapshot_id") for r in ack_rows)):
        raise ValueError("ACK row count or candidate binding mismatch.")
    for index,(selected,ack_row) in enumerate(zip(selected_rows,ack_rows),1):
        expected_snapshot="piintake_"+hashlib.sha256(f"pi-intake:{handoff['batch_id']}:{selected['candidate_id']}".encode()).hexdigest()
        if (ack_row.get("candidate_id")!=selected["candidate_id"] or ack_row.get("row_id")!=f"row_{index:04d}"
                or ack_row.get("snapshot_id")!=expected_snapshot):
            raise ValueError("ACK row/snapshot identity does not match the deterministic PI intake binding.")
    rows = _read_pool(pool)
    for row in rows:
        if row["candidate_id"] in ack_ids:
            if row.get("selected_batch_id") not in ("", handoff["batch_id"]): raise ValueError("Pool reservation does not match ACK batch.")
            if row.get("state") not in ("OPEN", "SELECTED", "HANDED_OFF"): raise ValueError("ACK candidate is not eligible for pool reconciliation.")
            row["selected_batch_id"] = handoff["batch_id"]
            row["state"] = "HANDED_OFF"
    write_pool(pool, rows)
    return {"batch_id":handoff["batch_id"], "handed_off":len(ack_ids)}

def main(argv=None):
    p=argparse.ArgumentParser(description="Normalize pinned Sheets snapshots and commit agent selections; no fetching or auto-selection.")
    sp=p.add_subparsers(dest="command", required=True)
    n=sp.add_parser("normalize"); n.add_argument("--source", action="append", required=True, help="sheet-id=path, exactly twice"); n.add_argument("--pool", type=Path, required=True); n.add_argument("--previous-pool", type=Path)
    s=sp.add_parser("publish"); s.add_argument("--pool", type=Path, required=True); s.add_argument("--selection-file", type=Path, required=True); s.add_argument("--batch-root", type=Path, required=True); s.add_argument("--batch-date", required=True); s.add_argument("--limit", type=int, default=15)
    a=sp.add_parser("ack"); a.add_argument("--pool", type=Path, required=True); a.add_argument("--batch-dir", type=Path, required=True)
    r=sp.add_parser("resolve"); r.add_argument("--pool", type=Path, required=True); r.add_argument("--candidate-id", required=True); r.add_argument("--url", required=True); r.add_argument("--reason", required=True)
    args=p.parse_args(argv)
    try:
        if args.command == "normalize":
            sources=[]
            for value in args.source:
                sid, sep, filename=value.partition("=")
                if not sep: raise ValueError("--source must be sheet-id=path")
                sources.append((sid, Path(filename)))
            with _pool_lock(args.pool):
                write_pool(args.pool, normalize_sources(sources, args.previous_pool))
                atomic_json(args.pool.with_name(args.pool.stem + ".sources.json"), {"sources":[
                    {"sheet_id":sid,"file":f"sources/{sid}_{path.name}","source_path":str(path.resolve()),"sha256":digest(path)}
                    for sid,path in sources]})
                result={"pool":str(args.pool),"count":len(_read_pool(args.pool))}
        elif args.command == "publish":
            selection = read_json(args.selection_file)
            selected = selection.get("selected", [])
            if not isinstance(selected, list): raise ValueError("selection-file requires a selected array.")
            result = publish_selection(args.pool, args.batch_root / "selected_products.csv", args.batch_root,
                batch_date=args.batch_date, candidate_ids=[r.get("candidate_id", "") for r in selected],
                drafts={r.get("candidate_id", ""):r for r in selected}, limit=args.limit)
        elif args.command == "resolve": result=resolve_candidate(args.pool,args.candidate_id,args.url,reason=args.reason)
        else: result=apply_import_ack(args.pool,args.batch_dir)
        print(json.dumps(result,ensure_ascii=False,indent=2)); return 0
    except (ValueError,OSError,KeyError,TypeError) as exc:
        print(json.dumps({"status":"error","error":str(exc)},ensure_ascii=False),file=sys.stderr); return 2

if __name__ == "__main__": raise SystemExit(main())

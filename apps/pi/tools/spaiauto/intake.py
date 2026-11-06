"""Ordered request intake; the agent resolves one row before acquiring the next."""

from __future__ import annotations

import math
import csv
import hashlib
import json
import re
import shutil
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from .intake_parser import parse_input
from .links import direct_product_url
from .storage import atomic_json, digest, item_key, now, read_json
from .workflow import export_run, initialize

TERMINAL = {"completed", "needs_review", "skipped"}


class SkipReason(StrEnum):
    UNSUPPORTED_URL = "unsupported_url"
    NO_MATCHING_IMAGES = "no_matching_images"
    IDENTITY_UNVERIFIED = "identity_unverified"
    BROWSER_ERROR = "browser_error"
    REVIEW_ERROR = "review_error"
    UNKNOWN = "unknown"


def start_intake(source: Path, run_dir: Path, target_images: int = 5, *,
                 handoff: Path | None = None, db_path: Path | None = None) -> dict:
    if not 1 <= target_images <= 100:
        raise ValueError("target_images must be between 1 and 100.")
    source_hash = digest(source)
    contract = _validate_handoff(source, handoff) if handoff else None
    rows = parse_input(source)
    if contract:
        if len(rows) != contract["handoff"]["selected_count"]:
            raise ValueError("Handoff count mismatch.")
        by_source_row = {r["source_row"]: r for r in rows}
        for number, named in enumerate(contract["csv_rows"], 2):
            row = by_source_row.get(number)
            if row is None:
                raise ValueError("Handoff CSV row binding mismatch.")
            row["candidate_id"] = named["candidate_id"]
            row["source_url"] = named["source_url"]
            row["links"] = [{"url": named["source_url"], "kind": "direct"}]
            row["input_metadata"] = {key: named.get(key, "") for key in (
                "candidate_id", "category", "brand_hint", "requested_variant", "price_hint_vnd",
                "selection_rank", "selection_reason", "video_concept", "script_draft", "claims_to_verify",
                "source_refs_json", "observed_at")}
            row["creative_input"] = {"kind": "creative-input-v1", "candidate_id": named["candidate_id"],
                "csv_sha256": contract["handoff"]["csv_sha256"], "video_concept": named.get("video_concept", ""),
                "script_draft": named.get("script_draft", ""), "claims_to_verify": named.get("claims_to_verify", ""),
                "source_refs_json": named["source_refs_json"]}
            row["price_hint_vnd"] = named.get("price_hint_vnd", "")
            row["price_min"] = row["price_max"] = None
            row["price_text"] = ""
            row["parse_warnings"] = []
    for row in rows:
        if not any(link["kind"] != "unsupported" for link in row["links"]):
            row["parse_warnings"].append("missing_supported_source_link")
    if digest(source) != source_hash:
        raise ValueError("Input changed while parsing; retry from a stable file.")
    state_path = run_dir / "intake.json"
    if state_path.exists():
        state = _load(run_dir)
        if state["input_sha256"] != source_hash or state["target_images"] != target_images:
            raise ValueError("Input changed; initialize a new run rather than silently reusing progress.")
        if state.get("mode") == "pi_selection" and not contract:
            raise ValueError("This PI run is bound to a selection handoff; replay it with --handoff and its original DB path.")
        if contract:
            if (state.get("mode") != "pi_selection"
                    or state.get("selection_handoff") != str(handoff.resolve())
                    or state.get("selection_handoff_sha256") != digest(handoff)):
                raise ValueError("Existing PI run is bound to a different handoff; use its original batch/run binding.")
            _persist_handoff(run_dir, state, contract, db_path)
        return state
    if run_dir.exists() and any(run_dir.iterdir()):
        raise ValueError("Intake directory is not empty and has no intake.json.")
    source_copy = run_dir / "input" / source.name
    source_copy.parent.mkdir(parents=True)
    shutil.copy2(source, source_copy)
    if digest(source_copy) != source_hash:
        raise ValueError("Input changed during copy; use a new intake directory.")
    state = {"schema_version": 1, "mode": "file_products", "created_at": now(),
             "input_name": source.name, "input_sha256": source_hash,
             "input_file": source_copy.relative_to(run_dir).as_posix(),
             "target_images": target_images,
             "rows": [{**row, "status": "needs_review" if row["parse_warnings"] else "pending_resolution",
                       "reason": ", ".join(row["parse_warnings"]) or None, "search_attempts": 0}
                      for row in rows], "events": []}
    if contract:
        state.update(mode="pi_selection", batch_id=contract["handoff"]["batch_id"],
                     selection_csv_sha256=contract["handoff"]["csv_sha256"],
                     selection_handoff=str(handoff.resolve()), selection_handoff_sha256=digest(handoff))
    atomic_json(state_path, state)
    if contract:
        _persist_handoff(run_dir, state, contract, db_path)
    return state


def _validate_handoff(source: Path, handoff_path: Path | None) -> dict:
    from .storage import digest
    from .links import direct_product_url
    if handoff_path is None:
        return {}
    handoff_path = handoff_path.resolve(); batch_dir = handoff_path.parent
    data = read_json(handoff_path); csv_name = data.get("csv_file")
    if (not (batch_dir / "READY").is_file() or data.get("kind") != "pi_selection"
            or data.get("schema_version") != "1.0.0" or not isinstance(data.get("batch_id"), str)):
        raise ValueError("Selection handoff is not a valid READY pi_selection snapshot.")
    if not isinstance(csv_name, str) or Path(csv_name).is_absolute() or ".." in Path(csv_name).parts:
        raise ValueError("Handoff CSV path must be a safe relative path.")
    csv_path = (batch_dir / csv_name).resolve()
    if not csv_path.is_relative_to(batch_dir) or (batch_dir / csv_name).is_symlink() or digest(csv_path) != data.get("csv_sha256"):
        raise ValueError("Handoff CSV SHA/path validation failed.")
    if source.resolve() != csv_path:
        raise ValueError("--file must be the selected CSV bound by handoff.json.")
    if (not re.fullmatch(r"\d{8}", str(data.get("batch_date", "")))
            or data.get("batch_id") not in (f"sel_{data['batch_date']}_{data.get('csv_sha256', '')[:12]}", f"fix_{data['batch_date']}_{data.get('csv_sha256', '')[:12]}")):
        raise ValueError("Handoff batch_id/date do not bind to the selected CSV SHA.")
    if not isinstance(data.get("sources"), list) or not data["sources"]:
        raise ValueError("Handoff must list source snapshot files and SHA values.")
    source_keys = set()
    for item in data["sources"]:
        if (not isinstance(item, dict) or not item.get("sheet_id") or not item.get("file")
                or not re.fullmatch(r"[a-f0-9]{64}", str(item.get("sha256", "")))):
            raise ValueError("Handoff source entries require sheet_id, file and SHA-256.")
        source_rel = Path(item["file"])
        if source_rel.is_absolute() or ".." in source_rel.parts or not item["file"].startswith("sources/"):
            raise ValueError("Handoff source paths must stay under the relative sources/ directory.")
        source_file = (batch_dir / source_rel).resolve()
        if (not source_file.is_relative_to(batch_dir) or (batch_dir / source_rel).is_symlink()
                or not source_file.is_file() or digest(source_file) != item["sha256"]):
            raise ValueError("Handoff source snapshot file/SHA validation failed.")
        source_keys.add((item["sheet_id"], item["file"], item["sha256"]))
    with csv_path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["product_name", "source_url", "category", "candidate_id", "platform", "brand_hint", "requested_variant", "price_hint_vnd", "selection_rank", "selection_reason", "video_concept", "script_draft", "claims_to_verify", "source_refs_json", "observed_at"]:
            raise ValueError("Handoff selected CSV schema mismatch.")
        rows = list(reader)
    count = data.get("selected_count")
    if not isinstance(count, int) or count != len(rows) or count > 15:
        raise ValueError("Handoff selected count mismatch.")
    candidates, ranks = set(), []
    for row in rows:
        if not all(row.get(k, "").strip() for k in ("product_name", "source_url", "candidate_id", "platform", "selection_rank", "selection_reason", "source_refs_json", "observed_at")):
            raise ValueError("Handoff row is missing required values.")
        if row["candidate_id"] in candidates: raise ValueError("Duplicate candidate_id in handoff.")
        candidates.add(row["candidate_id"])
        try: ranks.append(int(row["selection_rank"]))
        except ValueError as exc: raise ValueError("Handoff rank must be an integer.") from exc
        url = direct_product_url(row["source_url"])
        host = urlsplit(url).hostname or ""
        if row["platform"] not in ("shopee", "tiktok") or (row["platform"] == "shopee") != host.endswith("shopee.vn"):
            raise ValueError("Handoff platform and source URL binding mismatch.")
        parts = urlsplit(url)
        if row["platform"] == "shopee":
            match = re.search(r"/product/(\d+)/(\d+)$", parts.path) or re.search(r"-i\.(\d+)\.(\d+)$", parts.path)
            parsed_id = f"shopee:{match.group(1)}:{match.group(2)}" if match else None
        else:
            match = re.search(r"/(?:pdp|product)/(?:[^/]+/)*(\d+)$", parts.path)
            parsed_id = f"tiktok:{match.group(1)}" if match else None
        if parsed_id != row["candidate_id"]:
            raise ValueError("Handoff candidate_id does not match its direct PDP URL.")
        try: refs = json.loads(row["source_refs_json"])
        except json.JSONDecodeError as exc: raise ValueError("Invalid source_refs_json.") from exc
        if not isinstance(refs, list) or not refs or any(not isinstance(x, dict) or not {"sheet_id", "file", "tab", "row", "snapshot_sha256"} <= x.keys()
                or not isinstance(x.get("row"), int) or not re.fullmatch(r"[a-f0-9]{64}", str(x.get("snapshot_sha256", "")))
                or (x["sheet_id"], x["file"], x["snapshot_sha256"]) not in source_keys for x in refs):
            raise ValueError("Handoff source provenance is incomplete.")
    if sorted(ranks) != list(range(1, count + 1)): raise ValueError("Handoff ranks must be unique 1..N.")
    return {"handoff": data, "csv_rows": rows, "batch_dir": batch_dir, "handoff_path":handoff_path}


def _persist_handoff(run_dir: Path, state: dict, contract: dict, db_path: Path | None) -> None:
    from media.config import research_snapshots_db_path
    from media.research_persistence import ResearchSnapshot, SQLiteResearchRepository
    from .storage import digest
    if state.get("mode") != "pi_selection":
        raise ValueError("A legacy file intake cannot be resumed as a selection handoff.")
    if state.get("batch_id") != contract["handoff"]["batch_id"] or state.get("selection_csv_sha256") != contract["handoff"]["csv_sha256"]:
        raise ValueError("Existing PI run is bound to a different selection snapshot.")
    if (state.get("selection_handoff") != str(contract["handoff_path"])
            or state.get("selection_handoff_sha256") != digest(contract["handoff_path"])):
        raise ValueError("Selection handoff file/path changed after this PI run was initialized.")
    resolved_db=(db_path or research_snapshots_db_path()).resolve()
    if state.get("pi_db_path") and Path(state["pi_db_path"]).resolve()!=resolved_db:
        raise ValueError("PI run is already bound to a different SQLite database.")
    if not state.get("pi_db_path"):
        state["pi_db_path"]=str(resolved_db)
        atomic_json(run_dir/"intake.json",state)
    copied = (run_dir / state["input_file"]).resolve()
    if not copied.is_relative_to(run_dir.resolve()) or digest(copied) != contract["handoff"]["csv_sha256"]:
        raise ValueError("Saved selected CSV changed; cannot resume.")
    repo = SQLiteResearchRepository(resolved_db)
    try:
        snapshots = []
        for row in state["rows"]:
            cid = row["candidate_id"]; identity = f"pi-intake:{state['batch_id']}:{cid}"
            sid = "piintake_" + hashlib.sha256(identity.encode()).hexdigest()
            snapshots.append(ResearchSnapshot(snapshot_id=sid, research_id=identity,
                created_at=contract["handoff"]["created_at"], status="input_received",
                request={"kind":"pi_csv_intake_v1", "batch_id":state["batch_id"], "candidate_id":cid,
                         "input_metadata":row["input_metadata"], "source_url":row["source_url"], "source_row":row["source_row"]},
                result={}, research_metadata={"kind":"pi_csv_intake_v1", "batch_id":state["batch_id"],
                    "csv_sha256":state["selection_csv_sha256"]}))
        repo.save_intake_batch(snapshots)
        readback = [repo.get_snapshot(s.snapshot_id) for s in snapshots]
        if [s.model_dump(mode="json") for s in snapshots] != [s.model_dump(mode="json") for s in readback]:
            raise ValueError("SQLite intake snapshot read-back mismatch.")
    finally:
        repo.close()
    if _load(run_dir).get("selection_csv_sha256") != contract["handoff"]["csv_sha256"]:
        raise ValueError("Intake JSON read-back mismatch.")
    ack = {"schema_version":"1.0.0", "batch_id":state["batch_id"], "csv_sha256":state["selection_csv_sha256"],
        "status":"accepted", "input_row_count":len(state["rows"]), "accepted_row_count":len(state["rows"]),
        "pi_db_path":str(resolved_db), "run_dir":str(run_dir.resolve()),
        "rows":[{"candidate_id":r["candidate_id"], "row_id":r["row_id"], "snapshot_id":"piintake_"+hashlib.sha256(f"pi-intake:{state['batch_id']}:{r['candidate_id']}".encode()).hexdigest()} for r in state["rows"]]}
    atomic_json(contract["batch_dir"] / "import_ack.json", ack)


def _load(run_dir: Path) -> dict:
    state = read_json(run_dir / "intake.json")
    source = (run_dir / state["input_file"]).resolve()
    if not source.is_relative_to(run_dir.resolve()) or digest(source) != state["input_sha256"]:
        raise ValueError("Saved original input changed; cannot resume this intake.")
    return state


def _next(state: dict) -> dict | None:
    return next((row for row in state["rows"] if row["status"] not in TERMINAL), None)


def _current(run_dir: Path, row_id: str) -> tuple[dict, dict]:
    state = _load(run_dir)
    row = _next(state)
    if row is None or row["row_id"] != row_id:
        raise ValueError("Only the first unfinished row may be processed; inspect next.")
    return state, row


def _save(run_dir: Path, state: dict, action: str, row_id: str, **details) -> None:
    state["events"].append({"at": now(), "action": action, "row_id": row_id, **details})
    atomic_json(run_dir / "intake.json", state)


def next_request(run_dir: Path) -> dict | None:
    state = _load(run_dir)
    row = _next(state)
    if row is None:
        return None
    result = dict(row)
    useful = [link["url"] for link in row["links"] if link["kind"] != "unsupported"]
    result["suggested_search_url"] = useful[0] if useful else None
    result["next_action"] = ("Acquire the selected PDP via Spaiauto, then finish this row."
                             if row["status"] == "resolved" else
                             "Open the supplied source link; select the matching product PDP only. No new product discovery.")
    return result


def search_attempt(run_dir: Path, row_id: str) -> dict:
    state, row = _current(run_dir, row_id)
    if not any(link["kind"] != "unsupported" for link in row["links"]):
        raise ValueError("Supply a source link for this row; autonomous product discovery is paused.")
    if row["status"] == "resolved" or row["search_attempts"] >= 3:
        raise ValueError("Row already resolved or search attempts exhausted.")
    row["search_attempts"] += 1
    row["status"] = "searching"
    _save(run_dir, state, "search_begin", row_id, attempt=row["search_attempts"])
    return row


def search_failed(run_dir: Path, row_id: str, reason: str, evidence_file: Path | None = None) -> dict:
    state, row = _current(run_dir, row_id)
    if row["status"] != "searching" or not reason.strip():
        raise ValueError("A reason and an active search attempt are required.")
    if evidence_file is not None:
        evidence = read_json(evidence_file)
        candidates = evidence.get("candidates", [])
        if not isinstance(candidates, list) or len(candidates) > 5:
            raise ValueError("Record at most five inspected candidates per search attempt.")
        for candidate in candidates:
            if (not isinstance(candidate, dict) or candidate.get("decision") != "rejected"
                    or not candidate.get("title") or not candidate.get("reason")
                    or not str(candidate.get("url", "")).startswith("https://")):
                raise ValueError("Failed-search candidates require HTTPS URL, title and rejection reason.")
        path = run_dir / "rows" / row_id / f"search_{row['search_attempts']:02d}.json"
        atomic_json(path, {**evidence, "recorded_at": now(), "reason": reason})
        row.setdefault("search_evidence", []).append({"path": path.relative_to(run_dir).as_posix(),
                                                      "sha256": digest(path)})
    row.update(status="needs_review" if row["search_attempts"] >= 3 else "pending_resolution", reason=reason)
    _save(run_dir, state, "search_failed", row_id, reason=reason)
    return row


def _validate_selection(selection: dict, row: dict) -> str:
    url = direct_product_url(selection.get("url", ""))
    for field in ("observed_title", "selected_by", "reason"):
        if not isinstance(selection.get(field), str) or not selection[field].strip():
            raise ValueError(f"Selection requires {field}.")
    checks = selection.get("criteria_checks", {})
    if not isinstance(checks, dict) or any(checks.get(k) is not True for k in
                                         ("requested_features_match", "variant_checked")):
        raise ValueError("Confirm requested_features_match and variant_checked after inspecting the PDP.")
    price = selection.get("observed_price")
    if price is not None:
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price < 0:
            raise ValueError("observed_price must be a non-negative number or null.")
        if row["price_min"] is not None:
            if row.get("currency") and selection.get("currency") != row["currency"]:
                raise ValueError("Observed currency does not match the requested price constraint.")
            if not row["price_min"] <= price <= row["price_max"]:
                raise ValueError("Selected variant price is outside the requested range.")
    elif row["price_min"] is not None and not str(selection.get("unknown_price_reason", "")).strip():
        raise ValueError("Requested price cannot be checked; supply unknown_price_reason and retain needs_review.")
    for field in ("brand", "model", "variant"):
        if selection.get(field) is not None and not isinstance(selection[field], str):
            raise ValueError(f"{field} must be text or null; do not invent generic identities.")
    if row.get("alternatives"):
        chosen = selection.get("chosen_alternative", "")
        if not isinstance(chosen, str) or chosen.casefold() not in {a.casefold() for a in row["alternatives"]}:
            raise ValueError("Record one chosen_alternative from the original row; do not split or substitute it.")
    candidates = selection.get("candidates")
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 5:
        raise ValueError("Record 1-5 actually inspected candidates for this attempt.")
    selected = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("Candidate evidence must be an object.")
        parts = urlsplit(candidate.get("url", ""))
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("Candidate evidence needs a public HTTPS URL.")
        if not candidate.get("title") or not candidate.get("reason"):
            raise ValueError("Each inspected candidate needs a title and decision reason.")
        if candidate.get("decision") == "selected":
            selected.append(candidate["url"])
        elif candidate.get("decision") != "rejected":
            raise ValueError("Candidate decision must be selected or rejected.")
    if len(selected) != 1 or direct_product_url(selected[0]) != url:
        raise ValueError("Exactly one inspected candidate must match the selected PDP.")
    return url


def resolve_request(run_dir: Path, row_id: str, selection_file: Path) -> dict:
    state, row = _current(run_dir, row_id)
    if row["status"] != "searching":
        raise ValueError("Begin a search/inspection attempt before resolving the row.")
    selection = read_json(selection_file)
    url = _validate_selection(selection, row)
    child = run_dir / "rows" / row_id / "acquisition"
    initialize(child, [url], state["target_images"])
    evidence_file = child.parent / "selection.json"
    atomic_json(evidence_file, {**selection, "url": url, "selected_at": now(), "row_id": row_id})
    row.update(status="resolved", selected_url=url,
               selection_file=evidence_file.relative_to(run_dir).as_posix(),
               selection_sha256=digest(evidence_file),
               acquisition_run=str(child.resolve()), acquisition_item_id=item_key(url))
    _save(run_dir, state, "resolve", row_id, selected_url=url)
    return row


def _selection(run_dir: Path, row: dict) -> dict | None:
    if not row.get("selection_file"):
        return None
    path = (run_dir / row["selection_file"]).resolve()
    if not path.is_relative_to(run_dir.resolve()) or digest(path) != row["selection_sha256"]:
        raise ValueError("Selection evidence changed; do not reuse this row approval.")
    selection = read_json(path)
    _validate_selection(selection, row)
    return selection


def finish_request(run_dir: Path, row_id: str, outcome: str, reason: str, *,
                   after_review: bool = False, reason_code: SkipReason = SkipReason.UNKNOWN,
                   retryable: bool = False) -> dict:
    if after_review:
        state = _load(run_dir)
        row = next((r for r in state["rows"] if r["row_id"] == row_id), None)
        if (_next(state) is not None or outcome != "completed" or row is None
                or row.get("status") != "needs_review"):
            raise ValueError("After-review finish requires a needs_review row in a fully processed batch and completed outcome.")
    else:
        state, row = _current(run_dir, row_id)
    if outcome not in TERMINAL or not reason.strip():
        raise ValueError("Finish requires completed, needs_review or skipped plus a reason.")
    selection = _selection(run_dir, row)
    child = Path(row["acquisition_run"]) if row.get("acquisition_run") else None
    if child is not None and not child.resolve().is_relative_to(run_dir.resolve()):
        raise ValueError("Acquisition run must stay inside the intake directory.")
    if outcome == "completed":
        if child is None or selection is None:
            raise ValueError("Cannot complete a row before selecting and acquiring a product.")
        payload = export_run(child)
        if payload["issues"] or len(payload["products"]) != 1 or payload["products"][0]["status"] != "reviewed":
            raise ValueError("Complete requires one actually imported, visually reviewed product.")
        if row["price_min"] is not None and selection.get("observed_price") is None:
            raise ValueError("Requested price remains unknown; finish as needs_review.")
        product = payload["products"][0]
        for field in ("brand", "model", "variant"):
            expected = selection.get(field)
            if expected and str(product.get(field) or "").strip().casefold() != expected.strip().casefold():
                raise ValueError(f"Reviewed {field} conflicts with the selected listing identity.")
    details = {"reason_code": SkipReason(reason_code).value, "retryable": retryable} if outcome == "skipped" else {}
    row.update(status=outcome, reason=reason, finished_at=now(), **details)
    _save(run_dir, state, "finish", row_id, status=outcome, reason=reason, **details)
    return row


def intake_summary(run_dir: Path) -> dict:
    state = _load(run_dir)
    rows, products, resources = [], [], []
    for row in state["rows"]:
        entry = {**row, "metadata_verification": "user_supplied_unverified"}
        try:
            entry["selected_product"] = _selection(run_dir, row)
            if row.get("acquisition_run"):
                child = Path(row["acquisition_run"]).resolve()
                if not child.is_relative_to(run_dir.resolve()):
                    raise ValueError("Acquisition run must stay inside the intake directory.")
                payload = export_run(child)
                entry["acquisition_issues"] = payload["issues"]
                if any(issue.get("status") == "integrity_error" for issue in payload["issues"]):
                    raise ValueError("Acquisition integrity/pending issue: " + str(payload["issues"]))
                entry["output_products_json"] = str(child / "products.json")
                for product in payload["products"]:
                    products.append({**product, "id": row["row_id"],
                                     "acquisition_item_id": row["acquisition_item_id"],
                                     "request_name": row["name"], "request_row": row["source_row"],
                                     "request_status": row["status"], "input_metadata": row.get("input_metadata", row["raw_cells"]),
                                     "candidate_id":row.get("candidate_id"), "creative_input":row.get("creative_input"),
                                     "metadata_verification":"user_supplied_unverified"})
                for product in read_json(child / "resources.json")["products"]:
                    product = {**product, **{kind: [{**asset, "local_path": Path(asset["absolute_path"]).relative_to(run_dir.resolve()).as_posix()}
                                                   for asset in product[kind]] for kind in ("images", "videos")}}
                    resources.append({**product, "id": row["row_id"],
                                      "acquisition_item_id": row["acquisition_item_id"],
                                      "input_file": state["input_name"], "input_sha256": state["input_sha256"],
                                      "source_sheet": row.get("source_sheet"), "source_row": row["source_row"],
                                      "request_name": row["name"], "input_metadata": row.get("input_metadata", row["raw_cells"]),
                                      "candidate_id":row.get("candidate_id"), "creative_input":row.get("creative_input"),
                                      "metadata_verification":"user_supplied_unverified"})
        except (ValueError, OSError, KeyError, TypeError) as error:
            entry.update(queue_status=row["status"], status="integrity_error", reason=str(error))
        rows.append(entry)
    report = {"schema_version": 1, "mode": "sequential_list", "input_sha256": state["input_sha256"],
              "updated_at": now(), "total_rows": len(rows), "rows": rows,
              "counts": {status: sum(r["status"] == status for r in rows)
                         for status in sorted({r["status"] for r in rows})}}
    atomic_json(run_dir / "summary.json", report)
    atomic_json(run_dir / "products.json", {"version": "2.0", "is_canonical": False,
                "acquisition_tool": "spaiauto", "products": products, "total_products": len(products)})
    atomic_json(run_dir / "resources.json", {"schema_version": "1.0.0", "kind": "pi_resources",
                "created_at": now(), "input_file": state["input_name"], "input_sha256": state["input_sha256"],
                "products": resources, "total_products": len(resources),
                "issues": [{"row_id": row["row_id"], "status": row["status"], "reason": row.get("reason")}
                           for row in rows if row["status"] != "completed"]})
    lines = ["# PI sequential intake", "", f"Input: {state['input_name']}", "",
             "| Row | Request | Status | Selected PDP | Reason |", "|---|---|---|---|---|"]
    def safe(value):
        return str(value or "").replace("|", "\\|").replace("\n", " ")
    lines.extend("| " + " | ".join(safe(v) for v in
                 (row["row_id"], row["name"], row["status"], row.get("selected_url"), row.get("reason"))) + " |"
                 for row in rows)
    lines.extend(["", "Input trend/sales claims are user-supplied and unverified.",
                  "Products are acquired only when their child run contains verified local files.", ""])
    (run_dir / "README.md").write_text("\n".join(lines), encoding="utf-8")
    return report

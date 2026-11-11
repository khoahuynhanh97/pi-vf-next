"""Resumable queue, image review and local projections for Spaiauto captures."""

from __future__ import annotations

import logging
import re
import shutil
import uuid
import hashlib
import json
from datetime import datetime
from pathlib import Path
from zipfile import BadZipFile

from .importer import import_capture, validate_capture, verify_import
from .videos import verified_videos
from .storage import (SPAIAUTO_URL, atomic_json, bangkok_date, digest, item_key,
                      now, product_url, read_json)


def initialize(run_dir: Path, urls: list[str], target_images: int = 5) -> dict:
    urls = list(dict.fromkeys(product_url(url) for url in urls))
    if not urls or not 1 <= target_images <= 100:
        raise ValueError("Supply product URLs and target_images between 1 and 100.")
    state_path = run_dir / "state.json"
    if state_path.exists():
        state = read_json(state_path)
        if ([item["source_url"] for item in state["items"].values()] != urls
                or state["target_images"] != target_images):
            raise ValueError("Run already exists with different inputs; use another run directory.")
        return state
    if run_dir.exists() and any(run_dir.iterdir()):
        raise ValueError("Run directory is not empty and has no state.json.")
    state = {"schema_version": 1, "created_at": now(), "date": bangkok_date(),
             "target_images": target_images, "items": {}, "events": []}
    for url in urls:
        key = item_key(url)
        state["items"][key] = {"source_url": url, "status": "waiting_capture",
                               "attempts": {"scan": 0, "download": 0}}
        item_dir = run_dir / "items" / key
        atomic_json(item_dir / "capture-request.json", {
            "item_id": key, "source_url": url, "extractor_url": SPAIAUTO_URL,
            "next_action": "Scan in browser; save page metadata, screenshot and actual ZIP.",
            "capture_template": {"schema_version": 1, "acquisition_tool": "spaiauto",
                                 "extractor_url": SPAIAUTO_URL, "source_url": url,
                                 "captured_at": "", "title": "", "description": "",
                                 "price_display": "", "variations_text": "",
                                 "asset_sources": []},
        })
    atomic_json(state_path, state)
    return state


def _save(run_dir: Path, state: dict, action: str, key: str | None = None, **detail) -> None:
    state["events"].append({"at": now(), "action": action, "item_id": key, **detail})
    atomic_json(run_dir / "state.json", state)


def _load_item(run_dir: Path, key: str) -> tuple[dict, dict]:
    state = read_json(run_dir / "state.json")
    if key not in state["items"]:
        raise ValueError(f"Unknown item: {key}")
    return state, state["items"][key]


def begin_attempt(run_dir: Path, key: str, stage: str) -> dict:
    state, item = _load_item(run_dir, key)
    if stage not in ("scan", "download"):
        raise ValueError("Stage must be scan or download.")
    if item["status"] in ("skipped", "imported", "reviewed"):
        raise ValueError("Item is already finished; use a new run for reacquisition.")
    if item["attempts"][stage] >= 3:
        raise ValueError("Stage exhausted its initial attempt and two retries.")
    item["attempts"][stage] += 1
    item["active_stage"] = stage
    item["status"] = "capturing"
    _save(run_dir, state, "begin", key, stage=stage, attempt=item["attempts"][stage])
    return item


def fail_attempt(run_dir: Path, key: str, reason: str) -> dict:
    state, item = _load_item(run_dir, key)
    if not reason.strip() or item.get("active_stage") not in ("scan", "download"):
        raise ValueError("A reason and an active stage are required.")
    stage = item.pop("active_stage")
    item["status"] = "skipped" if item["attempts"][stage] >= 3 else "retry_pending"
    item["reason"] = reason
    _save(run_dir, state, "failure", key, stage=stage, reason=reason)
    return item


def skip_item(run_dir: Path, key: str, reason: str) -> dict:
    state, item = _load_item(run_dir, key)
    if not reason.strip():
        raise ValueError("Skip reason is required.")
    if item["status"] == "reviewed":
        raise ValueError("Reviewed imports are immutable; use a new run to replace them.")
    item.update(status="skipped", reason=reason)
    item.pop("active_stage", None)
    _save(run_dir, state, "skip", key, reason=reason)
    return item


def discard_imported_zip(archive: Path) -> None:
    try:
        archive.unlink(missing_ok=True)
    except OSError as exc:
        logging.getLogger(__name__).warning("Could not remove imported ZIP %s: %s", archive, exc)


def attach(run_dir: Path, key: str, capture: Path, archive: Path, screenshot: Path, *,
           prepared: Path | None = None, keep_archive: bool = False) -> Path:
    state, item = _load_item(run_dir, key)
    if item["status"] in ("skipped", "imported", "reviewed"):
        raise ValueError("Item is finished; resume other items or initialize a new run.")
    if not all(item["attempts"][stage] for stage in ("scan", "download")):
        raise ValueError("Record scan and download attempts with begin before attaching.")
    label = "import_scheduled" if prepared is not None else f"import_{uuid.uuid4().hex[:12]}"
    destination = run_dir / "items" / key / label
    try:
        if prepared is not None:
            if (not prepared.resolve().is_relative_to(run_dir.resolve().parent)
                    or not destination.resolve().is_relative_to(run_dir.resolve())):
                raise ValueError("Prepared import must stay inside its intake row.")
            if not destination.exists():
                record = verify_import(prepared)
                validate_capture(record["capture"], item["source_url"])
                prepared.rename(destination)
            record = verify_import(destination)
            validate_capture(record["capture"], item["source_url"])
            atomic_json(destination / "import.json", record)
        else:
            record = import_capture(capture, archive, screenshot, destination, item["source_url"])
    except (ValueError, OSError, KeyError, TypeError, BadZipFile) as error:
        item.update(status="import_error", reason=str(error))
        _save(run_dir, state, "import_error", key, reason=str(error))
        raise
    item.update(status="imported", import_dir=str(destination.resolve()),
                import_sha256=digest(destination / "import.json"))
    item.pop("active_stage", None)
    item.pop("reason", None)
    _save(run_dir, state, "import", key, import_dir=str(destination.resolve()))
    if record["assets"] and not keep_archive:
        discard_imported_zip(archive)
    return destination


def _record(item: dict) -> tuple[Path, dict]:
    directory = Path(item["import_dir"])
    if digest(directory / "import.json") != item["import_sha256"]:
        raise ValueError("Import manifest changed; review is invalid.")
    return directory, verify_import(directory)


def _validate_review(review: dict, record: dict, directory: Path) -> None:
    if review.get("capture_digest") != digest(directory / "raw" / "capture.json"):
        raise ValueError("Review is for a different capture.")
    if review.get("identity_verified") is not True:
        raise ValueError("Explicit identity_verified=true is required after visual review.")
    for field in ("reviewed_by", "note"):
        if not isinstance(review.get(field), str) or not review[field].strip():
            raise ValueError(f"Review needs {field}.")
    identity = review.get("identity")
    if not isinstance(identity, dict):
        raise ValueError("Review needs product identity.")
    if not isinstance(identity.get("category"), str) or not identity["category"].strip():
        identity["category"] = "food"
    for field in ("brand", "model", "variant"):
        if identity.get(field) is not None and not isinstance(identity[field], str):
            raise ValueError(f"{field} must be text or null.")
    selected = review.get("selected_asset_ids")
    if (not isinstance(selected, list) or not selected
            or any(not isinstance(a, str) for a in selected)
            or len(set(selected)) != len(selected)
            or not set(selected).issubset({a["asset_id"] for a in record["assets"]})):
        raise ValueError("Choose unique asset IDs from the actual imported images.")
    if not record["capture"].get("description", "").strip():
        raise ValueError("Description is missing; retain draft and recapture before publication.")


def review_item(run_dir: Path, key: str, review_path: Path) -> dict:
    state, item = _load_item(run_dir, key)
    if item["status"] != "imported":
        raise ValueError("Only imported items can be reviewed; create another run for revisions.")
    directory, record = _record(item)
    review = read_json(review_path)
    _validate_review(review, record, directory)
    approval = directory / "review.json"
    atomic_json(approval, {**review, "reviewed_at": now()})
    item.update(status="reviewed", review_sha256=digest(approval))
    _save(run_dir, state, "review", key, reviewer=review["reviewed_by"])
    return item


def _product(run_dir: Path, key: str, item: dict, target: int) -> tuple[dict, dict]:
    directory, record = _record(item)
    capture = record["capture"]
    approval = None
    if item["status"] == "reviewed":
        if digest(directory / "review.json") != item["review_sha256"]:
            raise ValueError("Saved review changed; cannot export approved images.")
        approval = read_json(directory / "review.json")
        _validate_review(approval, record, directory)
    identity = approval["identity"] if approval else {}
    selected = set(approval["selected_asset_ids"]) if approval else set()
    selected_dir = directory / "media" / "selected"
    selected_dir.mkdir(exist_ok=True)
    images = []
    for asset in record["assets"]:
        if asset["asset_id"] not in selected:
            continue
        path = selected_dir / Path(asset["absolute_path"]).name
        if not path.exists():
            shutil.copy2(asset["absolute_path"], path)
        if digest(path) != asset["sha256"]:
            raise ValueError("Selected image changed after review.")
        images.append({**asset, "absolute_path": str(path.resolve()),
                       "local_path": path.relative_to(run_dir).as_posix(),
                       "match_status": "matched", "match_score": 1.0,
                       "match_method": "operator_visual_review"})
    product = {
        "id": key, "name": capture["title"], "brand": identity.get("brand"),
        "model": identity.get("model"), "variant": identity.get("variant"),
        "category": identity.get("category", ""), "description": capture.get("description", ""),
        "price_display": capture.get("price_display", ""), "price_min": None,
        "price_max": None, "currency": capture.get("currency"),
        "variations_text": capture.get("variations_text", ""),
        "pdp_url": item["source_url"], "source_url": item["source_url"],
        "acquisition_tool": "spaiauto", "captured_at": capture["captured_at"],
        "archive_sha256": record.get("archive_sha256") or record["raw_checksums"].get("download.zip"),
        "status": "reviewed" if approval else "needs_review", "images": images,
        "videos": verified_videos(run_dir, key, item),
        "video_urls": capture.get("video_urls", []),
        "candidate_images_count": len(record["assets"]), "saved_images_count": len(record["assets"]),
        "image_count": len(images), "target_images": target, "quota_met": len(images) >= target,
        "shortfall_reason": None if len(images) >= target else "insufficient_reviewed_images",
        "trend_evidence": [], "key_features": "", "keywords": [],
        "claims_verified": False, "provenance_dir": str((directory / "raw").resolve()),
        "review": approval, "warnings": ["Seller description is not independently verified."],
    }
    return product, record


def export_run(run_dir: Path) -> dict:
    run_dir = run_dir.resolve()
    state = read_json(run_dir / "state.json")
    products, issues, resources = [], [], []
    for key, item in state["items"].items():
        if item["status"] not in ("imported", "reviewed"):
            issues.append({"item_id": key, "status": item["status"], "reason": item.get("reason")})
            continue
        try:
            product, record = _product(run_dir, key, item, state["target_images"])
            resource_images = [{**asset, "local_path": Path(asset["absolute_path"]).relative_to(run_dir).as_posix()}
                               for asset in (product["images"] or record["assets"])]
            resources.append({**product,
                              "images": resource_images,
                              "image_count": len(product["images"] or record["assets"]),
                              "resource_status": "acquired" if record["assets"] or product["videos"] else "empty",
                              "review_status": "reviewed" if product["review"] else "needs_visual_review"})
            products.append(product)
            directory = Path(item["import_dir"])
            machine = directory / "machine"
            atomic_json(machine / "spaiauto-product.json", product)
            atomic_json(machine / "media-manifest.json", {
                "assets": [{**asset, "selected": asset["asset_id"] in
                            {a["asset_id"] for a in product["images"]}} for asset in record["assets"]],
            })
            reports = directory / "reports"
            reports.mkdir(exist_ok=True)
            (reports / "product.md").write_text(
                f"# {product['name']}\n\nStatus: {product['status']}\n\n"
                f"Source: {product['source_url']}\n\nCaptured: {product['captured_at']}\n\n"
                f"Price as displayed: {product['price_display']}\n\n"
                f"## Seller description (unverified)\n\n{product['description']}\n\n"
                f"## Variations as displayed\n\n{product['variations_text']}\n",
                encoding="utf-8")
            (reports / "provenance.md").write_text(
                f"# Provenance\n\nSource product: {product['source_url']}\n\n"
                f"Acquisition tool: {SPAIAUTO_URL}\n\nCaptured: {product['captured_at']}\n\n"
                f"Raw directory: {product['provenance_dir']}\n\n"
                "Hashes: raw/checksums.json. Seller claims are not independently verified.\n",
                encoding="utf-8")
            (directory / "README.md").write_text(
                f"# {product['name']}\n\nStatus: {product['status']}\n\n"
                f"Candidates: {product['candidate_images_count']}; reviewed selected: {product['image_count']}\n\n"
                f"Target: {product['target_images']}; quota_met: {product['quota_met']}\n\n"
                "[Product report](reports/product.md) | [Image contact sheet](contact-sheet.jpg)\n\n"
                "Original capture, ZIP and screenshot are in raw/. No canonical MCP research was generated.\n",
                encoding="utf-8")
        except (ValueError, OSError, KeyError, TypeError) as error:
            issues.append({"item_id": key, "status": "integrity_error", "reason": str(error)})
    payload = {"version": "2.0", "is_canonical": False, "acquisition_tool": "spaiauto",
               "date": state["date"], "created_at": now(), "products": products,
               "total_products": len(products), "issues": issues}
    atomic_json(run_dir / "products.json", payload)
    atomic_json(run_dir / "resources.json", {"schema_version": "1.0.0", "kind": "pi_resources",
                "created_at": now(), "products": resources, "total_products": len(resources), "issues": issues})
    lines = ["# Spaiauto product batch", "", f"Date: {state['date']}", "",
             "| Product | Status | Candidates | Selected | Quota met |", "|---|---|---|---|---|"]
    for product in products:
        name = product["name"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {name} | {product['status']} | {product['candidate_images_count']} | "
                     f"{product['image_count']} | {product['quota_met']} |")
    lines.extend(["", "## Pending, skipped or failed", ""])
    lines.extend(f"- {issue['item_id']}: {issue['status']} — {issue.get('reason') or ''}" for issue in issues)
    lines.extend(["", "Only explicitly reviewed images can be published. Claims remain unverified.", ""])
    (run_dir / "README.md").write_text("\n".join(lines), encoding="utf-8")
    _save(run_dir, state, "export", products=len(products), issues=len(issues))
    return payload


def publish(run_dir: Path, inbox_root: Path, batch_id: str | None = None,
            eligible_only: bool = False, *, row_id: str | None = None) -> Path:
    run_dir = run_dir.resolve()
    if (run_dir / "intake.json").is_file():
        intake = read_json(run_dir / "intake.json")
        if intake.get("mode") == "pi_selection":
            return _publish_root_intake(run_dir, inbox_root, batch_id, eligible_only, row_id=row_id)
    if row_id is not None:
        raise ValueError("row_id is only supported in pi_selection mode.")
    # Reuse only the legacy key functions; do not change or execute its runner.
    from scripts.publish_exchange import compute_canonical_product_key, compute_product_key

    payload = export_run(run_dir)
    ready = [p for p in payload["products"] if p["status"] == "reviewed" and p["images"]]
    if not ready or (not eligible_only and (payload["issues"] or len(ready) != len(payload["products"]))):
        raise ValueError("Batch has pending/failed products; review them or explicitly use --eligible-only.")
    batch_id = batch_id or f"spaiauto_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}", batch_id):
        raise ValueError("Invalid batch_id.")
    root = inbox_root.resolve()
    if root == run_dir.resolve() or root.is_relative_to(run_dir.resolve()):
        raise ValueError("Exchange destination must be outside the source run.")
    day = root / payload["date"]
    final = day / batch_id
    if final.exists():
        raise ValueError("Exchange batch already exists; never overwrite an existing batch.")
    staging = day / f".tmp_{batch_id}_{uuid.uuid4().hex[:8]}"
    staging.mkdir(parents=True, exist_ok=False)
    products = []
    keys: set[str] = set()
    for priority, product in enumerate(ready, start=1):
        brand = str(product.get("brand") or "").strip()
        model = str(product.get("model") or "").strip()
        has_sku = bool(brand and model)
        if has_sku:
            identity_mode = "sku"
            canonical = compute_canonical_product_key(brand, model, product.get("variant"))
        else:
            identity_mode = "source_product"
            canonical = f"source:{product['id']}"
        if canonical in keys:
            raise ValueError("Multiple listings resolve to the same identity; split or reconcile before publishing.")
        keys.add(canonical)
        pkey = compute_product_key(canonical)
        images = []
        for index, asset in enumerate(product["images"], start=1):
            source = Path(asset["absolute_path"])
            relative = Path("products") / canonical.replace(":", "_") / "images" / f"image_{index}{source.suffix}"
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            if digest(destination) != asset["sha256"]:
                raise ValueError("Exchange copy checksum mismatch.")
            images.append({k: asset.get(k) for k in (
                "asset_id", "original_url", "width", "height", "format", "sha256", "match_score", "match_method")})
            images[-1]["relative_path"] = relative.as_posix()
            if not images[-1]["original_url"]:
                images[-1].pop("original_url")
        provenance_relative = Path("products") / canonical.replace(":", "_") / "provenance.json"
        atomic_json(staging / provenance_relative, {
            "source_url": product["source_url"], "captured_at": product["captured_at"],
            "acquisition_tool": "spaiauto", "review": product["review"],
            "archive_sha256": product["archive_sha256"], "claims_verified": False,
        })
        source_snapshot = {
            "canonical_product_id": canonical,
            "product": {
                "id": str(product["id"]),
                "canonical_key": canonical,
                "images": [{"asset_id": img["asset_id"]} for img in images],
            }
        }
        products.append({
            **{k: product[k] for k in ("name", "brand", "model", "variant", "category", "description",
                "price_display", "price_min", "price_max", "currency", "trend_evidence", "key_features", "keywords",
                "target_images", "quota_met", "shortfall_reason", "claims_verified")},
            "priority": priority, "canonical_product_key": canonical, "product_key": pkey,
            "images": images, "source_product_id": product["id"], "source_url": product["source_url"],
            "selection_reason": "operator_reviewed_spaiauto_capture", "hot_reason": "",
            "provenance_path": provenance_relative.as_posix(),
            "identity_mode": identity_mode, "source_snapshot": source_snapshot,
        })
    atomic_json(staging / "manifest.json", {
        "schema_version": "1.0.0", "batch_id": batch_id, "production_date": payload["date"],
        "created_at": now(), "acquisition_tool": "spaiauto", "products": products,
        "excluded_item_ids": [key for key in read_json(run_dir / "state.json")["items"]
                              if key not in {p["id"] for p in ready}],
    })
    staging.rename(final)
    (final / "READY").write_text(f"READY at {now()}\n", encoding="utf-8")
    state = read_json(run_dir / "state.json")
    _save(run_dir, state, "publish", output=str(final), products=len(products))
    return final


def _publish_root_intake(run_dir: Path, inbox_root: Path, batch_id: str | None,
                         eligible_only: bool, *, row_id: str | None = None) -> Path:
    """Aggregate reviewed child captures into one immutable Exchange run batch."""
    from scripts.publish_exchange import compute_canonical_product_key, compute_product_key

    state = read_json(run_dir / "intake.json")
    original = (run_dir / state["input_file"]).resolve()
    if not original.is_relative_to(run_dir) or digest(original) != state.get("input_sha256"):
        raise ValueError("Root selected CSV changed; publication is blocked.")
    if state.get("selection_csv_sha256") != state.get("input_sha256"):
        raise ValueError("Selection SHA binding does not match root intake input.")
    handoff_path = Path(state.get("selection_handoff", "")).resolve()
    if not handoff_path.is_file():
        raise ValueError("Root intake selection handoff is missing; publication requires the immutable source snapshot.")
    if state.get("selection_handoff_sha256") != digest(handoff_path):
        raise ValueError("Root selection handoff changed after PI intake.")
    from .intake import _validate_handoff
    contract = _validate_handoff(handoff_path.parent / read_json(handoff_path).get("csv_file", ""), handoff_path)
    if contract["handoff"]["csv_sha256"] != state["selection_csv_sha256"]:
        raise ValueError("Root intake no longer matches its selection handoff SHA.")
    if len(contract["csv_rows"]) != len(state.get("rows", [])):
        raise ValueError("Root intake row count differs from its selection snapshot.")
    for selected, row in zip(contract["csv_rows"], state["rows"]):
        if (selected["candidate_id"] != row.get("candidate_id") or selected["source_url"] != row.get("source_url")
                or selected["product_name"] != row.get("name")
                or selected["selection_rank"] != str(row.get("input_metadata", {}).get("selection_rank", ""))
                or any(selected[field] != str(row.get("input_metadata", {}).get(field, ""))
                       for field in ("category","brand_hint","requested_variant","price_hint_vnd",
                                     "selection_reason","video_concept","script_draft","claims_to_verify",
                                     "source_refs_json","observed_at"))):
            raise ValueError("Root intake row binding differs from its selected CSV.")
    rows = state.get("rows", [])
    if not rows:
        raise ValueError("Root intake has no selected rows.")

    if not state.get("batch_id") or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}", str(state["batch_id"])):
        raise ValueError("Invalid source selection batch_id.")

    if row_id is not None:
        if not isinstance(row_id, str) or not re.fullmatch(r"row_\d{4,}", row_id):
            raise ValueError(f"Invalid row_id format: {row_id}")
        target_row = next((r for r in rows if r.get("row_id") == row_id), None)
        if target_row is None:
            raise ValueError(f"Unrecognized row_id: {row_id}")
        if target_row.get("status") != "completed":
            raise ValueError(f"Row {row_id} is not completed (status={target_row.get('status')}).")
        expected_batch = f"run_{state['batch_id']}_{row_id}"
        prefix_msg = "run_<selection_batch_id>_<row_id>"
        candidate_rows = [target_row]
        publication_state = target_row
    else:
        expected_batch = f"run_{state['batch_id']}"
        prefix_msg = "run_<selection_batch_id>"
        candidate_rows = rows
        publication_state = state

    effective = batch_id or expected_batch
    if effective != expected_batch:
        raise ValueError(f"Root intake batch_id is bound; publication prefix must be {prefix_msg}.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}", effective):
        raise ValueError("Invalid batch_id.")

    eligible: list[tuple[dict, dict, dict, Path, dict]] = []
    outcomes = []
    seen: set[str] = set()
    bindings = []
    for row in candidate_rows:
        outcome = {"row_id": row["row_id"], "candidate_id": row.get("candidate_id"),
                   "input_status": row.get("status"), "status": "needs_review",
                   "reason": row.get("reason") or "Row is not eligible for Exchange publication."}
        try:
            child = Path(row["acquisition_run"]).resolve()
            if not child.is_relative_to(run_dir):
                raise ValueError("Child acquisition run must remain within the root intake.")
            child_state = read_json(child / "state.json")
            payload = export_run(child)
            if payload["issues"] or len(payload["products"]) != 1:
                raise ValueError("Child acquisition is incomplete or has multiple product outcomes.")
            product = payload["products"][0]
            if row.get("status") != "completed" or product.get("status") != "reviewed" or not product.get("images"):
                raise ValueError("Root row and child visual review are not complete.")
            brand = str(product.get("brand") or "").strip()
            model = str(product.get("model") or "").strip()
            has_sku = bool(brand and model)
            if has_sku:
                identity_mode = "sku"
                canonical = compute_canonical_product_key(brand, model, product.get("variant"))
            else:
                identity_mode = "source_product"
                stable_id = str(row.get("candidate_id") or product.get("id") or row["row_id"]).strip()
                canonical = f"source:{stable_id}"
            if canonical in seen:
                raise ValueError("Two selected rows resolve to the same canonical identity.")
            seen.add(canonical)
            review_path = Path(product["provenance_dir"]).parent / "review.json"
            record_path = Path(product["provenance_dir"]).parent / "import.json"
            bindings.append({"candidate_id": row.get("candidate_id"), "row_id": row["row_id"],
                "import_sha256": digest(record_path),
                "review_sha256": digest(review_path), "image_sha256": [img["sha256"] for img in product["images"]],
                "canonical_product_key": canonical})
            outcome.update(status="eligible", reason=None, canonical_product_key=canonical)
            eligible.append((row, product, child_state, child, {"canonical": canonical, "product_key": compute_product_key(canonical), "identity_mode": identity_mode}))
        except (ValueError, OSError, KeyError, TypeError) as exc:
            outcome["reason"] = str(exc)
        outcomes.append(outcome)

    if not eligible:
        raise ValueError("No eligible reviewed products; no Exchange READY batch was created.")
    if len(eligible) != len(candidate_rows) and not eligible_only:
        raise ValueError("Root intake has blocked rows; explicitly use --eligible-only for a partial publication.")
    binding = {"selection_csv_sha256": state["selection_csv_sha256"], "rows": bindings,
               "eligible_candidate_ids": [item[0].get("candidate_id") for item in eligible],
               "outcomes_sha256": hashlib.sha256(json.dumps(outcomes, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
    binding_sha = hashlib.sha256(json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    intent = publication_state.get("publish_intent")
    if intent and (intent.get("binding_sha256") != binding_sha or intent.get("eligible_only") != eligible_only):
        raise ValueError("Root publication inputs changed after intent was pinned; do not overwrite the batch.")
    if not intent:
        intent = {"batch_id": effective, "created_at": now(), "eligible_only": eligible_only,
                  "binding": binding, "binding_sha256": binding_sha}
        publication_state["publish_intent"] = intent
        atomic_json(run_dir / "intake.json", state)
    elif batch_id and batch_id != intent["batch_id"]:
        raise ValueError("Publication batch ID differs from the pinned intent.")

    inbox = inbox_root.resolve()
    if inbox == run_dir or inbox.is_relative_to(run_dir):
        raise ValueError("Exchange destination must be outside the source intake.")
    day = inbox / str(state.get("date") or bangkok_date())
    final = day / effective
    if final.exists():
        manifest_path = final / "manifest.json"
        if not manifest_path.is_file():
            raise ValueError("Existing Exchange directory is incomplete and cannot be overwritten.")
        manifest = read_json(manifest_path)
        if manifest.get("source_binding_sha256") != binding_sha:
            raise ValueError("Existing Exchange batch binding differs; immutable publication conflict.")
        outcomes_path = final / "outcomes.json"
        if not outcomes_path.is_file() or digest(outcomes_path) != manifest.get("outcomes_sha256"):
            raise ValueError("Existing Exchange outcomes changed; immutable publication conflict.")
        receipt = publication_state.get("publication_receipt")
        if receipt and receipt.get("manifest_sha256") != digest(manifest_path):
            raise ValueError("Published Exchange manifest changed after receipt; immutable publication conflict.")
        for product in manifest.get("products", []):
            for image in product.get("images", []):
                path = final / image["relative_path"]
                if not path.is_file() or digest(path) != image.get("sha256"):
                    raise ValueError("Existing Exchange image revision changed; immutable publication conflict.")
        if not (final / "READY").is_file():
            (final / "READY").write_text(f"READY {intent['created_at']}\n", encoding="utf-8")
        publication_state["publication_receipt"] = {"path": str(final), "manifest_sha256": digest(manifest_path), "binding_sha256": binding_sha}
        atomic_json(run_dir / "intake.json", state)
        return final

    day.mkdir(parents=True, exist_ok=True)
    staging = day / f".tmp_{effective}"
    if staging.is_symlink() or (staging.exists() and not staging.is_dir()):
        raise ValueError("Exchange staging path is not a safe directory.")
    staging.mkdir(exist_ok=True)
    out_products = []
    for priority, (row, product, _child_state, child, identity) in enumerate(eligible, 1):
        canonical = identity["canonical"]
        image_out = []
        for index, asset in enumerate(product["images"], 1):
            src = Path(asset["absolute_path"])
            rel = Path("products") / canonical.replace(":", "_") / "images" / f"image_{index}{src.suffix}"
            dest = _staged_file(staging, rel); shutil.copy2(src, dest)
            if digest(dest) != asset["sha256"]: raise ValueError("Exchange image checksum mismatch.")
            image_out.append({k: asset.get(k) for k in ("asset_id", "original_url", "width", "height", "format", "sha256", "match_score", "match_method")})
            image_out[-1]["relative_path"] = rel.as_posix()
            if not image_out[-1]["original_url"]:
                image_out[-1].pop("original_url")
        provenance_rel = Path("products") / canonical.replace(":", "_") / "provenance.json"
        atomic_json(_staged_file(staging,provenance_rel), {"source_url": product["source_url"], "captured_at": product["captured_at"],
            "acquisition_tool":"spaiauto", "review":product["review"], "claims_verified":False,
            "archive_sha256": product["archive_sha256"],
            "candidate_id":row.get("candidate_id"), "source_refs_json":row.get("input_metadata",{}).get("source_refs_json")})
        creative = {"kind":"creative-input-v1", "candidate_id":row.get("candidate_id"),
            "csv_sha256":state["selection_csv_sha256"],
            **{k:row.get("input_metadata",{}).get(k, "") for k in ("video_concept","script_draft","claims_to_verify","source_refs_json")}}
        source_snapshot = {
            "canonical_product_id": canonical,
            "product": {
                "id": str(product["id"]),
                "canonical_key": canonical,
                "images": [{"asset_id": img["asset_id"]} for img in image_out],
            }
        }
        out_products.append({**{k:product[k] for k in ("name","brand","model","variant","category","description","price_display","price_min","price_max","currency","trend_evidence","key_features","keywords","target_images","quota_met","shortfall_reason","claims_verified")},
            "priority":priority,"canonical_product_key":canonical,"product_key":identity["product_key"],"images":image_out,
            "source_product_id":product["id"],"source_url":product["source_url"],"selection_reason":"operator_reviewed_spaiauto_capture",
            "hot_reason":"","provenance_path":provenance_rel.as_posix(),"candidate_id":row.get("candidate_id"),
            "source_refs_json":row.get("input_metadata",{}).get("source_refs_json"),"creative_input":creative,
            "seller_description":product.get("description", ""),
            "identity_mode":identity.get("identity_mode", "sku"),"source_snapshot":source_snapshot})
    outcomes_path=_staged_file(staging,Path("outcomes.json"))
    atomic_json(outcomes_path, {"schema_version":"1.0.0", "batch_id":effective, "rows":outcomes,
        "counts":{status:sum(x["status"]==status for x in outcomes) for status in sorted({x["status"] for x in outcomes})}})
    manifest_path=_staged_file(staging,Path("manifest.json"))
    atomic_json(manifest_path, {"schema_version":"1.0.0", "batch_id":effective,
        "production_date":str(state.get("date") or bangkok_date()), "created_at":intent["created_at"],
        "acquisition_tool":"spaiauto", "source_selection_batch_id":state["batch_id"],
        "selection_csv_sha256":state["selection_csv_sha256"], "source_binding_sha256":binding_sha,
        "outcomes_sha256":digest(outcomes_path),
        "eligible_only":eligible_only,"products":out_products,
        "excluded_item_ids":[r["row_id"] for r in candidate_rows if r["row_id"] not in {x[0]["row_id"] for x in eligible}]})
    staged_sha = digest(manifest_path)
    staging.rename(final)
    (final / "READY").write_text(f"READY {intent['created_at']}\n", encoding="utf-8")
    publication_state["publication_receipt"] = {"path":str(final),"manifest_sha256":staged_sha,"binding_sha256":binding_sha}
    atomic_json(run_dir / "intake.json", state)
    return final


def _staged_file(staging: Path, relative: Path) -> Path:
    target=staging/relative
    target.parent.mkdir(parents=True,exist_ok=True)
    root=staging.resolve(); parent=target.parent.resolve()
    if not parent.is_relative_to(root) or target.is_symlink():
        raise ValueError("Exchange staging path escapes its batch directory.")
    return target

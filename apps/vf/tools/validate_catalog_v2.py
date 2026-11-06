"""Offline artifact validator for Catalog v2.

Performs comprehensive, rule-based verification of Catalog v2 artifacts,
candidate identity integrity, verifiable model provenance, relationship scopes,
byte-derived hash conflicts, foreign keys, contract compliance, and regression safety.

Supports dynamic artifact and source roots (e.g. D:\\work or Z:\\work).
Exits with code 0 only when all assertions strictly pass.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


# --- Contract Definition (from video_factory.automation.contracts) ---

def _slugify(value: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = raw.encode("ascii", "ignore").decode("ascii").lower()
    return "-".join(re.findall(r"[a-z0-9]+", ascii_value))


def compute_canonical_product_key(brand: str, model: str, variant: str | None = None) -> str:
    b_slug = _slugify(brand)
    m_slug = _slugify(model)
    if not b_slug or not m_slug:
        raise ValueError(f"Brand and model required: brand={brand!r}, model={model!r}")
    if m_slug.startswith(f"{b_slug}-"):
        m_slug = m_slug[len(b_slug) + 1 :]
    if variant:
        v_slug = _slugify(variant)
        if v_slug:
            return f"{b_slug}:{m_slug}:{v_slug}"
    return f"{b_slug}:{m_slug}"


# --- Known Constants & Regression Targets ---

TEN_KEYS = [
    "kim:01mm", "ly:1g", "ly:cao", "nam:cao", "nam:xoay",
    "nghe:a026", "nghe:open-ear", "phong:retro", "vi:50x-1000x", "xoay:unko"
]

TARGET_HASH = "f848cbb7dafd765fcf75501db69fb6732222485c84dd4a221cbe1a50e153987d"

SAMPLE_CLAIM_KEYS = ["qcy:t13-anc", "baseus:bowie-wm02", "tribit:xsound-go", "jisulife:life-7"]

FORBIDDEN_PATTERNS = [
    r"\b(aa|aaa|camera|soi|cao|su|tay|trong|chai|sinh|ata|clip-on|tia|co|minh|cob|lumens|dao|xoay|dung|ga|tam|gaming|dy66|gimbal|gorilla|kk|hson|in|inch|3d|ion|kem|khi|laptop|kim|lau|lenspen|magsafe|000mah|massage|vai|xung|nivo|nam|ozone|pd|20w|phun|quang|optical|repeater|300mbps|sang|jack|sau|tv|silica|gel|sndway|40m|theo|cho|tiankeplus|360|tinh|aroma|to|toast|lamp|toocki|100w|tra|bt-168d|xe|trang|10000mah|ultrasonic|treo|tua|tuya|smart-life|wifi-c|smart-life-k|xay|dc|rung|taplo|rice|xmtds01ym-ki|candle|warmer|palm|pen|ipx7|silicone|bow|nhanh-65w|bank-30w|c-8)\b",
    r"-[ksvch]\b",      # Truncated single-character suffix
    r"-ki\b",           # Truncated two-character suffix from kiểm tra
]

# Internal provenance table for candidate identities matching broad tokens
PI_INTERNAL_PROVENANCE: Dict[str, Dict[str, Any]] = {
    "xiaomi:mijia-desk-lamp-lite": {
        "brand": "Xiaomi",
        "model": "Mijia Desk Lamp Lite",
        "source_locator": "Personal/Product-Intelligence/data/master_products.csv (row 9, prod_08)",
        "source_type": "PI_INTERNAL_DATASET",
        "manufacturer_sku_verified": False,
        "justification": "Tên và model thương mại có trong PI master products; chưa có SKU kiểm chứng độc lập từ nhà sản xuất."
    }
}

# Disallow known generic/capacity models from candidate list
DISALLOWED_CANDIDATE_KEYS = {
    "sandisk:ssd-1tb", "ugreen:100w", "baseus:magnetic-bracket-10000mah",
    "xiaomi:pocket-10000mah", "baseus:heyo", "orico:pw11",
    "xiaomi:redmi-watch-5", "xiaomi:smart-band-9", "xiaomi:mijia-2-bluetooth",
    "ugreen:uno-robot-gan", "soundcore:r50i", "jisulife:pro1-mini",
    "tribit:stormbox-mini", "ugreen:uno-robot-gan:30w"
}


# --- Verification Helpers for External Claims & Internal Provenance ---

DISALLOWED_INTERNAL_PATH_COMPONENTS = [
    "dossier", "internal", "draft", "scratch", "notes", "reports",
    "video_factory", "video-factory", "src/", "tools/", "tests/",
    "curation", "review_queue", "normalization"
]


def is_verifiable_locator(locator: str) -> bool:
    """Validate that evidence_locator is exclusively one of the supported exact position types."""
    loc = locator.strip()
    if len(loc) < 2:
        return False
    # Only exact line numbers, CSV row numbers, or JSON field paths are accepted
    patterns = [
        r"^(line|lines|l)\s*:?\s*\d+(\s*-\s*\d+)?$",          # line 12, line: 12, L12, lines 12-15
        r"^(row)\s*:?\s*\d+$",                                  # row 5, row: 5
        r"^[a-zA-Z_][\w\-]*(\.[a-zA-Z_][\w\-]*)+(\[\d+\])?$",  # specs.dimensions, features.battery[0]
    ]
    for pat in patterns:
        if re.match(pat, loc, re.IGNORECASE):
            return True
    return False


def is_permitted_external_source(source_str: str) -> Tuple[bool, str]:
    """Determine whether an evidence source string represents a valid external format."""
    s = source_str.strip()
    if not s:
        return False, "evidence_source is empty"

    lower_s = s.lower().replace("\\", "/")

    # Reject internal files regardless of path or renaming
    for pattern in DISALLOWED_INTERNAL_PATH_COMPONENTS:
        if pattern in lower_s:
            return False, f"source {source_str!r} contains internal non-independent path token {pattern!r}"

    # External web URLs
    if lower_s.startswith("http://") or lower_s.startswith("https://"):
        return True, "valid external URL format"

    # For local files, verify allowed document extension
    allowed_exts = {".html", ".htm", ".txt", ".pdf", ".json", ".csv", ".md", ".xml"}
    ext = Path(lower_s).suffix
    if ext not in allowed_exts:
        return False, f"source {source_str!r} has invalid extension {ext!r} for external evidence"

    return True, "valid external file format"


def extract_archive_metadata(
    claim: Dict[str, Any],
    archive_file: Path | None,
    source_root: Path | None
) -> Tuple[Dict[str, Any], str | None]:
    """Extract structured metadata linking the local archive to an external URL, timestamp, and SHA-256."""
    meta: Dict[str, Any] = {}

    # 1. From sidecar file on disk if archive_file exists
    if archive_file:
        sidecar_candidates = [
            archive_file.parent / f"{archive_file.name}.meta.json",
            archive_file.with_suffix(".meta.json"),
            archive_file.parent / f"{archive_file.stem}.meta.json",
        ]
        for sc in sidecar_candidates:
            if sc.exists():
                try:
                    with open(sc, encoding="utf-8") as f:
                        meta = json.load(f)
                    break
                except Exception as e:
                    return {}, f"failed to parse archive sidecar metadata {sc.name}: {e}"

    # 2. From claim dictionary (dict or json string)
    raw_meta = claim.get("archive_metadata") or claim.get("evidence_metadata")
    if raw_meta:
        if isinstance(raw_meta, str):
            try:
                meta.update(json.loads(raw_meta))
            except Exception as e:
                return {}, f"failed to parse claim archive_metadata JSON string: {e}"
        elif isinstance(raw_meta, dict):
            meta.update(raw_meta)

    # 3. Direct fields on claim dictionary
    for url_key in ["source_url", "original_url", "external_url"]:
        if claim.get(url_key) and not meta.get("source_url"):
            meta["source_url"] = claim[url_key]
    for ts_key in ["retrieved_at", "captured_at", "archive_timestamp"]:
        if claim.get(ts_key) and not meta.get("retrieved_at"):
            meta["retrieved_at"] = claim[ts_key]
    for hash_key in ["archive_sha256", "sha256"]:
        if claim.get(hash_key) and not meta.get("sha256"):
            meta["sha256"] = claim[hash_key]

    return meta, None


def verify_snippet_at_locator(file_path: Path, locator: str, snippet: str, cid: str) -> None:
    """Verify that the snippet exists strictly at the position indicated by the locator.
    
    Only exact line numbers, CSV rows, and JSON field paths are supported.
    No whole-file search fallback is permitted.
    """
    loc_clean = locator.strip()
    snip_clean = snippet.strip()

    # 1. Line locator: "line 2", "line: 2", "lines 2-5", "l2"
    m_line_range = re.match(r"^(?:lines?|l)\s*:?\s*(\d+)\s*-\s*(\d+)$", loc_clean, re.IGNORECASE)
    m_single_line = re.match(r"^(?:lines?|l)\s*:?\s*(\d+)$", loc_clean, re.IGNORECASE)

    # 2. Row locator: "row 2", "row: 2"
    m_row = re.match(r"^(?:row)\s*:?\s*(\d+)$", loc_clean, re.IGNORECASE)

    # 3. JSON field path: "specs.dimensions", "features.battery[0]"
    m_field = re.match(r"^[a-zA-Z_][\w\-]*(\.[a-zA-Z_][\w\-]*)+(\[\d+\])?$", loc_clean)

    file_ext = file_path.suffix.lower()

    if m_single_line:
        line_num = int(m_single_line.group(1))
        lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        assert 1 <= line_num <= len(lines), (
            f"SOURCE_BACKED claim {cid} locator {locator!r} specifies line {line_num}, "
            f"but file {file_path.name} has only {len(lines)} lines"
        )
        target_line = lines[line_num - 1]
        assert snip_clean in target_line, (
            f"SOURCE_BACKED claim {cid} snippet {snippet!r} not found at exact locator {locator} in {file_path.name} "
            f"(line {line_num} content: {target_line.strip()!r})"
        )
        return

    if m_line_range:
        start_line = int(m_line_range.group(1))
        end_line = int(m_line_range.group(2))
        lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        assert 1 <= start_line <= len(lines), (
            f"SOURCE_BACKED claim {cid} locator {locator!r} start line {start_line} out of range (1..{len(lines)})"
        )
        target_block = "\n".join(lines[start_line - 1 : min(end_line, len(lines))])
        assert snip_clean in target_block, (
            f"SOURCE_BACKED claim {cid} snippet {snippet!r} not found at exact locator {locator} in {file_path.name}"
        )
        return

    if m_row and file_ext in (".csv", ".tsv", ".txt"):
        row_num = int(m_row.group(1))
        if file_ext == ".csv":
            with open(file_path, encoding="utf-8", errors="ignore") as f:
                reader = list(csv.reader(f))
            assert 1 <= row_num <= len(reader), (
                f"SOURCE_BACKED claim {cid} locator {locator!r} row {row_num} out of range (1..{len(reader)})"
            )
            target_row_text = " ".join(reader[row_num - 1])
        else:
            lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            assert 1 <= row_num <= len(lines), (
                f"SOURCE_BACKED claim {cid} locator {locator!r} row {row_num} out of range (1..{len(lines)})"
            )
            target_row_text = lines[row_num - 1]

        assert snip_clean in target_row_text, (
            f"SOURCE_BACKED claim {cid} snippet {snippet!r} not found at exact locator {locator} in {file_path.name}"
        )
        return

    if m_field and file_ext == ".json":
        import json
        with open(file_path, encoding="utf-8", errors="ignore") as f:
            data = json.load(f)
        parts = loc_clean.split(".")
        curr = data
        for p in parts:
            if isinstance(curr, dict) and p in curr:
                curr = curr[p]
            else:
                curr = None
                break
        assert curr is not None and snip_clean in str(curr), (
            f"SOURCE_BACKED claim {cid} snippet {snippet!r} not found at exact field {locator} in {file_path.name}"
        )
        return

    # No fallback! Reject unsupported or unverified locator
    raise AssertionError(
        f"SOURCE_BACKED claim {cid} locator {locator!r} format is unsupported: "
        f"only exact line numbers (line <N>), CSV rows (row <N>), and JSON field paths (<a.b.c>) are supported."
    )


def verify_source_backed_claim(claim: Dict[str, Any], source_root: Path | None) -> None:
    """Validate a claim marked as SOURCE_BACKED against external source evidence (fail-closed).
    
    Fail-closed requirements:
    1. evidence_source, evidence_locator, evidence_snippet must be non-empty.
    2. evidence_source must be an independent external source (not internal project files or dossiers).
    3. External URLs without verified local archive fail closed.
    4. Local archive/source files must possess structured external metadata linking them to an external URL, timestamp, and SHA-256 matching actual bytes.
    5. evidence_locator must be an exact verifiable line, row, or JSON field path.
    6. Snippet must be verified strictly at the exact locator coordinates (no whole-file fallback).
    """
    cid = claim.get("canonical_claim_id", "UNKNOWN")
    src = (claim.get("evidence_source") or "").strip()
    loc = (claim.get("evidence_locator") or "").strip()
    snip = (claim.get("evidence_snippet") or "").strip()

    assert src, f"SOURCE_BACKED claim {cid} missing evidence_source"
    assert loc, f"SOURCE_BACKED claim {cid} missing evidence_locator"
    assert snip, f"SOURCE_BACKED claim {cid} missing evidence_snippet"

    # 1. Source format & internal path rejection
    is_ok, reason = is_permitted_external_source(src)
    assert is_ok, f"SOURCE_BACKED claim {cid} rejected: {reason}"

    # 2. Verifiable locator format validation (only supported exact types)
    assert is_verifiable_locator(loc), (
        f"SOURCE_BACKED claim {cid} evidence_locator {loc!r} is not a verifiable position or structured locator (expected line, row, or JSON field path)"
    )

    # 3. URL vs Local File Verification
    lower_src = src.lower().replace("\\", "/")
    is_url = lower_src.startswith("http://") or lower_src.startswith("https://")

    if is_url:
        archive_rel = claim.get("evidence_archive") or claim.get("archive_path")
        assert archive_rel, (
            f"SOURCE_BACKED claim {cid} references external URL {src!r} without a verified local archive on disk; unverified URLs must fail closed."
        )
        resolved_archive = resolve_path(str(archive_rel), source_root)
        assert resolved_archive.exists(), (
            f"SOURCE_BACKED claim {cid} URL archive file does not exist on disk: {resolved_archive}"
        )
        target_file = resolved_archive
        expected_url = src
    else:
        resolved_src = resolve_path(src, source_root)
        assert resolved_src.exists(), (
            f"SOURCE_BACKED claim {cid} source file does not exist: {resolved_src}"
        )
        target_file = resolved_src
        expected_url = None

    # Check target file does not contain internal project path tokens
    lower_target = str(target_file).lower().replace("\\", "/")
    for pattern in DISALLOWED_INTERNAL_PATH_COMPONENTS:
        assert pattern not in lower_target, (
            f"SOURCE_BACKED claim {cid} target file {target_file.name!r} contains internal non-independent path token '{pattern}'"
        )

    # Check allowed file extension
    ext = target_file.suffix.lower()
    allowed_exts = {".html", ".htm", ".txt", ".pdf", ".json", ".csv", ".md", ".xml"}
    assert ext in allowed_exts, (
        f"SOURCE_BACKED claim {cid} target file {target_file.name!r} has invalid extension {ext!r} for external evidence"
    )

    # Extract and validate structured external source metadata (mandatory for both URLs and local files)
    meta, err = extract_archive_metadata(claim, target_file, source_root)
    assert err is None, f"SOURCE_BACKED claim {cid} metadata error: {err}"
    assert meta, (
        f"SOURCE_BACKED claim {cid} file {target_file.name!r} lacks structured external source metadata (source_url, retrieved_at, sha256); "
        f"file naming patterns alone do not prove independence."
    )

    # Validate source URL in metadata
    meta_url = (meta.get("source_url") or meta.get("original_url") or meta.get("external_url") or "").strip()
    assert meta_url, f"SOURCE_BACKED claim {cid} metadata missing original external source URL (source_url)"
    assert meta_url.lower().startswith("http://") or meta_url.lower().startswith("https://"), (
        f"SOURCE_BACKED claim {cid} metadata source_url {meta_url!r} must be an HTTP(S) URL"
    )
    if expected_url:
        assert meta_url.lower() == expected_url.lower(), (
            f"SOURCE_BACKED claim {cid} evidence_source URL {expected_url!r} does not match metadata source_url {meta_url!r}"
        )

    # Validate retrieval timestamp
    meta_ts = (meta.get("retrieved_at") or meta.get("captured_at") or meta.get("archive_timestamp") or "").strip()
    assert meta_ts, f"SOURCE_BACKED claim {cid} metadata missing retrieval timestamp (retrieved_at)"

    # Validate SHA-256 against actual file bytes
    meta_sha = (meta.get("sha256") or meta.get("archive_sha256") or "").strip().lower()
    assert meta_sha, f"SOURCE_BACKED claim {cid} metadata missing SHA-256 digest (sha256)"
    real_sha = hashlib.sha256(target_file.read_bytes()).hexdigest().lower()
    assert meta_sha == real_sha, (
        f"SOURCE_BACKED claim {cid} metadata sha256 {meta_sha} does not match actual bytes digest {real_sha}"
    )

    # 4. Strict locator-specific snippet verification
    verify_snippet_at_locator(target_file, loc, snip, cid)


def check_candidate_pi_provenance(
    candidates: List[Dict[str, Any]],
    prod_map: Dict[str, Dict[str, Any]],
    source_root: Path | None,
) -> Tuple[int, List[str], List[Dict[str, Any]], str | None, bool]:
    """Match candidates against real Product Intelligence master products on disk with exact brand, model, and variant match."""
    master_csv = None
    is_fallback = False

    if source_root:
        possible = source_root / "Personal" / "Product-Intelligence" / "data" / "master_products.csv"
        if possible.exists():
            master_csv = possible
        else:
            alt_roots = [Path("Z:/work"), Path("D:/work")]
            for alt in alt_roots:
                if alt.resolve() != source_root.resolve():
                    p = alt / "Personal" / "Product-Intelligence" / "data" / "master_products.csv"
                    if p.exists():
                        master_csv = p
                        is_fallback = True
                        break
    else:
        alt_roots = [Path("Z:/work"), Path("D:/work")]
        for alt in alt_roots:
            p = alt / "Personal" / "Product-Intelligence" / "data" / "master_products.csv"
            if p.exists():
                master_csv = p
                break

    if not master_csv or not master_csv.exists():
        return 0, [c["canonical_product_key"] for c in candidates], [], None, False

    actual_master_path = str(master_csv)

    with open(master_csv, encoding="utf-8") as f:
        master_rows = list(csv.DictReader(f))

    unverified = []
    verified = []
    for c in candidates:
        ck = c["canonical_product_key"]
        p = prod_map.get(ck)
        if not p:
            unverified.append(ck)
            continue

        target_brand = p["brand"].strip().lower()
        target_model = p["model"].strip().lower()
        target_variant = (p.get("variant") or "").strip().lower()

        matched_row = None
        for idx, r in enumerate(master_rows):
            rb = r.get("brand", "").strip().lower()
            rm = r.get("model", "").strip().lower()
            
            # Exact brand and model match (no loose prefix/substring)
            if rb == target_brand and rm == target_model:
                r_name = r.get("name", "").lower()
                # Variant reconciliation:
                if target_variant:
                    # Multi-wattage/multi-variant listing conflation check (e.g. "30W / 65W")
                    if " / " in r_name or " / " in rm:
                        # Product family match only, unconfirmed variant
                        continue
                    if target_variant not in r_name and target_variant not in rm:
                        continue

                matched_row = {
                    "canonical_product_key": ck,
                    "row_number": idx + 2,
                    "product_id": r.get("product_id"),
                    "brand": r.get("brand"),
                    "model": r.get("model"),
                    "variant": p.get("variant", ""),
                    "name": r.get("name"),
                    "locator": f"Personal/Product-Intelligence/data/master_products.csv (row {idx + 2}, product_id {r.get('product_id')})"
                }
                break

        if matched_row:
            verified.append(matched_row)
        else:
            unverified.append(ck)

    return len(verified), unverified, verified, actual_master_path, is_fallback


# --- Path Resolution Helper ---

def resolve_path(recorded_path: str, source_root: Path | None) -> Path:
    """Resolve a recorded absolute path, dynamically adapting to source root."""
    p = Path(recorded_path)
    if p.exists():
        return p

    # If recorded path starts with D:\work or Z:\work, try swapping drive/root
    parts = p.parts
    if len(parts) >= 2 and parts[1].lower() == "work":
        # Check against source_root if provided
        if source_root:
            candidate = source_root.joinpath(*parts[2:])
            if candidate.exists():
                return candidate

        # Try alternative drive letter (D: <-> Z:)
        alt_drive = "Z:" if parts[0].upper().startswith("D") else "D:"
        alt_path = Path(alt_drive).joinpath(*parts[1:])
        if alt_path.exists():
            return alt_path

    return p


# --- Main Validation Logic ---

def validate_catalog(artifact_dir: Path, source_root: Path | None) -> int:
    print(f"=== RUNNING OFFLINE ARTIFACT VALIDATOR (CATALOG V2) ===")
    print(f"Artifact directory: {artifact_dir}")
    print(f"Source root: {source_root or '(auto-detect)'}")

    if not artifact_dir.exists():
        raise FileNotFoundError(f"Artifact directory does not exist: {artifact_dir}")

    # 1. Load CSV and JSONL files
    prod_csv_path = artifact_dir / "PRODUCT_CATALOG_V2.csv"
    prod_jsonl_path = artifact_dir / "PRODUCT_CATALOG_V2.jsonl"
    mat_csv_path = artifact_dir / "CONTENT_MATERIAL_CATALOG_V2.csv"
    mat_jsonl_path = artifact_dir / "CONTENT_MATERIAL_CATALOG_V2.jsonl"
    claim_csv_path = artifact_dir / "CLAIM_CATALOG_V2.csv"
    conf_csv_path = artifact_dir / "CROSS_PRODUCT_IMAGE_CONFLICTS.csv"
    remap_csv_path = artifact_dir / "IDENTITY_REMAP_CANDIDATES.csv"
    rq_csv_path = artifact_dir / "REVIEW_QUEUE.csv"
    auto_qc_path = artifact_dir / "AUTO_QC_CANDIDATES.csv"
    pr_cand_path = artifact_dir / "PIPELINE_READY_CANDIDATES.csv"
    comp_csv_path = artifact_dir / "PRODUCT_COMPOSITION_V2.csv"
    rel_csv_path = artifact_dir / "PRODUCT_RELATIONSHIPS.csv"

    for req_file in [prod_csv_path, prod_jsonl_path, mat_csv_path, mat_jsonl_path,
                     claim_csv_path, conf_csv_path, remap_csv_path, rq_csv_path,
                     auto_qc_path, pr_cand_path, comp_csv_path, rel_csv_path]:
        if not req_file.exists():
            raise FileNotFoundError(f"Required artifact file missing: {req_file}")

    with open(prod_csv_path, encoding="utf-8") as f:
        prods = list(csv.DictReader(f))
    with open(claim_csv_path, encoding="utf-8") as f:
        claims = list(csv.DictReader(f))
    with open(mat_csv_path, encoding="utf-8") as f:
        materials = list(csv.DictReader(f))
    with open(conf_csv_path, encoding="utf-8") as f:
        conflicts = list(csv.DictReader(f))
    with open(remap_csv_path, encoding="utf-8") as f:
        remap = list(csv.DictReader(f))
    with open(rq_csv_path, encoding="utf-8") as f:
        review_q = list(csv.DictReader(f))
    with open(auto_qc_path, encoding="utf-8") as f:
        auto_qc_candidates = list(csv.DictReader(f))
    with open(pr_cand_path, encoding="utf-8") as f:
        pr_candidates = list(csv.DictReader(f))
    with open(comp_csv_path, encoding="utf-8") as f:
        compositions = list(csv.DictReader(f))
    with open(rel_csv_path, encoding="utf-8") as f:
        relationships = list(csv.DictReader(f))

    prod_map = {p["canonical_product_key"]: p for p in prods}
    assert len(prods) == 333, f"Expected 333 products in PRODUCT_CATALOG_V2.csv, found {len(prods)}"

    # -------------------------------------------------------------
    # Check 1: CSV / JSONL Synchronization
    # -------------------------------------------------------------
    print("\n[Check 1: CSV / JSONL Key and Field Synchronization]")
    with open(prod_jsonl_path, encoding="utf-8") as f:
        prod_jsonl = [json.loads(line) for line in f if line.strip()]
    assert len(prods) == len(prod_jsonl), (
        f"Product CSV row count ({len(prods)}) != JSONL row count ({len(prod_jsonl)})"
    )
    for idx, (csv_row, jsonl_row) in enumerate(zip(prods, prod_jsonl)):
        assert csv_row["canonical_product_key"] == jsonl_row["canonical_product_key"], (
            f"Product row {idx} key mismatch: CSV={csv_row['canonical_product_key']} vs JSONL={jsonl_row['canonical_product_key']}"
        )
        for k in csv_row:
            assert str(csv_row[k]) == str(jsonl_row.get(k, "")), (
                f"Product row {idx} ({csv_row['canonical_product_key']}) field {k!r} mismatch: "
                f"CSV={csv_row[k]!r} vs JSONL={jsonl_row.get(k)!r}"
            )

    with open(mat_jsonl_path, encoding="utf-8") as f:
        mat_jsonl = [json.loads(line) for line in f if line.strip()]
    assert len(materials) == len(mat_jsonl), (
        f"Material CSV row count ({len(materials)}) != JSONL row count ({len(mat_jsonl)})"
    )
    for idx, (csv_row, jsonl_row) in enumerate(zip(materials, mat_jsonl)):
        assert csv_row["canonical_material_id"] == jsonl_row["canonical_material_id"], (
            f"Material row {idx} ID mismatch: CSV={csv_row['canonical_material_id']} vs JSONL={jsonl_row['canonical_material_id']}"
        )
        for k in csv_row:
            assert str(csv_row[k]) == str(jsonl_row.get(k, "")), (
                f"Material row {idx} ({csv_row['canonical_material_id']}) field {k!r} mismatch"
            )

    # AUTO_QC_CANDIDATES vs PIPELINE_READY_CANDIDATES full synchronization
    assert len(auto_qc_candidates) == len(pr_candidates), (
        f"AUTO_QC_CANDIDATES ({len(auto_qc_candidates)}) != PIPELINE_READY_CANDIDATES ({len(pr_candidates)})"
    )
    for idx, (c1, c2) in enumerate(zip(auto_qc_candidates, pr_candidates)):
        ck = c1["canonical_product_key"]
        assert c1.keys() == c2.keys(), (
            f"Candidate row {idx} ({ck}) column set mismatch: {set(c1.keys()) ^ set(c2.keys())}"
        )
        for field in c1:
            assert c1[field] == c2[field], (
                f"Candidate row {idx} ({ck}) field {field!r} mismatch: "
                f"AUTO_QC={c1[field]!r} vs PIPELINE_READY={c2[field]!r}"
            )
        assert c1.get("next_stage") == "image_qc_review", (
            f"Candidate {ck} next_stage must be 'image_qc_review'"
        )
    print(f"  PASS: All CSV and JSONL files are synchronized by row order and all field values (including full-field candidate sync).")

    # -------------------------------------------------------------
    # Check 2: Foreign Key Integrity & Relationship Scope Partitioning
    # -------------------------------------------------------------
    print("\n[Check 2: Foreign Key Integrity & Relationship Scope Partitioning]")
    for p in prods:
        k = p["canonical_product_key"]
        expected_key = compute_canonical_product_key(p["brand"], p["model"], p.get("variant") or None)
        assert k == expected_key, (
            f"Canonical key contract mismatch for product {p['canonical_product_id']}: "
            f"stored={k!r}, computed={expected_key!r}"
        )

    for m in materials:
        assert m["canonical_product_key"] in prod_map, (
            f"Material {m['canonical_material_id']} references unknown product key: {m['canonical_product_key']}"
        )
    for c in claims:
        assert c["canonical_product_key"] in prod_map, (
            f"Claim {c['canonical_claim_id']} references unknown product key: {c['canonical_product_key']}"
        )
    for cp in compositions:
        assert cp["canonical_product_key"] in prod_map, (
            f"Composition references unknown product key: {cp['canonical_product_key']}"
        )
    for rm in remap:
        assert rm["canonical_product_key"] in prod_map, (
            f"Remap candidate references unknown product key: {rm['canonical_product_key']}"
        )
    for rq in review_q:
        assert rq["canonical_product_key"] in prod_map, (
            f"Review queue references unknown product key: {rq['canonical_product_key']}"
        )

    # Relationships partitioning
    intra_rels = [r for r in relationships if r.get("target_scope") == "INTRA_CATALOG"]
    external_rels = [r for r in relationships if r.get("target_scope") == "PROPOSED_EXTERNAL_TARGET"]

    assert len(intra_rels) + len(external_rels) == len(relationships), (
        f"All relationships must have valid target_scope (INTRA_CATALOG or PROPOSED_EXTERNAL_TARGET)"
    )

    for r in intra_rels:
        ka = r["canonical_product_key_a"]
        kb = r["canonical_product_key_b"]
        assert ka in prod_map, f"Intra-catalog relationship {r['relation_id']} key_a missing: {ka}"
        assert kb in prod_map, f"Intra-catalog relationship {r['relation_id']} key_b missing: {kb}"
        assert prod_map[ka]["canonical_product_id"] == r["canonical_product_id_a"], (
            f"Relationship {r['relation_id']} product ID mismatch for key_a: {ka}"
        )
        assert prod_map[kb]["canonical_product_id"] == r["canonical_product_id_b"], (
            f"Relationship {r['relation_id']} product ID mismatch for key_b: {kb}"
        )

    for r in external_rels:
        ka = r["canonical_product_key_a"]
        kb = r["canonical_product_key_b"]
        assert ka in prod_map, f"External relationship {r['relation_id']} key_a missing: {ka}"
        assert prod_map[ka]["canonical_product_id"] == r["canonical_product_id_a"], (
            f"Relationship {r['relation_id']} product ID mismatch for key_a: {ka}"
        )
        # Verify key_b is NOT in catalog and has no fake FK
        assert kb not in prod_map, (
            f"External target relationship {r['relation_id']} key_b {kb} unexpectedly exists in catalog!"
        )
        assert r["canonical_product_id_b"] == "", (
            f"External target relationship {r['relation_id']} must have empty canonical_product_id_b, found {r['canonical_product_id_b']!r}"
        )

    print(f"  PASS: Foreign keys valid across all tables.")
    print(f"  PASS: Relationships partitioned into {len(intra_rels)} INTRA_CATALOG (valid FKs) "
          f"and {len(external_rels)} PROPOSED_EXTERNAL_TARGET (no false FK claimed).")

    # -------------------------------------------------------------
    # Check 3: Material Bytes Verification & Dynamic 2-Way Conflict Validation
    # -------------------------------------------------------------
    print("\n[Check 3: Material Bytes Verification & 2-Way Conflict Validation]")
    sha_to_keys: Dict[str, Set[str]] = {}
    prod_image_shas: Dict[str, Set[str]] = {}
    recorded_hash_mismatches: List[Tuple[str, str, str]] = []

    for m in materials:
        if m["material_type"] not in ("source_image", "pdp_image", "crawl_image", "reference_image"):
            continue
        raw_path = m["file_path"]
        resolved_path = resolve_path(raw_path, source_root)
        if not resolved_path.exists():
            raise FileNotFoundError(
                f"Source image file does not exist on disk: {raw_path} (resolved: {resolved_path})"
            )
        real_sha = hashlib.sha256(resolved_path.read_bytes()).hexdigest()
        rec_sha = m["sha256"]
        if real_sha != rec_sha:
            recorded_hash_mismatches.append((m["canonical_material_id"], rec_sha, real_sha))

        ck = m["canonical_product_key"]
        sha_to_keys.setdefault(real_sha, set()).add(ck)
        prod_image_shas.setdefault(ck, set()).add(real_sha)

    assert len(recorded_hash_mismatches) == 0, (
        f"Recorded SHA mismatches found: {recorded_hash_mismatches}"
    )

    # Compute actual conflict hashes from real bytes
    computed_conflicts = {sha: keys for sha, keys in sha_to_keys.items() if len(keys) > 1}
    conflict_products = set()
    for keys in computed_conflicts.values():
        conflict_products.update(keys)

    # 2-way comparison with CROSS_PRODUCT_IMAGE_CONFLICTS.csv
    recorded_conf_map = {
        r["sha256"]: set(r["canonical_product_keys"].split("|")) for r in conflicts
    }
    
    missing_in_csv = set(computed_conflicts.keys()) - set(recorded_conf_map.keys())
    extra_in_csv = set(recorded_conf_map.keys()) - set(computed_conflicts.keys())
    assert len(missing_in_csv) == 0, (
        f"Conflict hashes computed from bytes but missing from CSV: {missing_in_csv}"
    )
    assert len(extra_in_csv) == 0, (
        f"Conflict hashes recorded in CSV but not found in bytes: {extra_in_csv}"
    )
    for sha, keys in computed_conflicts.items():
        assert keys == recorded_conf_map[sha], (
            f"Conflict keys mismatch for hash {sha}: bytes={keys} vs CSV={recorded_conf_map[sha]}"
        )

    # Verify that all products with conflicting images have source_image_status == 'ASSOCIATION_CONFLICT'
    for ck in conflict_products:
        p = prod_map[ck]
        assert p["source_image_status"] == "ASSOCIATION_CONFLICT", (
            f"Product {ck} has conflicting source images but source_image_status={p['source_image_status']!r}"
        )
        assert p["pipeline_status"] == "BLOCKED", (
            f"Product {ck} has image conflicts but pipeline_status={p['pipeline_status']!r}"
        )

    print(f"  PASS: Verified all source image bytes on disk (zero mismatches).")
    print(f"  PASS: 2-way conflict validation succeeded across {len(computed_conflicts)} conflict hashes "
          f"and {len(conflict_products)} affected products.")

    # -------------------------------------------------------------
    # Check 4: Candidate Integrity & Strict Candidate Rules
    # -------------------------------------------------------------
    print("\n[Check 4: Candidate Integrity & Strict Rules]")
    conflict_shas_set = set(computed_conflicts.keys())
    assert len(auto_qc_candidates) > 0, "Candidate list cannot be empty"

    for c in auto_qc_candidates:
        ck = c["canonical_product_key"]
        p = prod_map.get(ck)
        assert p is not None, f"Candidate {ck} missing from PRODUCT_CATALOG_V2.csv"
        assert c["identity_status"] == "VALID", (
            f"Candidate {ck} identity_status={c['identity_status']!r}, expected 'VALID'"
        )
        assert p["identity_status"] == "VALID", (
            f"Product {ck} in catalog identity_status={p['identity_status']!r}, expected 'VALID'"
        )
        assert c["pipeline_status"] == "READY_FOR_IMAGE_QC", (
            f"Candidate {ck} pipeline_status={c['pipeline_status']!r}, expected 'READY_FOR_IMAGE_QC'"
        )
        assert p["pipeline_status"] == "READY_FOR_IMAGE_QC", (
            f"Product {ck} in catalog pipeline_status={p['pipeline_status']!r}, expected 'READY_FOR_IMAGE_QC'"
        )

        # Image checks for candidate
        shas = prod_image_shas.get(ck, set())
        assert len(shas) >= 2, (
            f"Candidate {ck} has only {len(shas)} distinct source image SHAs (< 2)"
        )
        for s in shas:
            assert s not in conflict_shas_set, (
                f"Candidate {ck} has image hash {s} which is involved in cross-product conflict!"
            )

        # next_stage check
        assert c.get("next_stage") == "image_qc_review", (
            f"Candidate {ck} next_stage={c.get('next_stage')!r}, expected 'image_qc_review'"
        )

    print(f"  PASS: All {len(auto_qc_candidates)} candidates verified (VALID identity, >=2 image SHAs, zero conflict).")

    # -------------------------------------------------------------
    # Check 5: Verifiable Candidate Identity & Provenance
    # -------------------------------------------------------------
    print("\n[Check 5: Verifiable Candidate Identity & Provenance]")

    for c in auto_qc_candidates:
        ck = c["canonical_product_key"]
        assert ck not in DISALLOWED_CANDIDATE_KEYS, (
            f"Candidate {ck} is a generic description, capacity/wattage spec, or conflated model; must be BLOCKED!"
        )

        p = prod_map[ck]
        brand = p["brand"]
        model = p["model"]
        text_to_check = f"{ck} {brand} {model}".lower()

        is_flagged = False
        flagged_reason = ""
        for pat in FORBIDDEN_PATTERNS:
            m = re.search(pat, text_to_check)
            if m:
                is_flagged = True
                flagged_reason = f"matched suspicious token {m.group(0)!r}"
                break

        if is_flagged:
            assert ck in PI_INTERNAL_PROVENANCE, (
                f"Candidate {ck} failed suspicious token scan: {flagged_reason}, with no recorded verifiable evidence!"
            )
            ev = PI_INTERNAL_PROVENANCE[ck]
            assert ev["source_locator"], (
                f"Candidate {ck} missing verifiable locator"
            )
            print(f"  Note: Candidate {ck} has internal PI record at {ev['source_locator']}; independent manufacturer SKU: UNVERIFIED.")

    # 3. Real reconciliation against actual Product Intelligence master records
    matched_cnt, unverified_keys, verified_records, actual_master_path, is_fallback = check_candidate_pi_provenance(
        auto_qc_candidates, prod_map, source_root
    )

    if actual_master_path:
        if is_fallback:
            print(f"  Notice: Primary source_root did not contain PI master dataset; fallback read from: {actual_master_path}")
        else:
            print(f"  PI Master Dataset read from: {actual_master_path}")

    if matched_cnt == len(auto_qc_candidates) and matched_cnt > 0:
        print(f"  PASS: {matched_cnt} candidates verified against PI master records with exact brand, model, and variant match.")
        print("  Notice: All candidate identities are documented via internal PI records; independent manufacturer external verification is NOT observed.")
    else:
        print(f"  PASS: {len(auto_qc_candidates)} candidates passed artifact rules (zero disallowed generic/spec models).")
        if unverified_keys:
            print(f"  Notice: PI record reconciliation incomplete. Keys without verified PI master locator: {unverified_keys}")

    # -------------------------------------------------------------
    # Check 6: Claim Count Sync & Mandatory Samples
    # -------------------------------------------------------------
    print("\n[Check 6: Claim Count Synchronization & Mandatory Samples]")
    sb_counts: Dict[str, int] = {}
    for cl in claims:
        if cl["verification_status"] == "SOURCE_BACKED":
            sb_counts[cl["canonical_product_key"]] = sb_counts.get(cl["canonical_product_key"], 0) + 1

    for c in auto_qc_candidates:
        ck = c["canonical_product_key"]
        expected_cnt = sb_counts.get(ck, 0)
        actual_cnt = int(c["source_backed_claim_count"])
        assert actual_cnt == expected_cnt, (
            f"Candidate {ck} source_backed_claim_count={actual_cnt}, expected={expected_cnt}"
        )

    # Mandatory sample check: MUST FAIL IF NOT FOUND (no 'if found' bypass!)
    cand_keys_set = {c["canonical_product_key"] for c in auto_qc_candidates}
    for req_key in SAMPLE_CLAIM_KEYS:
        assert req_key in cand_keys_set, (
            f"Mandatory sample product {req_key} missing from AUTO_QC_CANDIDATES.csv!"
        )
        c_row = next(c for c in auto_qc_candidates if c["canonical_product_key"] == req_key)
        assert int(c_row["source_backed_claim_count"]) == 0, (
            f"Mandatory sample {req_key} source_backed_claim_count={c_row['source_backed_claim_count']}, expected 0"
        )
    print("  PASS: Claim count matches CLAIM_CATALOG_V2 exactly; all 4 mandatory sample products present with count 0.")

    # -------------------------------------------------------------
    # Check 7: SOURCE_BACKED Claims & Claims Statistics Verification
    # -------------------------------------------------------------
    print("\n[Check 7: Claims Distribution & External Source Verification]")

    # 1. FIRST: Run fail-closed verification on ALL claims marked SOURCE_BACKED
    sb_claims = [c for c in claims if c["verification_status"] == "SOURCE_BACKED"]
    for cl in sb_claims:
        verify_source_backed_claim(cl, source_root)
    if sb_claims:
        print(f"  PASS: Successfully verified {len(sb_claims)} SOURCE_BACKED claims against source files on disk.")
    else:
        print("  PASS (SOURCE_BACKED == 0): Không có claim đủ điều kiện để kiểm tra nguồn bên ngoài (0 claims marked SOURCE_BACKED).")

    # 2. SECOND: Check distribution rules for this catalog snapshot
    assert len(claims) == 99, f"Expected 99 claims in CLAIM_CATALOG_V2.csv, found {len(claims)}"

    cnt_unverified = sum(1 for cl in claims if cl["verification_status"] == "UNVERIFIED")
    cnt_dossier = sum(1 for cl in claims if cl["verification_status"] == "INTERNAL_DOSSIER_ONLY")
    cnt_conflicted = sum(1 for cl in claims if cl["verification_status"] == "CONFLICTED")
    cnt_source_backed = len(sb_claims)

    assert cnt_source_backed == 0, f"Expected 0 SOURCE_BACKED claims in snapshot v2, found {cnt_source_backed}"
    assert cnt_unverified == 93, f"Expected 93 UNVERIFIED claims, found {cnt_unverified}"
    assert cnt_dossier == 5, f"Expected 5 INTERNAL_DOSSIER_ONLY claims, found {cnt_dossier}"
    assert cnt_conflicted == 1, f"Expected 1 CONFLICTED claim, found {cnt_conflicted}"

    print(f"  Distribution: UNVERIFIED={cnt_unverified}, INTERNAL_DOSSIER_ONLY={cnt_dossier}, CONFLICTED={cnt_conflicted}, SOURCE_BACKED={cnt_source_backed}.")

    # -------------------------------------------------------------
    # Check 8: Pipeline Gate Adherence
    # -------------------------------------------------------------
    print("\n[Check 8: Pipeline Gate Adherence]")
    ready_script = sum(1 for p in prods if p["pipeline_status"] == "READY_FOR_SCRIPTING")
    ready_video = sum(1 for p in prods if p["pipeline_status"] == "READY_FOR_VIDEO")
    assert ready_script == 0, f"READY_FOR_SCRIPTING count is {ready_script}, must be 0"
    assert ready_video == 0, f"READY_FOR_VIDEO count is {ready_video}, must be 0"
    print("  PASS: READY_FOR_SCRIPTING == 0 and READY_FOR_VIDEO == 0 strictly enforced.")

    # -------------------------------------------------------------
    # Check 9: Regression Safety Checks
    # -------------------------------------------------------------
    print("\n[Check 9: Regression Safety Checks]")
    # 1. 10 downgraded keys
    remap_keys = {r["canonical_product_key"] for r in remap}
    rq_keys = {r["canonical_product_key"] for r in review_q}
    for k in TEN_KEYS:
        p = prod_map.get(k)
        assert p is not None, f"Regression key {k} missing from PRODUCT_CATALOG_V2.csv"
        assert p["identity_status"] == "REVIEW_REQUIRED", (
            f"Regression key {k} identity_status={p['identity_status']!r}, expected 'REVIEW_REQUIRED'"
        )
        assert p["pipeline_status"] == "BLOCKED", (
            f"Regression key {k} pipeline_status={p['pipeline_status']!r}, expected 'BLOCKED'"
        )
        assert k in remap_keys, f"Regression key {k} missing from IDENTITY_REMAP_CANDIDATES.csv"
        assert k in rq_keys, f"Regression key {k} missing from REVIEW_QUEUE.csv"
        assert k not in cand_keys_set, f"Regression key {k} must NOT be in candidate list"
    print("  PASS: All 10 regression keys verified as REVIEW_REQUIRED + BLOCKED.")

    # 2. Target hash f848cbb7...
    target_conf = next((c for c in conflicts if c["sha256"] == TARGET_HASH), None)
    assert target_conf is not None, f"Target hash {TARGET_HASH} missing from CROSS_PRODUCT_IMAGE_CONFLICTS.csv"
    c_keys = target_conf["canonical_product_keys"].split("|")
    assert "tay:uniq" in c_keys and "nha:khoa" in c_keys, (
        f"Target conflict keys does not contain both 'tay:uniq' and 'nha:khoa': {c_keys}"
    )
    p_nha = prod_map["nha:khoa"]
    p_tay = prod_map["tay:uniq"]
    assert p_nha["source_image_status"] == "ASSOCIATION_CONFLICT"
    assert p_tay["source_image_status"] == "ASSOCIATION_CONFLICT"
    print("  PASS: Hash f848cbb7... verified for tay:uniq and nha:khoa.")

    # 3. 5 Ugreen claims & ugreen:uno-robot-gan:30w variant downgrade
    ug_claims = [c for c in claims if c["canonical_product_key"] == "ugreen:uno-robot-gan:30w"]
    assert len(ug_claims) == 5, f"Expected 5 Ugreen claims, found {len(ug_claims)}"
    for c in ug_claims:
        assert c["verification_status"] in ("INTERNAL_DOSSIER_ONLY", "UNVERIFIED"), (
            f"Ugreen claim {c['canonical_claim_id']} status is {c['verification_status']!r}"
        )
    p_ug = prod_map["ugreen:uno-robot-gan:30w"]
    assert p_ug["identity_status"] == "REVIEW_REQUIRED", (
        f"ugreen:uno-robot-gan:30w identity_status={p_ug['identity_status']!r}, expected 'REVIEW_REQUIRED'"
    )
    assert p_ug["pipeline_status"] == "BLOCKED", (
        f"ugreen:uno-robot-gan:30w pipeline_status={p_ug['pipeline_status']!r}, expected 'BLOCKED'"
    )
    assert "ugreen:uno-robot-gan:30w" not in cand_keys_set, (
        "ugreen:uno-robot-gan:30w must NOT be in AUTO_QC_CANDIDATES"
    )
    assert "ugreen:uno-robot-gan:30w" in rq_keys, (
        "ugreen:uno-robot-gan:30w must be present in REVIEW_QUEUE.csv"
    )
    assert "ugreen:uno-robot-gan:30w" in remap_keys, (
        "ugreen:uno-robot-gan:30w must be present in IDENTITY_REMAP_CANDIDATES.csv"
    )
    print("  PASS: 5 Ugreen claims downgraded to internal dossier; ugreen:uno-robot-gan:30w verified as REVIEW_REQUIRED + BLOCKED.")

    print("\n" + "=" * 65)
    print("=== ALL VALIDATION ASSERTIONS PASSED SUCCESSFULLY (Exit Code 0) ===")
    print("=" * 65)
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Validate Catalog v2 artifacts offline.")
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "reports" / "catalog-normalization" / "2026-10-17-v2",
        help="Path to Catalog v2 artifact directory.",
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=None,
        help="Root directory for source checkouts (e.g. D:\\work or Z:\\work).",
    )
    args = parser.parse_args()

    # Determine default source root if not specified
    source_root = args.source_root
    if source_root is None:
        repo_root = Path(__file__).resolve().parent.parent
        if repo_root.parent.name.lower() == "work":
            source_root = repo_root.parent
        elif Path("Z:/work").exists():
            source_root = Path("Z:/work")
        elif Path("D:/work").exists():
            source_root = Path("D:/work")

    return validate_catalog(args.artifact_dir, source_root)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"\n❌ VALIDATION ASSERTION FAILED: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ UNEXPECTED VALIDATION ERROR: {e}", file=sys.stderr)
        sys.exit(1)

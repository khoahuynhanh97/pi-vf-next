"""Unit and rule verification tests for Catalog v2 offline validator.

Features:
1. Strict test isolation: ALL mutation tests run on temporary copies in a tempdir;
   reports/catalog-normalization/2026-10-17-v2/ is NEVER written to.
2. Artifact integrity verification: Baseline SHA-256 hashes of all original artifacts
   are recorded and asserted before and after tests.
3. Full-field candidate synchronization verification: AUTO_QC_CANDIDATES vs PIPELINE_READY_CANDIDATES.
4. Fail-closed SOURCE_BACKED claim verification: Comprehensive tests for valid source claims,
   missing sources, missing locators, missing snippets, dossier-only sources, nonexistent files,
   and snippet content mismatches on isolated fixtures.
5. Internal provenance rules and pipeline gate adherence.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# Add project root and tools to path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import validate_catalog_v2
from validate_catalog_v2 import validate_catalog, verify_source_backed_claim

ORIGINAL_ARTIFACT_DIR = REPO_ROOT / "reports" / "catalog-normalization" / "2026-10-17-v2"


def _hash_directory(directory: Path) -> dict[str, str]:
    """Compute SHA-256 hashes for all files in a directory."""
    hashes = {}
    for fn in sorted(os.listdir(directory)):
        fp = directory / fn
        if fp.is_file():
            hashes[fn] = hashlib.sha256(fp.read_bytes()).hexdigest()
    return hashes


class TestCatalogV2ValidatorRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Capture baseline SHA-256 hashes of the original artifact directory
        cls.baseline_hashes = _hash_directory(ORIGINAL_ARTIFACT_DIR)
        assert len(cls.baseline_hashes) > 0, "Original artifact directory is empty!"

    @classmethod
    def tearDownClass(cls):
        # Verify that original artifact directory was never modified
        current_hashes = _hash_directory(ORIGINAL_ARTIFACT_DIR)
        assert current_hashes == cls.baseline_hashes, (
            f"Original artifacts in {ORIGINAL_ARTIFACT_DIR} were modified during tests!\n"
            f"Differences: {set(current_hashes.items()) ^ set(cls.baseline_hashes.items())}"
        )

    def setUp(self):
        # Create an isolated temporary copy of all artifacts for this test
        self.temp_dir = tempfile.TemporaryDirectory()
        self.artifact_dir = Path(self.temp_dir.name)
        for fn in os.listdir(ORIGINAL_ARTIFACT_DIR):
            src_fp = ORIGINAL_ARTIFACT_DIR / fn
            if src_fp.is_file():
                shutil.copy2(src_fp, self.artifact_dir / fn)

        self.source_root = Path("Z:/work") if Path("Z:/work").exists() else Path("D:/work")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_original_artifacts_remain_unmodified_on_disk(self):
        """Original artifact directory content must remain strictly identical to baseline."""
        current_hashes = _hash_directory(ORIGINAL_ARTIFACT_DIR)
        self.assertEqual(current_hashes, self.baseline_hashes)

    def test_baseline_validation_passes(self):
        """Standard catalog artifacts must pass validation with exit code 0."""
        code = validate_catalog(self.artifact_dir, self.source_root)
        self.assertEqual(code, 0)

    def test_candidate_full_field_sync_fails_on_field_mismatch(self):
        """A field mismatch between AUTO_QC_CANDIDATES and PIPELINE_READY_CANDIDATES must fail."""
        pr_path = self.artifact_dir / "PIPELINE_READY_CANDIDATES.csv"
        with open(pr_path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        # Mutate a specific field in the second candidate
        rows[1]["product_name"] = "Different Name For Testing Mismatch"

        with open(pr_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

        with self.assertRaises(AssertionError) as ctx:
            validate_catalog(self.artifact_dir, self.source_root)
        self.assertIn("field 'product_name' mismatch", str(ctx.exception))

    def test_candidate_full_field_sync_fails_on_column_mismatch(self):
        """A column set difference between candidate files must fail."""
        pr_path = self.artifact_dir / "PIPELINE_READY_CANDIDATES.csv"
        with open(pr_path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        # Remove a column
        new_fieldnames = [k for k in rows[0].keys() if k != "next_stage"]
        mutated_rows = [{k: r[k] for k in new_fieldnames} for r in rows]

        with open(pr_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=new_fieldnames)
            w.writeheader()
            w.writerows(mutated_rows)

        with self.assertRaises(AssertionError) as ctx:
            validate_catalog(self.artifact_dir, self.source_root)
        self.assertIn("column set mismatch", str(ctx.exception))

    def test_relationship_intra_catalog_missing_key_b_raises(self):
        """An INTRA_CATALOG relationship with key_b missing from catalog must raise AssertionError."""
        rel_path = self.artifact_dir / "PRODUCT_RELATIONSHIPS.csv"
        with open(rel_path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        rows[0]["canonical_product_key_b"] = "nonexistent:model"

        with open(rel_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

        with self.assertRaises(AssertionError) as ctx:
            validate_catalog(self.artifact_dir, self.source_root)
        self.assertIn("key_b missing", str(ctx.exception))

    def test_relationship_proposed_external_target_with_fake_fk_raises(self):
        """A PROPOSED_EXTERNAL_TARGET relationship claiming a catalog ID must fail."""
        rel_path = self.artifact_dir / "PRODUCT_RELATIONSHIPS.csv"
        with open(rel_path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        mc5 = next(r for r in rows if r["relation_id"] == "MC_005")
        mc5["canonical_product_id_b"] = "prod_fake_123"

        with open(rel_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

        with self.assertRaises(AssertionError) as ctx:
            validate_catalog(self.artifact_dir, self.source_root)
        self.assertIn("must have empty canonical_product_id_b", str(ctx.exception))

    def test_candidate_generic_model_downgrade_enforced(self):
        """Generic/capacity models like sandisk:ssd-1tb or ugreen:100w in candidates must be rejected."""
        auto_path = self.artifact_dir / "AUTO_QC_CANDIDATES.csv"
        pipe_path = self.artifact_dir / "PIPELINE_READY_CANDIDATES.csv"
        prod_csv_path = self.artifact_dir / "PRODUCT_CATALOG_V2.csv"
        prod_jsonl_path = self.artifact_dir / "PRODUCT_CATALOG_V2.jsonl"

        with open(auto_path, encoding="utf-8") as f:
            orig_auto = list(csv.DictReader(f))
        with open(pipe_path, encoding="utf-8") as f:
            orig_pipe = list(csv.DictReader(f))
        with open(prod_csv_path, encoding="utf-8") as f:
            orig_prod_csv = list(csv.DictReader(f))
        with open(prod_jsonl_path, encoding="utf-8") as f:
            orig_prod_jsonl_text = f.read()

        mut_auto = [dict(r) for r in orig_auto]
        mut_pipe = [dict(r) for r in orig_pipe]
        mut_auto[0]["canonical_product_key"] = "sandisk:ssd-1tb"
        mut_pipe[0]["canonical_product_key"] = "sandisk:ssd-1tb"

        mut_prod_csv = [dict(r) for r in orig_prod_csv]
        for p in mut_prod_csv:
            if p["canonical_product_key"] == "sandisk:ssd-1tb":
                p["identity_status"] = "VALID"
                p["pipeline_status"] = "READY_FOR_IMAGE_QC"

        mut_prod_jsonl_lines = []
        for line in orig_prod_jsonl_text.strip().split("\n"):
            item = json.loads(line)
            if item["canonical_product_key"] == "sandisk:ssd-1tb":
                item["identity_status"] = "VALID"
                item["pipeline_status"] = "READY_FOR_IMAGE_QC"
            mut_prod_jsonl_lines.append(json.dumps(item, ensure_ascii=False))

        with open(auto_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(orig_auto[0].keys()))
            w.writeheader()
            w.writerows(mut_auto)
        with open(pipe_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(orig_pipe[0].keys()))
            w.writeheader()
            w.writerows(mut_pipe)
        with open(prod_csv_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(orig_prod_csv[0].keys()))
            w.writeheader()
            w.writerows(mut_prod_csv)
        with open(prod_jsonl_path, "w", encoding="utf-8") as f:
            f.write("\n".join(mut_prod_jsonl_lines) + "\n")

        with self.assertRaises(AssertionError) as ctx:
            validate_catalog(self.artifact_dir, self.source_root)
        self.assertIn("is a generic description, capacity/wattage spec, or conflated model", str(ctx.exception))

    def test_candidate_flagged_token_without_internal_provenance_raises(self):
        """Candidates matching broad/suspicious tokens without internal provenance record must fail."""
        orig_prov = validate_catalog_v2.PI_INTERNAL_PROVENANCE
        try:
            validate_catalog_v2.PI_INTERNAL_PROVENANCE = {}
            with self.assertRaises(AssertionError) as ctx:
                validate_catalog(self.artifact_dir, self.source_root)
            self.assertIn("failed suspicious token scan", str(ctx.exception))
        finally:
            validate_catalog_v2.PI_INTERNAL_PROVENANCE = orig_prov

    def test_claim_statistics_fails_if_conflicted_misreported(self):
        """Ensures that claim distribution strictly asserts 1 CONFLICTED claim."""
        claim_path = self.artifact_dir / "CLAIM_CATALOG_V2.csv"
        with open(claim_path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        # Mutate the 1 CONFLICTED claim to REVIEW_REQUIRED (leaves UNVERIFIED=93, DOSSIER=5, CONFLICTED=0)
        for c in rows:
            if c["verification_status"] == "CONFLICTED":
                c["verification_status"] = "REVIEW_REQUIRED"

        with open(claim_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

        with self.assertRaises(AssertionError) as ctx:
            validate_catalog(self.artifact_dir, self.source_root)
        self.assertIn("Expected 1 CONFLICTED claim", str(ctx.exception))

    def test_claim_statistics_fails_if_unverified_mismatched(self):
        """Ensures that claim distribution fails if UNVERIFIED claim count is not 93."""
        claim_path = self.artifact_dir / "CLAIM_CATALOG_V2.csv"
        with open(claim_path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        for c in rows:
            if c["verification_status"] == "UNVERIFIED":
                c["verification_status"] = "INTERNAL_DOSSIER_ONLY"
                break

        with open(claim_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

        with self.assertRaises(AssertionError) as ctx:
            validate_catalog(self.artifact_dir, self.source_root)
        self.assertIn("Expected 93 UNVERIFIED claims", str(ctx.exception))

    def test_pipeline_gate_blocks_if_ready_for_scripting(self):
        """Ensures that pipeline gate enforces READY_FOR_SCRIPTING == 0."""
        prod_csv_path = self.artifact_dir / "PRODUCT_CATALOG_V2.csv"
        prod_jsonl_path = self.artifact_dir / "PRODUCT_CATALOG_V2.jsonl"
        with open(prod_csv_path, encoding="utf-8") as f:
            orig_prods_csv = list(csv.DictReader(f))
        with open(prod_jsonl_path, encoding="utf-8") as f:
            orig_prod_jsonl_text = f.read()

        mut_prods_csv = [dict(p) for p in orig_prods_csv]
        for p in mut_prods_csv:
            if p["canonical_product_key"] == "120db:ra":
                p["pipeline_status"] = "READY_FOR_SCRIPTING"

        lines = []
        for l in orig_prod_jsonl_text.strip().split("\n"):
            obj = json.loads(l)
            if obj["canonical_product_key"] == "120db:ra":
                obj["pipeline_status"] = "READY_FOR_SCRIPTING"
            lines.append(json.dumps(obj, ensure_ascii=False))
        mut_jsonl_text = "\n".join(lines) + "\n"

        with open(prod_csv_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(orig_prods_csv[0].keys()))
            w.writeheader()
            w.writerows(mut_prods_csv)
        with open(prod_jsonl_path, "w", encoding="utf-8") as f:
            f.write(mut_jsonl_text)

        with self.assertRaises(AssertionError) as ctx:
            validate_catalog(self.artifact_dir, self.source_root)
        self.assertIn("READY_FOR_SCRIPTING count is 1, must be 0", str(ctx.exception))


    def test_validate_catalog_rejects_source_backed_claim_with_internal_dossier_source(self):
        """validate_catalog must fail closed if a SOURCE_BACKED claim uses an internal dossier source."""
        claim_path = self.artifact_dir / "CLAIM_CATALOG_V2.csv"
        with open(claim_path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        rows[0]["verification_status"] = "SOURCE_BACKED"
        rows[0]["evidence_source"] = "dossier/ugreen_uno_robot_dossier.json"
        rows[0]["evidence_locator"] = "line 1"
        rows[0]["evidence_snippet"] = "Fast charging"

        with open(claim_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

        with self.assertRaises(AssertionError) as ctx:
            validate_catalog(self.artifact_dir, self.source_root)
        self.assertIn("contains internal non-independent path token", str(ctx.exception))

    def test_validate_catalog_rejects_source_backed_claim_with_renamed_internal_source(self):
        """validate_catalog must fail closed if an internal source is renamed (e.g. notes or draft)."""
        claim_path = self.artifact_dir / "CLAIM_CATALOG_V2.csv"
        with open(claim_path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        rows[0]["verification_status"] = "SOURCE_BACKED"
        rows[0]["evidence_source"] = "notes/product_summary.json"
        rows[0]["evidence_locator"] = "line 1"
        rows[0]["evidence_snippet"] = "Fast charging"

        with open(claim_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

        with self.assertRaises(AssertionError) as ctx:
            validate_catalog(self.artifact_dir, self.source_root)
        self.assertIn("contains internal non-independent path token", str(ctx.exception))

    def test_validate_catalog_rejects_source_backed_claim_with_arbitrary_locator(self):
        """validate_catalog must fail closed if a SOURCE_BACKED claim has an arbitrary non-verifiable locator."""
        claim_path = self.artifact_dir / "CLAIM_CATALOG_V2.csv"
        with open(claim_path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        rows[0]["verification_status"] = "SOURCE_BACKED"
        rows[0]["evidence_source"] = "https://example.com/pdp.html"
        rows[0]["evidence_locator"] = "arbitrary text string without line or field coordinates"
        rows[0]["evidence_snippet"] = "Fast charging"

        with open(claim_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

        with self.assertRaises(AssertionError) as ctx:
            validate_catalog(self.artifact_dir, self.source_root)
        self.assertIn("not a verifiable position or structured locator", str(ctx.exception))

    def test_validate_catalog_executes_verify_source_backed_claim_on_valid_source_before_snapshot_rules(self):
        """A valid SOURCE_BACKED claim passes verify_source_backed_claim before failing snapshot v2 distribution rule."""
        ext_source_file = self.artifact_dir / "external_spec_sheet.txt"
        ext_source_file.write_text("GaN III 65W Fast Charging official specification.", encoding="utf-8")
        ext_sha = hashlib.sha256(ext_source_file.read_bytes()).hexdigest()
        meta_file = self.artifact_dir / "external_spec_sheet.txt.meta.json"
        meta_file.write_text(json.dumps({
            "source_url": "https://www.ugreen.com/specs/gan-65w.html",
            "retrieved_at": "2026-10-17T00:00:00Z",
            "sha256": ext_sha,
        }), encoding="utf-8")

        claim_path = self.artifact_dir / "CLAIM_CATALOG_V2.csv"
        with open(claim_path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        rows[0]["verification_status"] = "SOURCE_BACKED"
        rows[0]["evidence_source"] = str(ext_source_file)
        rows[0]["evidence_locator"] = "line 1"
        rows[0]["evidence_snippet"] = "GaN III 65W Fast Charging"

        with open(claim_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

        # verify_source_backed_claim passes, then snapshot rule cnt_source_backed == 0 triggers
        with self.assertRaises(AssertionError) as ctx:
            validate_catalog(self.artifact_dir, self.source_root)
        self.assertIn("Expected 0 SOURCE_BACKED claims in snapshot v2, found 1", str(ctx.exception))


    def test_candidate_with_unconfirmed_variant_fails(self):
        """Candidate with unconfirmed multi-wattage listing must fail candidate validation."""
        auto_path = self.artifact_dir / "AUTO_QC_CANDIDATES.csv"
        with open(auto_path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        new_row = dict(rows[0])
        new_row["canonical_product_key"] = "ugreen:uno-robot-gan:30w"
        new_row["canonical_product_id"] = "prod_1ea5b909c78b4078"
        new_row["product_name"] = "Ugreen Uno Robot GaN 30W Charger"
        rows.append(new_row)

        with open(auto_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

        pr_path = self.artifact_dir / "PIPELINE_READY_CANDIDATES.csv"
        with open(pr_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

        with self.assertRaises(AssertionError) as ctx:
            validate_catalog(self.artifact_dir, self.source_root)
        self.assertTrue(
            "is a generic description, capacity/wattage spec, or conflated model" in str(ctx.exception)
            or "ugreen:uno-robot-gan:30w" in str(ctx.exception)
        )


class TestSourceBackedClaimVerification(unittest.TestCase):
    """Fail-closed rule tests for external SOURCE_BACKED claim verification."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)

        # Create a sample valid source file
        self.source_file = self.temp_path / "spec_sheet.txt"
        self.source_file.write_text(
            "Product: Ugreen Uno GaN 30W Charger\n"
            "Key feature: Fast charging up to 30W for iPhones and tablets.\n"
            "Efficiency: GaN III technology reduces heat by 25%.\n",
            encoding="utf-8",
        )
        self.source_sha = hashlib.sha256(self.source_file.read_bytes()).hexdigest()
        self.meta_file = self.temp_path / "spec_sheet.txt.meta.json"
        self.meta_file.write_text(
            json.dumps({
                "source_url": "https://www.ugreen.com/products/uno-robot-30w",
                "retrieved_at": "2026-10-17T00:00:00Z",
                "sha256": self.source_sha,
            }),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_valid_source_backed_claim_passes(self):
        """A well-formed claim pointing to a real source file with exact snippet passes."""
        claim = {
            "canonical_claim_id": "CLM_VALID_01",
            "verification_status": "SOURCE_BACKED",
            "evidence_source": str(self.source_file),
            "evidence_locator": "line 2",
            "evidence_snippet": "Fast charging up to 30W for iPhones and tablets.",
        }
        # Should execute cleanly without error
        verify_source_backed_claim(claim, self.temp_path)

    def test_valid_url_with_verified_local_archive_passes(self):
        """Claim referencing an external URL with a verified local archive passes."""
        claim = {
            "canonical_claim_id": "CLM_URL_02",
            "verification_status": "SOURCE_BACKED",
            "evidence_source": "https://www.ugreen.com/products/uno-robot-30w",
            "evidence_archive": str(self.source_file),
            "evidence_locator": "line 2",
            "evidence_snippet": "Fast charging up to 30W for iPhones and tablets.",
        }
        verify_source_backed_claim(claim, self.temp_path)

    def test_unverified_url_fails_closed(self):
        """Claim referencing an external URL without a verified local archive must fail closed."""
        claim = {
            "canonical_claim_id": "CLM_URL_01",
            "verification_status": "SOURCE_BACKED",
            "evidence_source": "https://www.ugreen.com/products/uno-robot-30w",
            "evidence_locator": "line 1",
            "evidence_snippet": "30W Fast Charging",
        }
        with self.assertRaises(AssertionError) as ctx:
            verify_source_backed_claim(claim, self.temp_path)
        self.assertIn("without a verified local archive on disk", str(ctx.exception))

    def test_missing_evidence_source_fails_closed(self):
        """Claim with empty evidence_source must fail closed."""
        claim = {
            "canonical_claim_id": "CLM_ERR_01",
            "verification_status": "SOURCE_BACKED",
            "evidence_source": "",
            "evidence_locator": "line 2",
            "evidence_snippet": "Fast charging up to 30W",
        }
        with self.assertRaises(AssertionError) as ctx:
            verify_source_backed_claim(claim, self.temp_path)
        self.assertIn("missing evidence_source", str(ctx.exception))

    def test_missing_evidence_locator_fails_closed(self):
        """Claim with empty evidence_locator must fail closed."""
        claim = {
            "canonical_claim_id": "CLM_ERR_02",
            "verification_status": "SOURCE_BACKED",
            "evidence_source": str(self.source_file),
            "evidence_locator": "  ",
            "evidence_snippet": "Fast charging up to 30W",
        }
        with self.assertRaises(AssertionError) as ctx:
            verify_source_backed_claim(claim, self.temp_path)
        self.assertIn("missing evidence_locator", str(ctx.exception))

    def test_arbitrary_locator_fails_closed(self):
        """Claim with arbitrary non-verifiable locator must fail closed."""
        claim = {
            "canonical_claim_id": "CLM_ERR_02B",
            "verification_status": "SOURCE_BACKED",
            "evidence_source": str(self.source_file),
            "evidence_locator": "random unformatted description of location",
            "evidence_snippet": "Fast charging up to 30W",
        }
        with self.assertRaises(AssertionError) as ctx:
            verify_source_backed_claim(claim, self.temp_path)
        self.assertIn("not a verifiable position or structured locator", str(ctx.exception))

    def test_missing_evidence_snippet_fails_closed(self):
        """Claim with empty evidence_snippet must fail closed."""
        claim = {
            "canonical_claim_id": "CLM_ERR_03",
            "verification_status": "SOURCE_BACKED",
            "evidence_source": str(self.source_file),
            "evidence_locator": "line 2",
            "evidence_snippet": "",
        }
        with self.assertRaises(AssertionError) as ctx:
            verify_source_backed_claim(claim, self.temp_path)
        self.assertIn("missing evidence_snippet", str(ctx.exception))

    def test_internal_dossier_only_fails_closed(self):
        """Claim relying on internal dossier path must fail closed."""
        claim = {
            "canonical_claim_id": "CLM_ERR_04",
            "verification_status": "SOURCE_BACKED",
            "evidence_source": "dossier/ugreen_uno_robot_dossier.json",
            "evidence_locator": "features.fast_charge",
            "evidence_snippet": "Fast charging up to 30W",
        }
        with self.assertRaises(AssertionError) as ctx:
            verify_source_backed_claim(claim, self.temp_path)
        self.assertIn("contains internal non-independent path token 'dossier'", str(ctx.exception))

    def test_renamed_internal_file_fails_closed(self):
        """Claim relying on renamed internal file (e.g. notes or draft) must fail closed."""
        claim = {
            "canonical_claim_id": "CLM_ERR_04B",
            "verification_status": "SOURCE_BACKED",
            "evidence_source": "custom_draft/data.json",
            "evidence_locator": "features.fast_charge",
            "evidence_snippet": "Fast charging up to 30W",
        }
        with self.assertRaises(AssertionError) as ctx:
            verify_source_backed_claim(claim, self.temp_path)
        self.assertIn("contains internal non-independent path token 'draft'", str(ctx.exception))

    def test_arbitrary_local_file_without_external_provenance_fails_closed(self):
        """Local file without external archive provenance must fail closed."""
        local_file = self.temp_path / "raw_data.txt"
        local_file.write_text("Fast charging up to 30W\n", encoding="utf-8")
        claim = {
            "canonical_claim_id": "CLM_NO_PROV_01",
            "verification_status": "SOURCE_BACKED",
            "evidence_source": str(local_file),
            "evidence_locator": "line 1",
            "evidence_snippet": "Fast charging up to 30W",
        }
        with self.assertRaises(AssertionError) as ctx:
            verify_source_backed_claim(claim, self.temp_path)
        self.assertIn("lacks structured external source metadata", str(ctx.exception))

    def test_local_file_named_external_spec_without_metadata_fails_closed(self):
        """A local file named external_spec.txt but lacking metadata must fail closed."""
        tricky_file = self.temp_path / "external_spec.txt"
        tricky_file.write_text("Fast charging up to 30W\n", encoding="utf-8")
        claim = {
            "canonical_claim_id": "CLM_TRICKY_01",
            "verification_status": "SOURCE_BACKED",
            "evidence_source": str(tricky_file),
            "evidence_locator": "line 1",
            "evidence_snippet": "Fast charging up to 30W",
        }
        with self.assertRaises(AssertionError) as ctx:
            verify_source_backed_claim(claim, self.temp_path)
        self.assertIn("lacks structured external source metadata", str(ctx.exception))

    def test_metadata_hash_mismatch_fails_closed(self):
        """Claim with metadata SHA-256 mismatch against actual file bytes must fail closed."""
        mismatch_file = self.temp_path / "mismatch.txt"
        mismatch_file.write_text("Legit content\n", encoding="utf-8")
        meta_file = self.temp_path / "mismatch.txt.meta.json"
        meta_file.write_text(
            json.dumps({
                "source_url": "https://example.com/spec.txt",
                "retrieved_at": "2026-10-17T00:00:00Z",
                "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
            }),
            encoding="utf-8",
        )
        claim = {
            "canonical_claim_id": "CLM_HASH_MISMATCH",
            "verification_status": "SOURCE_BACKED",
            "evidence_source": str(mismatch_file),
            "evidence_locator": "line 1",
            "evidence_snippet": "Legit content",
        }
        with self.assertRaises(AssertionError) as ctx:
            verify_source_backed_claim(claim, self.temp_path)
        self.assertIn("does not match actual bytes digest", str(ctx.exception))

    def test_url_metadata_mismatch_fails_closed(self):
        """Claim URL mismatching metadata source_url must fail closed."""
        claim = {
            "canonical_claim_id": "CLM_URL_MISMATCH",
            "verification_status": "SOURCE_BACKED",
            "evidence_source": "https://www.ugreen.com/products/different-product",
            "evidence_archive": str(self.source_file),
            "evidence_locator": "line 2",
            "evidence_snippet": "Fast charging up to 30W for iPhones and tablets.",
        }
        with self.assertRaises(AssertionError) as ctx:
            verify_source_backed_claim(claim, self.temp_path)
        self.assertIn("does not match metadata source_url", str(ctx.exception))

    def test_locator_valid_syntax_wrong_line_fails_closed(self):
        """Claim with valid locator syntax pointing to a line that lacks the snippet must fail closed."""
        # Line 1 is "Product: Ugreen Uno GaN 30W Charger", line 2 has the fast charging snippet
        claim = {
            "canonical_claim_id": "CLM_WRONG_LINE_01",
            "verification_status": "SOURCE_BACKED",
            "evidence_source": str(self.source_file),
            "evidence_locator": "line 1",
            "evidence_snippet": "Fast charging up to 30W for iPhones and tablets.",
        }
        with self.assertRaises(AssertionError) as ctx:
            verify_source_backed_claim(claim, self.temp_path)
        self.assertIn("not found at exact locator line 1 in spec_sheet.txt", str(ctx.exception))

    def test_snippet_present_in_csv_but_wrong_row_fails_closed(self):
        """Snippet present in CSV row 2 must fail closed if locator specifies row 1."""
        csv_file = self.temp_path / "data.csv"
        csv_file.write_text("header1,header2\nFast charging,30W\n", encoding="utf-8")
        csv_sha = hashlib.sha256(csv_file.read_bytes()).hexdigest()
        (self.temp_path / "data.csv.meta.json").write_text(
            json.dumps({
                "source_url": "https://example.com/data.csv",
                "retrieved_at": "2026-10-17T00:00:00Z",
                "sha256": csv_sha,
            }),
            encoding="utf-8",
        )
        claim = {
            "canonical_claim_id": "CLM_CSV_WRONG_ROW",
            "verification_status": "SOURCE_BACKED",
            "evidence_source": str(csv_file),
            "evidence_locator": "row 1",
            "evidence_snippet": "Fast charging",
        }
        with self.assertRaises(AssertionError) as ctx:
            verify_source_backed_claim(claim, self.temp_path)
        self.assertIn("not found at exact locator row 1 in data.csv", str(ctx.exception))

    def test_snippet_present_in_json_but_wrong_field_fails_closed(self):
        """Snippet present in JSON features.speed must fail closed if locator specifies features.battery."""
        json_file = self.temp_path / "data.json"
        json_file.write_text(json.dumps({"features": {"speed": "Fast charging", "battery": "5000mAh"}}), encoding="utf-8")
        json_sha = hashlib.sha256(json_file.read_bytes()).hexdigest()
        (self.temp_path / "data.json.meta.json").write_text(
            json.dumps({
                "source_url": "https://example.com/data.json",
                "retrieved_at": "2026-10-17T00:00:00Z",
                "sha256": json_sha,
            }),
            encoding="utf-8",
        )
        claim = {
            "canonical_claim_id": "CLM_JSON_WRONG_FIELD",
            "verification_status": "SOURCE_BACKED",
            "evidence_source": str(json_file),
            "evidence_locator": "features.battery",
            "evidence_snippet": "Fast charging",
        }
        with self.assertRaises(AssertionError) as ctx:
            verify_source_backed_claim(claim, self.temp_path)
        self.assertIn("not found at exact field features.battery in data.json", str(ctx.exception))

    def test_unsupported_locator_format_fails_closed(self):
        """Unsupported locator formats like 'page 1' or 'section 2' must fail closed."""
        for unsupported_loc in ["page 1", "section 2", "table 3", "p. 42"]:
            claim = {
                "canonical_claim_id": "CLM_UNSUPPORTED_LOC",
                "verification_status": "SOURCE_BACKED",
                "evidence_source": str(self.source_file),
                "evidence_locator": unsupported_loc,
                "evidence_snippet": "Fast charging",
            }
            with self.assertRaises(AssertionError) as ctx:
                verify_source_backed_claim(claim, self.temp_path)
            self.assertIn("is not a verifiable position or structured locator", str(ctx.exception))

    def test_nonexistent_source_file_fails_closed(self):
        """Claim referencing a file that does not exist on disk must fail closed."""
        claim = {
            "canonical_claim_id": "CLM_ERR_05",
            "verification_status": "SOURCE_BACKED",
            "evidence_source": str(self.temp_path / "external_spec_missing.txt"),
            "evidence_locator": "line 1",
            "evidence_snippet": "Fast charging up to 30W",
        }
        with self.assertRaises(AssertionError) as ctx:
            verify_source_backed_claim(claim, self.temp_path)
        self.assertIn("source file does not exist", str(ctx.exception))

    def test_snippet_mismatch_fails_closed(self):
        """Claim snippet that is not found verbatim in the source file must fail closed."""
        claim = {
            "canonical_claim_id": "CLM_ERR_06",
            "verification_status": "SOURCE_BACKED",
            "evidence_source": str(self.source_file),
            "evidence_locator": "line 2",
            "evidence_snippet": "100W wireless super fast charging",  # Not in file
        }
        with self.assertRaises(AssertionError) as ctx:
            verify_source_backed_claim(claim, self.temp_path)
        self.assertIn("not found at exact locator line 2 in spec_sheet.txt", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

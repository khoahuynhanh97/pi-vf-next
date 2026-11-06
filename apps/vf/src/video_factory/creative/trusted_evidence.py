"""Publish and recheck independent source evidence for the creative gate.

This store is separate from the frozen Catalog V2 snapshot. A record is useful
only while its archived bytes and exact locator still pass the Catalog validator.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_RECORD_ID = re.compile(r"^src_[0-9a-f]{64}$")
_BOUND_FIELDS = (
    "canonical_product_key", "canonical_claim_id", "claim_text", "source_url",
    "evidence_locator", "evidence_snippet", "archive_path", "archive_sha256",
)


def _validator() -> Any:
    path = Path(__file__).resolve().parents[3] / "tools" / "validate_catalog_v2.py"
    spec = importlib.util.spec_from_file_location("video_factory_catalog_v2_validator", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Catalog V2 validator is unavailable: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _record_id(record: Mapping[str, Any]) -> str:
    bound = {field: record[field] for field in _BOUND_FIELDS}
    encoded = json.dumps(bound, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "src_" + hashlib.sha256(encoded).hexdigest()


def _validator_claim(record: Mapping[str, Any]) -> dict[str, str]:
    return {
        "canonical_claim_id": str(record["canonical_claim_id"]),
        "evidence_source": str(record["source_url"]),
        "evidence_locator": str(record["evidence_locator"]),
        "evidence_snippet": str(record["evidence_snippet"]),
        "evidence_archive": str(record["archive_path"]),
    }


def publish_validated_record(
    claim: Mapping[str, Any], *, source_root: Path | None, record_dir: Path
) -> str:
    """Validate an archived external source and publish one immutable claim binding.

    Claim-supplied metadata is deliberately ignored: the archive sidecar must
    carry source URL, retrieval time and digest independently of the PI claim.
    """
    product_key = str(claim.get("canonical_product_key") or "").strip()
    claim_id = str(claim.get("canonical_claim_id") or "").strip()
    claim_text = str(claim.get("claim_text") or "").strip()
    if not product_key or not claim_id or not claim_text:
        raise ValueError("product key, claim ID and claim text are required")
    snippet = str(claim.get("evidence_snippet") or "").strip()
    if claim_text not in snippet:
        raise ValueError("claim text must appear verbatim in the verified source snippet")
    archive_path = Path(str(claim.get("evidence_archive") or "")).resolve()
    if not archive_path.is_file():
        raise ValueError("external evidence archive is missing")
    archive_sha = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    record = {
        "canonical_product_key": product_key,
        "canonical_claim_id": claim_id,
        "claim_text": claim_text,
        "source_url": str(claim.get("evidence_source") or "").strip(),
        "evidence_locator": str(claim.get("evidence_locator") or "").strip(),
        "evidence_snippet": snippet,
        "archive_path": str(archive_path),
        "archive_sha256": archive_sha,
    }
    _validator().verify_source_backed_claim(_validator_claim(record), source_root)
    record_id = _record_id(record)
    record.update({
        "record_id": record_id,
        "validator": "validate_catalog_v2",
        "validation_status": "passed",
        "snapshot_id": f"sha256:{archive_sha}",
        "validated_at": datetime.now(timezone.utc).isoformat(),
    })
    record_dir.mkdir(parents=True, exist_ok=True)
    target = record_dir / f"{record_id}.json"
    if target.exists():
        if validated_record_lookup(record_dir, source_root=source_root)(record_id) is None:
            raise ValueError("existing evidence record is invalid; refusing to overwrite")
        return record_id
    temporary = record_dir / f".{record_id}.{os.getpid()}.tmp"
    try:
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return record_id


def validated_record_lookup(
    record_dir: Path, *, source_root: Path | None = None
) -> Callable[[str], Mapping[str, Any] | None]:
    """Return a read-only lookup that revalidates archive bytes on every use."""
    record_dir = Path(record_dir)

    def lookup(record_id: str) -> Mapping[str, Any] | None:
        if not _RECORD_ID.fullmatch(record_id):
            return None
        try:
            record = json.loads((record_dir / f"{record_id}.json").read_text(encoding="utf-8"))
            if not isinstance(record, dict) or any(not str(record.get(field) or "").strip() for field in _BOUND_FIELDS):
                return None
            if record.get("record_id") != record_id or _record_id(record) != record_id:
                return None
            if record.get("validator") != "validate_catalog_v2" or record.get("validation_status") != "passed":
                return None
            archive = Path(str(record["archive_path"]))
            if not archive.is_file():
                return None
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            if record["archive_sha256"] != digest or record.get("snapshot_id") != f"sha256:{digest}":
                return None
            if str(record["claim_text"]) not in str(record["evidence_snippet"]):
                return None
            _validator().verify_source_backed_claim(_validator_claim(record), source_root)
            return record
        except (OSError, ValueError, TypeError, KeyError, AssertionError, json.JSONDecodeError):
            return None

    return lookup


def locked_product_key(aggregate: Mapping[str, Any]) -> str:
    """Return the approved lock's commercial identity, or reject pre-lock input."""
    lock = aggregate.get("locked_pack")
    approval = aggregate.get("latest_approval_receipt")
    pack = aggregate.get("resource_pack")
    if not isinstance(lock, dict) or not isinstance(approval, dict) or not isinstance(pack, dict):
        raise ValueError("approved Product Lock and resource pack are required")
    pack_id = str(pack.get("resource_pack_id") or "")
    if not pack_id or str(lock.get("resource_pack_id") or "") != pack_id:
        raise ValueError("approved Product Lock does not match resource pack")
    if not str(approval.get("approval_request_id") or "").strip():
        raise ValueError("approved Product Lock receipt is missing")
    raw = pack.get("raw_result")
    if isinstance(raw, dict):
        pack = {**pack, **raw}
    identity = pack.get("product_identity")
    if not isinstance(identity, dict):
        identity = {}
    if (identity.get('identity_mode') or pack.get('identity_mode'))=='source_product':
        canonical_id=identity.get('canonical_product_id') or pack.get('canonical_product_id')
        research=aggregate.get('research_result') or {}
        original=research.get('raw_result') or {}
        products=original.get('canonical_products') or []
        upstream_id=original.get('canonical_product_id')
        if not upstream_id and len(products)==1 and isinstance(products[0],dict):
            upstream_id=products[0].get('canonical_product_id')
        if not canonical_id or canonical_id!=upstream_id:
            raise ValueError('source_product lock requires matching upstream canonical identity')
        return str(canonical_id)
    resolved = pack.get("resolved_values")
    if not isinstance(resolved, dict):
        resolved = {}
    source = aggregate.get("source_context")
    if not isinstance(source, dict):
        source = {}
    brand = str(identity.get("brand") or resolved.get("brand") or source.get("brand") or "").strip()
    # A pack with no explicit model field may use a display name that contains
    # the verified model (e.g. 'TRIBIT XSound Go Bluetooth Speaker' for 'XSound Go').
    # Only borrow the source model when the pack display name confirms it;
    # disparate names (e.g. 'Desk Lamp Lite' vs 'Mijia Desk Lamp Lite') still conflict.
    model = str(identity.get("model") or resolved.get("model") or "").strip()
    if not model:
        pack_name = str(
            identity.get("name") or identity.get("display_name") or resolved.get("name") or ""
        ).strip()
        source_model = str(source.get("model") or "").strip()
        from video_factory.automation.contracts import _slugify
        if source_model and pack_name and _slugify(source_model) in _slugify(pack_name):
            model = source_model
        else:
            model = pack_name or source_model
    variant = str(identity.get("variant") or resolved.get("variant") or source.get("variant") or "").strip()
    from video_factory.automation.contracts import compute_canonical_product_key

    product_key = compute_canonical_product_key(brand, model, variant)
    declared_keys = (
        str(source.get("canonical_product_key") or "").strip(),
        str(pack.get("canonical_product_key") or "").strip(),
    )
    if any(declared and declared != product_key for declared in declared_keys):
        raise ValueError("approved Product Lock identity conflict with canonical product key")
    return product_key


def attach_validated_source_claim(
    connection: sqlite3.Connection,
    workflow_id: str,
    record_id: str,
    *,
    record_dir: Path,
    operator_id: str,
    review_note: str,
) -> dict[str, Any]:
    """Attach a reviewed record to a locked workflow without changing the pack.

    The receipt contains only a record ID and trusted store path. Runtime
    resolves and revalidates the record again when it builds the dossier.
    """
    operator_id = operator_id.strip()
    review_note = review_note.strip()
    if not operator_id or not review_note:
        raise ValueError("operator ID and review note are required")
    record_dir = Path(record_dir).resolve()
    record = validated_record_lookup(record_dir)(record_id)
    if record is None:
        raise ValueError("evidence record is missing or failed Catalog V2 validation")

    from video_factory.workflows.repository import WorkflowRepository

    repository = WorkflowRepository(connection)
    current = repository.get_workflow(workflow_id)
    if current is None:
        raise ValueError(f"workflow '{workflow_id}' not found")
    aggregate, version = current
    product_key = locked_product_key(aggregate)
    if record["canonical_product_key"] != product_key:
        raise ValueError("evidence record product key differs from approved Product Lock")

    pack = aggregate["resource_pack"]
    raw = pack.get("raw_result")
    if isinstance(raw, dict):
        pack = {**pack, **raw}
    for item in pack.get("claims") or []:
        if not isinstance(item, dict) or str(item.get("claim_id") or item.get("id") or "") != record["canonical_claim_id"]:
            continue
        if str(item.get("text") or item.get("claim") or "") != record["claim_text"]:
            raise ValueError("evidence record claim text differs from existing resource-pack claim ID")

    receipts = aggregate.get("trusted_source_claims") or []
    if not isinstance(receipts, list):
        raise ValueError("trusted source claim receipts are invalid")
    for receipt in receipts:
        if not isinstance(receipt, dict):
            raise ValueError("trusted source claim receipt is invalid")
        if receipt.get("record_id") == record_id:
            if receipt.get("record_dir") != str(record_dir):
                raise ValueError("evidence record is already attached from a different store")
            return {"status": "already_attached", "workflow_id": workflow_id,
                    "record_id": record_id, "workflow_version": version}
        previous_dir = receipt.get("record_dir")
        previous_id = receipt.get("record_id")
        if not isinstance(previous_dir, str) or not isinstance(previous_id, str):
            raise ValueError("trusted source claim receipt is invalid")
        previous = validated_record_lookup(Path(previous_dir))(previous_id)
        if previous is None:
            raise ValueError("existing trusted source claim requires review before another attachment")
        if previous["canonical_claim_id"] == record["canonical_claim_id"]:
            raise ValueError("claim ID already has a different trusted evidence record")
    if not (aggregate.get("state") == "WaitingForOperator"
            and aggregate.get("operator_action") == "attach_source_evidence"):
        raise ValueError("new source evidence requires the pre-creative source evidence review state")
    now = datetime.now(timezone.utc).isoformat()
    receipt = {
        "record_id": record_id,
        "record_dir": str(record_dir),
        "operator_id": operator_id,
        "review_note": review_note,
        "attached_at": now,
    }
    aggregate["trusted_source_claims"] = [*receipts, receipt]
    outbox_message = None
    if aggregate.get("state") == "WaitingForOperator" and aggregate.get("operator_action") == "attach_source_evidence":
        from video_factory.workflow import ProductVideoWorkflowState, transition
        from video_factory.workflows.envelope import build_envelope

        project_id = str(aggregate.get("project_id") or "").strip()
        owner_user_id = str(aggregate.get("owner_user_id") or operator_id).strip()
        if not project_id:
            raise ValueError("project ID is required to resume creative after source review")
        locked_pack = aggregate["locked_pack"]
        raw_pack = locked_pack.get("resource_pack", locked_pack)
        product_id = str(
            raw_pack.get("product_id") or raw_pack.get("resource_pack_id")
            or raw_pack.get("id") or f"prod_{workflow_id}"
        )
        aggregate["state"] = transition(
            ProductVideoWorkflowState.WAITING_FOR_OPERATOR,
            ProductVideoWorkflowState.CREATING_BRIEF,
        ).value
        for key in ("operator_action", "operator_stage", "operator_reason"):
            aggregate.pop(key, None)
        outbox_message = build_envelope(
            workflow_id=workflow_id,
            topic="job.dispatch",
            idempotency_key=f"idemp_brief_{workflow_id}_evidence_{record_id}",
            payload={
                "job_name": "product_video.creative_brief",
                "workflow_id": workflow_id,
                "args": [product_id, {"resource_pack": locked_pack, "project_id": project_id}],
                "kwargs": {"project_id": project_id},
                "owner_user_id": owner_user_id,
                "project_id": project_id,
            },
        )
    repository.commit_workflow(
        workflow_id, json.dumps(aggregate, ensure_ascii=False),
        [{
            "event_id": f"evt_trusted_source_{uuid.uuid4().hex}",
            "command_id": f"attach_trusted_source:{record_id}",
            "event_type": "TrustedSourceClaimAttached",
            "timestamp": now,
            "payload": {"record_id": record_id, "operator_id": operator_id},
        }],
        outbox_message=outbox_message,
        expected_version=version,
    )
    return {"status": "attached", "workflow_id": workflow_id,
            "record_id": record_id, "workflow_version": version + 1}


def validated_workflow_record_lookup(
    connection: sqlite3.Connection,
) -> Callable[[str], Mapping[str, Any] | None]:
    """Resolve attached records from durable workflow receipts after restart.

    This lookup never treats a receipt as evidence: it rechecks Product Lock,
    the exact product key, the source sidecar, archive bytes, and Catalog V2
    validation on every use. A few personal workflows make a scan sufficient.
    """

    def lookup(record_id: str) -> Mapping[str, Any] | None:
        if not _RECORD_ID.fullmatch(record_id):
            return None
        try:
            rows = connection.execute("SELECT aggregate_json FROM workflow").fetchall()
        except sqlite3.Error:
            return None
        for row in rows:
            try:
                aggregate = json.loads(row[0])
                receipts = aggregate.get("trusted_source_claims") or []
                if not isinstance(receipts, list):
                    continue
                product_key = locked_product_key(aggregate)
                for receipt in receipts:
                    if not isinstance(receipt, dict) or receipt.get("record_id") != record_id:
                        continue
                    if not str(receipt.get("operator_id") or "").strip() or not str(receipt.get("review_note") or "").strip():
                        continue
                    directory = Path(str(receipt.get("record_dir") or ""))
                    if not directory.is_absolute():
                        continue
                    record = validated_record_lookup(directory)(record_id)
                    if record is not None and record.get("canonical_product_key") == product_key:
                        return record
            except (TypeError, ValueError, KeyError, AttributeError):
                continue
        return None

    return lookup

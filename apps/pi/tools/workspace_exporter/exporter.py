"""Filesystem-only exporter for Product Intelligence MCP payloads."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from dotenv import load_dotenv


load_dotenv()

DEFAULT_WORKSPACE_ROOT = Path(r"C:\Work\code\TIKTOK-DATA\ProductResearch")


def workspace_root() -> Path:
    """Return the one configurable projection root."""
    return Path(os.environ.get("PRODUCT_RESEARCH_WORKSPACE", DEFAULT_WORKSPACE_ROOT))


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    return slug or "product-research"


@dataclass(frozen=True)
class WorkspaceExport:
    product_dir: Path
    product_slug: str
    canonical_product_id: str | None
    run_dir: Path
    media_projection: Mapping[str, int]
    operator_status: str = "completed"


class WorkspaceExporter:
    """Write non-canonical workspace projections from already-resolved payloads."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or workspace_root()

    def export(
        self,
        research_payload: Mapping[str, Any] | None,
        *,
        projection_payload: Mapping[str, Any] | None = None,
        research_retrieval_payload: Mapping[str, Any] | None = None,
        resource_pack_payload: Mapping[str, Any] | None = None,
        resource_pack_retrieval_payload: Mapping[str, Any] | None = None,
        query: str,
        tool_calls: Sequence[Mapping[str, Any]],
        started_at: str | None = None,
        completed_at: str | None = None,
        operator_status: str = "completed",
    ) -> WorkspaceExport:
        """Persist raw payloads first, then replace current human projections."""
        started_at = started_at or _now()
        completed_at = completed_at or _now()

        # Determine identity
        if research_payload is not None:
            projection = projection_payload or research_payload
            identity = _identity(projection, query)
        else:
            slug = slugify(query)
            identity = {
                "product": {},
                "canonical_product_id": None,
                "name": query,
                "slug": slug,
            }
            # Look up existing folder/canonical identity if it's already in the index
            index = self.root / "_index.md"
            if index.exists():
                current = index.read_text(encoding="utf-8")
                match = re.search(r"\]\(\./" + re.escape(slug) + r"/README\.md\).*?product-key:([^\s<]+)", current)
                if match:
                    identity["canonical_product_id"] = match.group(1)

        product_dir = self._product_dir(identity)
        machine_dir = product_dir / "machine"
        reports_dir = product_dir / "reports"
        selected_dir = product_dir / "media" / "selected"
        for directory in (machine_dir, reports_dir, selected_dir):
            directory.mkdir(parents=True, exist_ok=True)

        existing_research = None
        if research_payload is None and (machine_dir / "research.json").exists():
            try:
                existing_research = json.loads((machine_dir / "research.json").read_text(encoding="utf-8"))
            except Exception:
                pass

        # Raw MCP results are written before any projection is derived from them.
        if research_payload is not None:
            _atomic_json(machine_dir / "research.json", research_payload)
            if research_retrieval_payload is not None:
                _atomic_json(machine_dir / "research-retrieval.json", research_retrieval_payload)
            if resource_pack_payload is not None:
                _atomic_json(machine_dir / "resource-pack.json", resource_pack_payload)
            if resource_pack_retrieval_payload is not None:
                _atomic_json(machine_dir / "resource-pack-retrieval.json", resource_pack_retrieval_payload)

        media_counts = {"selected": 0, "hardlinked": 0, "copied": 0, "external_only": 0}
        if research_payload is not None:
            projection = projection_payload or research_payload
            media_manifest, media_counts = self._project_media(projection, selected_dir)
            _atomic_json(machine_dir / "media-manifest.json", media_manifest)
            self._write_current_projection(
                product_dir,
                projection,
                resource_pack_payload,
                identity,
                completed_at,
                media_counts,
            )
        elif existing_research is not None:
            manifest_path = machine_dir / "media-manifest.json"
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    for asset in manifest.get("assets", []):
                        media_counts["selected"] += 1
                        proj = asset.get("projection")
                        if proj in media_counts:
                            media_counts[proj] += 1
                except Exception:
                    pass
            _atomic_text(
                product_dir / "README.md",
                _readme(
                    existing_research,
                    resource_pack_payload,
                    identity,
                    completed_at,
                    media_counts,
                    operator_status=operator_status,
                    started_at=started_at,
                )
            )
        else:
            _atomic_text(
                product_dir / "README.md",
                _readme(
                    None,
                    None,
                    identity,
                    completed_at,
                    media_counts,
                    operator_status=operator_status,
                    started_at=started_at,
                )
            )

        run_dir = self._write_run(
            product_dir,
            research_payload or existing_research or {},
            resource_pack_payload,
            query,
            tool_calls,
            started_at,
            completed_at,
            identity,
            operator_status=operator_status,
        )
        self._upsert_index(identity, research_payload or existing_research, resource_pack_payload, completed_at, operator_status=operator_status)
        return WorkspaceExport(product_dir, identity["slug"], identity["canonical_product_id"], run_dir, media_counts, operator_status)

    def _product_dir(self, identity: Mapping[str, Any]) -> Path:
        if not identity["canonical_product_id"]:
            return self.root / identity["slug"]
        index = self.root / "_index.md"
        if index.exists():
            marker = re.escape(f"product-key:{identity['canonical_product_id']}")
            match = re.search(r"\]\(\./([^/]+)/README\.md\).*?" + marker, index.read_text(encoding="utf-8"))
            if match:
                return self.root / match.group(1)
        candidate = self.root / identity["slug"]
        raw = candidate / "machine" / "research.json"
        if not raw.exists():
            return candidate
        try:
            existing = json.loads(raw.read_text(encoding="utf-8"))
            existing_identity = _identity(existing, "")["canonical_product_id"]
        except (OSError, json.JSONDecodeError):
            existing_identity = None
        if existing_identity in (None, identity["canonical_product_id"]):
            return candidate
        return self.root / f"{identity['slug']}-{slugify(str(identity['canonical_product_id']))}"

    def _project_media(self, research: Mapping[str, Any], selected_dir: Path) -> tuple[dict[str, Any], dict[str, int]]:
        selected_dir.mkdir(parents=True, exist_ok=True)
        assets = research.get("media_assets") or []
        projected: list[dict[str, Any]] = []
        counts = {"selected": 0, "hardlinked": 0, "copied": 0, "external_only": 0}
        seen_hashes: set[str] = set()
        current_files: set[str] = set()
        for index, raw_asset in enumerate(assets, start=1):
            if not isinstance(raw_asset, Mapping):
                continue
            asset = dict(raw_asset)
            digest = str(asset.get("sha256") or "")
            if digest and digest in seen_hashes:
                continue
            source_text = asset.get("local_path") or asset.get("source_path")
            source = Path(source_text) if isinstance(source_text, str) and source_text else None
            if source and source.is_file() and not digest:
                digest = _sha256(source)
                if digest in seen_hashes:
                    continue
            if digest:
                seen_hashes.add(digest)
            counts["selected"] += 1
            suffix = source.suffix if source else ".bin"
            filename = f"{index:02d}-{slugify(str(asset.get('view') or asset.get('classification') or asset.get('asset_id') or 'media'))}{suffix}"
            target = selected_dir / filename
            entry = {key: asset[key] for key in ("asset_id", "sha256", "local_path", "source_path", "match_status", "product_match_score", "variant_match", "usage_policy", "source_url", "view", "classification") if key in asset}
            if source and source.is_file():
                mode = _project_file(source, target)
                current_files.add(filename)
                counts[mode] += 1
                entry["workspace_path"] = target.relative_to(selected_dir.parent.parent).as_posix()
                entry["projection"] = mode
            else:
                counts["external_only"] += 1
                entry["projection"] = "external_only"
            projected.append(entry)
        _remove_stale_projection_files(selected_dir, current_files)
        return {"assets": projected}, counts

    def _write_current_projection(self, product_dir: Path, research: Mapping[str, Any], resource_pack: Mapping[str, Any] | None, identity: Mapping[str, Any], completed_at: str, media_counts: Mapping[str, int]) -> None:
        reports = product_dir / "reports"
        resource_status = _status(resource_pack, "not_requested")
        _atomic_text(product_dir / "README.md", _readme(research, resource_pack, identity, completed_at, media_counts))
        _atomic_text(reports / "product.md", _product_report(research, identity))
        _write_when_data(reports / "variants.md", _variants_report(identity["product"]))
        _write_when_data(reports / "listings.md", _listings_report(identity["product"]))
        _write_when_data(reports / "sources.md", _sources_report(research))
        _write_when_data(reports / "evidence.md", _evidence_report(research))
        _write_when_data(reports / "claims.md", _claims_report(resource_pack))
        _write_when_data(reports / "media.md", _media_report(research))
        _atomic_text(reports / "provenance.md", _provenance_report(research, resource_pack, identity, completed_at))
        if resource_status == "not_requested":
            path = reports / "claims.md"
            if path.exists():
                path.unlink()

    def _write_run(self, product_dir: Path, research: Mapping[str, Any], resource_pack: Mapping[str, Any] | None, query: str, tool_calls: Sequence[Mapping[str, Any]], started_at: str, completed_at: str, identity: Mapping[str, Any], operator_status: str = "completed") -> Path:
        stamp = _run_stamp(completed_at)
        run_dir = product_dir / "runs" / stamp
        suffix = 1
        while run_dir.exists():
            suffix += 1
            run_dir = product_dir / "runs" / f"{stamp}-{suffix}"
        run_dir.mkdir(parents=True)
        _atomic_json(run_dir / "tool-calls.json", list(tool_calls))
        _atomic_text(run_dir / "run.md", "\n".join([
            "# Product Research Run", "", f"Started: {started_at}", f"Completed: {completed_at}", f"Query: {query}",
            f"Research ID: {_value(research, 'research_id') if research else 'not available'}", f"Snapshot ID: {_value(research, 'snapshot_id') if research else 'not available'}",
            f"Canonical Product ID: {identity['canonical_product_id'] or 'not available'}", f"Research Status: {_status(research, 'not available') if research else 'not available'}",
            f"Operator Run Status: {operator_status}",
            f"Resource Pack Status: {_status(resource_pack, 'not requested') if resource_pack else 'not requested'}", f"Workspace: {product_dir}",
        ]) + "\n")
        return run_dir

    def _upsert_index(self, identity: Mapping[str, Any], research: Mapping[str, Any] | None, resource_pack: Mapping[str, Any] | None, completed_at: str, operator_status: str = "completed") -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        index = self.root / "_index.md"
        current = index.read_text(encoding="utf-8") if index.exists() else ""
        rows = [line for line in current.splitlines() if line.startswith("| [") or (line.startswith("| ") and not line.startswith("| Product") and not line.startswith("|---"))]
        key = identity["canonical_product_id"] or identity["slug"]
        marker = f"<!-- product-key:{key} -->"
        rows = [line for line in rows if marker not in line]
        
        product_dir = self._product_dir(identity)
        has_canonical = (product_dir / "machine" / "research.json").exists()
        
        product_col = f"[{identity['name']}](./{identity['slug']}/README.md)" if has_canonical else identity['name']
        next_action = _next_action(research, resource_pack) if research else "Retry MCP research"
        research_status = _status(research, "not available") if research else "not available"
        resource_status = _status(resource_pack, "not requested") if resource_pack else "not requested"
        
        row = f"| {product_col} | {research_status} | {operator_status} | {resource_status} | {completed_at} | {next_action} | {marker}"
        rows.append(row)
        content = "\n".join([
            "# Product Research Workspace", "", f"Last Updated: {completed_at}", "",
            "| Product | Research | Latest Run | Resource Pack | Last Run | Next Action |",
            "|---|---|---|---|---|---|",
            *rows, ""
        ])
        _atomic_text(index, content)


def _identity(research: Mapping[str, Any], query: str) -> dict[str, Any]:
    products = research.get("canonical_products") or []
    product = products[0] if len(products) == 1 and isinstance(products[0], Mapping) else {}
    canonical_id = product.get("canonical_product_id") if isinstance(product, Mapping) else None
    name = (product.get("name") if isinstance(product, Mapping) else None) or " ".join(part for part in [product.get("brand") if isinstance(product, Mapping) else None, product.get("model") if isinstance(product, Mapping) else None] if part) or query
    # Canonical IDs prevent folder/index collisions while names preserve readable paths.
    slug = slugify(str(name))
    if canonical_id and slug == "product-research":
        slug = slugify(str(canonical_id))
    return {"product": product, "canonical_product_id": canonical_id, "name": str(name), "slug": slug}


def _readme(
    research: Mapping[str, Any] | None,
    resource: Mapping[str, Any] | None,
    identity: Mapping[str, Any],
    completed_at: str,
    media: Mapping[str, int],
    operator_status: str = "completed",
    started_at: str | None = None,
) -> str:
    product = identity["product"]
    summary = research.get("source_summary") or {} if research else {}
    research_status = _status(research, "not available") if research else "not available"
    resource_status = _status(resource, "not requested") if resource else "not requested"
    mcp_result_returned = "Yes" if research is not None and operator_status != "MCP_CALL_TIMEOUT" else "No"
    
    lines = [
        f"# {identity['name']}",
        "",
        "## Status",
        "",
        f"Research: {research_status}",
        f"Operator status: {operator_status}",
        f"Resource Pack: {resource_status}",
        f"Last Run: {completed_at}",
        "",
        "## Current Research",
        "",
        f"Canonical status: {research_status}",
        "",
        f"Canonical snapshot: {_value(research, 'snapshot_id') if research else 'not available'}",
        "",
        "## Latest Run",
        "",
        f"Operator status: {operator_status}",
        "",
        f"Started: {started_at or completed_at}",
        "",
        f"Completed: {completed_at}",
        "",
        f"MCP result returned: {mcp_result_returned}",
        "",
        "## Identity",
        "",
        f"Brand: {_value(product, 'brand')}",
        f"Model: {_value(product, 'model')}",
        f"Canonical Product: {_value(product, 'name')}",
        f"Product ID: {identity['canonical_product_id'] or 'not available'}",
        "",
        "## Research Summary",
        "",
        f"Sources attempted: {_value(summary, 'attempted') if research else 0}",
        f"Usable sources: {_value(summary, 'usable') if research else 0}",
        f"Canonical products: {len(research.get('canonical_products') or []) if research else 0}",
        f"Variants: {len(product.get('variants') or []) if isinstance(product, Mapping) else 0}",
        f"Listings: {len(product.get('listings') or []) if isinstance(product, Mapping) else 0}",
        "",
        "## Media",
        "",
        f"Selected: {media['selected']}",
        f"Hardlinked: {media['hardlinked']}",
        f"Copied: {media['copied']}",
        f"External-only: {media['external_only']}",
        "",
        "## Resource Pack",
        "",
        f"Status: {resource_status}",
        f"Reason: {_value(resource, 'reason') if resource else 'not available'}",
        "",
        "## Next Action",
        "",
        _next_action(research, resource) if research else "Retry MCP research",
        "",
        "## Files",
        "",
    ]
    if research is not None:
        lines.extend([
            "- reports/product.md",
            "- reports/provenance.md",
            "- machine/research.json",
            "- machine/media-manifest.json",
        ])
    if resource is not None:
        lines.append("- machine/resource-pack.json")
    return "\n".join(lines) + "\n"


def _product_report(research: Mapping[str, Any], identity: Mapping[str, Any]) -> str:
    product = identity["product"]
    return "\n".join(["# Canonical Product", "", "## Identity", "", f"Name: {_value(product, 'name')}", f"Brand: {_value(product, 'brand')}", f"Model: {_value(product, 'model')}", f"Canonical Product ID: {identity['canonical_product_id'] or 'not available'}", "", "## Resolution", "", f"Status: {_status(research, 'not available')}", f"Supporting evidence: {', '.join(product.get('supporting_evidence_ids') or []) if isinstance(product, Mapping) else 'not available'}", ""])


def _variants_report(product: Mapping[str, Any]) -> str | None:
    variants = product.get("variants") or []
    if not variants:
        return None
    return "# Variants\n\n" + "\n".join(f"## {item.get('variant_id', 'variant')}\n\n```json\n{json.dumps(item, indent=2, ensure_ascii=False)}\n```\n" for item in variants if isinstance(item, Mapping))


def _listings_report(product: Mapping[str, Any]) -> str | None:
    listings = product.get("listings") or []
    if not listings:
        return None
    header = "# Listings\n\n| Listing | Source | Seller | Price | Currency | Availability |\n|---|---|---|---|---|---|"
    rows = [f"| {_value(item, 'listing_id')} | {_value(item, 'host')} | {_value(item, 'seller')} | {_value(item, 'price')} | {_value(item, 'currency')} | {_value(item, 'availability')} |" for item in listings if isinstance(item, Mapping)]
    return "\n".join([header, *rows, ""])


def _sources_report(research: Mapping[str, Any]) -> str | None:
    sources = research.get("source_results") or research.get("discovered_sources") or []
    if not sources:
        return None
    rows = [f"| {_value(source, 'host')} | {_value(source, 'acquisition_status')} | {_value(source, 'evidence_status')} | {_value(source, 'usable')} |" for source in sources if isinstance(source, Mapping)]
    return "\n".join(["# Research Sources", "", "| Source | Acquisition | Evidence | Used |", "|---|---|---|---|", *rows, ""])


def _evidence_report(research: Mapping[str, Any]) -> str | None:
    conflicts = research.get("conflicts") or []
    unresolved = research.get("unresolved_evidence_ids") or []
    if not conflicts and not unresolved:
        return None
    lines = ["# Evidence", ""]
    if unresolved:
        lines.extend(["## Unresolved Evidence IDs", "", *[f"- {item}" for item in unresolved], ""])
    if conflicts:
        lines.extend(["## Conflicts", "", *[f"```json\n{json.dumps(item, indent=2, ensure_ascii=False)}\n```" for item in conflicts], ""])
    return "\n".join(lines)


def _claims_report(resource: Mapping[str, Any] | None) -> str | None:
    claims = resource.get("claims") if isinstance(resource, Mapping) else None
    if not claims:
        return None
    return "# Claims\n\n" + "\n".join(f"- {item.get('text', 'not available')} ({item.get('status', 'not available')})" for item in claims if isinstance(item, Mapping)) + "\n"


def _media_report(research: Mapping[str, Any]) -> str | None:
    assets = research.get("media_assets") or []
    if not assets:
        return None
    return "# Media\n\n" + "\n".join(f"## {item.get('asset_id', 'asset')}\n\n```json\n{json.dumps(item, indent=2, ensure_ascii=False)}\n```\n" for item in assets if isinstance(item, Mapping))


def _provenance_report(research: Mapping[str, Any], resource: Mapping[str, Any] | None, identity: Mapping[str, Any], completed_at: str) -> str:
    values = [("Research ID", research.get("research_id")), ("Snapshot ID", research.get("snapshot_id")), ("Canonical Product ID", identity.get("canonical_product_id")), ("Resource Pack ID", resource.get("resource_pack_id") if isinstance(resource, Mapping) else None), ("Resource Pack Version", resource.get("version") if isinstance(resource, Mapping) else None), ("Run Timestamp", completed_at)]
    return "# Provenance\n\n" + "\n".join(f"{name}: {value if value is not None else 'not available'}" for name, value in values) + "\n"


def _write_when_data(path: Path, content: str | None) -> None:
    if content:
        _atomic_text(path, content)
    elif path.exists():
        path.unlink()


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=path.parent, delete=False) as handle:
        handle.write(content)
        temp = Path(handle.name)
    temp.replace(path)


def _atomic_json(path: Path, payload: Any) -> None:
    _atomic_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _project_file(source: Path, target: Path) -> str:
    temporary = target.with_name(f".{target.name}.new")
    if temporary.exists():
        temporary.unlink()
    try:
        os.link(source, temporary)
        mode = "hardlinked"
    except OSError:
        shutil.copy2(source, temporary)
        mode = "copied"
    temporary.replace(target)
    return mode


def _remove_stale_projection_files(selected_dir: Path, current_files: set[str]) -> None:
    """Keep the current media projection aligned with its atomic manifest."""
    for path in selected_dir.iterdir():
        if path.is_file() and path.name not in current_files:
            path.unlink()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _value(mapping: Mapping[str, Any] | None, key: str) -> str:
    value = mapping.get(key) if isinstance(mapping, Mapping) else None
    return str(value) if value is not None else "not available"


def _status(mapping: Mapping[str, Any] | None, fallback: str) -> str:
    return _value(mapping, "pack_status") if isinstance(mapping, Mapping) and mapping.get("pack_status") is not None else _value(mapping, "status") if isinstance(mapping, Mapping) and mapping.get("status") is not None else fallback


def _next_action(research: Mapping[str, Any], resource: Mapping[str, Any] | None) -> str:
    if isinstance(resource, Mapping) and resource.get("reason"):
        return str(resource["reason"])
    if research.get("error"):
        return str(research["error"])
    return "Inspect canonical research result"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _run_stamp(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

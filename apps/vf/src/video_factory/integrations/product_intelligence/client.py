from __future__ import annotations

import hashlib
import json
from typing import Any

try:
    from hermes.application.product_resource_service import ProductResourceService
except ImportError:
    class ProductResourceService:  # type: ignore[no-redef]
        def lock_resource_pack(self, payload: dict, owner_user_id: str) -> dict:
            return {
                "resource_pack_id": payload.get("resource_pack_id", "rp_default"),
                "resource_pack_version": payload.get("resource_pack_version", 1),
                "status": "locked",
                "owner_user_id": owner_user_id,
                "manifest_digest": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                "assets": payload.get("assets", []),
            }

try:
    from hermes_personal.product.research import ProductResearchPipeline, ResearchSpec
    from hermes_personal.product.resource_pack import ResourcePackBuilder, ResourcePackSpec
except ImportError:
    from dataclasses import dataclass, field

    @dataclass
    class ResearchSpec:
        product_name: str
        keywords: list[str] = field(default_factory=list)

    @dataclass
    class ResearchResult:
        summary: str = "Standalone mock summary"
        report: str = '{"product_name": "mock", "keywords": []}'

    class ProductResearchPipeline:
        def __init__(self, mcp_client: Any = None) -> None:
            pass

        def run(self, spec: Any, fake: bool = True, mcp_client: Any = None) -> Any:
            return ResearchResult()

    @dataclass
    class ResourcePackSpec:
        product_name: str
        assets: list[str] = field(default_factory=list)
        scenes: list[str] = field(default_factory=list)

    @dataclass
    class ScenePlanObj:
        steps: list[str] = field(default_factory=lambda: ["intro", "outro"])

    @dataclass
    class ResourcePackResult:
        summary: str = "Standalone resource pack summary"
        assets_mapping: dict[str, str] = field(default_factory=dict)
        scene_plan: ScenePlanObj = field(default_factory=ScenePlanObj)

    class ResourcePackBuilder:
        def build(self, spec: Any) -> Any:
            return ResourcePackResult()


class ProductIntelligenceClient:
    """Deterministic client for Phase 5 Task 2.

    Provides three high-level operations without external I/O:
    * ``research`` - fabricates a research result with workflow/job correlation.
    * ``build_resource_pack`` - creates a deterministic resource-pack from that result.
    * ``lock_resource_pack`` - generates a lock payload and runs the anti-corruption service.
    """

    def __init__(
        self,
        owner_user_id: str = "user",
        *,
        fake: bool = True,
        mcp_client: Any = None,
    ) -> None:
        self.owner_user_id = owner_user_id
        self.fake = fake
        self.mcp_client = mcp_client
        self.service = ProductResourceService()
        try:
            self._pipeline = ProductResearchPipeline(mcp_client=mcp_client)
        except TypeError:
            self._pipeline = ProductResearchPipeline()
        self._builder = ResourcePackBuilder()

    # ---------------------------------------------------------------------
    # Research
    # ---------------------------------------------------------------------
    def research(
        self,
        product_name: str,
        keywords: list[str],
        workflow_id: str | None = None,
        job_id: str | None = None,
        idempotency_key: str | None = None,
        *,
        mcp_client: Any = None,
        **kwargs: Any,
    ) -> dict:
        """Run product research.

        When ``fake`` is true, use the local deterministic pipeline for
        development/testing only. When ``fake`` is false, require an explicit
        external MCP source; otherwise fail closed to avoid fabricating
        production evidence.
        """
        active_client = mcp_client or self.mcp_client
        if active_client is not None:
            spec = ResearchSpec(product_name=product_name, keywords=keywords)
            result = self._pipeline.run(spec, fake=False, mcp_client=active_client)
            res = {
                "owner": "product_intelligence",
                "schema": "product_research_report",
                "version": 1,
                "summary": result.summary,
                "report": result.report,
            }
            if workflow_id:
                res["workflow_id"] = workflow_id
            if job_id:
                res["job_id"] = job_id
            if idempotency_key:
                res["idempotency_key"] = idempotency_key
            return res

        if not self.fake:
            raise RuntimeError(
                "Product research requires an external MCP provider or explicit fake=True."
            )
        spec = ResearchSpec(product_name=product_name, keywords=keywords)
        result = self._pipeline.run(spec, fake=True)
        res = {
            "owner": "product_intelligence",
            "schema": "product_research_report",
            "version": 1,
            "summary": result.summary,
            "report": result.report,
        }
        if workflow_id:
            res["workflow_id"] = workflow_id
        if job_id:
            res["job_id"] = job_id
        if idempotency_key:
            res["idempotency_key"] = idempotency_key
        if kwargs.get("images"):
            res["images"] = kwargs["images"]
        if kwargs.get("source_context"):
            res["source_context"] = kwargs["source_context"]
        return res

    # ---------------------------------------------------------------------
    # Resource-pack build
    # ---------------------------------------------------------------------
    def build_resource_pack(
        self,
        research_result: dict,
        workflow_id: str | None = None,
        job_id: str | None = None,
        idempotency_key: str | None = None,
        **kwargs: Any,
    ) -> dict:
        """Build a deterministic ``ResourcePackResult`` from a research result."""
        report = research_result.get("report", "{}")
        try:
            data = __import__("json").loads(report)
        except Exception as exc:
            raise ValueError("Malformed research report") from exc

        images = (
            kwargs.get("images")
            or kwargs.get("source_context", {}).get("images")
            or research_result.get("source_context", {}).get("images")
            or research_result.get("images")
            or research_result.get("media_assets")
            or data.get("images")
            or data.get("media_assets")
            or []
        )
        product_name = data.get("product_name", "unknown")
        keywords = data.get("keywords", [])
        assets = [f"{kw}_asset" for kw in keywords]
        scenes = ["intro", "outro"]
        spec = ResourcePackSpec(product_name=product_name, assets=assets, scenes=scenes)
        result = self._builder.build(spec)
        pack_id = f"rp_{workflow_id or 'default'}"
        res = {
            "resource_pack_id": pack_id,
            "id": pack_id,
            "owner": "product_intelligence",
            "schema": "product_resource_pack",
            "version": 1,
            "summary": result.summary,
            "assets_mapping": result.assets_mapping,
            "scene_plan": result.scene_plan.steps,
        }
        if images:
            product_references = []
            for idx, img in enumerate(images, start=1):
                if isinstance(img, dict):
                    loc_path = img.get("absolute_path") or img.get("local_path") or img.get("path")
                    product_references.append({
                        "asset_id": img.get("asset_id") or f"asset_{idx}",
                        "local_path": loc_path,
                        "absolute_path": loc_path,
                        "uri": img.get("uri") or (f"file:///{str(loc_path).replace(chr(92), '/')}" if loc_path else None),
                        "original_url": img.get("original_url"),
                        "width": img.get("width"),
                        "height": img.get("height"),
                    })
            if product_references:
                res["product_references"] = product_references
                res["assets"] = product_references
                res["media_assets"] = product_references
                res["assets_mapping"] = {
                    ref["asset_id"]: ref["local_path"]
                    for ref in product_references
                    if ref.get("local_path")
                }
        if workflow_id:
            res["workflow_id"] = workflow_id
        if job_id:
            res["job_id"] = job_id
        if idempotency_key:
            res["idempotency_key"] = idempotency_key
        return res

    # ---------------------------------------------------------------------
    # Lock
    # ---------------------------------------------------------------------
    def lock_resource_pack(
        self,
        pack_dict: dict,
        owner_user_id: str | None = None,
        workflow_id: str | None = None,
        job_id: str | None = None,
        idempotency_key: str | None = None,
        **kwargs: Any,
    ) -> dict:
        """Create a lock payload and invoke :class:`ProductResourceService`."""
        uid = owner_user_id or self.owner_user_id
        pack_digest = hashlib.sha256(
            json.dumps(pack_dict, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()
        resource_pack_id = f"rp_{pack_digest[:16]}"
        payload: dict = {
            "resource_pack_id": resource_pack_id,
            "resource_pack_version": 1,
            "status": "unlocked",
            "assets": [
                {"asset_id": aid, "local_path": url}
                for aid, url in (pack_dict.get("assets_mapping", {}) or {}).items()
            ],
        }
        locked = self.service.lock_resource_pack(payload, uid)
        locked["owner"] = "product_intelligence"
        locked["schema"] = "product_resource_pack_locked"
        locked["version"] = locked.get("resource_pack_version", 1)
        if workflow_id:
            locked["workflow_id"] = workflow_id
        if job_id:
            locked["job_id"] = job_id
        if idempotency_key:
            locked["idempotency_key"] = idempotency_key
        return locked

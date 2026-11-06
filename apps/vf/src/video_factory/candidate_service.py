"""Service to inspect, aggregate, and select daily candidate products for video production."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from video_factory.daily_importer import (
    _ensure_ledger,
    _latest_products_file,
    _normalized,
    _product_key,
    _validate_product,
)
from video_factory.handlers import (
    ProductVideoWorkflowState,
    start_product_video_workflow,
)
from video_factory.workflows.repository import WorkflowRepository
from .automation.contracts import BatchStartResult, SkippedProduct, StartedProduct


@dataclass(frozen=True)
class CandidateItem:
    source_date: str
    source_product_id: str
    product_key: str
    name: str
    brand: str
    model: str
    price_display: str
    price_min: int | None
    price_max: int | None
    hot_reason: str
    key_features: str
    keywords: list[str]
    images: list[dict[str, Any]]
    status: str  # 'unprocessed' | 'invalid' | 'awaiting_product_lock' | 'in_production' | 'completed'
    rejection_reason: str | None = None
    workflow_id: str | None = None
    approval_id: str | None = None
    gate: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DailyCandidateService:
    """Aggregates daily scout products against SQLite durable workflows."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        source_root: Path | None = None,
        inbox_root: Path | None = None,
        db_path: Path | None = None,
        catalog_candidates_path: Path | None = None,
    ) -> None:
        import os

        self.connection = connection
        self.db_path = db_path

        if inbox_root is not None:
            self.inbox_root = Path(inbox_root)
        else:
            env_inbox = os.environ.get("HERMES_EXCHANGE_INBOX")
            self.inbox_root = Path(env_inbox) if env_inbox else None

        env_root = os.environ.get("HERMES_PRODUCT_RUNS_ROOT")
        self.source_root = Path(source_root) if source_root is not None else (Path(env_root) if env_root else None)

        _ensure_ledger(self.connection)
        self.workflow_repo = WorkflowRepository(self.connection)

        # Coordinate with ExchangeCoordinator if inbox available
        from video_factory.automation.coordinator import ProductVideoCoordinator
        from video_factory.automation.catalog_candidates import DEFAULT_CATALOG_V2_CANDIDATES

        if catalog_candidates_path is None:
            catalog_candidates_path = Path(
                os.environ.get("HERMES_CATALOG_V2_CANDIDATES") or DEFAULT_CATALOG_V2_CANDIDATES
            )

        self._coordinator: ProductVideoCoordinator | None = None

        if self.source_root is not None:
            # Check if custom source itself is an exchange inbox
            if list(self.source_root.glob("????-??-??/run_*/READY")) or (self.source_root / "READY").is_file():
                self._coordinator = ProductVideoCoordinator(
                    connection=self.connection,
                    inbox_root=self.source_root,
                    db_path=self.db_path,
                    catalog_candidates_path=catalog_candidates_path,
                )
            # Otherwise, remains None to run explicit legacy source_root path
        else:
            self._coordinator = ProductVideoCoordinator(
                connection=self.connection,
                inbox_root=self.inbox_root,
                db_path=self.db_path,
                catalog_candidates_path=catalog_candidates_path,
            )

    def get_candidates(self, target_date: date | None = None, qc_level: str = "middle") -> list[CandidateItem]:
        if self._coordinator is not None:
            return self._coordinator.list_candidates(target_date=target_date, qc_level=qc_level)
        if self.source_root is None:
            raise FileNotFoundError("No ready exchange batch found and no legacy source root provided.")
        source_file = _latest_products_file(self.source_root, target_date)
        raw = json.loads(source_file.read_text(encoding="utf-8"))
        products = raw.get("products") or []
        source_date = str(raw.get("date") or source_file.parent.name)

        ledger_rows = {
            row["product_key"]: row
            for row in self.connection.execute(
                "SELECT * FROM daily_product_video_import WHERE source_date = ?",
                (source_date,),
            ).fetchall()
        }

        candidates: list[CandidateItem] = []

        for prod in products:
            prod_id = str(prod.get("id") or "")
            name = str(prod.get("name") or "")
            brand = str(prod.get("brand") or "")
            model = str(prod.get("model") or "")
            price_display = str(prod.get("price_display") or "")
            price_min = prod.get("price_min")
            price_max = prod.get("price_max")
            hot_reason = str(prod.get("hot_reason") or "")
            key_features = str(prod.get("key_features") or "")
            keywords = list(prod.get("keywords") or [])

            try:
                pkey, valid_imgs = _validate_product(prod, source_file.parent)
                is_valid = True
                rejection_reason = None
            except Exception as exc:  # noqa: BLE001
                is_valid = False
                rejection_reason = str(exc)
                try:
                    pkey = _product_key(prod)
                except Exception:  # noqa: BLE001
                    pkey = f"invalid_{prod_id}"
                valid_imgs = []

            if not is_valid:
                candidates.append(
                    CandidateItem(
                        source_date=source_date,
                        source_product_id=prod_id,
                        product_key=pkey,
                        name=name,
                        brand=brand,
                        model=model,
                        price_display=price_display,
                        price_min=price_min,
                        price_max=price_max,
                        hot_reason=hot_reason,
                        key_features=key_features,
                        keywords=keywords,
                        images=prod.get("images") or [],
                        status="invalid",
                        rejection_reason=rejection_reason,
                    )
                )
                continue

            ledger_entry = ledger_rows.get(pkey)
            if ledger_entry is None or ledger_entry["status"] != "started":
                candidates.append(
                    CandidateItem(
                        source_date=source_date,
                        source_product_id=prod_id,
                        product_key=pkey,
                        name=name,
                        brand=brand,
                        model=model,
                        price_display=price_display,
                        price_min=price_min,
                        price_max=price_max,
                        hot_reason=hot_reason,
                        key_features=key_features,
                        keywords=keywords,
                        images=valid_imgs,
                        status="unprocessed",
                    )
                )
                continue

            wf_id = ledger_entry["workflow_id"]
            wf_data = self.workflow_repo.get_workflow(wf_id)
            if wf_data is None:
                candidates.append(
                    CandidateItem(
                        source_date=source_date,
                        source_product_id=prod_id,
                        product_key=pkey,
                        name=name,
                        brand=brand,
                        model=model,
                        price_display=price_display,
                        price_min=price_min,
                        price_max=price_max,
                        hot_reason=hot_reason,
                        key_features=key_features,
                        keywords=keywords,
                        images=valid_imgs,
                        status="unprocessed",
                    )
                )
                continue

            agg, _version = wf_data
            wf_state = str(agg.get("state") or "")
            approval_id = agg.get("pending_approval_id")
            gate = agg.get("pending_approval_gate")

            rejection_reason = None
            if wf_state == ProductVideoWorkflowState.COMPLETED.value:
                status = "completed"
            elif wf_state == ProductVideoWorkflowState.AWAITING_PRODUCT_LOCK.value:
                status = "awaiting_product_lock"
            elif wf_state == ProductVideoWorkflowState.WAITING_FOR_OPERATOR.value:
                status = "waiting_for_operator"
                rejection_reason = agg.get("operator_reason") or agg.get("failure_code")
            elif wf_state == "skipped":
                status = "blocked"
                rejection_reason = agg.get("skip_reason") or agg.get("failure_code") or agg.get("operator_reason")
            elif wf_state in {"failed", "contract_broken"}:
                status = "technical_error"
                rejection_reason = agg.get("failure_code") or agg.get("operator_reason") or wf_state
            else:
                status = "in_production"

            candidates.append(
                CandidateItem(
                    source_date=source_date,
                    source_product_id=prod_id,
                    product_key=pkey,
                    name=name,
                    brand=brand,
                    model=model,
                    price_display=price_display,
                    price_min=price_min,
                    price_max=price_max,
                    hot_reason=hot_reason,
                    key_features=key_features,
                    keywords=keywords,
                    images=valid_imgs,
                    status=status,
                    rejection_reason=rejection_reason,
                    workflow_id=wf_id,
                    approval_id=approval_id,
                    gate=gate,
                )
            )

        return candidates


    def start_selection(
        self,
        product_keys: list[str],
        owner_user_id: str = "local-owner",
        target_date: date | None = None,
        qc_authorization: dict[str, Any] | None = None,
        qc_max_cost_usd: float | None = None,
        research_snapshot_id: str | None = None,
        input_reviews: dict[str, dict[str, Any]] | None = None,
        max_qc_retries: int | None = None,
        execution_mode: str = "manual",
    ) -> BatchStartResult:
        """Start workflows for specific selected product keys."""
        if self._coordinator is not None:
            return self._coordinator.start_selection(
                product_keys=product_keys,
                owner_user_id=owner_user_id,
                target_date=target_date,
                qc_authorization=qc_authorization,
                qc_max_cost_usd=qc_max_cost_usd,
                research_snapshot_id=research_snapshot_id,
                input_reviews=input_reviews,
                max_qc_retries=max_qc_retries,
                execution_mode=execution_mode,
            )
        if self.source_root is None:
            raise FileNotFoundError("No ready exchange batch found and no legacy source root provided.")
        source_file = _latest_products_file(self.source_root, target_date)
        raw = json.loads(source_file.read_text(encoding="utf-8"))
        products = raw.get("products") or []
        source_date = str(raw.get("date") or source_file.parent.name)

        prod_by_key: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        for prod in products:
            try:
                pkey, imgs = _validate_product(prod, source_file.parent)
                prod_by_key[pkey] = (prod, imgs)
            except Exception:  # noqa: BLE001
                continue

        started_items: list[StartedProduct] = []
        skipped_items: list[SkippedProduct] = []

        for pkey in product_keys:
            if pkey not in prod_by_key:
                skipped_items.append(SkippedProduct(product_key=pkey, reason="NOT_FOUND_IN_MANIFEST"))
                continue
            prod, images = prod_by_key[pkey]
            workflow_id = f"pv-daily-{pkey[:16]}"

            existing = self.connection.execute(
                "SELECT workflow_id, status FROM daily_product_video_import WHERE product_key = ?",
                (pkey,),
            ).fetchone()
            if existing is not None and self.workflow_repo.get_workflow(existing["workflow_id"]) is not None:
                skipped_items.append(SkippedProduct(product_key=pkey, reason="ALREADY_STARTED", workflow_id=existing["workflow_id"]))
                continue

            now = datetime.now(timezone.utc).isoformat()
            canon_key = f"{_normalized(prod.get('brand'))}:{_normalized(prod.get('model'))}"
            with self.connection:
                self.connection.execute(
                    """
                    INSERT INTO daily_product_video_import (
                        product_key, canonical_product_key, source_date, source_batch_id,
                        source_product_id, source_path, workflow_id, status, last_error,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', NULL, ?, ?)
                    ON CONFLICT(product_key) DO UPDATE SET
                        canonical_product_key=excluded.canonical_product_key,
                        source_date=excluded.source_date,
                        source_batch_id=excluded.source_batch_id,
                        source_product_id=excluded.source_product_id,
                        source_path=excluded.source_path,
                        workflow_id=excluded.workflow_id,
                        status='reserved',
                        last_error=NULL,
                        updated_at=excluded.updated_at
                    """,
                    (
                        pkey,
                        canon_key,
                        source_date,
                        source_date,
                        str(prod.get("id") or ""),
                        str(source_file),
                        workflow_id,
                        now,
                        now,
                    ),
                )

            exact_identity = f"{prod['brand']} {prod['model']}".strip()
            keywords = list(dict.fromkeys([exact_identity, str(prod["name"]), *(prod.get("keywords") or [])]))
            source_context = {
                "source": "product_intelligence_daily_runs",
                "source_date": source_date,
                "source_file": str(source_file.resolve()),
                "source_product_id": prod.get("id"),
                "product_key": pkey,
                "brand": prod.get("brand"),
                "model": prod.get("model"),
                "hot_reason": prod.get("hot_reason"),
                "key_features": prod.get("key_features"),
                "images": images,
            }

            try:
                start_product_video_workflow(
                    workflow_repository=self.workflow_repo,
                    workflow_id=workflow_id,
                    product_name=str(prod["name"]),
                    keywords=keywords,
                    pdp_url=prod.get("pdp_url"),
                    owner_user_id=owner_user_id,
                    idempotency_key=f"daily-import:{pkey}",
                    project_id=f"proj-{workflow_id}",
                    source_context=source_context,
                    qc_authorization=qc_authorization,
                    qc_max_cost_usd=qc_max_cost_usd,
                    research_snapshot_id=research_snapshot_id,
                    execution_mode=execution_mode,
                )
                with self.connection:
                    self.connection.execute(
                        "UPDATE daily_product_video_import SET status='started', last_error=NULL, updated_at=? WHERE product_key=?",
                        (datetime.now(timezone.utc).isoformat(), pkey),
                    )
                started_items.append(StartedProduct(
                    product_key=pkey,
                    canonical_product_key=canon_key,
                    workflow_id=workflow_id,
                    name=str(prod["name"]),
                ))
            except Exception as exc:  # noqa: BLE001
                with self.connection:
                    self.connection.execute(
                        "UPDATE daily_product_video_import SET status='error', last_error=?, updated_at=? WHERE product_key=?",
                        (str(exc), datetime.now(timezone.utc).isoformat(), pkey),
                    )
                skipped_items.append(SkippedProduct(
                    product_key=pkey,
                    reason=f"START_FAILED: {exc}",
                    workflow_id=workflow_id,
                    error=str(exc),
                ))

        return BatchStartResult(started=tuple(started_items), skipped=tuple(skipped_items))

    def start_auto(
        self,
        limit: int = 5,
        owner_user_id: str = "local-owner",
        target_date: date | None = None,
        qc_authorization: dict[str, Any] | None = None,
        qc_max_cost_usd: float | None = None,
        research_snapshot_id: str | None = None,
        max_qc_retries: int | None = None,
        execution_mode: str = "auto",
    ) -> BatchStartResult:
        """Automatically pick up to `limit` unprocessed eligible products and start workflows."""
        if self._coordinator is not None:
            return self._coordinator.start_auto(
                limit=limit,
                owner_user_id=owner_user_id,
                target_date=target_date,
                qc_authorization=qc_authorization,
                qc_max_cost_usd=qc_max_cost_usd,
                research_snapshot_id=research_snapshot_id,
                max_qc_retries=max_qc_retries,
                execution_mode=execution_mode,
            )
        if limit < 1 or limit > 100:
            raise ValueError(f"Limit must be between 1 and 100, got: {limit}")
        candidates = self.get_candidates(target_date)
        unprocessed = [c for c in candidates if c.status == "unprocessed"]
        to_start = [c.product_key for c in unprocessed[:limit]]
        if not to_start:
            return BatchStartResult(started=(), skipped=())
        return self.start_selection(
            to_start,
            owner_user_id=owner_user_id,
            target_date=target_date,
            qc_authorization=qc_authorization,
            qc_max_cost_usd=qc_max_cost_usd,
            research_snapshot_id=research_snapshot_id,
            max_qc_retries=max_qc_retries,
            execution_mode=execution_mode,
        )

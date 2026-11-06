"""Candidate products & Product Discovery REST API for Hermes Product Video.

Provides endpoints to inspect scouted daily products, start video workflows,
serve raw assets, and render the Product Video Operator Console.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import sqlite3
import threading
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from video_factory.constants import get_hermes_db_path
from video_factory.application.operator_handler import approve_and_advance_workflow
from video_factory.automation.importer import DEFAULT_EXCHANGE_INBOX
from video_factory.operator_console import render_operator_console_html
from video_factory.storage_infra.sqlite import connect


_studio_batch_lock = threading.Lock()


def _run_studio_batch_background(**kwargs: Any) -> None:
    try:
        run_studio_selected_batch(**kwargs)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception("Studio PR-1 creative batch failed")
    finally:
        _studio_batch_lock.release()


def run_studio_selected_batch(**kwargs: Any) -> int:
    """Lazily dispatch Studio through the same PR-1 batch path as the CLI."""
    from video_factory.automation.creative_cli import run_studio_selected_batch as run_batch

    return run_batch(**kwargs)


def _verified_export(aggregate: dict[str, Any]) -> tuple[Path, Any]:
    """Verify the recorded local MP4 bytes before exposing a playback URL."""
    from video_factory.integrations.composer import _normalize_file_path, probe_video

    exported = aggregate.get("export_result") or {}
    if isinstance(exported.get("payload"), str):
        exported = json.loads(exported["payload"])
    latest = aggregate.get("latest_artifact") or {}
    render = aggregate.get("render_result") or {}
    raw = exported.get("file_path") or exported.get("video_path") or latest.get("file_path") or render.get("file_path") or exported.get("video_url") or latest.get("video_url") or render.get("video_url")
    if not raw:
        raise ValueError("Không tìm thấy file MP4 đã export")
    path = _normalize_file_path(raw)
    if path.suffix.lower() != ".mp4":
        raise ValueError("File export không phải MP4")
    probe = probe_video(path, decode_all=True)
    expected = str(exported.get("sha256") or render.get("sha256") or latest.get("sha256") or "").removeprefix("sha256:").lower()
    if not expected:
        raise ValueError("Thiếu SHA đã ghi nhận cho MP4")
    if probe.frame_count < 1 or probe.duration_seconds <= 0:
        raise ValueError("MP4 không có frame hoặc duration hợp lệ")
    if probe.sha256 != expected:
        raise ValueError("SHA MP4 không khớp export đã ghi nhận")
    technical = exported.get("technical_qc") or render.get("technical_qc") or {}
    if technical and (technical.get("verdict") != "PASS" or str(technical.get("sha256") or "").removeprefix("sha256:").lower() != expected):
        raise ValueError("Technical QC không PASS trên đúng SHA hiện tại")
    return path, probe


def create_candidate_router(connection: sqlite3.Connection | None = None) -> APIRouter:
    """Create Candidate and Product Discovery API router."""
    from video_factory.candidate_service import DailyCandidateService

    router = APIRouter(tags=["product-discovery"])

    def get_connection() -> sqlite3.Connection:
        if connection is not None:
            return connection
        try:
            db_path = get_hermes_db_path()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            return connect(db_path)
        except FileNotFoundError:
            return JSONResponse(
                content={
                    "ok": True,
                    "source_date": target_date or "",
                    "candidates": [],
                }
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Database connection error: {exc}")

    # ─────────────────────────────────────────────────────────────
    # Web HTML Console Page
    # ─────────────────────────────────────────────────────────────
    @router.get("/products", response_class=HTMLResponse)
    @router.get("/console", response_class=HTMLResponse)
    @router.get("/personal/product-video", response_class=HTMLResponse)
    @router.get("/api/personal/product-video/console", response_class=HTMLResponse)
    def get_product_discovery_console(
        target_date: str | None = Query(None),
        conn: sqlite3.Connection = Depends(get_connection),
    ) -> HTMLResponse:
        """Serve the Product Discovery & Daily Video Operator Console."""
        service = DailyCandidateService(conn)
        parsed_date = date.fromisoformat(target_date) if target_date else None
        initial_candidates = []
        try:
            candidates = service.get_candidates(target_date=parsed_date)
            initial_candidates = [c.to_dict() for c in candidates]
        except Exception:
            initial_candidates = []

        return HTMLResponse(content=render_operator_console_html(initial_candidates))

    # ─────────────────────────────────────────────────────────────
    # Candidate Data Endpoints
    # ─────────────────────────────────────────────────────────────
    @router.get("/api/personal/product-video/candidates")
    def get_product_video_candidates(
        target_date: str | None = Query(None),
        qc_level: str = Query("high", pattern="^(low|middle|high)$"),
        conn: sqlite3.Connection = Depends(get_connection),
    ) -> JSONResponse:
        """Return scouted candidate products for a given date."""
        service = DailyCandidateService(conn)
        parsed_date = date.fromisoformat(target_date) if target_date else None
        try:
            candidates = service.get_candidates(target_date=parsed_date, qc_level=qc_level)
            candidate_rows = []
            for candidate in candidates:
                item = candidate.to_dict()
                item["qc_level"] = qc_level
                if candidate.workflow_id:
                    workflow = conn.execute(
                        "SELECT aggregate_json FROM workflow WHERE workflow_id=?", (candidate.workflow_id,)
                    ).fetchone()
                    if workflow:
                        aggregate = json.loads(workflow[0])
                        if (aggregate.get("state") == "building_resource_pack"
                                and aggregate.get("source_input_recovery_history")
                                and not _studio_batch_lock.locked()):
                            item["status"] = "ready_to_resume"
                        policy = (aggregate.get("creative_policy") or {})
                        item["qc_level"] = policy.get("qc_level", "middle")
                candidate_rows.append(item)
            return JSONResponse(
                content={
                    "ok": True,
                    "source_date": candidates[0].source_date if candidates else (target_date or ""),
                    "candidates": candidate_rows,
                }
            )
        except FileNotFoundError:
            return JSONResponse(
                content={
                    "ok": True,
                    "source_date": target_date or "",
                    "candidates": [],
                }
            )
        except Exception as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

    @router.post("/api/personal/product-video/start-batch")
    async def start_product_video_batch(
        request: Request,
        background_tasks: BackgroundTasks,
        conn: sqlite3.Connection = Depends(get_connection),
    ) -> JSONResponse:
        """Run the existing PR-1 creative batch for Studio selections."""
        try:
            body = await request.json()
        except Exception:
            body = {}

        try:
            minimum_daily = int(body.get("minimum_daily") or 0)
            target_date = date.fromisoformat(body["target_date"]) if body.get("target_date") else None
            owner = "local-owner"
            from video_factory.automation.creative_policy import normalize_qc_level
            qc_level = normalize_qc_level(body.get("qc_level") or "high")
            disable_keyframe_qc = bool(body.get("disable_keyframe_qc", False))
            if disable_keyframe_qc and qc_level != "low":
                raise ValueError("disable_keyframe_qc requires qc_level=low")
            if minimum_daily < 0 or minimum_daily > 5:
                raise ValueError("minimum_daily must be between 0 and 5")
            is_auto = bool(body.get("auto"))
            limit = int(body.get("limit") or 5) if is_auto else None
            if limit is not None and (limit < 1 or limit > 100):
                raise ValueError("limit must be between 1 and 100")
            keys = list(body.get("product_keys") or [])
            if not is_auto and (not keys or any(not isinstance(key, str) or not key for key in keys)):
                raise ValueError("product_keys must contain at least one product key")
            if len(keys) > 5:
                raise ValueError("Cannot select more than 5 products at a time")

            from video_factory.automation.importer import ExchangeImporter, get_default_exchange_inbox

            inbox_root = Path(os.environ.get("HERMES_EXCHANGE_INBOX") or get_default_exchange_inbox()).resolve()
            importer = ExchangeImporter(inbox_root)
            batch_dir = importer.find_latest_ready_batch(target_date)
            manifest = importer.load_manifest(batch_dir)
            target_date = date.fromisoformat(manifest.production_date)
            manifest_key_map = {
                key: product.product_key
                for product in manifest.products
                for key in (product.product_key, product.canonical_product_key)
            }
            if not is_auto:
                missing = [key for key in keys if key not in manifest_key_map]
                if missing:
                    raise ValueError(f"Selected product is not in the current READY batch: {missing[0]}")
                keys = [manifest_key_map[key] for key in keys]

            main_db = next(
                (Path(row["file"]).resolve() for row in conn.execute("PRAGMA database_list")
                 if row["name"] == "main" and row["file"]),
                None,
            )
            if main_db is None:
                raise ValueError("Studio batch requires a persistent SQLite database")
            profile = os.environ.get("VF_CREATIVE_RUN_CONFIG")
            if not profile:
                profile = str(Path(__file__).resolve().parents[3] / "config" / "creative_run_profile.json")
            profile_path = Path(profile).resolve()
            if not profile_path.is_file():
                raise ValueError(f"Creative profile not found: {profile_path}")
            try:
                from video_factory.creative.agents.contracts import BackendProfile
                config = json.loads(profile_path.read_text(encoding="utf-8"))
                BackendProfile.from_dict(config["profile"])
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                raise ValueError(f"Cấu hình creative profile không hợp lệ: {exc}") from exc
            if not _studio_batch_lock.acquire(blocking=False):
                return JSONResponse(status_code=409, content={
                    "ok": False,
                    "error": "A Studio creative batch is already running; check Results before starting another.",
                })
            try:
                background_tasks.add_task(
                    _run_studio_batch_background,
                    db_path=main_db,
                    inbox_root=inbox_root,
                    profile_path=profile_path,
                    product_keys=keys,
                    target_date=target_date.isoformat(),
                    owner_user_id=owner,
                    limit=limit,
                    qc_level=qc_level,
                    disable_keyframe_qc=disable_keyframe_qc,
                )
            except Exception:
                _studio_batch_lock.release()
                raise

            return JSONResponse(status_code=202, content={
                "ok": True,
                "started": [{"product_key": key, "state": "running"} for key in keys],
                "skipped": [],
                "started_workflows": [],
                "batch_id": manifest.batch_id,
                "qc_level": qc_level,
                "error": None,
            })
        except Exception as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

    @router.get("/api/personal/product-video/videos")
    def get_product_video_videos(
        conn: sqlite3.Connection = Depends(get_connection),
    ) -> JSONResponse:
        """Return completed exports; final content QC is optional for this personal workflow."""
        try:
            rows = conn.execute(
                "SELECT workflow_id, aggregate_json, version FROM workflow WHERE json_extract(aggregate_json, '$.state') = ?",
                ("completed",),
            ).fetchall()
            videos = []
            for r in rows:
                agg = json.loads(r["aggregate_json"])
                latest = agg.get("latest_artifact") or {}
                result_status = "completed"
                technical_error = None
                probe = None
                video_url = None
                try:
                    video_path, probe = _verified_export(agg)
                    video_url = f"/api/personal/product-video/videos/{quote(r['workflow_id'], safe='')}/media"
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    result_status = "technical_error"
                    technical_error = str(exc)[:300]
                video_report = next(
                    (rpt for rpt in agg.get("qc_reports", []) if rpt.get("stage") == "video"),
                    None,
                )
                videos.append({
                    "workflow_id": r["workflow_id"],
                    "product_key": agg.get("source_context", {}).get("product_key"),
                    "product_name": agg.get("product_name"),
                    "brand": agg.get("source_context", {}).get("brand"),
                    "model": agg.get("source_context", {}).get("model"),
                    "completed_at": agg.get("completed_at"),
                    "created_at": agg.get("created_at"),
                    "updated_at": agg.get("updated_at") or agg.get("completed_at") or agg.get("created_at"),
                    "version": r["version"],
                    "duration_seconds": probe.duration_seconds if probe else None,
                    "thumbnail_url": latest.get("thumbnail_url"),
                    "video_url": video_url,
                    "result_status": result_status,
                    "technical_error": technical_error,
                    "sha256": probe.sha256 if probe else None,
                    "frame_count": probe.frame_count if probe else None,
                    "qc_score": video_report.get("score") if video_report else None,
                    "qc_verdict": video_report.get("verdict") if video_report else None,
                    "qc_level": (agg.get("creative_policy") or {}).get("qc_level", "middle"),
                })
            return JSONResponse(content={"ok": True, "videos": videos})
        except Exception as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

    @router.get("/api/personal/product-video/videos/{workflow_id}/media")
    def get_export_media(
        workflow_id: str, download: bool = False,
        conn: sqlite3.Connection = Depends(get_connection),
    ) -> FileResponse:
        row = conn.execute("SELECT aggregate_json FROM workflow WHERE workflow_id=?", (workflow_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Không tìm thấy video")
        try:
            aggregate = json.loads(row[0])
            if aggregate.get("state") != "completed":
                raise ValueError("Video chưa hoàn tất")
            path, _ = _verified_export(aggregate)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise HTTPException(status_code=409, detail=f"MP4 chưa hợp lệ: {exc}") from exc
        return FileResponse(path, media_type="video/mp4", filename=path.name if download else None)

    @router.post("/api/personal/product-video/workflows/{workflow_id}/source-input")
    async def correct_source_input(
        workflow_id: str, request: Request,
        conn: sqlite3.Connection = Depends(get_connection),
    ) -> JSONResponse:
        from video_factory.application.operator_handler import replace_blocked_source_images

        # The personal Studio uses the same fixed owner as its existing resume path.
        # Retain the handler's ownership, active-operation and revision/replay gates.
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            raise HTTPException(status_code=403, detail="Chỉ sửa input từ Studio cùng origin")
        from video_factory.application.studio_uploads import (
            MAX_UPLOAD_BODY,
            discard_upload,
            resolve_image_selection,
            store_uploaded_images,
        )

        upload_dir = None
        upload_root = get_hermes_db_path().resolve().parent / "studio-inputs"
        # Use the injected DB's directory in isolated tests and alternate deployments.
        for database in conn.execute("PRAGMA database_list"):
            if database["name"] == "main" and database["file"]:
                upload_root = Path(database["file"]).resolve().parent / "studio-inputs"
        try:
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > MAX_UPLOAD_BODY:
                    raise HTTPException(status_code=413, detail="Bộ ảnh tải lên vượt giới hạn 9 MB JSON")
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise TypeError("Nội dung yêu cầu phải là JSON object")
            paths = body.get("image_paths", [])
            info = body.get("product_info", {})
            if not isinstance(paths, list) or len(paths) > 10 or any(not isinstance(p, str) for p in paths):
                raise ValueError("Cần danh sách tối đa 10 đường dẫn ảnh")
            if not isinstance(info, dict) or any(not isinstance(v, str) for v in info.values()):
                raise ValueError("Thông tin sản phẩm phải là văn bản")
            if "expected_version" in body:
                row = conn.execute("SELECT version FROM workflow WHERE workflow_id=?", (workflow_id,)).fetchone()
                if row is None:
                    raise KeyError(workflow_id)
                if row["version"] != body["expected_version"]:
                    raise HTTPException(status_code=409, detail="Workflow đã thay đổi; hãy làm mới trước khi thay ảnh")
            source_images = []
            if "image_selection" in body:
                if origin != str(request.base_url).rstrip("/"):
                    raise HTTPException(status_code=403, detail="Sửa bộ ảnh cần Origin cùng Studio")
                if paths:
                    raise ValueError("Chọn image_selection hoặc image_paths, không dùng đồng thời")
                from video_factory.workflows.repository import WorkflowRepository

                loaded = WorkflowRepository(conn).get_workflow(workflow_id)
                if loaded is None:
                    raise KeyError(workflow_id)
                if loaded[0].get("owner_user_id") != "local-owner":
                    raise HTTPException(status_code=403, detail="Workflow thuộc người dùng khác")
                source_images = (loaded[0].get("source_context") or {}).get("images") or []
            if "uploaded_images" in body:
                if origin != str(request.base_url).rstrip("/"):
                    raise HTTPException(status_code=403, detail="Tải ảnh cần Origin cùng Studio")
                if paths:
                    raise ValueError("Chọn uploaded_images hoặc image_paths, không dùng đồng thời")
                if "image_selection" not in body or body["uploaded_images"] != []:
                    upload_dir, paths = store_uploaded_images(body["uploaded_images"], upload_root)
            if "image_selection" in body:
                paths = resolve_image_selection(body["image_selection"], source_images, paths)
            result = replace_blocked_source_images(
                conn, workflow_id=workflow_id, owner_user_id="local-owner",
                image_paths=paths, product_info=info, reason=body.get("reason", ""),
            )
            if upload_dir and result.get("reused"):
                discard_upload(upload_dir, upload_root)
            return JSONResponse(content={"ok": True, **result})
        except KeyError as exc:
            if upload_dir:
                discard_upload(upload_dir, upload_root)
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ValueError, TypeError, AttributeError) as exc:
            if upload_dir:
                discard_upload(upload_dir, upload_root)
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/api/personal/product-video/assets/{source_date}/{source_batch_id}/{asset_path:path}")
    def get_product_video_asset(
        source_date: str,
        source_batch_id: str,
        asset_path: str,
    ) -> Response:
        """Serve raw image or video asset from product exchange inbox."""
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", source_date):
            return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid date format"})
        if not re.match(r"^[a-zA-Z0-9_\-]+$", source_batch_id):
            return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid batch_id format"})

        env_inbox = os.environ.get("HERMES_EXCHANGE_INBOX")
        inbox_root = Path(env_inbox).resolve() if env_inbox else DEFAULT_EXCHANGE_INBOX.resolve()
        batch_dir = (inbox_root / source_date / source_batch_id).resolve()

        try:
            batch_dir.relative_to(inbox_root)
        except ValueError:
            return JSONResponse(status_code=403, content={"ok": False, "error": "Access denied"})

        if not batch_dir.is_dir():
            return JSONResponse(status_code=404, content={"ok": False, "error": "Batch directory not found"})

        target_file = (batch_dir / asset_path).resolve()
        try:
            target_file.relative_to(batch_dir)
        except ValueError:
            return JSONResponse(
                status_code=403, content={"ok": False, "error": "Access denied: path traversal detected"}
            )

        allowed_extensions = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".webm"}
        if target_file.suffix.lower() not in allowed_extensions:
            return JSONResponse(
                status_code=400, content={"ok": False, "error": "Unsupported media asset extension"}
            )

        if not target_file.is_file():
            return JSONResponse(status_code=404, content={"ok": False, "error": "Asset not found"})

        mime_type, _ = mimetypes.guess_type(str(target_file))
        return FileResponse(target_file, media_type=mime_type or "application/octet-stream")

    return router

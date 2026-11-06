"""Bounded permanent deletion for explicitly selected, inactive Studio workflows."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlparse

from video_factory.application.studio_commands import _events, _loaded, _table
from video_factory.workflows.repository import WorkflowRepository

TERMINAL = {"completed", "succeeded", "failed", "cancelled", "canceled", "rejected", "not_sent", "exhausted", "pass", "fail"}
PATH_KEYS = {"file_path", "video_path", "image_path", "output_path", "video_url", "final_video_url", "absolute_path", "local_path", "uri"}


def _paths(value, *, references=True):
    if isinstance(value, dict):
        for key, child in value.items():
            if not references and key in {'research_dossier','research_result','source_context','locked_pack','references','reference_images','source_images','reference_assets','review_context'}:
                continue
            if key == 'payload' and isinstance(child, str):
                try:
                    yield from _paths(json.loads(child), references=references)
                except ValueError:
                    pass
                continue
            if key in PATH_KEYS and isinstance(child, str):
                raw = unquote(urlparse(child).path).lstrip("/") if child.startswith("file:") else child
                if Path(raw).is_absolute():
                    yield Path(raw)
            else:
                yield from _paths(child, references=references)
    elif isinstance(value, list):
        for child in value:
            yield from _paths(child, references=references)


def _operation_guard(conn, workflow_id, project_id):
    for table in ("product_video_operation_attempt", "workflow_qc_retries", "video_factory_keyframe_operation", "video_factory_scene_operation", "video_factory_render_batches"):
        if not _table(conn, table):
            continue
        columns = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
        scope, identity = ("workflow_id", workflow_id) if "workflow_id" in columns else ("project_id", project_id)
        if scope not in columns or not identity:
            continue
        statuses = conn.execute(f'SELECT status FROM "{table}" WHERE "{scope}"=?', (identity,)).fetchall()
        if any(str(row[0]).lower() not in TERMINAL for row in statuses):
            raise ValueError("Còn operation/QC/render batch đang chạy hoặc chưa rõ kết quả; đối soát trước khi xóa.")


def _project_output_root(path: Path, project: str) -> Path | None:
    """Recognize the existing project/export layout, including older workspace roots."""
    if not project:
        return None
    for root in path.parents:
        if root.name not in {'video_factory','video-factory'} or root.parent.name != 'workspaces':
            continue
        parts=path.relative_to(root).parts
        if len(parts) >= 3 and parts[0]=='projects' and parts[1]==project:
            return root
        prefix=f'export_{project}_'
        if len(parts) >= 3 and parts[0]=='exports' and parts[1].startswith(prefix) and parts[1][len(prefix):].isdigit():
            return root
    return None


def delete_workflows(conn, items, *, workspace: Path, integration_db: Path | None = None):
    """Delete independently; refusal for one item never deletes another item's files."""
    results = []
    workspace = workspace.resolve()
    db_file = next((Path(r["file"]) for r in conn.execute('PRAGMA database_list') if r["name"] == "main" and r["file"]), None)
    uploads = (db_file.parent / "studio-inputs").resolve() if db_file else workspace / "studio-inputs"
    external = None
    if integration_db and integration_db.is_file() and (not db_file or integration_db.resolve() != db_file.resolve()):
        external = sqlite3.connect(integration_db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        for item in items:
            wid = item["workflow_id"]
            deleted_files = []
            try:
                conn.execute("BEGIN IMMEDIATE")
                current = WorkflowRepository(conn).get_workflow(wid)
                if current is None:
                    conn.rollback()
                    results.append({"workflow_id": wid, "status": "already_deleted", "deleted_files": []})
                    continue
                agg, version = _loaded(conn, wid)
                if version != item["expected_version"]:
                    raise ValueError("Workflow đã thay đổi; làm mới rồi chọn lại.")
                if _table(conn, 'product_video_runtime_control') and conn.execute('SELECT 1 FROM product_video_runtime_control WHERE paused=1 AND workflow_id=?', (wid,)).fetchone():
                    raise ValueError('Pipeline đang tạm dừng tại workflow này; cần xử lý pause trước khi xóa.')
                parent=agg.get('production_run_id')
                if parent and parent != wid:
                    run=WorkflowRepository(conn).get_workflow(parent)
                    if run and str(run[0].get('state','')).lower() not in {'completed','skipped','failed','cancelled'}:
                        raise ValueError('Workflow thuộc batch chưa kết thúc; chưa thể xóa an toàn.')
                if conn.execute("SELECT 1 FROM workflow_outbox WHERE workflow_id=? AND state IN ('pending','leased') LIMIT 1", (wid,)).fetchone():
                    raise ValueError("Còn job chờ hoặc lease; không xóa workflow đang thực thi.")
                if _table(conn, 'daily_product_video_import') and conn.execute("SELECT 1 FROM daily_product_video_import WHERE workflow_id=? AND status IN ('claimed','reserved','started') LIMIT 1", (wid,)).fetchone():
                    raise ValueError("Sản phẩm còn được batch giữ/claim; chưa thể xóa an toàn.")
                events = _events(conn, wid)
                finished = {e.get('request_id') for e in events if e['type'] in {'StudioCommandFinished','StudioCommandRejected'}}
                if any(e['type'] == 'StudioCommandStarted' and e.get('request_id') not in finished for e in events):
                    raise ValueError("Command trước chưa rõ kết quả; đối soát trước khi xóa.")
                project = agg.get('project_id') or ''
                _operation_guard(conn, wid, project)
                if external:
                    _operation_guard(external, wid, project)
                candidates = set()
                for key in ('storyboard','render_result','export_result','latest_artifact'):
                    artifact = agg.get(key) or {}
                    if isinstance(artifact, dict) and isinstance(artifact.get('payload'), str):
                        artifact = json.loads(artifact['payload'])
                    candidates.update(_paths(artifact, references=False))
                candidates.update(p for p in _paths(agg.get('source_context') or {}) if p.resolve().is_relative_to(uploads))
                if _table(conn, 'video_factory_artifacts'):
                    for row in conn.execute('SELECT relative_path FROM video_factory_artifacts WHERE workflow_id=?', (wid,)):
                        candidates.add(workspace / row[0])
                for store in (conn, external):
                    if store is None:
                        continue
                    for table in ('video_factory_keyframe_operation', 'video_factory_scene_operation'):
                        if _table(store, table):
                            columns = {r[1] for r in store.execute(f'PRAGMA table_info("{table}")')}
                            scope, identity = ('workflow_id', wid) if 'workflow_id' in columns else ('project_id', project)
                            for row in store.execute(f'SELECT output_path FROM "{table}" WHERE "{scope}"=?', (identity,)):
                                if row[0]:
                                    candidates.add(Path(row[0]))
                shared = set()
                for row in conn.execute('SELECT aggregate_json FROM workflow WHERE workflow_id!=?', (wid,)):
                    shared.update(p.resolve() for p in _paths(json.loads(row[0])))
                for store in (conn, external):
                    if store is not None and _table(store, 'video_factory_artifacts'):
                        shared.update((workspace / row[0]).resolve() for row in store.execute('SELECT relative_path FROM video_factory_artifacts WHERE workflow_id!=?', (wid,)))
                paths = set()
                preserved = []
                for raw in candidates:
                    p = raw.resolve()
                    old_root=_project_output_root(p, project)
                    if raw.is_symlink() or not (any(p.is_relative_to(root) and p != root for root in (workspace, uploads)) or old_root):
                        raise ValueError(f"File ngoài vùng quản lý hoặc symlink; không xóa: {raw}")
                    if old_root and old_root != workspace:
                        old_db=old_root.parent.parent / 'data' / 'video_factory.sqlite'
                        if old_db.is_file() and (not db_file or old_db.resolve()!=db_file.resolve()):
                            ledger=sqlite3.connect(old_db.resolve().as_uri()+'?mode=ro',uri=True)
                            try:
                                _operation_guard(ledger,wid,project)
                                if _table(ledger,'video_factory_artifacts') and any((old_root/row[0]).resolve()==p for row in ledger.execute('SELECT relative_path FROM video_factory_artifacts WHERE workflow_id!=?',(wid,))):
                                    shared.add(p)
                            finally:
                                ledger.close()
                    if p in shared:
                        preserved.append(str(p))
                    elif p.exists():
                        if not p.is_file():
                            raise ValueError(f"Không xóa cả thư mục: {p}")
                        paths.add(p)
                for p in paths:
                    p.unlink()
                    deleted_files.append(str(p))
                for table in ('product_video_qc_reports','workflow_qc_retries','video_factory_artifacts','video_factory_render_batches','daily_product_video_import'):
                    if _table(conn, table):
                        conn.execute(f'DELETE FROM "{table}" WHERE workflow_id=?', (wid,))
                WorkflowRepository(conn).purge_workflow(wid, in_transaction=True)
                if WorkflowRepository(conn).get_workflow(wid) is not None:
                    raise ValueError("Không xóa được workflow; kiểm tra ràng buộc dữ liệu.")
                conn.commit()
                results.append({"workflow_id":wid,"status":"deleted","deleted_files":deleted_files,"shared_files_preserved":preserved})
            except (ValueError, OSError, sqlite3.Error, PermissionError, KeyError) as exc:
                conn.rollback()
                results.append({"workflow_id":wid,"status":"blocked","reason":str(exc),"deleted_files":deleted_files})
    finally:
        if external:
            external.close()
    return results

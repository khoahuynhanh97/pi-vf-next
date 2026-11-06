from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from video_factory.storage_infra.sqlite import connect
from video_factory.workflows.envelope import OutboxEnvelope
from video_factory.workflows.outbox_dispatcher import (
    DispatchReport,
    FatalPersistenceError,
    OutboxDispatcher,
)
from video_factory.workflows.outbox_repository import OutboxRepository

logger = logging.getLogger(__name__)


def _tick_ui_maintenance(connection):
    try:
        from video_factory.maintenance.events import configured_maintenance
        configured_maintenance(connection)
    except Exception as maintenance_error:
        logger.warning('Flow maintenance tick stopped (%s)', type(maintenance_error).__name__)


def _should_pause_worker(
    pause: dict[str, Any] | None,
    workflow_ids: list[str] | None,
    connection: Any | None = None,
) -> bool:
    if not pause:
        return False
    from video_factory.workflows.runtime_control import (
        is_shared_infra_blocker,
        workflow_has_inflight_operation,
    )
    pause_code = str(pause.get("code") or "")
    paused_wf = str(pause.get("workflow_id") or "")
    if pause_code == "PROVIDER_CAPACITY_WAIT":
        scope = workflow_ids
        if scope is None and connection is not None:
            scope = [str(row[0]) for row in connection.execute("SELECT workflow_id FROM workflow")]
        if connection is not None and any(workflow_has_inflight_operation(connection, wf) for wf in (scope or [])):
            return False
        return True
    is_shared = is_shared_infra_blocker(pause_code)
    return is_shared or not paused_wf


def _scoped_workflow_ids(connection, workflow_ids, workflow_id_prefix):
    """Intersect explicit worker filters so pause checks and outbox claims share one scope."""
    if workflow_ids is None and workflow_id_prefix is None:
        return None
    rows = connection.execute("SELECT workflow_id FROM workflow").fetchall()
    scope = {str(row[0]) for row in rows}
    if workflow_ids is not None:
        scope.intersection_update(str(value) for value in workflow_ids)
    if workflow_id_prefix is not None:
        scope = {value for value in scope if value.startswith(workflow_id_prefix)}
    return sorted(scope)


def _capacity_dispatch_scope(connection, workflow_ids, workflow_id_prefix):
    scope = _scoped_workflow_ids(connection, workflow_ids, workflow_id_prefix)
    if scope is None:
        scope = [str(row[0]) for row in connection.execute("SELECT workflow_id FROM workflow")]
    from video_factory.workflows.runtime_control import workflow_has_inflight_operation
    return [workflow_id for workflow_id in scope if workflow_has_inflight_operation(connection, workflow_id)]


def _pause_dispatch_scope(connection, workflow_ids, workflow_id_prefix, pause):
    scope = _scoped_workflow_ids(connection, workflow_ids, workflow_id_prefix)
    from video_factory.workflows.runtime_control import is_shared_infra_blocker
    local_pause = bool(pause and not is_shared_infra_blocker(pause.get("code")) and pause.get("workflow_id"))
    if scope is None and local_pause:
        scope = [str(row[0]) for row in connection.execute("SELECT workflow_id FROM workflow")]
    if scope is None:
        return None
    if local_pause:
        paused_workflow = str(pause["workflow_id"])
        scope = [workflow_id for workflow_id in scope if workflow_id != paused_workflow]
    return scope


def run_worker(
    db_path: str | Path,
    poll_interval: float = 1.0,
    stop_event: Optional[threading.Event] = None,
    topic_handlers: Optional[Dict[str, Callable[[OutboxEnvelope], None]]] = None,
    delivery_fn: Optional[Callable[[str, str], None]] = None,
    worker_id: Optional[str] = None,
    tool_registry: Any | None = None,
    provider_mode: Optional[str] = None,
    render_poll_timeout: float = 60.0,
    render_poll_interval: float = 0.5,
    once: bool = False,
    batch_size: int = 1,
    workflow_id_prefix: str | None = None,
    workflow_ids: list[str] | None = None,
) -> DispatchReport | None:
    """Run the durable outbox worker loop."""
    if stop_event is None:
        stop_event = threading.Event()

    from video_factory.provider_runtime import (
        resolve_provider_mode,
        validate_polling_setting,
    )

    _provider_mode = resolve_provider_mode(
        cli_arg=provider_mode,
        env_val=os.environ.get("HERMES_PROVIDER_MODE"),
    )
    _render_poll_timeout = validate_polling_setting("render_poll_timeout", render_poll_timeout, 60.0)
    _render_poll_interval = validate_polling_setting("render_poll_interval", render_poll_interval, 0.5)

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect(db_path)
    repo = OutboxRepository(connection)
    _worker_id = worker_id or f"durable-worker-{os.getpid()}"

    from video_factory.workflows.runtime_control import ensure_pipeline_pause

    pause = ensure_pipeline_pause(connection)
    # A maintenance tick may collect/repair UI evidence while an unrelated QC
    # pause remains in force. It never clears that pause or dispatches a Send.
    _tick_ui_maintenance(connection)
    if _should_pause_worker(pause, workflow_ids, connection=connection):
        logger.error(
            "Video Factory pipeline is paused (%s/%s): %s",
            pause.get("category"), pause.get("code"), pause.get("message"),
        )
        connection.close()
        return DispatchReport(claimed=0, succeeded=0, failed=0, quarantined=0)

    if _provider_mode == "configured":
        try:
            import sys
            ws_parent = Path(__file__).resolve().parents[3]
            for extra in (
                Path(r"D:\work\hermes-agent"),
                Path(r"Z:\work\hermes-agent"),
                ws_parent / "hermes-agent",
                Path(r"D:\work\hermes-upstream-rearchitecture"),
                Path(r"Z:\work\hermes-upstream-rearchitecture"),
                ws_parent / "hermes-upstream-rearchitecture",
            ):
                if extra.is_dir() and str(extra) not in sys.path:
                    sys.path.insert(0, str(extra))
            from tools.mcp_tool import discover_mcp_tools
            discover_mcp_tools()
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP tool discovery failed in run_worker: %s", exc)

    from video_factory.workflows.budgets import BudgetPolicy, init_default_engine
    init_default_engine(
        policy=BudgetPolicy(max_total=100000, per_channel={"video_factory": 10000, "telegram": 10000, "default": 10000}),
        connection=connection,
        owner_user_id="default-owner",
    )

    if topic_handlers is None and delivery_fn is None:
        from video_factory.handlers import (
            create_configured_creative_generator,
            create_product_video_topic_handlers,
        )
        from video_factory.workflows.engine import WorkflowEngine
        from video_factory.workflows.repository import WorkflowRepository
        from video_factory.creative.trusted_evidence import validated_workflow_record_lookup

        wf_repo = WorkflowRepository(connection)
        wf_engine = WorkflowEngine(wf_repo)
        creative_generator = (
            create_configured_creative_generator(
                record_lookup=validated_workflow_record_lookup(connection),
            )
            if _provider_mode == "configured"
            else None
        )
        if tool_registry is None and _provider_mode == "configured":
            from video_factory.mcp_adapters import _get_global_tool_registry
            tool_registry = _get_global_tool_registry()

        topic_handlers = create_product_video_topic_handlers(
            workflow_engine=wf_engine,
            workflow_repository=wf_repo,
            tool_registry=tool_registry,
            provider_mode=_provider_mode,
            poll_timeout=_render_poll_timeout,
            poll_interval=_render_poll_interval,
            creative_generator=creative_generator,
        )
    # Scale lease duration so it comfortably covers polling duration + buffer (prevents stale ACK failure)
    # A storyboard job can make several sequential keyframe calls and then run
    # image QC before it ACKs. Renewals below keep this bounded lease alive.
    lease_duration = max(300.0, _render_poll_timeout + 60.0)
    dispatcher = OutboxDispatcher(
        repository=repo,
        delivery_fn=delivery_fn,
        worker_id=_worker_id,
        lease_duration_seconds=lease_duration,
        topic_handlers=topic_handlers,
    )

    logger.info("Durable worker started (db=%s, worker_id=%s, provider_mode=%s, lease_duration=%.1fs)", db_path, _worker_id, _provider_mode, lease_duration)
    try:
        if once:
            current_pause = ensure_pipeline_pause(connection)
            if _should_pause_worker(current_pause, workflow_ids, connection=connection):
                return DispatchReport(claimed=0, succeeded=0, failed=0, quarantined=0)
            dispatch_ids = _pause_dispatch_scope(connection, workflow_ids, workflow_id_prefix, current_pause)
            if current_pause and current_pause.get("code") == "PROVIDER_CAPACITY_WAIT":
                dispatch_ids = _capacity_dispatch_scope(connection, workflow_ids, workflow_id_prefix)
                if not dispatch_ids:
                    return DispatchReport(claimed=0, succeeded=0, failed=0, quarantined=0)
            dispatcher.reclaim_expired()
            report = dispatcher.process_batch(
                # Keep auto progression serial: do not lease jobs whose QC
                # predecessor has not reported a safe result yet.
                batch_size=1,
                workflow_id_prefix=None if dispatch_ids is not None else workflow_id_prefix,
                workflow_ids=dispatch_ids,
            )
            _tick_ui_maintenance(connection)
            return report
        while not stop_event.is_set():
            try:
                current_pause = ensure_pipeline_pause(connection)
                if _should_pause_worker(current_pause, workflow_ids, connection=connection):
                    stop_event.wait(poll_interval)
                    continue
                dispatch_ids = _pause_dispatch_scope(connection, workflow_ids, workflow_id_prefix, current_pause)
                if current_pause and current_pause.get("code") == "PROVIDER_CAPACITY_WAIT":
                    dispatch_ids = _capacity_dispatch_scope(connection, workflow_ids, workflow_id_prefix)
                    if not dispatch_ids:
                        stop_event.wait(poll_interval)
                        continue
                dispatcher.reclaim_expired()
                dispatcher.process_batch(batch_size=1,
                    workflow_id_prefix=None if dispatch_ids is not None else workflow_id_prefix,
                    workflow_ids=dispatch_ids)
                _tick_ui_maintenance(connection)
            except FatalPersistenceError as exc:
                logger.error("Fatal persistence error in durable worker: %s", exc, exc_info=True)
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error("Error in durable worker polling loop: %s", exc, exc_info=True)
            stop_event.wait(poll_interval)
    finally:
        logger.info("Durable worker shutting down, closing database connection...")
        try:
            connection.close()
        except Exception:  # noqa: BLE001
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes durable background worker")
    parser.add_argument("--db-path", default=None, help="Path to SQLite database file")
    parser.add_argument(
        "--poll-interval", type=float, default=1.0, help="Worker outbox poll interval in seconds"
    )
    parser.add_argument(
        "--provider-mode",
        default=None,
        help="Provider mode ('configured' for live MCP/provider resolution, 'fake' for offline deterministic test). Defaults to config.yaml value.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process one eligible batch and exit",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1,
        help="Maximum outbox rows processed per batch",
    )
    parser.add_argument(
        "--workflow-id-prefix",
        default=None,
        help="Only process workflows whose IDs start with this value",
    )
    parser.add_argument(
        "--render-poll-timeout",
        type=float,
        default=None,
        help="Video render status polling timeout in seconds",
    )
    parser.add_argument(
        "--render-poll-interval",
        type=float,
        default=None,
        help="Video render status polling interval in seconds",
    )
    args = parser.parse_args(argv)
    from video_factory.constants import get_hermes_home

    home = get_hermes_home()
    if args.db_path:
        db_path = Path(args.db_path)
    elif (home / "hermes.db").exists():
        db_path = home / "hermes.db"
    elif (home / "hermes_personal.db").exists():
        db_path = home / "hermes_personal.db"
    else:
        db_path = home / "hermes.db"

    # Bootstrap configuration and environment secrets
    try:
        from hermes_cli.env_loader import load_hermes_dotenv
        load_hermes_dotenv(hermes_home=home)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to load hermes dotenv: %s", exc)

    try:
        from video_factory.config import load_config
        config = load_config()
    except Exception:
        try:
            from hermes_cli.config import load_config
            config = load_config()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to load hermes config: %s", exc)
            config = {}

    from video_factory.provider_runtime import (
        resolve_polling_setting,
        resolve_provider_mode,
    )

    resolved_provider_mode = resolve_provider_mode(
        cli_arg=args.provider_mode,
        config=config,
        env_val=os.environ.get("HERMES_PROVIDER_MODE"),
    )

    vf_config = config.get("video_factory", {}) if isinstance(config.get("video_factory"), dict) else (config.get("workflows", {}) if isinstance(config.get("workflows"), dict) else {})
    render_poll_timeout = resolve_polling_setting(
        "polling_timeout_seconds",
        cli_val=args.render_poll_timeout,
        cfg_val=vf_config.get("polling_timeout_seconds"),
        default=60.0,
    )
    render_poll_interval = resolve_polling_setting(
        "polling_interval_seconds",
        cli_val=args.render_poll_interval,
        cfg_val=vf_config.get("polling_interval_seconds"),
        default=0.5,
    )

    # Initialize MCP tool discovery if running in configured mode
    if resolved_provider_mode == "configured":
        try:
            from tools.mcp_tool import discover_mcp_tools
            discover_mcp_tools()
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP tool discovery failed on worker startup: %s", exc)

    stop_event = threading.Event()

    def _signal_handler(signum: int, frame: Any) -> None:
        logger.info("Received signal %s, stopping durable worker...", signum)
        stop_event.set()

    try:
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
    except (ValueError, AttributeError):
        pass

    try:
        report = run_worker(
            db_path=db_path,
            poll_interval=args.poll_interval,
            stop_event=stop_event,
            provider_mode=resolved_provider_mode,
            render_poll_timeout=render_poll_timeout,
            render_poll_interval=render_poll_interval,
            once=args.once,
            batch_size=args.batch_size,
            workflow_id_prefix=args.workflow_id_prefix,
        )
    finally:
        if resolved_provider_mode == "configured":
            try:
                from tools.mcp_tool import shutdown_mcp_servers
                shutdown_mcp_servers()
            except Exception as exc:  # noqa: BLE001
                logger.debug("MCP shutdown failed: %s", exc)
    if isinstance(report, DispatchReport):
        logger.info(
            "One-shot worker result: claimed=%d succeeded=%d failed=%d quarantined=%d stale=%d",
            report.claimed,
            report.succeeded,
            report.failed,
            report.quarantined,
            report.stale,
        )
        if report.failed or report.quarantined or report.stale:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

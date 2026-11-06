"""SQLite persistence for trend discovery history, observations, and selections."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .models import (
    AggregatedCandidate,
    CandidateObservation,
    PreviousTrendSignal,
    ProductHistory,
    ProviderRunSummary,
    SelectionResult,
)


@dataclass
class SessionCategoryRun:
    """Discovery run outcome for a single category within a daily session."""
    run_id: str
    selections: SelectionResult | None
    provider_summary: list[ProviderRunSummary]
    candidate_count: int = 0


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.fromisoformat(val)
    except ValueError:
        return None


class TrendHistoryRepository:
    """Manages SQLite tables for trend observations, history, and selections."""

    def __init__(self, db_path: Path | str):
        self.path = Path(db_path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS discovery_runs (
                    run_id TEXT PRIMARY KEY,
                    run_date TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    criteria_json TEXT,
                    provider_summary_json TEXT,
                    candidate_count INTEGER DEFAULT 0,
                    selected_count INTEGER DEFAULT 0,
                    failure_code TEXT,
                    failure_message TEXT,
                    is_current INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS product_history (
                    canonical_key TEXT PRIMARY KEY,
                    canonical_name TEXT NOT NULL,
                    brand TEXT,
                    model TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_selected_at TEXT,
                    selection_count INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS trend_observations (
                    observation_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    run_date TEXT NOT NULL,
                    canonical_key TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    source_host TEXT NOT NULL,
                    source_rank INTEGER DEFAULT 0,
                    signal_type TEXT NOT NULL,
                    signal_value REAL,
                    observed_at TEXT NOT NULL,
                    published_at TEXT,
                    title TEXT,
                    snippet TEXT,
                    metadata_json TEXT,
                    UNIQUE(run_id, canonical_key, provider, source_url)
                );

                CREATE TABLE IF NOT EXISTS daily_selections (
                    run_id TEXT NOT NULL,
                    run_date TEXT NOT NULL,
                    canonical_key TEXT NOT NULL,
                    trend_score REAL NOT NULL,
                    freshness_score REAL NOT NULL,
                    confidence_score REAL NOT NULL,
                    final_score REAL NOT NULL,
                    selection_status TEXT NOT NULL,
                    selected_rank INTEGER NOT NULL,
                    reason_codes_json TEXT,
                    score_breakdown_json TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, canonical_key)
                );

                CREATE TABLE IF NOT EXISTS schema_migrations (
                    migration_version TEXT NOT NULL,
                    run_date TEXT NOT NULL,
                    canonical_key TEXT NOT NULL,
                    applied_at TEXT NOT NULL,
                    UNIQUE(run_date, canonical_key, migration_version)
                );

                CREATE INDEX IF NOT EXISTS idx_discovery_runs_date_current ON discovery_runs(run_date, is_current);
                CREATE INDEX IF NOT EXISTS idx_trend_observations_key ON trend_observations(canonical_key);
                CREATE INDEX IF NOT EXISTS idx_daily_selections_key_date ON daily_selections(canonical_key, run_date);
            """)

    def begin_run(self, run_date: date, criteria: dict[str, Any]) -> str:
        run_id = f"run_{run_date.isoformat()}_{uuid.uuid4().hex[:8]}"
        now = _iso_now()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute(
                """
                INSERT INTO discovery_runs (
                    run_id, run_date, started_at, status, criteria_json, is_current
                ) VALUES (?, ?, ?, 'running', ?, 0);
                """,
                (run_id, run_date.isoformat(), now, json.dumps(criteria, ensure_ascii=False)),
            )
            conn.execute("COMMIT;")
        return run_id

    def save_observations(self, run_id: str, rows: list[tuple[str, CandidateObservation]]) -> None:
        if not rows:
            return
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            for canonical_key, obs in rows:
                obs_id = uuid.uuid4().hex
                now = _iso_now()
                # Upsert observation
                conn.execute(
                    """
                    INSERT OR IGNORE INTO trend_observations (
                        observation_id, run_id, run_date, canonical_key, provider,
                        source_url, source_host, source_rank, signal_type, signal_value,
                        observed_at, published_at, title, snippet, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        obs_id,
                        run_id,
                        obs.observed_at.date().isoformat(),
                        canonical_key,
                        obs.provider,
                        obs.source_url,
                        obs.source_host,
                        obs.source_rank,
                        obs.signal_type,
                        obs.signal_value,
                        obs.observed_at.isoformat(),
                        obs.published_at.isoformat() if obs.published_at else None,
                        obs.title,
                        obs.snippet,
                        json.dumps(obs.metadata, ensure_ascii=False),
                    ),
                )
                # Ensure product_history has entry
                conn.execute(
                    """
                    INSERT INTO product_history (
                        canonical_key, canonical_name, brand, model,
                        first_seen_at, last_seen_at, selection_count,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                    ON CONFLICT(canonical_key) DO UPDATE SET
                        last_seen_at = excluded.last_seen_at,
                        updated_at = excluded.updated_at;
                    """,
                    (
                        canonical_key,
                        obs.candidate_name,
                        obs.brand,
                        obs.model,
                        now,
                        now,
                        now,
                        now,
                    ),
                )
            conn.execute("COMMIT;")

    def get_product_history(self, canonical_key: str) -> ProductHistory | None:
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT canonical_key, canonical_name, brand, model,
                       first_seen_at, last_seen_at, last_selected_at, selection_count
                FROM product_history
                WHERE canonical_key = ?;
                """,
                (canonical_key,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return ProductHistory(
                canonical_key=row["canonical_key"],
                canonical_name=row["canonical_name"],
                brand=row["brand"] or "",
                model=row["model"] or "",
                first_seen_at=_parse_dt(row["first_seen_at"]) or datetime.now(timezone.utc),
                last_seen_at=_parse_dt(row["last_seen_at"]) or datetime.now(timezone.utc),
                last_selected_at=_parse_dt(row["last_selected_at"]),
                selection_count=row["selection_count"],
            )

    def get_previous_signal(self, canonical_key: str, before: datetime) -> PreviousTrendSignal | None:
        before_date_str = before.date().isoformat()
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT ds.canonical_key, ds.trend_score, ds.run_date, ds.created_at,
                       COUNT(DISTINCT obs.source_host) as host_count,
                       MIN(obs.source_rank) as min_rank
                FROM daily_selections ds
                LEFT JOIN trend_observations obs
                  ON obs.run_id = ds.run_id AND obs.canonical_key = ds.canonical_key
                WHERE ds.canonical_key = ? AND ds.run_date < ?
                GROUP BY ds.run_id, ds.canonical_key
                ORDER BY ds.run_date DESC, ds.created_at DESC
                LIMIT 1;
                """,
                (canonical_key, before_date_str),
            )
            row = cursor.fetchone()
            if not row:
                return None
            rec_dt = _parse_dt(row["created_at"]) or datetime.combine(
                date.fromisoformat(row["run_date"]), datetime.min.time(), timezone.utc
            )
            return PreviousTrendSignal(
                canonical_key=row["canonical_key"],
                trend_score=row["trend_score"],
                recorded_at=rec_dt,
                independent_source_count=row["host_count"] or 0,
                min_source_rank=row["min_rank"],
            )

    def get_recent_selected_identities(self, limit: int = 40) -> list[dict[str, str]]:
        """Return recently selected product identities so query planner can avoid repeating them."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT canonical_key, canonical_name, brand, model, last_selected_at
                FROM product_history
                WHERE last_selected_at IS NOT NULL AND selection_count > 0
                ORDER BY last_selected_at DESC
                LIMIT ?;
                """,
                (limit,),
            )
            rows = cursor.fetchall()
            return [
                {
                    "canonical_key": row["canonical_key"] or "",
                    "name": row["canonical_name"] or "",
                    "brand": row["brand"] or "",
                    "model": row["model"] or "",
                }
                for row in rows
            ]

    def _insert_selection(
        self,
        cursor: sqlite3.Cursor,
        run_id: str,
        run_date: date,
        item: Any,
        status: str,
        reasons: list[str],
        rank: int,
    ) -> None:
        now = _iso_now()
        cursor.execute(
            """
            INSERT INTO daily_selections (
                run_id, run_date, canonical_key, trend_score, freshness_score,
                confidence_score, final_score, selection_status, selected_rank,
                reason_codes_json, score_breakdown_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                run_id,
                run_date.isoformat(),
                item.candidate.canonical_key,
                item.trend_score,
                item.freshness_score,
                item.confidence_score,
                item.final_score,
                status,
                rank,
                json.dumps(reasons, ensure_ascii=False),
                json.dumps(item.score_breakdown.model_dump(), ensure_ascii=False),
                now,
            ),
        )

        selected_at = datetime.combine(run_date, datetime.min.time(), tzinfo=timezone.utc).isoformat()

        cursor.execute(
            """
            INSERT INTO product_history (
                canonical_key, canonical_name, brand, model,
                first_seen_at, last_seen_at, last_selected_at, selection_count,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(canonical_key) DO UPDATE SET
                canonical_name = excluded.canonical_name,
                brand = excluded.brand,
                model = excluded.model,
                last_seen_at = excluded.last_seen_at,
                last_selected_at = excluded.last_selected_at,
                selection_count = product_history.selection_count + 1,
                updated_at = excluded.updated_at;
            """,
            (
                item.candidate.canonical_key,
                item.candidate.name,
                item.candidate.brand,
                item.candidate.model,
                selected_at,
                selected_at,
                selected_at,
                now,
                now,
            ),
        )

    def complete_session_runs(
        self,
        run_date: date,
        session_runs: list[SessionCategoryRun],
    ) -> None:
        """Atomically complete multiple category runs belonging to the same daily session.

        Inserts all selections into daily_selections, updates product_history for all selected items,
        marks all participating discovery_runs as completed and current (is_current = 1),
        while resetting any prior runs for that date to is_current = 0.
        """
        now = _iso_now()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                # 1. Clear previous is_current for this date once
                conn.execute(
                    "UPDATE discovery_runs SET is_current = 0 WHERE run_date = ?;",
                    (run_date.isoformat(),),
                )

                # 2. Insert selections and mark each run completed and current
                for cat_run in session_runs:
                    if cat_run.selections:
                        for rank, item in enumerate(cat_run.selections.selected, start=1):
                            ck = item.candidate.canonical_key
                            status = cat_run.selections.statuses.get(ck, "new")
                            reasons = cat_run.selections.reason_codes.get(ck, [])
                            self._insert_selection(conn, cat_run.run_id, run_date, item, status, reasons, rank)

                    summary_data = [
                        s.model_dump() if hasattr(s, "model_dump") else dict(s)
                        for s in cat_run.provider_summary
                    ]
                    selected_cnt = len(cat_run.selections.selected) if cat_run.selections else 0
                    conn.execute(
                        """
                        UPDATE discovery_runs
                        SET status = 'completed',
                            completed_at = ?,
                            is_current = 1,
                            candidate_count = ?,
                            selected_count = ?,
                            provider_summary_json = ?
                        WHERE run_id = ?;
                        """,
                        (now, cat_run.candidate_count, selected_cnt, json.dumps(summary_data, ensure_ascii=False), cat_run.run_id),
                    )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def complete_run(
        self,
        run_id: str,
        selections: SelectionResult,
        provider_summary: list[ProviderRunSummary],
        candidate_count: int = 0,
    ) -> None:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT run_date FROM discovery_runs WHERE run_id = ?;", (run_id,))
            run_row = cur.fetchone()
            if not run_row:
                raise RuntimeError(f"Run {run_id} not found.")
            run_date = date.fromisoformat(run_row["run_date"])

        cat_run = SessionCategoryRun(
            run_id=run_id,
            selections=selections,
            provider_summary=provider_summary,
            candidate_count=candidate_count,
        )
        self.complete_session_runs(run_date, [cat_run])

    def mark_export_failed(self, run_id: str, message: str) -> None:
        self.mark_session_export_failed([run_id], message)

    def mark_session_export_failed(self, run_ids: list[str], message: str) -> None:
        if not run_ids:
            return
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            for rid in run_ids:
                conn.execute(
                    """
                    UPDATE discovery_runs
                    SET status = 'export_failed',
                        is_current = 0,
                        failure_message = ?
                    WHERE run_id = ?;
                    """,
                    (message, rid),
                )
            conn.execute("COMMIT;")

    def fail_run(self, run_id: str, code: str, message: str) -> None:
        now = _iso_now()
        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute(
                """
                UPDATE discovery_runs
                SET status = 'failed',
                    completed_at = ?,
                    is_current = 0,
                    failure_code = ?,
                    failure_message = ?
                WHERE run_id = ?;
                """,
                (now, code, message, run_id),
            )
            conn.execute("COMMIT;")

    def get_current_run(self, run_date: date) -> dict[str, Any] | None:
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT run_id, run_date, started_at, completed_at, status,
                       criteria_json, provider_summary_json, candidate_count,
                       selected_count, failure_code, failure_message, is_current
                FROM discovery_runs
                WHERE run_date = ? AND is_current = 1 AND status = 'completed'
                ORDER BY started_at ASC, run_id ASC;
                """,
                (run_date.isoformat(),),
            )
            row = cur.fetchone()
            if not row:
                return None
            return dict(row)

    def get_current_runs(self, run_date: date) -> list[dict[str, Any]]:
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                SELECT run_id, run_date, started_at, completed_at, status,
                       criteria_json, provider_summary_json, candidate_count,
                       selected_count, failure_code, failure_message, is_current
                FROM discovery_runs
                WHERE run_date = ? AND is_current = 1 AND status = 'completed'
                ORDER BY started_at ASC, run_id ASC;
                """,
                (run_date.isoformat(),),
            )
            return [dict(row) for row in cur.fetchall()]

    def apply_backfill_selection(
        self,
        run_date: date,
        candidate: AggregatedCandidate,
        migration_version: str = "daily-products-v1",
    ) -> bool:
        """Apply a historical product selection from legacy products.json idempotently."""
        now = _iso_now()
        date_str = run_date.isoformat()
        run_dt_str = datetime.combine(run_date, datetime.min.time(), timezone.utc).isoformat()

        with self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                # Check migration marker
                cur = conn.execute(
                    """
                    SELECT 1 FROM schema_migrations
                    WHERE run_date = ? AND canonical_key = ? AND migration_version = ?;
                    """,
                    (date_str, candidate.canonical_key, migration_version),
                )
                if cur.fetchone():
                    conn.execute("COMMIT;")
                    return False

                # Record migration marker
                conn.execute(
                    """
                    INSERT INTO schema_migrations (
                        migration_version, run_date, canonical_key, applied_at
                    ) VALUES (?, ?, ?, ?);
                    """,
                    (migration_version, date_str, candidate.canonical_key, now),
                )

                # Upsert product history
                cur = conn.execute(
                    "SELECT last_selected_at, selection_count FROM product_history WHERE canonical_key = ?;",
                    (candidate.canonical_key,),
                )
                existing = cur.fetchone()

                if existing:
                    last_selected = existing["last_selected_at"]
                    # If this backfilled date is newer than existing last_selected_at, update it
                    new_last_selected = (
                        run_dt_str
                        if not last_selected or run_dt_str > last_selected
                        else last_selected
                    )
                    conn.execute(
                        """
                        UPDATE product_history
                        SET last_selected_at = ?,
                            selection_count = selection_count + 1,
                            last_seen_at = ?,
                            updated_at = ?
                        WHERE canonical_key = ?;
                        """,
                        (new_last_selected, now, now, candidate.canonical_key),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO product_history (
                            canonical_key, canonical_name, brand, model,
                            first_seen_at, last_seen_at, last_selected_at,
                            selection_count, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?);
                        """,
                        (
                            candidate.canonical_key,
                            candidate.name,
                            candidate.brand,
                            candidate.model,
                            run_dt_str,
                            now,
                            run_dt_str,
                            now,
                            now,
                        ),
                    )

                conn.execute("COMMIT;")
                return True
            except Exception:
                conn.execute("ROLLBACK;")
                raise

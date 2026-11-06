from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS global_user_identity (
  owner_user_id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_identity (
  channel TEXT NOT NULL,
  external_subject TEXT NOT NULL,
  owner_user_id TEXT NOT NULL REFERENCES global_user_identity(owner_user_id),
  created_at TEXT NOT NULL,
  PRIMARY KEY (channel, external_subject)
);
CREATE INDEX IF NOT EXISTS channel_identity_owner_idx
  ON channel_identity(owner_user_id, channel);
CREATE TABLE IF NOT EXISTS conversation_session (
  session_id TEXT PRIMARY KEY,
  owner_user_id TEXT NOT NULL REFERENCES global_user_identity(owner_user_id) ON DELETE RESTRICT,
  channel TEXT NOT NULL,
  external_conversation_id TEXT NOT NULL,
  active_workflow_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(owner_user_id, channel, external_conversation_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS conversation_session_identity_idx
  ON conversation_session(session_id, owner_user_id);
CREATE TABLE IF NOT EXISTS workflow_handoff (
  handoff_id TEXT PRIMARY KEY,
  owner_user_id TEXT NOT NULL REFERENCES global_user_identity(owner_user_id) ON DELETE RESTRICT,
  workflow_id TEXT NOT NULL,
  source_session_id TEXT NOT NULL REFERENCES conversation_session(session_id) ON DELETE RESTRICT,
  target_session_id TEXT NOT NULL REFERENCES conversation_session(session_id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL,
  UNIQUE(workflow_id, target_session_id)
);
CREATE TABLE IF NOT EXISTS turn_execution (
  owner_user_id TEXT NOT NULL REFERENCES global_user_identity(owner_user_id) ON DELETE RESTRICT,
  idempotency_key TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  turn_id TEXT NOT NULL UNIQUE,
  session_id TEXT NOT NULL,
  user_message TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK(status IN ('in_progress', 'completed', 'failed')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (owner_user_id, idempotency_key),
  FOREIGN KEY (session_id, owner_user_id)
    REFERENCES conversation_session(session_id, owner_user_id) ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS turn_event (
  turn_id TEXT NOT NULL REFERENCES turn_execution(turn_id) ON DELETE RESTRICT,
  sequence INTEGER NOT NULL CHECK(sequence >= 0),
  event_id TEXT NOT NULL UNIQUE,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (turn_id, sequence)
);
CREATE TABLE IF NOT EXISTS owner_event_cursor (
  cursor INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_user_id TEXT NOT NULL REFERENCES global_user_identity(owner_user_id) ON DELETE RESTRICT,
  event_id TEXT NOT NULL UNIQUE REFERENCES turn_event(event_id) ON DELETE RESTRICT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS owner_event_cursor_replay_idx
  ON owner_event_cursor(owner_user_id, cursor);
CREATE TABLE IF NOT EXISTS legacy_session_cutover (
  session_id TEXT PRIMARY KEY REFERENCES conversation_session(session_id) ON DELETE CASCADE,
  cutover_turn_id TEXT NOT NULL,
  legacy_messages_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS capability_snapshot (
  snapshot_id TEXT PRIMARY KEY,
  digest TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS capability_snapshot_binding (
  snapshot_id TEXT NOT NULL REFERENCES capability_snapshot(snapshot_id) ON DELETE CASCADE,
  capability_id TEXT NOT NULL,
  binding_json TEXT NOT NULL,
  PRIMARY KEY (snapshot_id, capability_id)
);
CREATE TABLE IF NOT EXISTS capability_catalog_state (
  state_id INTEGER PRIMARY KEY CHECK(state_id = 1),
  active_snapshot_id TEXT NOT NULL REFERENCES capability_snapshot(snapshot_id) ON DELETE RESTRICT,
  activated_at TEXT NOT NULL
);
-- New workflow persistence tables
CREATE TABLE IF NOT EXISTS workflow (
  workflow_id TEXT PRIMARY KEY,
  aggregate_json TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS workflow_event (
  event_id TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL REFERENCES workflow(workflow_id) ON DELETE CASCADE,
  event_type TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_command (
  workflow_id TEXT NOT NULL REFERENCES workflow(workflow_id) ON DELETE CASCADE,
  command_id TEXT NOT NULL,
  UNIQUE(workflow_id, command_id)
);
CREATE TABLE IF NOT EXISTS workflow_outbox (
  outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_id TEXT NOT NULL,
  message TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending', 'leased', 'quarantined')),
  lease_token TEXT,
  lease_owner TEXT,
  lease_expires_at TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 5,
  next_attempt_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Governed learning tables
CREATE TABLE IF NOT EXISTS task_outcome (
  outcome_id TEXT PRIMARY KEY,
  source_type TEXT NOT NULL,
  source_id TEXT NOT NULL,
  status TEXT NOT NULL,
  details_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS learning_candidate (
  candidate_id TEXT PRIMARY KEY,
  owner_user_id TEXT NOT NULL REFERENCES global_user_identity(owner_user_id) ON DELETE RESTRICT,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  digest TEXT NOT NULL,
  source_outcome_id TEXT REFERENCES task_outcome(outcome_id) ON DELETE SET NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(owner_user_id, kind, digest)
);

CREATE TABLE IF NOT EXISTS evidence_ref (
  evidence_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL REFERENCES learning_candidate(candidate_id) ON DELETE CASCADE,
  source_type TEXT NOT NULL,
  source_id TEXT NOT NULL,
  digest TEXT NOT NULL,
  excerpt TEXT NOT NULL,
  polarity TEXT NOT NULL DEFAULT 'positive',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS learning_version (
  version_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL REFERENCES learning_candidate(candidate_id) ON DELETE CASCADE,
  owner_user_id TEXT NOT NULL REFERENCES global_user_identity(owner_user_id) ON DELETE RESTRICT,
  version INTEGER NOT NULL,
  content TEXT NOT NULL,
  digest TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(candidate_id, version)
);

CREATE TABLE IF NOT EXISTS candidate_evaluation (
  evaluation_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL REFERENCES learning_candidate(candidate_id) ON DELETE CASCADE,
  evaluator_name TEXT NOT NULL,
  decision TEXT NOT NULL,
  score REAL NOT NULL,
  reason TEXT NOT NULL,
  evidence_summary TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS learning_event (
    event_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES learning_candidate(candidate_id) ON DELETE CASCADE,
    version_id TEXT REFERENCES learning_version(version_id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Full-text search index for approved knowledge retrieval
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
  title,
  content,
  candidate_id UNINDEXED,
  version_id UNINDEXED,
  owner_user_id UNINDEXED,
  kind UNINDEXED,
  tokenize='unicode61 remove_diacritics 2'
);

-- Source artifacts for knowledge grounding (P1.1)
CREATE TABLE IF NOT EXISTS source_artifact (
  artifact_id TEXT PRIMARY KEY,
  candidate_id TEXT NOT NULL REFERENCES learning_candidate(candidate_id) ON DELETE CASCADE,
  owner_user_id TEXT NOT NULL REFERENCES global_user_identity(owner_user_id) ON DELETE RESTRICT,
  kind TEXT NOT NULL,
  filename TEXT NOT NULL,
  relative_path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL DEFAULT 0,
  mime_type TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_source_artifact_cand ON source_artifact(candidate_id, kind);
CREATE INDEX IF NOT EXISTS idx_source_artifact_sha ON source_artifact(sha256);

-- Personal memo / Quick notes storage (decoupled from knowledge base)
CREATE TABLE IF NOT EXISTS personal_memo (
  memo_id TEXT PRIMARY KEY,
  owner_user_id TEXT NOT NULL REFERENCES global_user_identity(owner_user_id) ON DELETE RESTRICT,
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  group_name TEXT NOT NULL DEFAULT 'general',
  tags_json TEXT NOT NULL DEFAULT '[]',
  pinned INTEGER NOT NULL DEFAULT 0,
  archived INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memo_owner_group ON personal_memo(owner_user_id, group_name, archived);
CREATE INDEX IF NOT EXISTS idx_memo_owner_pinned ON personal_memo(owner_user_id, pinned, updated_at);

CREATE VIRTUAL TABLE IF NOT EXISTS memo_fts USING fts5(
  title,
  content,
  tags,
  memo_id UNINDEXED,
  owner_user_id UNINDEXED,
  group_name UNINDEXED,
  tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS budget_reservation (
  reservation_id TEXT PRIMARY KEY,
  owner_user_id TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  channel TEXT NOT NULL,
  amount INTEGER NOT NULL CHECK(amount > 0),
  currency TEXT NOT NULL DEFAULT 'USD',
  idempotency_key TEXT NOT NULL,
  request_digest TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('reserved', 'committed', 'released')),
  capability_id TEXT NOT NULL DEFAULT 'general',
  operation_input_digest TEXT NOT NULL DEFAULT '',
  policy_id TEXT NOT NULL DEFAULT 'default',
  policy_version INTEGER NOT NULL DEFAULT 1,
  policy_snapshot_json TEXT NOT NULL DEFAULT '{}',
  external_reference TEXT,
  campaign_id TEXT NOT NULL DEFAULT '',
  authorization_id TEXT NOT NULL DEFAULT '',
  attempt_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(owner_user_id, scope_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS budget_reservation_owner_scope_idx
  ON budget_reservation(owner_user_id, scope_id, state, channel);

CREATE TABLE IF NOT EXISTS product_video_operation_attempt (
  attempt_id TEXT PRIMARY KEY,
  authorization_id TEXT NOT NULL,
  campaign_id TEXT NOT NULL,
  owner_user_id TEXT NOT NULL,
  workflow_id TEXT NOT NULL,
  capability_id TEXT NOT NULL,
  attempt_number INTEGER NOT NULL CHECK(attempt_number > 0),
  input_fingerprint TEXT NOT NULL,
  estimated_cost REAL,
  reservation_id TEXT,
  provider_operation_id TEXT,
  status TEXT NOT NULL,
  actual_cost INTEGER,
  cost_status TEXT NOT NULL DEFAULT 'unknown',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(authorization_id, workflow_id, capability_id, attempt_number)
);
CREATE INDEX IF NOT EXISTS product_video_attempt_workflow_idx
  ON product_video_operation_attempt(workflow_id, capability_id, attempt_number);

CREATE TABLE IF NOT EXISTS daily_product_video_import (
  product_key TEXT PRIMARY KEY,
  canonical_product_key TEXT NOT NULL DEFAULT '',
  source_date TEXT NOT NULL,
  source_batch_id TEXT NOT NULL DEFAULT '',
  source_product_id TEXT NOT NULL,
  source_path TEXT NOT NULL,
  workflow_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN ('discovered', 'imported', 'claimed', 'started', 'error', 'reserved')),
  worker_id TEXT,
  claimed_at TEXT,
  lease_expires_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS daily_product_video_import_date_idx
  ON daily_product_video_import(source_date, status);

-- Video Factory Artifacts & Render Batches
CREATE TABLE IF NOT EXISTS video_factory_artifacts (
  artifact_id TEXT PRIMARY KEY,
  owner_user_id TEXT NOT NULL,
  workflow_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  scene_id TEXT NOT NULL DEFAULT '',
  frame_role TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 1,
  attempt INTEGER NOT NULL DEFAULT 1,
  relative_path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size INTEGER NOT NULL DEFAULT 0,
  mime_type TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  parent_artifact_ids_json TEXT NOT NULL DEFAULT '[]',
  provider_metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vf_artifacts_proj ON video_factory_artifacts(project_id, kind);
CREATE INDEX IF NOT EXISTS idx_vf_artifacts_sha ON video_factory_artifacts(sha256);

CREATE TABLE IF NOT EXISTS video_factory_render_batches (
  render_batch_id TEXT PRIMARY KEY,
  owner_user_id TEXT NOT NULL,
  workflow_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'pending',
  scene_count INTEGER NOT NULL DEFAULT 0,
  completed_scene_count INTEGER NOT NULL DEFAULT 0,
  timeline_artifact_id TEXT,
  composite_artifact_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vf_batches_proj ON video_factory_render_batches(project_id, version);

"""


def _migrate_daily_product_video_import(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='daily_product_video_import'"
    ).fetchone()
    if not row:
        return
    current_sql = row["sql"] if hasattr(row, "keys") else row[0]
    if not current_sql:
        return
    if "'claimed'" in current_sql and "daily_product_video_import" in current_sql:
        return

    cols = [
        r["name"] if hasattr(r, "keys") else r[1]
        for r in connection.execute("PRAGMA table_info(daily_product_video_import)").fetchall()
    ]
    old_count = connection.execute("SELECT COUNT(*) FROM daily_product_video_import").fetchone()[0]

    connection.execute(
        """
        CREATE TABLE daily_product_video_import_v2 (
          product_key TEXT PRIMARY KEY,
          canonical_product_key TEXT NOT NULL DEFAULT '',
          source_date TEXT NOT NULL,
          source_batch_id TEXT NOT NULL DEFAULT '',
          source_product_id TEXT NOT NULL,
          source_path TEXT NOT NULL,
          workflow_id TEXT NOT NULL UNIQUE,
          status TEXT NOT NULL CHECK(status IN ('discovered', 'imported', 'claimed', 'started', 'error', 'reserved')),
          worker_id TEXT,
          claimed_at TEXT,
          lease_expires_at TEXT,
          last_error TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )

    canon_col = "canonical_product_key" if "canonical_product_key" in cols else "''"
    batch_col = "source_batch_id" if "source_batch_id" in cols else "''"
    worker_col = "worker_id" if "worker_id" in cols else "NULL"
    claimed_col = "claimed_at" if "claimed_at" in cols else "NULL"
    lease_col = "lease_expires_at" if "lease_expires_at" in cols else "NULL"
    last_err_col = "last_error" if "last_error" in cols else "NULL"

    connection.execute(
        f"""
        INSERT INTO daily_product_video_import_v2 (
            product_key, canonical_product_key, source_date, source_batch_id,
            source_product_id, source_path, workflow_id, status, worker_id,
            claimed_at, lease_expires_at, last_error, created_at, updated_at
        )
        SELECT
            product_key, {canon_col}, source_date, {batch_col},
            source_product_id, source_path, workflow_id, status, {worker_col},
            {claimed_col}, {lease_col}, {last_err_col}, created_at, updated_at
        FROM daily_product_video_import
        """
    )

    new_count = connection.execute("SELECT COUNT(*) FROM daily_product_video_import_v2").fetchone()[0]
    if old_count != new_count:
        raise RuntimeError(
            f"Migration failed: row count mismatch (old={old_count}, new={new_count})"
        )

    connection.execute("DROP TABLE daily_product_video_import")
    connection.execute("ALTER TABLE daily_product_video_import_v2 RENAME TO daily_product_video_import")
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS daily_product_video_import_date_idx
          ON daily_product_video_import(source_date, status);
        """
    )
    _backfill_canonical_product_keys(connection)


def _backfill_canonical_product_keys(connection: sqlite3.Connection) -> None:
    tbl = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_product_video_import'"
    ).fetchone()
    if not tbl:
        return
    rows = connection.execute(
        "SELECT product_key, workflow_id FROM daily_product_video_import WHERE canonical_product_key = '' OR canonical_product_key IS NULL"
    ).fetchall()
    for row in rows:
        pkey = row["product_key"] if hasattr(row, "keys") else row[0]
        wfid = row["workflow_id"] if hasattr(row, "keys") else row[1]
        wf_row = connection.execute(
            "SELECT aggregate_json FROM workflow WHERE workflow_id = ?",
            (wfid,),
        ).fetchone()
        if not wf_row:
            continue
        raw_json = wf_row["aggregate_json"] if hasattr(wf_row, "keys") else wf_row[0]
        try:
            import json
            agg = json.loads(raw_json)
            src = agg.get("source_context", {})
            brand = src.get("brand")
            model = src.get("model")
            variant = src.get("variant")
            if brand and model:
                from video_factory.automation.contracts import compute_canonical_product_key
                ckey = compute_canonical_product_key(brand, model, variant)
                connection.execute(
                    "UPDATE daily_product_video_import SET canonical_product_key = ? WHERE product_key = ?",
                    (ckey, pkey),
                )
        except Exception:
            pass


def _populate_knowledge_fts(connection: sqlite3.Connection) -> None:
    """Populate knowledge FTS index from existing approved learning versions."""
    try:
        count = connection.execute("SELECT COUNT(*) FROM knowledge_fts").fetchone()[0]
        if count > 0:
            return  # Already populated
    except sqlite3.OperationalError:
        return  # Table doesn't exist yet

    try:
        connection.execute(
            """
            INSERT INTO knowledge_fts (title, content, candidate_id, version_id, owner_user_id, kind)
            SELECT c.title, v.content, c.candidate_id, v.version_id, v.owner_user_id, c.kind
            FROM learning_version v
            JOIN learning_candidate c ON v.candidate_id = c.candidate_id
            WHERE v.status = 'active' AND c.status = 'approved'
            """
        )
    except sqlite3.OperationalError:
        pass  # learning tables may not exist yet


def _migrate_schema(connection: sqlite3.Connection) -> None:
    with connection:
        _migrate_daily_product_video_import(connection)
        _backfill_canonical_product_keys(connection)
        columns = [
            row["name"]

            for row in connection.execute("PRAGMA table_info(turn_execution)").fetchall()
        ]
        if columns and "user_message" not in columns:
            connection.execute(
                "ALTER TABLE turn_execution ADD COLUMN user_message TEXT NOT NULL DEFAULT ''"
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS legacy_session_cutover (
              session_id TEXT PRIMARY KEY REFERENCES conversation_session(session_id) ON DELETE CASCADE,
              cutover_turn_id TEXT NOT NULL,
              legacy_messages_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS capability_snapshot (
              snapshot_id TEXT PRIMARY KEY,
              digest TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS capability_snapshot_binding (
              snapshot_id TEXT NOT NULL REFERENCES capability_snapshot(snapshot_id) ON DELETE CASCADE,
              capability_id TEXT NOT NULL,
              binding_json TEXT NOT NULL,
              PRIMARY KEY (snapshot_id, capability_id)
            );
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS capability_catalog_state (
              state_id INTEGER PRIMARY KEY CHECK(state_id = 1),
              active_snapshot_id TEXT NOT NULL REFERENCES capability_snapshot(snapshot_id) ON DELETE RESTRICT,
              activated_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS budget_reservation (
              reservation_id TEXT PRIMARY KEY,
              owner_user_id TEXT NOT NULL,
              scope_id TEXT NOT NULL,
              channel TEXT NOT NULL,
              amount INTEGER NOT NULL CHECK(amount > 0),
              currency TEXT NOT NULL DEFAULT 'USD',
              idempotency_key TEXT NOT NULL,
              request_digest TEXT NOT NULL,
              state TEXT NOT NULL CHECK(state IN ('reserved', 'committed', 'released')),
              capability_id TEXT NOT NULL DEFAULT 'general',
              operation_input_digest TEXT NOT NULL DEFAULT '',
              policy_id TEXT NOT NULL DEFAULT 'default',
              policy_version INTEGER NOT NULL DEFAULT 1,
              policy_snapshot_json TEXT NOT NULL DEFAULT '{}',
              external_reference TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(owner_user_id, scope_id, idempotency_key)
            );
            """
        )
        b_cols = [
            row["name"]
            for row in connection.execute("PRAGMA table_info(budget_reservation)").fetchall()
        ]
        if b_cols:
            if "capability_id" not in b_cols:
                connection.execute("ALTER TABLE budget_reservation ADD COLUMN capability_id TEXT NOT NULL DEFAULT 'general'")
            if "operation_input_digest" not in b_cols:
                connection.execute("ALTER TABLE budget_reservation ADD COLUMN operation_input_digest TEXT NOT NULL DEFAULT ''")
            if "policy_id" not in b_cols:
                connection.execute("ALTER TABLE budget_reservation ADD COLUMN policy_id TEXT NOT NULL DEFAULT 'default'")
            if "policy_version" not in b_cols:
                connection.execute("ALTER TABLE budget_reservation ADD COLUMN policy_version INTEGER NOT NULL DEFAULT 1")
            if "policy_snapshot_json" not in b_cols:
                connection.execute("ALTER TABLE budget_reservation ADD COLUMN policy_snapshot_json TEXT NOT NULL DEFAULT '{}'")
            if "campaign_id" not in b_cols:
                connection.execute("ALTER TABLE budget_reservation ADD COLUMN campaign_id TEXT NOT NULL DEFAULT ''")
            if "authorization_id" not in b_cols:
                connection.execute("ALTER TABLE budget_reservation ADD COLUMN authorization_id TEXT NOT NULL DEFAULT ''")
            if "attempt_id" not in b_cols:
                connection.execute("ALTER TABLE budget_reservation ADD COLUMN attempt_id TEXT NOT NULL DEFAULT ''")

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS budget_reservation_owner_scope_idx
              ON budget_reservation(owner_user_id, scope_id, state, channel);
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS budget_reservation_campaign_idx
              ON budget_reservation(campaign_id, state, channel);
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS product_video_operation_attempt (
              attempt_id TEXT PRIMARY KEY,
              authorization_id TEXT NOT NULL,
              campaign_id TEXT NOT NULL,
              owner_user_id TEXT NOT NULL,
              workflow_id TEXT NOT NULL,
              capability_id TEXT NOT NULL,
              attempt_number INTEGER NOT NULL CHECK(attempt_number > 0),
              input_fingerprint TEXT NOT NULL,
              estimated_cost REAL,
              reservation_id TEXT,
              provider_operation_id TEXT,
              status TEXT NOT NULL,
              actual_cost INTEGER,
              cost_status TEXT NOT NULL DEFAULT 'unknown',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(authorization_id, workflow_id, capability_id, attempt_number)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS product_video_attempt_workflow_idx
              ON product_video_operation_attempt(workflow_id, capability_id, attempt_number);
            """
        )
        attempt_cols = {row["name"] for row in connection.execute("PRAGMA table_info(product_video_operation_attempt)").fetchall()}
        if "cost_status" not in attempt_cols:
            connection.execute("ALTER TABLE product_video_operation_attempt ADD COLUMN cost_status TEXT NOT NULL DEFAULT 'unknown'")
        if "actual_cost_usd" not in attempt_cols:
            connection.execute("ALTER TABLE product_video_operation_attempt ADD COLUMN actual_cost_usd REAL")
        if "actual_provider" not in attempt_cols:
            connection.execute("ALTER TABLE product_video_operation_attempt ADD COLUMN actual_provider TEXT")
        if "actual_model" not in attempt_cols:
            connection.execute("ALTER TABLE product_video_operation_attempt ADD COLUMN actual_model TEXT")
        attempt_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='product_video_operation_attempt'"
        ).fetchone()
        if attempt_sql and "estimated_cost > 0" in str(attempt_sql[0]).lower():
            # QC attempts no longer reserve an estimated dollar amount. Rebuild
            # this small local ledger table while preserving every existing row.
            connection.execute("DROP INDEX IF EXISTS product_video_attempt_workflow_idx")
            connection.execute("ALTER TABLE product_video_operation_attempt RENAME TO product_video_operation_attempt_legacy")
            connection.execute(
                """CREATE TABLE product_video_operation_attempt (
                    attempt_id TEXT PRIMARY KEY,
                    authorization_id TEXT NOT NULL,
                    campaign_id TEXT NOT NULL,
                    owner_user_id TEXT NOT NULL,
                    workflow_id TEXT NOT NULL,
                    capability_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL CHECK(attempt_number > 0),
                    input_fingerprint TEXT NOT NULL,
                    estimated_cost REAL,
                    reservation_id TEXT,
                    provider_operation_id TEXT,
                    status TEXT NOT NULL,
                    actual_cost INTEGER,
                    cost_status TEXT NOT NULL DEFAULT 'unknown',
                    actual_cost_usd REAL,
                    actual_provider TEXT,
                    actual_model TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(authorization_id, workflow_id, capability_id, attempt_number)
                )"""
            )
            connection.execute(
                """INSERT INTO product_video_operation_attempt (
                    attempt_id, authorization_id, campaign_id, owner_user_id, workflow_id,
                    capability_id, attempt_number, input_fingerprint, estimated_cost,
                    reservation_id, provider_operation_id, status, actual_cost, cost_status,
                    actual_cost_usd, actual_provider, actual_model, created_at, updated_at
                ) SELECT attempt_id, authorization_id, campaign_id, owner_user_id, workflow_id,
                    capability_id, attempt_number, input_fingerprint, estimated_cost,
                    reservation_id, provider_operation_id, status, actual_cost, cost_status,
                    NULL, NULL, NULL, created_at, updated_at
                    FROM product_video_operation_attempt_legacy"""
            )
            connection.execute("DROP TABLE product_video_operation_attempt_legacy")
            connection.execute(
                "CREATE INDEX product_video_attempt_workflow_idx ON product_video_operation_attempt(workflow_id, capability_id, attempt_number)"
            )

        wo_cols = [
            row["name"]
            for row in connection.execute("PRAGMA table_info(workflow_outbox)").fetchall()
        ]
        if wo_cols:
            if "state" not in wo_cols:
                connection.execute("ALTER TABLE workflow_outbox ADD COLUMN state TEXT NOT NULL DEFAULT 'pending'")
            if "lease_token" not in wo_cols:
                connection.execute("ALTER TABLE workflow_outbox ADD COLUMN lease_token TEXT")
            if "lease_owner" not in wo_cols:
                connection.execute("ALTER TABLE workflow_outbox ADD COLUMN lease_owner TEXT")
            if "lease_expires_at" not in wo_cols:
                connection.execute("ALTER TABLE workflow_outbox ADD COLUMN lease_expires_at TEXT")
            if "attempt_count" not in wo_cols:
                connection.execute("ALTER TABLE workflow_outbox ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0")
            if "max_attempts" not in wo_cols:
                connection.execute("ALTER TABLE workflow_outbox ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 5")
            if "next_attempt_at" not in wo_cols:
                connection.execute("ALTER TABLE workflow_outbox ADD COLUMN next_attempt_at TEXT")
            if "last_error" not in wo_cols:
                connection.execute("ALTER TABLE workflow_outbox ADD COLUMN last_error TEXT")
            if "updated_at" not in wo_cols:
                connection.execute("ALTER TABLE workflow_outbox ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
                connection.execute("UPDATE workflow_outbox SET updated_at = created_at WHERE updated_at = ''")

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS workflow_outbox_claim_idx
              ON workflow_outbox(state, next_attempt_at, outbox_id);
            """
        )
        
        # Populate knowledge FTS from existing approved versions if FTS is empty
        _populate_knowledge_fts(connection)


def connect(database_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.executescript(SCHEMA)
    _migrate_schema(connection)
    return connection

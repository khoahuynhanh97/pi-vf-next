from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4


@dataclass
class OutboxMessage:
    outbox_id: int
    workflow_id: str
    message: str
    state: str = "pending"
    lease_token: str | None = None
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    attempt_count: int = 0
    max_attempts: int = 5
    next_attempt_at: str | None = None
    last_error: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass
class OutboxClaim:
    outbox_id: int
    workflow_id: str
    message: str
    lease_token: str
    lease_owner: str
    lease_expires_at: str
    attempt_count: int = 0
    max_attempts: int = 5
    created_at: str = ""
    updated_at: str = ""


def _now_iso(now_iso: str | None = None) -> str:
    if now_iso is not None:
        return now_iso
    return datetime.now(timezone.utc).isoformat()


def _add_seconds(iso_str: str, seconds: float) -> str:
    dt = datetime.fromisoformat(iso_str)
    dt_new = dt + timedelta(seconds=seconds)
    return dt_new.isoformat()


class OutboxRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.connection.row_factory = sqlite3.Row

    def enqueue(
        self,
        workflow_id: str,
        message: str,
        max_attempts: int = 5,
        created_at: str | None = None,
    ) -> OutboxMessage:
        now = _now_iso(created_at)
        with self.connection:
            cur = self.connection.execute(
                """
                INSERT INTO workflow_outbox (
                    workflow_id, message, state, attempt_count, max_attempts, created_at, updated_at
                ) VALUES (?, ?, 'pending', 0, ?, ?, ?)
                """,
                (workflow_id, message, max_attempts, now, now),
            )
            outbox_id = cur.lastrowid
        return OutboxMessage(
            outbox_id=outbox_id,
            workflow_id=workflow_id,
            message=message,
            state="pending",
            attempt_count=0,
            max_attempts=max_attempts,
            created_at=now,
            updated_at=now,
        )

    def claim(
        self,
        worker_id: str,
        lease_duration_seconds: float = 30.0,
        batch_size: int = 10,
        now_iso: str | None = None,
        workflow_id_prefix: str | None = None,
        workflow_ids: list[str] | None = None,
    ) -> list[OutboxClaim]:
        """Atomically claim a batch of outbox rows for delivery.

        attempt_count is incremented exactly once per successful claim of a
        pending row.  Expired-lease rows are also incremented on takeover
        (repeated crash/expiry consumes the bounded attempt budget).

        Quarantine boundary: a pending row is quarantined ON CLAIM when
        new_attempt_count > max_attempts, i.e. max_attempts deliveries have
        already occurred and there is no budget for another one.  With
        max_attempts=3, delivery is attempted on claims where attempt_count
        becomes 1, 2, and 3; only the hypothetical 4th claim quarantines.

        Transaction safety: if the caller has already opened a transaction,
        claim() uses a savepoint to avoid committing/rolling back the caller's
        work.  If no transaction is active, claim() opens BEGIN IMMEDIATE and
        commits/rollbacks at the end.
        """
        if workflow_ids == []:
            return []
        now = _now_iso(now_iso)
        claims: list[OutboxClaim] = []

        # Determine if we own the transaction or must use a savepoint.
        use_savepoint = self.connection.in_transaction
        savepoint_name = f"claim_sp_{uuid4().hex[:8]}"

        if use_savepoint:
            self.connection.execute(f"SAVEPOINT {savepoint_name}")
        else:
            # Propagate OperationalError if we cannot get the write lock.
            self.connection.execute("BEGIN IMMEDIATE")

        try:
            target_clause = ""
            params: list[object] = [now, now]
            if workflow_ids is not None:
                placeholders = ",".join("?" for _ in workflow_ids)
                target_clause = f" AND workflow_id IN ({placeholders})"
                params.extend(workflow_ids)
            elif workflow_id_prefix is not None:
                target_clause = " AND substr(workflow_id, 1, ?) = ?"
                params.extend([len(workflow_id_prefix), workflow_id_prefix])
            params.append(batch_size)
            cur = self.connection.execute(
                f"""
                SELECT outbox_id, workflow_id, message, state, attempt_count, max_attempts, created_at, updated_at
                FROM workflow_outbox
                WHERE ((state = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
                   OR (state = 'leased' AND lease_expires_at <= ?))
                  {target_clause}
                ORDER BY outbox_id ASC
                LIMIT ?
                """,
                params,
            )
            rows = cur.fetchall()

            for row in rows:
                state = row["state"]
                if state == "pending":
                    new_attempt_count = row["attempt_count"] + 1
                    if new_attempt_count > row["max_attempts"]:
                        # All delivery budget consumed; quarantine.
                        # Conditional WHERE guards against a concurrent UPDATE.
                        r = self.connection.execute(
                            """
                            UPDATE workflow_outbox
                            SET state = 'quarantined',
                                attempt_count = ?,
                                last_error = 'max_attempts_exceeded_on_claim',
                                lease_token = NULL,
                                lease_owner = NULL,
                                lease_expires_at = NULL,
                                updated_at = ?
                            WHERE outbox_id = ? AND state = 'pending'
                            """,
                            (new_attempt_count, now, row["outbox_id"]),
                        )
                        if r.rowcount != 1:
                            # Lost race – another worker already handled it.
                            pass
                        continue

                    token = f"claim_{uuid4().hex}"
                    expires_at = _add_seconds(now, lease_duration_seconds)
                    # Conditional UPDATE: only succeeds if state is still
                    # 'pending' and the backoff window has passed.  rowcount==0
                    # means another worker won the race.
                    r = self.connection.execute(
                        """
                        UPDATE workflow_outbox
                        SET state = 'leased',
                            lease_token = ?,
                            lease_owner = ?,
                            lease_expires_at = ?,
                            attempt_count = ?,
                            updated_at = ?
                        WHERE outbox_id = ?
                          AND state = 'pending'
                          AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                        """,
                        (token, worker_id, expires_at, new_attempt_count, now, row["outbox_id"], now),
                    )
                    if r.rowcount != 1:
                        continue  # Lost race – skip this row.
                    claims.append(
                        OutboxClaim(
                            outbox_id=row["outbox_id"],
                            workflow_id=row["workflow_id"],
                            message=row["message"],
                            lease_token=token,
                            lease_owner=worker_id,
                            lease_expires_at=expires_at,
                            attempt_count=new_attempt_count,
                            max_attempts=row["max_attempts"],
                            created_at=row["created_at"],
                            updated_at=now,
                        )
                    )
                else:
                    # Expired-lease takeover: increment attempt_count to count this retry.
                    # Repeated crash/expiry must consume the bounded attempt budget.
                    new_attempt_count = row["attempt_count"] + 1
                    if new_attempt_count > row["max_attempts"]:
                        # All delivery budget consumed; quarantine.
                        r = self.connection.execute(
                            """
                            UPDATE workflow_outbox
                            SET state = 'quarantined',
                                attempt_count = ?,
                                last_error = 'max_attempts_exceeded_on_claim',
                                lease_token = NULL,
                                lease_owner = NULL,
                                lease_expires_at = NULL,
                                updated_at = ?
                            WHERE outbox_id = ?
                              AND state = 'leased'
                              AND lease_expires_at <= ?
                            """,
                            (new_attempt_count, now, row["outbox_id"], now),
                        )
                        if r.rowcount != 1:
                            pass  # Lost race – another worker handled it.
                        continue

                    token = f"claim_{uuid4().hex}"
                    expires_at = _add_seconds(now, lease_duration_seconds)
                    r = self.connection.execute(
                        """
                        UPDATE workflow_outbox
                        SET lease_token = ?,
                            lease_owner = ?,
                            lease_expires_at = ?,
                            attempt_count = ?,
                            updated_at = ?
                        WHERE outbox_id = ?
                          AND state = 'leased'
                          AND lease_expires_at <= ?
                        """,
                        (token, worker_id, expires_at, new_attempt_count, now, row["outbox_id"], now),
                    )
                    if r.rowcount != 1:
                        continue  # Lost race – skip this row.
                    claims.append(
                        OutboxClaim(
                            outbox_id=row["outbox_id"],
                            workflow_id=row["workflow_id"],
                            message=row["message"],
                            lease_token=token,
                            lease_owner=worker_id,
                            lease_expires_at=expires_at,
                            attempt_count=new_attempt_count,
                            max_attempts=row["max_attempts"],
                            created_at=row["created_at"],
                            updated_at=now,
                        )
                    )

            if use_savepoint:
                self.connection.execute(f"RELEASE {savepoint_name}")
            else:
                self.connection.execute("COMMIT")
        except Exception:
            try:
                if use_savepoint:
                    self.connection.execute(f"ROLLBACK TO {savepoint_name}")
                else:
                    self.connection.execute("ROLLBACK")
            except Exception:
                pass
            raise

        return claims

    def extend_lease(
        self,
        outbox_id: int,
        lease_token: str,
        worker_id: str,
        extension_seconds: float = 60.0,
        now_iso: str | None = None,
    ) -> bool:
        """Extend the active lease expiration time for a running job (heartbeat).

        Guarantees that renewal NEVER reduces the current lease_expires_at.
        """
        now = _now_iso(now_iso)
        candidate_expires_at = _add_seconds(now, extension_seconds)
        with self.connection:
            cur = self.connection.execute(
                """
                UPDATE workflow_outbox
                SET lease_expires_at = CASE
                        WHEN lease_expires_at IS NOT NULL AND lease_expires_at > ? THEN lease_expires_at
                        ELSE ?
                    END,
                    updated_at = ?
                WHERE outbox_id = ?
                  AND lease_token = ?
                  AND lease_owner = ?
                  AND state = 'leased'
                  AND lease_expires_at > ?
                """,
                (candidate_expires_at, candidate_expires_at, now, outbox_id, lease_token, worker_id, now),
            )
            return cur.rowcount > 0

    def ack(
        self,
        outbox_id: int,
        lease_token: str,
        worker_id: str,
        now_iso: str | None = None,
    ) -> bool:
        """Delete the row iff the lease is still active and owned by worker_id."""
        now = _now_iso(now_iso)
        with self.connection:
            cur = self.connection.execute(
                """
                DELETE FROM workflow_outbox
                WHERE outbox_id = ?
                  AND lease_token = ?
                  AND lease_owner = ?
                  AND state = 'leased'
                  AND lease_expires_at > ?
                """,
                (outbox_id, lease_token, worker_id, now),
            )
            return cur.rowcount > 0

    def defer(
        self,
        outbox_id: int,
        lease_token: str,
        worker_id: str,
        delay_seconds: float,
        now_iso: str | None = None,
    ) -> bool:
        """Yield an active lease without consuming the failure allowance.

        Keep attempt_count as total claims; one max_attempts credit offsets this
        healthy claim without erasing any prior failures or expired leases.
        """
        now = _now_iso(now_iso)
        with self.connection:
            cur = self.connection.execute(
                """
                UPDATE workflow_outbox
                SET state = 'pending', next_attempt_at = ?,
                    max_attempts = max_attempts + 1,
                    lease_token = NULL, lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = ?
                WHERE outbox_id = ? AND lease_token = ? AND lease_owner = ?
                  AND state = 'leased' AND lease_expires_at > ?
                """,
                (_add_seconds(now, delay_seconds), now, outbox_id, lease_token, worker_id, now),
            )
            return cur.rowcount == 1

    def record_failure(
        self,
        outbox_id: int,
        lease_token: str,
        worker_id: str,
        error_message: str,
        backoff_base_seconds: float = 2.0,
        max_backoff_seconds: float = 300.0,
        now_iso: str | None = None,
        force_quarantine: bool = False,
    ) -> str:
        """Record a delivery failure. attempt_count is NOT incremented here;
        it was already incremented at claim time. Quarantine immediately if
        force_quarantine is True or when attempt_count >= max_attempts.
        """
        now = _now_iso(now_iso)
        with self.connection:
            cur = self.connection.execute(
                """
                SELECT attempt_count, max_attempts FROM workflow_outbox
                WHERE outbox_id = ? AND lease_token = ? AND lease_owner = ? AND state = 'leased'
                """,
                (outbox_id, lease_token, worker_id),
            )
            row = cur.fetchone()
            if row is None:
                return "stale_lease"

            attempt_count = row["attempt_count"]
            max_attempts = row["max_attempts"]

            if force_quarantine or attempt_count >= max_attempts:
                self.connection.execute(
                    """
                    UPDATE workflow_outbox
                    SET state = 'quarantined',
                        last_error = ?,
                        lease_token = NULL,
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        next_attempt_at = NULL,
                        updated_at = ?
                    WHERE outbox_id = ?
                    """,
                    (error_message, now, outbox_id),
                )
                return "quarantined"
            else:
                backoff = min(
                    max_backoff_seconds,
                    backoff_base_seconds ** attempt_count,
                )
                next_attempt_at = _add_seconds(now, backoff)
                self.connection.execute(
                    """
                    UPDATE workflow_outbox
                    SET state = 'pending',
                        next_attempt_at = ?,
                        last_error = ?,
                        lease_token = NULL,
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        updated_at = ?
                    WHERE outbox_id = ?
                    """,
                    (next_attempt_at, error_message, now, outbox_id),
                )
                return "retry_scheduled"

    def reclaim_expired(self, now_iso: str | None = None) -> int:
        """Reset or quarantine expired leases.

        Rows whose lease has expired AND attempt_count >= max_attempts are
        quarantined (they have no remaining delivery budget).  Others are
        reset to pending so the next claim can increment and retry.

        attempt_count is NOT incremented here – incrementing happens at claim.
        """
        now = _now_iso(now_iso)
        with self.connection:
            cur1 = self.connection.execute(
                """
                UPDATE workflow_outbox
                SET state = 'quarantined',
                    last_error = 'lease_expired_max_attempts_exceeded',
                    lease_token = NULL,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    updated_at = ?
                WHERE state = 'leased' AND lease_expires_at <= ? AND attempt_count >= max_attempts
                """,
                (now, now),
            )
            cur2 = self.connection.execute(
                """
                UPDATE workflow_outbox
                SET state = 'pending',
                    lease_token = NULL,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    updated_at = ?
                WHERE state = 'leased' AND lease_expires_at <= ? AND attempt_count < max_attempts
                """,
                (now, now),
            )
            return cur1.rowcount + cur2.rowcount

    def list_unacknowledged(self) -> list[OutboxMessage]:
        """Return only pending (not in-flight, not quarantined) outbox rows.

        Leased rows are actively being processed by a worker and must not be
        exposed as freely replayable messages.
        """
        with self.connection:
            cur = self.connection.execute(
                """
                SELECT outbox_id, workflow_id, message, state, lease_token, lease_owner,
                       lease_expires_at, attempt_count, max_attempts, next_attempt_at,
                       last_error, created_at, updated_at
                FROM workflow_outbox
                WHERE state = 'pending'
                ORDER BY outbox_id ASC
                """
            )
            rows = cur.fetchall()
            return [
                OutboxMessage(
                    outbox_id=row["outbox_id"],
                    workflow_id=row["workflow_id"],
                    message=row["message"],
                    state=row["state"],
                    lease_token=row["lease_token"],
                    lease_owner=row["lease_owner"],
                    lease_expires_at=row["lease_expires_at"],
                    attempt_count=row["attempt_count"],
                    max_attempts=row["max_attempts"],
                    next_attempt_at=row["next_attempt_at"],
                    last_error=row["last_error"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
                for row in rows
            ]

    def ack_message(
        self,
        workflow_id: str,
        message: str,
        worker_id: str = "recovery_worker",
        now_iso: str | None = None,
    ) -> bool:
        """Acknowledge one specific outbox row by workflow_id + message.

        If the row has an active lease, ACK using the current active owner's
        token – we do not override ownership.  If the row is pending or has an
        expired lease, we do a targeted single-row claim then ACK.

        Does NOT mass-claim other rows; only ever touches the one matching row.
        Returns False if the row does not exist, is quarantined, or the claim
        races with another worker.
        """
        now = _now_iso(now_iso)
        with self.connection:
            cur = self.connection.execute(
                """
                SELECT outbox_id, lease_token, lease_owner, lease_expires_at, state
                FROM workflow_outbox
                WHERE workflow_id = ? AND message = ? AND state != 'quarantined'
                ORDER BY outbox_id ASC
                LIMIT 1
                """,
                (workflow_id, message),
            )
            row = cur.fetchone()
            if row is None:
                return False

            # Case 1: row has an active lease owned by another worker.
            # We MUST NOT override ownership. Return False (stale/denied).
            if (
                row["lease_token"] is not None
                and row["lease_owner"] is not None
                and row["lease_expires_at"] is not None
                and row["lease_expires_at"] > now
            ):
                # If the caller's worker_id matches the active owner, allow ACK.
                # Otherwise, deny – cannot steal another worker's active lease.
                if row["lease_owner"] != worker_id:
                    return False
                return self.ack(
                    outbox_id=row["outbox_id"],
                    lease_token=row["lease_token"],
                    worker_id=row["lease_owner"],
                    now_iso=now,
                )

            # Case 2: row is pending or has an expired lease – targeted claim.
            token = f"recovery_{uuid4().hex}"
            expires_at = _add_seconds(now, 60.0)
            r = self.connection.execute(
                """
                UPDATE workflow_outbox
                SET state = 'leased',
                    lease_token = ?,
                    lease_owner = ?,
                    lease_expires_at = ?,
                    attempt_count = attempt_count + 1,
                    updated_at = ?
                WHERE outbox_id = ?
                  AND state != 'quarantined'
                  AND (state = 'pending' OR (state = 'leased' AND lease_expires_at <= ?))
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                """,
                (token, worker_id, expires_at, now, row["outbox_id"], now, now),
            )
            if r.rowcount != 1:
                return False  # Lost race with another worker.
            return self.ack(
                outbox_id=row["outbox_id"],
                lease_token=token,
                worker_id=worker_id,
                now_iso=now,
            )

    def requeue_quarantined(
        self,
        outbox_id: int,
        now_iso: str | None = None,
        *,
        in_transaction: bool = False,
        preserve_attempts: bool = False,
    ) -> bool:
        now = _now_iso(now_iso)
        if preserve_attempts:
            sql = """
                UPDATE workflow_outbox
                SET state = 'pending', next_attempt_at = NULL,
                    lease_token = NULL, lease_owner = NULL, lease_expires_at = NULL,
                    updated_at = ?
                WHERE outbox_id = ? AND state = 'quarantined'
            """
        else:
            sql = """
                UPDATE workflow_outbox
                SET state = 'pending', attempt_count = 0, next_attempt_at = NULL,
                    last_error = NULL, lease_token = NULL, lease_owner = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE outbox_id = ? AND state = 'quarantined'
            """
        if in_transaction:
            cur = self.connection.execute(sql, (now, outbox_id))
            return cur.rowcount > 0
        with self.connection:
            cur = self.connection.execute(sql, (now, outbox_id))
            return cur.rowcount > 0

    def requeue_quarantined_in_transaction(
        self, outbox_id: int, now_iso: str | None = None, *, preserve_attempts: bool = False
    ) -> bool:
        """Requeue a quarantined outbox row inside a caller-managed transaction."""
        return self.requeue_quarantined(
            outbox_id, now_iso=now_iso, in_transaction=True, preserve_attempts=preserve_attempts
        )

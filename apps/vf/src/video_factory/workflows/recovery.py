from __future__ import annotations

import sqlite3
from typing import List, Tuple

from .outbox_repository import OutboxRepository


class WorkflowRecoveryService:
    """Recovery service wrapping OutboxRepository while preserving backward compatibility.

    - ``recover_pending_workflows`` returns workflow IDs present in the
      ``workflow`` table.
    - ``replay_outbox`` returns unacknowledged messages without deleting them.
    - ``ack_outbox`` deletes a message after downstream delivery succeeds.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.conn = connection
        self.conn.row_factory = sqlite3.Row
        self.outbox_repository = OutboxRepository(connection)
        self.outbox_repo = self.outbox_repository

    def recover_pending_workflows(self) -> List[str]:
        cur = self.conn.cursor()
        cur.execute("SELECT workflow_id FROM workflow")
        return [row["workflow_id"] for row in cur.fetchall()]

    def replay_outbox(self) -> List[Tuple[str, str]]:
        messages = self.outbox_repository.list_unacknowledged()
        return [(msg.workflow_id, msg.message) for msg in messages]

    def ack_outbox(self, workflow_id: str, message: str) -> None:
        """Acknowledge one delivered outbox record using its stable payload."""
        self.outbox_repository.ack_message(workflow_id, message)

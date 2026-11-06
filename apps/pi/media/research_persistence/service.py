"""ResearchPersistenceService: persists research results as immutable snapshots.

Persistence failure never erases the in-memory computed result — the caller
keeps the ProductResearchResult and receives a clear persistence outcome.
"""

from __future__ import annotations

from .models import ResearchSnapshot, make_snapshot, research_id_from_result
from .repository import ResearchRepository, SQLiteResearchRepository
from media.config import research_snapshots_db_path


class ResearchPersistenceService:
    def __init__(self, repository: ResearchRepository | None = None):
        self.repository = repository or SQLiteResearchRepository(research_snapshots_db_path())

    def save(self, result, *, research_id: str | None = None) -> tuple[str, dict]:
        """Persist the result. Returns (snapshot_id, persistence_status).

        status: "saved" on success; "skipped" for ERROR results; raises a
        clearly-scoped persistence error otherwise (result is not lost).
        """
        if result.status.value == "error":
            return "", {"status": "skipped", "reason": "ERROR results are not automatically persisted"}
        snapshot = make_snapshot(result, research_id=research_id)
        try:
            snapshot_id = self.repository.save_snapshot(snapshot)
        except Exception as exc:  # noqa: BLE001 - persistence failure must be surfaced, not swallowed
            return "", {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}
        return snapshot_id, {"status": "saved", "research_id": snapshot.research_id}

    def get(self, snapshot_id: str) -> ResearchSnapshot:
        return self.repository.get_snapshot(snapshot_id)

    def get_latest(self, research_id: str) -> ResearchSnapshot | None:
        return self.repository.get_latest_snapshot(research_id)

    def list(self, research_id: str, limit: int = 20) -> list[ResearchSnapshot]:
        return self.repository.list_snapshots(research_id, limit=limit)

    def research_id_for(self, result) -> str:
        return research_id_from_result(result)

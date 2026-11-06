"""ResourcePackService: build drafts, evaluate readiness, and lock immutable packs."""

from __future__ import annotations

from media.research.models import ProductResearchResult
from media.config import resource_pack_locks_db_path

from .builder import ResourcePackBuilder
from .lock_store import ResourcePackLockStore
from .models import ResourcePack, ResourcePackLock


class ResourcePackService:
    def __init__(self, *, builder: ResourcePackBuilder | None = None, lock_store: ResourcePackLockStore | None = None):
        self.builder = builder or ResourcePackBuilder()
        self.lock_store = lock_store or ResourcePackLockStore(resource_pack_locks_db_path())

    def build(self, result: ProductResearchResult, *, variant_id: str | None = None, research_snapshot_id: str = "") -> ResourcePack:
        return self.builder.build(result, variant_id=variant_id, research_snapshot_id=research_snapshot_id)

    def build_from_snapshot(self, snapshot, *, variant_id: str | None = None) -> ResourcePack:
        """Build a pack from a persisted research snapshot (historical result)."""
        result = ProductResearchResult.model_validate(snapshot.result)
        return self.build(result, variant_id=variant_id, research_snapshot_id=snapshot.snapshot_id)

    def lock(self, pack: ResourcePack) -> ResourcePackLock:
        return self.lock_store.lock(pack)

    def get_latest(self, resource_pack_id: str) -> ResourcePackLock | None:
        return self.lock_store.get_latest_lock(resource_pack_id)

    def list_locks(self, resource_pack_id: str, limit: int = 20) -> list[ResourcePackLock]:
        return self.lock_store.list_locks(resource_pack_id, limit=limit)

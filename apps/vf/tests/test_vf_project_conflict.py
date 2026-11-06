from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from video_factory.integrations.repository import (
    SQLiteVideoFactoryRepository,
    VideoFactoryConflictError,
)
from video_factory.integrations.service import VideoFactoryService


def test_stale_project_writer_cannot_erase_committed_state(tmp_path: Path) -> None:
    database = tmp_path / "factory.sqlite"
    first_repo = SQLiteVideoFactoryRepository(database)
    second_repo = SQLiteVideoFactoryRepository(database)
    VideoFactoryService(first_repo).create_project("owner", "project")
    first = first_repo.get_owned("project", "owner")
    second = second_repo.get_owned("project", "owner")
    assert first is not None and second is not None
    saved = first_repo.save(replace(first, draft_video_asset_id="approved-clip"))
    assert first_repo.save(replace(first, draft_video_asset_id="approved-clip")) == saved
    with pytest.raises(VideoFactoryConflictError, match="VIDEO_FACTORY_CONFLICT"):
        second_repo.save(replace(second, final_video_asset_id="different-clip"))
    retained = second_repo.get_owned("project", "owner")
    assert retained is not None
    assert retained.draft_video_asset_id == "approved-clip"
    assert retained.final_video_asset_id is None
    resolved = second_repo.save(replace(retained, final_video_asset_id="different-clip"))
    assert resolved.revision == 2
    assert resolved.draft_video_asset_id == "approved-clip"
    assert resolved.final_video_asset_id == "different-clip"


def test_concurrent_project_writers_get_one_conflict(tmp_path: Path) -> None:
    database = tmp_path / "factory.sqlite"
    repo = SQLiteVideoFactoryRepository(database)
    VideoFactoryService(repo).create_project("owner", "project")
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def worker(field: str) -> None:
        local_repo = SQLiteVideoFactoryRepository(database)
        project = local_repo.get_owned("project", "owner")
        assert project is not None
        barrier.wait()
        try:
            local_repo.save(replace(project, **{field: field}))
            outcomes.append("saved")
        except VideoFactoryConflictError:
            outcomes.append("conflict")

    threads = [
        threading.Thread(target=worker, args=("draft_video_asset_id",)),
        threading.Thread(target=worker, args=("final_video_asset_id",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert sorted(outcomes) == ["conflict", "saved"]


def test_historical_project_revision_migrates_without_payload_rewrite(tmp_path: Path) -> None:
    database = tmp_path / "historical.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("""
        CREATE TABLE video_factory_projects (
            id TEXT PRIMARY KEY, owner_user_id TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
    """)
    connection.commit()
    connection.close()
    SQLiteVideoFactoryRepository(database)
    connection = sqlite3.connect(database)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(video_factory_projects)")}
    connection.close()
    assert "revision" in columns

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sqlite3
import subprocess

from PIL import Image


def _write_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (800, 800), color=(255, 255, 255)).save(path, format="PNG")


def test_real_module_entrypoint_execution(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "hermes_e2e.db"
    inbox_root = tmp_path / "exchange_inbox"
    batch_dir = inbox_root / "2026-09-13" / "run_test"
    batch_dir.mkdir(parents=True)

    img_path = batch_dir / "products" / "test_p" / "images" / "img_1.png"
    _write_image(img_path)

    manifest = {
        "schema_version": "1.0.0",
        "batch_id": "run_test",
        "target_date": "2026-09-13",
        "production_date": "2026-09-13",
        "producer_version": "1.0.0",
        "products": [
            {
                "priority": 1,
                "source_product_id": "p01",
                "name": "Jisulife Life 7",
                "brand": "Jisulife",
                "model": "Life 7",
                "canonical_product_key": "jisulife:life-7",
                "product_key": "1e96ac740ec2f41f35a92194ca77c69fa3249fd103fc4e792321eca63eafd38b",
                "price_display": "350.000đ",
                "images": [
                    {
                        "asset_id": "a01",
                        "relative_path": "products/test_p/images/img_1.png",
                        "match_score": 1.0,
                    }
                ],
            }
        ],
    }
    (batch_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (batch_dir / "READY").write_text("READY\n", encoding="utf-8")

    # Exercise the installed project's supported CLI entrypoint.
    completed = subprocess.run(
        [
            shutil.which("uv") or "uv",
            "run",
            "video-factory",
            "--db-path",
            str(db_path),
            "--inbox-root",
            str(inbox_root),
            "list",
            "--date",
            "2026-09-13",
        ],
        cwd=str(repo_root),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, f"Failed with {completed.stderr}"
    assert "Jisulife Life 7" in completed.stdout


def test_real_subprocess_start_auto_with_legacy_db(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    db_path = tmp_path / "hermes_legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE daily_product_video_import (
            product_key TEXT PRIMARY KEY,
            source_date TEXT NOT NULL,
            source_product_id TEXT NOT NULL,
            source_path TEXT NOT NULL,
            workflow_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK(status IN ('reserved', 'started', 'error')),
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            canonical_product_key TEXT NOT NULL DEFAULT '',
            source_batch_id TEXT NOT NULL DEFAULT '',
            worker_id TEXT,
            claimed_at TEXT,
            lease_expires_at TEXT
        );
        """
    )
    conn.close()

    inbox_root = tmp_path / "exchange_inbox"
    batch_dir = inbox_root / "2026-09-13" / "run_daily_01"
    batch_dir.mkdir(parents=True)

    img1 = batch_dir / "products" / "p01" / "images" / "img_1.png"
    img2 = batch_dir / "products" / "p01" / "images" / "img_2.png"
    img1.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (800, 800), color=(255, 255, 255)).save(img1, format="PNG")
    Image.new("RGB", (800, 800), color=(0, 0, 0)).save(img2, format="PNG")

    manifest = {
        "schema_version": "1.0.0",
        "batch_id": "run_daily_01",
        "target_date": "2026-09-13",
        "production_date": "2026-09-13",
        "producer_version": "1.0.0",
        "products": [
            {
                "priority": 1,
                "source_product_id": "p01",
                "name": "Jisulife Life 7",
                "brand": "Jisulife",
                "model": "Life 7",
                "canonical_product_key": "jisulife:life-7",
                "product_key": "1e96ac740ec2f41f35a92194ca77c69fa3249fd103fc4e792321eca63eafd38b",
                "price_display": "350.000đ",
                "images": [
                    {
                        "asset_id": "a01",
                        "relative_path": "products/p01/images/img_1.png",
                        "match_score": 1.0,
                    },
                    {
                        "asset_id": "a02",
                        "relative_path": "products/p01/images/img_2.png",
                        "match_score": 1.0,
                    },
                ],
            }
        ],
    }
    (batch_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (batch_dir / "READY").write_text("READY\n", encoding="utf-8")

    # Run start --auto with mock provider
    completed = subprocess.run(
        [
            shutil.which("uv") or "uv",
            "run",
            "video-factory",
            "--db-path",
            str(db_path),
            "--inbox-root",
            str(inbox_root),
            "start",
            "--auto",
            "--limit",
            "3",
            "--provider-mode",
            "fake",
            "--date",
            "2026-09-13",
        ],
        cwd=str(repo_root),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 1
    assert "no current image QC report matches artifact digest" in completed.stderr
    assert "Đã tạo 1 workflow mới" in completed.stdout

    # Verify migration succeeded and the created workflow is durably started.
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT status, canonical_product_key FROM daily_product_video_import WHERE canonical_product_key = ?",
        ("jisulife:life-7",),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "started"
    assert row[1] == "jisulife:life-7"

    # Replay safety: second run reports the existing workflow and exits with code 0.
    replay = subprocess.run(
        [
            shutil.which("uv") or "uv",
            "run",
            "video-factory",
            "--db-path",
            str(db_path),
            "--inbox-root",
            str(inbox_root),
            "start",
            "--auto",
            "--limit",
            "3",
            "--provider-mode",
            "fake",
            "--date",
            "2026-09-13",
        ],
        cwd=str(repo_root),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert replay.returncode == 0, f"Replay failed: {replay.stderr}\n{replay.stdout}"
    assert "Không có sản phẩm mới nào cần khởi động" in replay.stdout

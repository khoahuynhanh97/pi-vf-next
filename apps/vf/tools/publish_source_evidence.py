"""Publish one reviewed external claim archive into the creative evidence store.

Example: uv run python tools/publish_source_evidence.py --claim-json claim.json
         --record-dir D:/work/hermes-data/validated-source-evidence
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from video_factory.creative.trusted_evidence import publish_validated_record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claim-json", type=Path, required=True)
    parser.add_argument("--record-dir", type=Path, required=True)
    parser.add_argument("--source-root", type=Path)
    args = parser.parse_args()
    claim = json.loads(args.claim_json.read_text(encoding="utf-8"))
    if not isinstance(claim, dict):
        parser.error("claim JSON must be an object")
    record_id = publish_validated_record(
        claim, source_root=args.source_root, record_dir=args.record_dir,
    )
    print(record_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Offline test runner with network blocking guard and scratch isolation."""
from __future__ import annotations

import os
import socket
import sys
import tempfile
from pathlib import Path
import pytest

# Network isolation guard
_orig_socket_connect = socket.socket.connect

def _guarded_connect(self, address):
    # Allow loopback/unix sockets if needed, block all external network
    host = address[0] if isinstance(address, tuple) and len(address) > 0 else str(address)
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise RuntimeError(
            f"OFFLINE_GUARD: Outbound network call to {address} blocked during offline test run."
        )
    return _orig_socket_connect(self, address)

socket.socket.connect = _guarded_connect

# Ensure environment safety
os.environ.setdefault("HERMES_OFFLINE_GUARD", "1")
os.environ.setdefault("VF_PROVIDER_MODE", "configured")

# Add src to pythonpath if not present
src_dir = str(Path(__file__).resolve().parent.parent / "src")
if src_dir not in sys.path:
    sys.path.insert(0, src_dir)

if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        args = ["-q", "tests/test_auto_qc_pipeline_e2e.py"]
    exit_code = pytest.main(args)
    sys.exit(exit_code)

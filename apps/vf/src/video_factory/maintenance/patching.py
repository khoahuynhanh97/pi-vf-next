"""Scoped source transactions and an offline Docker validation boundary.

Receipts are authenticated within this controller process. JSON provided by a
coding model cannot authorize activation. Process restart deliberately requires
new validation/preflight and leaves ambiguous transactions for manual recovery.
The preflight issuer and idle callback are trusted controller ports, never model
tools. This module does not execute candidate Python in the host interpreter.
"""
from __future__ import annotations

import ast
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Iterable
import uuid


ALLOWED_SOURCE = frozenset({
    "src/video_factory/integrations/google_flow_provider.py",
    "src/video_factory/integrations/google_flow_keyframe_provider.py",
    "src/video_factory/integrations/flow_ui_recipe.py",
})
TRUSTED_TESTS = (
    "tests/test_google_flow_keyframe.py",
    "tests/test_end_keyframe_binding.py",
    "tests/test_keyframe_provider_flow_only.py",
)
REQUIRED_CAPABILITIES = frozenset({
    "model", "aspect", "output_count", "duration", "reference_limits",
    "upload_role", "start_binding", "end_behavior",
})
_RECEIPT_KEY = secrets.token_bytes(32)
_MAX_AGE = 600


@dataclass(frozen=True)
class PatchFile:
    path: str
    before_sha256: str | None
    content: str


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _issue(kind: str, **fields) -> dict:
    receipt = {"kind": kind, "issued_at": time.time(), **fields}
    receipt["signature"] = hmac.new(_RECEIPT_KEY, _canonical(receipt), hashlib.sha256).hexdigest()
    return receipt


def _authentic(receipt, kind: str) -> bool:
    if not isinstance(receipt, dict):
        return False
    try:
        value = {k: v for k, v in receipt.items() if k != "signature"}
        signature = hmac.new(_RECEIPT_KEY, _canonical(value), hashlib.sha256).hexdigest()
        age = time.time() - value["issued_at"]
        return (value["kind"] == kind and 0 <= age <= _MAX_AGE
                and hmac.compare_digest(signature, receipt.get("signature", "")))
    except (ValueError, TypeError, KeyError, OverflowError):
        return False


def _hashes(files: Iterable[PatchFile]) -> dict:
    return {f.path: _digest(f.content.encode("utf-8")) for f in files}


def verify_activation_receipt(receipt: dict, root: Path) -> bool:
    """A cached activation cannot outlive its process, age or source bytes."""
    if not _authentic(receipt, 'activation') or receipt.get('status') != 'activated':
        return False
    hashes = receipt.get('candidate_sha256', {})
    try:
        return bool(hashes) and all(_digest(_safe_target(Path(root), name).read_bytes()) == sha
                                    for name, sha in hashes.items())
    except (ValueError, OSError, TypeError):
        return False


def _safe_target(root: Path, name: str) -> Path:
    if not isinstance(name, str) or "\\" in name or ":" in name:
        raise ValueError("unsafe patch path")
    path = PurePosixPath(name)
    if path.is_absolute() or any(p in {".", ".."} for p in path.parts) or str(path) != name:
        raise ValueError("unsafe patch path")
    if name not in ALLOWED_SOURCE and not re.fullmatch(r"tests/test_flow_repair_[a-z0-9_]+\.py", name):
        raise ValueError("patch path outside whitelist")
    target = root / name
    _no_links(root, target)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("patch path outside source root")
    if target.exists() and not target.is_file():
        raise ValueError("patch target is not a file")
    return target


def _no_links(root: Path, target: Path) -> None:
    current = target
    while True:
        # is_junction is available on newer Python; reparse points also cover
        # Windows directory junctions on the supported Python 3.11 runtime.
        if current.is_symlink() or (current.exists() and getattr(current.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("symlink or reparse point forbidden")
        if current == root:
            break
        if current.parent == current:
            raise ValueError("target not beneath root")
        current = current.parent


def _source_hashes(root: Path) -> dict:
    result = {}
    for path in sorted((root / "src").rglob("*.py")):
        _no_links(root, path)
        result[path.relative_to(root).as_posix()] = _digest(path.read_bytes())
    return result


def _staged_source_hashes(root: Path) -> dict:
    for path in (root / "src").rglob("*"):
        _no_links(root, path)
        if path.is_file() and path.suffix != ".py":
            raise ValueError("unexpected executable/data file in staged source")
    return _source_hashes(root)


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def issue_policy_review_receipt(files: Iterable[PatchFile], *, reviewer_id: str,
                                review_reference: str, findings: list[str]) -> dict:
    """Trusted reviewer port, never exposed as a coding-model tool.

    The reference must identify a completed review of the exact diff, including
    the live action boundary. A successful test run is not a policy review.
    Supplying arbitrary web/model JSON to this issuer is a controller bug.
    """
    files = tuple(files)
    if (not isinstance(reviewer_id, str) or not reviewer_id.strip()
            or not isinstance(review_reference, str) or not review_reference.strip()
            or not isinstance(findings, list) or any(not isinstance(f, str) for f in findings)
            or not files):
        raise ValueError("completed trusted policy review identity and reference required")
    return _issue("policy_review", status="rejected" if findings else "passed",
                  candidate_sha256=_hashes(files), reviewer_id=reviewer_id,
                  review_reference=review_reference, findings=list(findings))


def verify_policy_review(review: dict, files: Iterable[PatchFile]) -> bool:
    return (_authentic(review, "policy_review") and review.get("status") == "passed"
            and review.get("findings") == [] and review.get("candidate_sha256") == _hashes(files))


def issue_preflight_receipt(files: Iterable[PatchFile], *, worker_id: str, profile_id: str,
                            evidence_fingerprint: str, capabilities: dict,
                            active_code_sha256: dict, policy_review: dict | None = None) -> dict:
    """Trusted controller only: attest measured controlled same-worker checks.

    Each capability value is an actual observation/evidence ID. The controller
    must first validate the action boundary in isolation, run its controlled
    pre-Send check, and verify which candidate code the worker loaded.
    """
    files = tuple(files)
    if not verify_policy_review(policy_review, files):
        raise ValueError("trusted policy review of exact candidate required before preflight")
    if (not worker_id or not profile_id or not evidence_fingerprint
            or not isinstance(capabilities, dict)
            or any(not isinstance(capabilities.get(key), str) or not capabilities[key].strip()
                   for key in REQUIRED_CAPABILITIES)):
        raise ValueError("measured capability evidence and worker/profile identity required")
    hashes = _hashes(files)
    if not hashes or active_code_sha256 != hashes:
        raise ValueError("preflight active code does not match candidate")
    return _issue("preflight", status="passed", candidate_sha256=hashes,
                  worker_id=worker_id, profile_id=profile_id,
                  evidence_fingerprint=evidence_fingerprint, capabilities=dict(capabilities),
                  active_code_sha256=dict(active_code_sha256), policy_review=dict(policy_review))


class PatchManager:
    def __init__(self, root: Path, artifact_dir: Path):
        self.root = Path(root).absolute()
        self.artifact_dir = Path(artifact_dir).absolute()
        _no_links(self.root, self.root)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        _no_links(Path(self.artifact_dir.anchor), self.artifact_dir)

    def _check(self, files: Iterable[PatchFile]) -> tuple[PatchFile, ...]:
        files = tuple(files)
        if not files or len(files) > 8 or len({f.path for f in files}) != len(files):
            raise ValueError("patch must contain one to eight unique files")
        if sum(len(f.content.encode("utf-8")) for f in files) > 256_000:
            raise ValueError("patch exceeds bounded size")
        for item in files:
            target = _safe_target(self.root, item.path)
            if item.before_sha256 is not None and not re.fullmatch("[0-9a-f]{64}", item.before_sha256):
                raise ValueError("invalid source hash")
            actual = _digest(target.read_bytes()) if target.exists() else None
            if actual != item.before_sha256:
                raise ValueError("source hash changed: " + item.path)
            try:
                ast.parse(item.content, filename=item.path)
            except SyntaxError as error:
                raise ValueError("candidate syntax invalid") from error
        return files

    def stage(self, files: Iterable[PatchFile]) -> Path:
        files = self._check(files)
        baseline = _source_hashes(self.root)
        stage = self.artifact_dir / "candidates" / uuid.uuid4().hex
        stage.mkdir(parents=True)
        # Copy only Python source, never config, DBs, profiles, env or bytecode.
        for name in baseline:
            destination = stage / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            data = (self.root / name).read_bytes()
            if _digest(data) != baseline[name]:
                raise ValueError("source changed while staging")
            destination.write_bytes(data)
        for item in files:
            destination = stage / item.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(item.content.encode("utf-8"))
        manifest = _issue("stage", source_root=str(self.root), candidate_sha256=_hashes(files),
                          before_sha256={f.path: f.before_sha256 for f in files},
                          baseline_source_sha256=baseline, staged_source_sha256=_staged_source_hashes(stage))
        _write_atomic(stage / "manifest.json", _canonical(manifest))
        return stage

    def activate(self, files: Iterable[PatchFile], validation: dict, preflight: dict,
                 idle_check: Callable[[], dict]) -> dict:
        """Replace bounded sources only while the controller owns an idle worker.

        idle_check is called before and after replacement; its second call must
        reload/verify the worker and return active_code_sha256 matching candidate.
        No module reload or provider action is performed here.
        """
        try:
            files = self._check(files)
            hashes = _hashes(files)
            for receipt, kind in ((validation, "validation"), (preflight, "preflight")):
                if (not _authentic(receipt, kind) or receipt.get("status") != "passed"
                        or receipt.get("candidate_sha256") != hashes):
                    raise ValueError("trusted bound " + kind + " receipt required")
            if validation.get("source_root") != str(self.root):
                raise ValueError("validation source root mismatch")
            if validation.get("baseline_source_sha256") != _source_hashes(self.root):
                raise ValueError("source changed after validation")
            trusted_tests = {name: _digest((self.root / name).read_bytes()) for name in TRUSTED_TESTS}
            if validation.get("trusted_test_sha256") != trusted_tests:
                raise ValueError("trusted tests changed after validation")
            if preflight.get("active_code_sha256") != hashes:
                raise ValueError("preflight code mismatch")
            if not verify_policy_review(preflight.get("policy_review"), files):
                raise ValueError("trusted policy review required")
        except (ValueError, OSError, TypeError, AttributeError) as error:
            return {"status": "review_only", "reason": str(error)}

        lock = self.root / ".flow-repair-activation.lock"
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return {"status": "review_only", "reason": "source activation ownership busy"}
        os.close(descriptor)
        transaction = None
        journal = None
        applied = []
        try:
            for prior in (self.artifact_dir / "transactions").glob("*/journal.json"):
                try:
                    state = json.loads(prior.read_text())["state"]
                except (OSError, ValueError, KeyError):
                    state = "ambiguous"
                if state not in {"committed", "rolled_back"}:
                    return {"status": "review_only", "reason": "interrupted transaction journal requires recovery"}
            self._check(files)
            observation = idle_check()
            if not self._idle_matches(observation, preflight):
                return {"status": "review_only", "reason": "same-worker idle ownership not proven"}
            if validation["baseline_source_sha256"] != _source_hashes(self.root):
                return {"status": "review_only", "reason": "source changed during ownership check"}
            transaction = self.artifact_dir / "transactions" / uuid.uuid4().hex
            backup = transaction / "backup"
            backup.mkdir(parents=True)
            for item in files:
                if item.before_sha256 is not None:
                    path = backup / item.path
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes((self.root / item.path).read_bytes())
            journal = transaction / "journal.json"
            state = {"state": "prepared", "candidate_sha256": hashes,
                     "before_sha256": {f.path: f.before_sha256 for f in files}, "applied": []}
            _write_atomic(journal, _canonical(state))
            state["state"] = "applying"
            _write_atomic(journal, _canonical(state))
            for item in files:
                self._check([item])
                _write_atomic(self.root / item.path, item.content.encode("utf-8"))
                applied.append(item)
                state["applied"].append(item.path)
                _write_atomic(journal, _canonical(state))
            observation = idle_check()
            if (not self._idle_matches(observation, preflight)
                    or observation.get("active_code_sha256") != hashes
                    or any(_digest((self.root / name).read_bytes()) != digest for name, digest in hashes.items())):
                raise ValueError("active worker code verification failed")
            state["state"] = "committed"
            _write_atomic(journal, _canonical(state))
            return _issue("activation", status="activated", candidate_sha256=hashes,
                          worker_id=preflight["worker_id"], profile_id=preflight["profile_id"],
                          journal=str(journal), backup_dir=str(backup))
        except Exception as error:
            if journal is None:
                return {"status": "review_only", "reason": str(error)}
            failures = []
            for item in reversed(applied):
                try:
                    target = _safe_target(self.root, item.path)
                    if _digest(target.read_bytes()) != hashes[item.path]:
                        raise ValueError("concurrent source edit during rollback")
                    if item.before_sha256 is None:
                        target.unlink()
                    else:
                        _write_atomic(target, (backup / item.path).read_bytes())
                except Exception as rollback_error:
                    failures.append(str(rollback_error))
            state["state"] = "rollback_failed" if failures else "rolled_back"
            state["reason"] = str(error)
            state["rollback_errors"] = failures
            _write_atomic(journal, _canonical(state))
            return {"status": state["state"], "reason": str(error), "journal": str(journal),
                    "backup_dir": str(backup)}
        finally:
            lock.unlink(missing_ok=True)

    @staticmethod
    def _idle_matches(observation, preflight) -> bool:
        return (isinstance(observation, dict) and observation.get("idle") is True
                and observation.get("worker_id") == preflight["worker_id"]
                and observation.get("profile_id") == preflight["profile_id"])


class DockerValidator:
    """Run only fixed original tests in an already-present digest-pinned image.

    A pre-provisioned Linux image must contain Python, pytest and project runtime
    dependencies. No install, build, pull, host Python or live provider fallback.
    runner/docker_path are trusted infrastructure injection ports for tests.
    """
    def __init__(self, trusted_root: Path, *, image: str | None = None,
                 runner=None, docker_path: str | None = None):
        self.root = Path(trusted_root).absolute()
        self.image = image
        self.runner = runner or subprocess.run
        self.docker = shutil.which("docker") if docker_path is None else docker_path

    def validate(self, candidate_root: Path, files: Iterable[PatchFile]) -> dict:
        files = tuple(files)
        unavailable = {"status": "unavailable", "executed": False,
                       "candidate_sha256": _hashes(files)}
        if not self.docker or not self.image or not re.fullmatch(r"[a-zA-Z0-9_./:-]+@sha256:[a-f0-9]{64}", self.image):
            return {**unavailable, "reason": "Docker and an existing digest-pinned validation image required"}
        container_name = None
        try:
            candidate = Path(candidate_root).absolute()
            manifest = json.loads((candidate / "manifest.json").read_text())
            if (not _authentic(manifest, "stage") or manifest["source_root"] != str(self.root)
                    or manifest["candidate_sha256"] != _hashes(files)
                    or manifest["staged_source_sha256"] != _staged_source_hashes(candidate)
                    or manifest["baseline_source_sha256"] != _source_hashes(self.root)):
                return {**unavailable, "reason": "candidate/source manifest mismatch"}
            for item in files:
                target = _safe_target(candidate, item.path)
                if _digest(target.read_bytes()) != _hashes(files)[item.path]:
                    return {**unavailable, "reason": "candidate content changed"}
            info = self.runner([self.docker, "info", "--format", "{{.OSType}}"],
                               capture_output=True, text=True, timeout=10)
            image = self.runner([self.docker, "image", "inspect", self.image, "--format", "{{.Id}}"],
                                capture_output=True, text=True, timeout=10)
            if info.returncode or info.stdout.strip() != "linux" or image.returncode or not image.stdout.strip().startswith("sha256:"):
                return {**unavailable, "reason": "enforceable Linux Docker runtime/image unavailable"}
            trusted = candidate.parent / ("trusted-" + uuid.uuid4().hex)
            trusted.mkdir()
            test_hashes = {}
            minimum_tests = 0
            for name in TRUSTED_TESTS:
                original = self.root / name
                _no_links(self.root, original)
                data = original.read_bytes()
                test_hashes[name] = _digest(data)
                minimum_tests += sum(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                                     and node.name.startswith("test_")
                                     for node in ast.walk(ast.parse(data, filename=name)))
                (trusted / Path(name).name).write_bytes(data)
            container_name = "vf-repair-" + uuid.uuid4().hex
            command = [self.docker, "run", "--rm", "--name", container_name, "--pull=never", "--network", "none", "--read-only",
                       "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=64",
                       "--memory=512m", "--cpus=1", "--user=65534:65534", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
                       "--mount", f"type=bind,src={candidate / 'src'},dst=/candidate/src,readonly",
                       "--mount", f"type=bind,src={trusted},dst=/trusted,readonly",
                       "--env", "PYTHONPATH=/candidate/src", "--env", "PYTHONDONTWRITEBYTECODE=1",
                       "--env", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1", "--entrypoint", "python", self.image,
                       "-I", "-c",
                       "import sys;sys.path.insert(0,'/candidate/src');import pytest;raise SystemExit(pytest.main(sys.argv[1:]))",
                       "-q", "-p", "no:cacheprovider", *["/trusted/" + Path(name).name for name in TRUSTED_TESTS]]
            run = self.runner(command, capture_output=True, text=True, timeout=120)
            summaries = re.findall(r"(?:^|\s)(\d+) passed(?:[,\s]|$)", run.stdout)
            tests_passed = int(summaries[-1]) if summaries else 0
            passed = run.returncode == 0 and minimum_tests > 0 and tests_passed >= minimum_tests
            return _issue("validation", status="passed" if passed else "failed",
                          executed=True, candidate_sha256=_hashes(files), source_root=str(self.root),
                          baseline_source_sha256=manifest["baseline_source_sha256"],
                          trusted_test_sha256=test_hashes,
                          trusted_tests_minimum=minimum_tests, trusted_tests_passed=tests_passed,
                          sandbox={"backend": "docker", "image": self.image, "image_id": image.stdout.strip(),
                                   "network": "none", "read_only": True}, returncode=run.returncode,
                          output_sha256=_digest((run.stdout + run.stderr).encode()))
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            return {**unavailable, "reason": "isolated validation unavailable: " + str(error)}
        finally:
            if container_name is not None:
                try:
                    self.runner([self.docker, "rm", "--force", container_name],
                                capture_output=True, text=True, timeout=10)
                except (OSError, subprocess.SubprocessError):
                    pass

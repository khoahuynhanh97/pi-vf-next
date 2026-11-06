"""Source transactions and isolation boundaries; no candidate executes on the host."""
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from video_factory.maintenance import patching


ADAPTER = "src/video_factory/integrations/google_flow_provider.py"
RECIPE = "src/video_factory/integrations/flow_ui_recipe.py"


def sha(content):
    return hashlib.sha256(content.encode()).hexdigest()


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "repo"
    target = root / ADAPTER
    target.parent.mkdir(parents=True)
    target.write_bytes(b"SELECTOR = 'old'\n")
    (root / "tests").mkdir()
    for name in patching.TRUSTED_TESTS:
        (root / name).write_text("def test_original():\n    assert True\n", encoding="utf-8")
    return root, patching.PatchManager(root, tmp_path / "artifacts")


def patch(content="SELECTOR = 'new'\n", path=ADAPTER):
    return patching.PatchFile(path, sha("SELECTOR = 'old'\n"), content)


class OfflineDocker:
    """Explicit external process fake, recording isolation arguments only."""
    def __init__(self, result=0):
        self.commands = []
        self.result = result

    def __call__(self, args, **kwargs):
        self.commands.append(args)
        if args[1] == "info":
            return subprocess.CompletedProcess(args, 0, "linux", "")
        if args[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(args, 0, "sha256:" + "a" * 64, "")
        return subprocess.CompletedProcess(args, self.result, "3 passed in 0.1s", "")


def receipts(root, manager, files, runner=None):
    candidate = manager.stage(files)
    validator = patching.DockerValidator(root, image="python@sha256:" + "a" * 64,
                                        runner=runner or OfflineDocker(), docker_path="docker")
    validation = validator.validate(candidate, files)
    hashes = {f.path: sha(f.content) for f in files}
    preflight = patching.issue_preflight_receipt(
        files, worker_id="worker", profile_id="profile", evidence_fingerprint="evidence-sha",
        capabilities={name: "measured-control-" + name for name in patching.REQUIRED_CAPABILITIES},
        active_code_sha256=hashes,
        policy_review=patching.issue_policy_review_receipt(
            files, reviewer_id="test-trusted-reviewer", review_reference="explicit-offline-test-review", findings=[]))
    return validation, preflight


def idle(files, verified=True):
    return lambda: {"idle": True, "worker_id": "worker", "profile_id": "profile",
                    "active_code_sha256": {f.path: sha(f.content) for f in files} if verified else {}}


@pytest.mark.parametrize("content", ["SELECTOR = '[role=tab]'\n", "SELECTOR = '[aria-label=Settings] button'\n"])
def test_two_recipes_stage_and_activate_with_retained_backup(workspace, content):
    root, manager = workspace
    files = [patch(content)]
    validation, preflight = receipts(root, manager, files)
    result = manager.activate(files, validation, preflight, idle(files))
    assert result["status"] == "activated"
    assert (root / ADAPTER).read_text() == content
    assert Path(result["backup_dir"], ADAPTER).read_text() == "SELECTOR = 'old'\n"
    assert json.loads(Path(result["journal"]).read_text())["state"] == "committed"


@pytest.mark.parametrize("path", ["../outside.py", "C:/escape.py", "/tmp/escape.py", "src/video_factory/workflows/worker.py", "src/video_factory/integrations/../config.py", "tests/test_other.py", "tests/test_flow_repair_x.py/other.py", "src\\video_factory\\integrations\\google_flow_provider.py"])
def test_untrusted_patch_paths_rejected(workspace, path):
    _, manager = workspace
    with pytest.raises(ValueError):
        manager.stage([patch(path=path)])


def test_source_hash_prevents_stale_stage_and_concurrent_activation(workspace):
    root, manager = workspace
    files = [patch()]
    validation, preflight = receipts(root, manager, files)
    (root / ADAPTER).write_text("HUMAN = True\n")
    with pytest.raises(ValueError, match="source"):
        manager.stage(files)
    result = manager.activate(files, validation, preflight, idle(files))
    assert result["status"] == "review_only"
    assert (root / ADAPTER).read_text() == "HUMAN = True\n"


def test_symlink_patch_target_rejected(workspace, tmp_path):
    root, manager = workspace
    original = root / ADAPTER
    original.unlink()
    outside = tmp_path / "outside.py"
    outside.write_text("SELECTOR = 'old'\n")
    try:
        original.symlink_to(outside)
    except OSError:
        pytest.skip("OS disallows symlinks for this account")
    with pytest.raises(ValueError, match="link"):
        manager.stage([patch()])


def test_plain_or_modified_receipts_cannot_authorize_activation(workspace):
    root, manager = workspace
    files = [patch()]
    validation, preflight = receipts(root, manager, files)
    forged = {"status": "passed", "sandbox": True, "candidate_sha256": {ADAPTER: sha(files[0].content)}}
    assert manager.activate(files, forged, preflight, idle(files))["status"] == "review_only"
    validation["candidate_sha256"][ADAPTER] = "b" * 64
    assert manager.activate(files, validation, preflight, idle(files))["status"] == "review_only"
    assert (root / ADAPTER).read_text() == "SELECTOR = 'old'\n"


def test_unavailable_isolation_never_runs_host_candidate(workspace):
    root, manager = workspace
    candidate = manager.stage([patch()])
    result = patching.DockerValidator(root, docker_path="").validate(candidate, [patch()])
    assert result["status"] == "unavailable"
    assert result["executed"] is False


def test_docker_failure_is_not_passed_and_runs_fixed_original_tests(workspace):
    root, manager = workspace
    files = [patch(), patching.PatchFile("tests/test_flow_repair_generated.py", None, "assert False\n")]
    runner = OfflineDocker(result=1)
    validation, preflight = receipts(root, manager, files, runner)
    assert validation["status"] == "failed"
    assert manager.activate(files, validation, preflight, idle(files))["status"] == "review_only"
    command = next(command for command in runner.commands if command[1] == "run")
    assert command[command.index("--network") + 1] == "none"
    assert "--read-only" in command
    assert "--cap-drop=ALL" in command
    assert "--pull=never" in command
    assert all("test_flow_repair_generated.py" not in arg for arg in command)


def test_failed_active_code_verification_rolls_back_and_keeps_journal(workspace):
    root, manager = workspace
    files = [patch(), patching.PatchFile(RECIPE, None, "RECIPE = 'new'\n")]
    validation, preflight = receipts(root, manager, files)
    result = manager.activate(files, validation, preflight, idle(files, verified=False))
    assert result["status"] == "rolled_back"
    assert (root / ADAPTER).read_text() == "SELECTOR = 'old'\n"
    assert not (root / RECIPE).exists()
    assert json.loads(Path(result["journal"]).read_text())["state"] == "rolled_back"


def test_interrupted_journal_survives_manager_restart(workspace):
    root, manager = workspace
    files = [patch()]
    validation, preflight = receipts(root, manager, files)
    journal = manager.artifact_dir / "transactions" / "interrupted" / "journal.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({"state": "applying"}))
    restarted = patching.PatchManager(root, manager.artifact_dir)
    result = restarted.activate(files, validation, preflight, idle(files))
    assert result["status"] == "review_only"
    assert "journal" in result["reason"]


def test_missing_measured_capability_cannot_issue_preflight(workspace):
    _, _manager = workspace
    with pytest.raises(ValueError, match="capabilit"):
        patching.issue_preflight_receipt([patch()], worker_id="worker", profile_id="profile",
            evidence_fingerprint="fingerprint", capabilities={"model": True},
            active_code_sha256={ADAPTER: sha(patch().content)},
            policy_review=patching.issue_policy_review_receipt(
                [patch()], reviewer_id="reviewer", review_reference="review-1", findings=[]))


def test_idle_callback_concurrent_edit_is_preserved(workspace):
    root, manager = workspace
    files = [patch()]
    validation, preflight = receipts(root, manager, files)

    def owner():
        (root / ADAPTER).write_bytes(b"HUMAN = True\n")
        return idle(files)()

    result = manager.activate(files, validation, preflight, owner)
    assert result["status"] in {"review_only", "rolled_back"}
    assert (root / ADAPTER).read_text() == "HUMAN = True\n"


def test_candidate_added_binary_cannot_bypass_staged_manifest(workspace):
    root, manager = workspace
    candidate = manager.stage([patch()])
    (candidate / "src" / "malicious.pyd").write_bytes(b"native code")
    result = patching.DockerValidator(root, image="python@sha256:" + "a" * 64,
        runner=OfflineDocker(), docker_path="docker").validate(candidate, [patch()])
    assert result["status"] == "unavailable"
    assert result["executed"] is False


def test_container_timeout_cleans_up_and_never_issues_success(workspace):
    root, manager = workspace
    candidate = manager.stage([patch()])
    runner = OfflineDocker()
    commands = runner.commands

    def timeout(args, **kwargs):
        if args[1] == "run":
            commands.append(args)
            raise subprocess.TimeoutExpired(args, 120)
        return runner(args, **kwargs)

    result = patching.DockerValidator(root, image="python@sha256:" + "a" * 64,
        runner=timeout, docker_path="docker").validate(candidate, [patch()])
    assert result["status"] == "unavailable"
    assert any(command[1:3] == ["rm", "--force"] for command in commands)


def test_unsafe_candidate_plain_review_cannot_authorize_preflight(workspace):
    malicious = patch("import os\nos._exit(0)\n")
    forged = {"status": "passed", "allowed": True, "candidate_sha256": {ADAPTER: sha(malicious.content)}}
    with pytest.raises(ValueError, match="policy review"):
        patching.issue_preflight_receipt([malicious], worker_id="worker", profile_id="profile",
            evidence_fingerprint="fingerprint",
            capabilities={name: "evidence" for name in patching.REQUIRED_CAPABILITIES},
            active_code_sha256={ADAPTER: sha(malicious.content)}, policy_review=forged)


def test_policy_review_binds_exact_patch_and_rejects_findings(workspace):
    reviewed = [patch()]
    review = patching.issue_policy_review_receipt(
        reviewed, reviewer_id="reviewer", review_reference="review-1", findings=[])
    assert patching.verify_policy_review(review, reviewed)
    assert not patching.verify_policy_review(review, [patch("import os\nos._exit(0)\n")])
    rejected = patching.issue_policy_review_receipt(
        reviewed, reviewer_id="reviewer", review_reference="review-2", findings=["Unsafe import"])
    assert not patching.verify_policy_review(rejected, reviewed)


def test_exit_zero_without_trusted_test_execution_summary_is_failure(workspace):
    root, manager = workspace
    candidate = manager.stage([patch()])
    docker = OfflineDocker()

    def exits_without_tests(args, **kwargs):
        result = docker(args, **kwargs)
        if args[1] == "run":
            result.stdout = ""
        return result

    result = patching.DockerValidator(root, image="python@sha256:" + "a" * 64,
        runner=exits_without_tests, docker_path="docker").validate(candidate, [patch()])
    assert result["status"] == "failed"
    assert result["trusted_tests_passed"] == 0
